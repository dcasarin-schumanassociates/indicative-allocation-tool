import re
from typing import List, Dict, Optional, Union
import io
import pandas as pd

# Prefer pdfplumber; fallback to pdfminer.six
def _pdf_to_text(file_like: Union[io.BytesIO, "UploadedFile"]) -> str:
    text_parts: List[str] = []
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(file_like) as pdf:
            for page in pdf.pages:
                t = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
                text_parts.append(t)
    except Exception:
        try:
            from pdfminer.high_level import extract_text  # type: ignore
            if hasattr(file_like, "seek"):
                try:
                    file_like.seek(0)
                except Exception:
                    pass
            text = extract_text(file_like)
            return text or ""
        except Exception:
            return ""
    return "\n".join(text_parts)

# --- Regex patterns -----------------------------------------------------------

SECTION_TITLE_GERMAN = r"Indikative Aufschlüsselung der Programmmittel \(EU\) nach Art der Intervention"
SECTION_ID = r"(?:\d+(?:\.\d+)*|(?:\d+\.)?[A-Z](?:\.\d+)*)"

RE_BLOCK_START = re.compile(
    rf"^\s*(?P<section>{SECTION_ID})\s+{SECTION_TITLE_GERMAN}\s*$",
    flags=re.MULTILINE
)
RE_NEXT_SECTION = re.compile(rf"^\s*(?:{SECTION_ID})\s+[A-ZÄÖÜa-zäöü]", flags=re.MULTILINE)

RE_DIMENSION = re.compile(r"^\s*Tabelle\s+\d+\s*:\s*Dimension\s+(?P<dimension>.+?)\s*$", flags=re.MULTILINE)
RE_TABLE_HEADER = re.compile(r"^\s*Code\s+Beschreibung\s+Betrag\s+\(EUR\)\s*$", flags=re.MULTILINE)

RE_CODE_LINE = re.compile(r"^\s*(?P<code>\d{2,3})\s+(?P<rest>.+)$")
RE_AMOUNT_ONLY = re.compile(r"^\s*(?P<amt>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*$")
RE_AMOUNT_TRAILING = re.compile(r"(?P<amt>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*$")

# --- Helpers -----------------------------------------------------------------

def _norm_amount(s: str) -> float:
    s = s.strip().replace(".", "").replace(" ", "").replace("\u00A0", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")

def _extract_blocks(full_text: str) -> List[Dict[str, str]]:
    """Locate target sections and slice their text."""
    blocks: List[Dict[str, str]] = []
    starts = list(RE_BLOCK_START.finditer(full_text))
    if not starts:
        return []

    for idx, m in enumerate(starts):
        start_idx = m.end()
        section_id = m.group("section").strip()
        if idx + 1 < len(starts):
            end_idx = starts[idx + 1].start()
        else:
            nxt = RE_NEXT_SECTION.search(full_text, pos=start_idx)
            end_idx = nxt.start() if nxt else len(full_text)
        block_text = full_text[start_idx:end_idx].strip("\n")
        blocks.append({"section": section_id, "text": block_text})
    return blocks

def _split_parts_by_slash(s: str) -> List[str]:
    # Normalise non-breaking spaces and split by ASCII slash only
    s = s.replace("\u00A0", " ")
    parts = [p.strip() for p in s.split("/") if p.strip()]
    return parts

def _extract_context(block_text: str) -> Dict[str, Optional[str]]:
    """
    Robustly capture context:
      - No single-char regex groups for Funding/Scope (prevents truncation).
      - Split by '/'.
      - Only stitch in next line if the first line has < 3 parts AND next line
        is not clearly the start of the next section/table.
      - Returns Scope="" when missing (3-part case).
    """
    ctx = {"Priorität": None, "Spezifisches Ziel": None, "Funding Programme": None, "Scope": ""}

    # Non-empty lines, with NBSP normalised
    lines = [ln.strip().replace("\u00A0", " ") for ln in block_text.splitlines() if ln.strip()]

    # Find the first line containing "Priorität"
    idx = None
    for i, ln in enumerate(lines):
        if "Priorität" in ln:
            idx = i
            break
    if idx is None:
        return ctx

    candidate = lines[idx]
    parts = _split_parts_by_slash(candidate)

    # Only stitch the next line if we still have <3 parts and the next line looks like a continuation
    if len(parts) < 3 and idx + 1 < len(lines):
        nxt = lines[idx + 1]
        if not re.match(r"^(Tabelle|Dimension|Code)\b", nxt):
            candidate = candidate + " / " + nxt
            parts = _split_parts_by_slash(candidate)

    # Extract values
    if len(parts) >= 2:
        m = re.search(r"Priorität\s+(.+)", parts[0], flags=re.IGNORECASE)
        if m:
            ctx["Priorität"] = m.group(1).strip()

        m = re.search(r"Spezifisches\s+Ziel\s+(.+)", parts[1], flags=re.IGNORECASE)
        if m:
            ctx["Spezifisches Ziel"] = m.group(1).strip()

    if len(parts) >= 4:
        ctx["Funding Programme"] = parts[2]
        ctx["Scope"] = parts[3]
    elif len(parts) == 3:
        ctx["Funding Programme"] = parts[2]
        ctx["Scope"] = ""

    return ctx

def _rows_from_block(section_id: str, block_text: str) -> List[Dict[str, Union[str, float]]]:
    rows: List[Dict[str, Union[str, float]]] = []
    ctx = _extract_context(block_text)

    # Each dimension table
    for dim_match in RE_DIMENSION.finditer(block_text):
        dim_start = dim_match.end()
        dimension_label = dim_match.group("dimension").strip()
        # Grab continuation lines until stop condition
        continuation_lines = []
        for extra_line in block_text[dim_start:].splitlines()[0:]:
            if not extra_line.strip():
                break
            if extra_line.strip().startswith("Code") or extra_line.strip().startswith("Tabelle"):
                break
            continuation_lines.append(extra_line.strip())
        if continuation_lines:
            dimension_label += " " + " ".join(continuation_lines)

        next_dim = RE_DIMENSION.search(block_text, pos=dim_start)
        local_end = next_dim.start() if next_dim else len(block_text)
        local_text = block_text[dim_start:local_end]

        th = RE_TABLE_HEADER.search(local_text)
        local_start = th.end() if th else 0

        snippet = local_text[local_start:].strip("\n")
        if not snippet:
            continue

        lines = [ln.rstrip() for ln in snippet.splitlines() if ln.strip() != ""]
        i = 0
        current_code = None
        current_desc_parts: List[str] = []

        def emit_row(amount_str: str):
            nonlocal current_code, current_desc_parts
            if current_code is None:
                return
            desc = " ".join(p.strip() for p in current_desc_parts if p.strip())
            amt_val = _norm_amount(amount_str)
            row = {
                "Indikative Aufschlüsselung (Section)": section_id,
                "Priorität": ctx.get("Priorität"),
                "Spezifisches Ziel": ctx.get("Spezifisches Ziel"),
                "Funding Programme": ctx.get("Funding Programme"),
                "Scope": ctx.get("Scope"),
                "Dimension": dimension_label,
                "Code": current_code,
                "Beschreibung": desc,
                "Betrag (EUR)": amt_val,
            }
            rows.append(row)
            current_code = None
            current_desc_parts = []

        while i < len(lines):
            ln = lines[i]

            mcode = RE_CODE_LINE.match(ln)
            if mcode:
                current_code = mcode.group("code")
                rest = mcode.group("rest").strip()

                trailing = RE_AMOUNT_TRAILING.search(rest)
                if trailing:
                    amt = trailing.group("amt")
                    desc_part = rest[: trailing.start()].strip()
                    current_desc_parts = [desc_part] if desc_part else []
                    emit_row(amt)
                else:
                    current_desc_parts = [rest] if rest else []
                i += 1
                continue

            mamt = RE_AMOUNT_ONLY.match(ln)
            if mamt and current_code is not None:
                emit_row(mamt.group("amt"))
                i += 1
                continue

            if current_code is not None:
                current_desc_parts.append(ln.strip())
            i += 1

    return rows

def parse_pdf_text(full_text: str) -> pd.DataFrame:
    blocks = _extract_blocks(full_text)
    all_rows: List[Dict[str, Union[str, float]]] = []
    for b in blocks:
        all_rows.extend(_rows_from_block(b["section"], b["text"]))
    df = pd.DataFrame(all_rows)
    if df.empty:
        return df

    ordered_cols = [
        "Indikative Aufschlüsselung (Section)",
        "Priorität",
        "Spezifisches Ziel",
        "Funding Programme",
        "Scope",
        "Dimension",
        "Code",
        "Beschreibung",
        "Betrag (EUR)",
    ]
    for c in ordered_cols:
        if c not in df.columns:
            df[c] = None
    return df[ordered_cols]

def parse_pdf_filelike(file_like) -> pd.DataFrame:
    if hasattr(file_like, "seek"):
        try:
            file_like.seek(0)
        except Exception:
            pass
    text = _pdf_to_text(file_like)
    return parse_pdf_text(text)
