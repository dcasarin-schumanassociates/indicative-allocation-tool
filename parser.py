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

# Allow 2- or 3-digit codes; require at least one space after code
RE_CODE_LINE = re.compile(r"^\s*(?P<code>\d{2,3})\s+(?P<rest>.+)$")

# Amount patterns (amount-only line or trailing at end of code/desc line)
RE_AMOUNT_ONLY = re.compile(r"^\s*(?P<amt>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:EUR)?\s*$")
RE_AMOUNT_TRAILING = re.compile(r"(?P<amt>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:EUR)?\s*$")

PAGE_NUMBER_LIKE = re.compile(r"^\s*\d{1,3}\s*$")  # catches small ints like 1, 23, 125

# --- Helpers -----------------------------------------------------------------

def _norm_amount(s: str) -> float:
    s = s.strip().replace(".", "").replace(" ", "").replace("\u00A0", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")

def _looks_like_valid_amount(s: str) -> bool:
    """Accept if string has thousand separator or >= 1000 EUR."""
    if "." in s:
        return True
    try:
        return float(s.replace(",", ".")) >= 1000
    except ValueError:
        return False

def _extract_blocks(full_text: str) -> List[Dict[str, str]]:
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
    s = s.replace("\u00A0", " ")
    return [p.strip() for p in s.split("/") if p.strip()]

def _extract_context(block_text: str) -> Dict[str, Optional[str]]:
    ctx = {"Priorität": None, "Spezifisches Ziel": None, "Funding Programme": None, "Scope": ""}

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

    # Stitch next line if we still have <3 parts and the next line looks like continuation
    if len(parts) < 3 and idx + 1 < len(lines):
        nxt = lines[idx + 1]
        if not re.match(r"^(Tabelle|Dimension|Code)\b", nxt):
            candidate = candidate + " / " + nxt
            parts = _split_parts_by_slash(candidate)

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

# --- Beschreibung stitching ---------------------------------------------------

def _normalise_soft_hyphen(s: str) -> str:
    return s.replace("\u00AD", "")

def _join_desc_parts(parts: List[str]) -> str:
    if not parts:
        return ""
    cleaned = [_normalise_soft_hyphen(p.strip()) for p in parts if p.strip()]
    if not cleaned:
        return ""
    out = cleaned[0]
    for nxt in cleaned[1:]:
        if out.endswith("-"):
            out = out[:-1] + nxt
        else:
            out = f"{out} {nxt}"
    return re.sub(r"\s{2,}", " ", out).strip()

def _looks_like_new_table_marker(s: str) -> bool:
    return bool(re.match(r"^\s*(Tabelle\s+\d+|Dimension\s+\d+|Code\s+Beschreibung\s+Betrag)", s))

# --- Row extraction -----------------------------------------------------------

def _rows_from_block(section_id: str, block_text: str) -> List[Dict[str, Union[str, float]]]:
    rows: List[Dict[str, Union[str, float]]] = []
    ctx = _extract_context(block_text)

    for dim_match in RE_DIMENSION.finditer(block_text):
        dim_start = dim_match.end()
        dimension_label = dim_match.group("dimension").strip()

        next_dim = RE_DIMENSION.search(block_text, pos=dim_start)
        local_end = next_dim.start() if next_dim else len(block_text)
        local_text = block_text[dim_start:local_end]

        # ✅ Checker step: only parse if header "Code Beschreibung Betrag (EUR)" exists
        th = RE_TABLE_HEADER.search(local_text)
        if not th:
            continue
        local_start = th.end()

        snippet = local_text[local_start:].strip("\n")
        if not snippet:
            continue

        lines = [ln.rstrip() for ln in snippet.splitlines() if ln.strip() != ""]
        i = 0

        current_code = None
        current_desc_parts: List[str] = []
        pending_amount: Optional[str] = None

        def emit_if_complete():
            nonlocal current_code, current_desc_parts, pending_amount
            if current_code is not None and pending_amount is not None:
                rows.append({
                    "Indikative Aufschlüsselung (Section)": section_id,
                    "Priorität": ctx.get("Priorität"),
                    "Spezifisches Ziel": ctx.get("Spezifisches Ziel"),
                    "Funding Programme": ctx.get("Funding Programme"),
                    "Scope": ctx.get("Scope"),
                    "Dimension": dimension_label,
                    "Code": current_code,
                    "Beschreibung": _join_desc_parts(current_desc_parts),
                    "Betrag (EUR)": _norm_amount(pending_amount),
                })
            current_code = None
            current_desc_parts = []
            pending_amount = None

        while i < len(lines):
            ln = lines[i]

            # New code row => boundary for previous row
            mcode = RE_CODE_LINE.match(ln)
            if mcode:
                emit_if_complete()
                current_code = mcode.group("code")
                rest = mcode.group("rest").strip()

                trailing = RE_AMOUNT_TRAILING.search(rest)
                if trailing:
                    amt_str = trailing.group("amt")
                    if _looks_like_valid_amount(amt_str):
                        pending_amount = amt_str
                        desc_part = rest[: trailing.start()].strip()
                        current_desc_parts = [desc_part] if desc_part else []
                    else:
                        current_desc_parts = [rest]
                        pending_amount = None
                else:
                    current_desc_parts = [rest] if rest else []
                    pending_amount = None
                i += 1
                continue

            # Amount-only line => remember or terminate
            mamt = RE_AMOUNT_ONLY.match(ln)
            if mamt and current_code is not None:
                amt_str = mamt.group("amt")
                if _looks_like_valid_amount(amt_str):
                    pending_amount = amt_str
                else:
                    # Too small => likely not Betrag => terminate
                    emit_if_complete()
                i += 1
                continue

            # Lone page-number-like line => boundary
            if PAGE_NUMBER_LIKE.match(ln):
                emit_if_complete()
                i += 1
                continue

            # New header/table marker => boundary
            if _looks_like_new_table_marker(ln):
                emit_if_complete()
                i += 1
                continue

            # Otherwise wrapped Beschreibung line
            if current_code is not None:
                current_desc_parts.append(ln.strip())
            i += 1

        # End of this table: flush last row if complete
        emit_if_complete()

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
