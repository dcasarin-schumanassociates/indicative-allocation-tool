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

# Accepts "2.1.1.1.3" or "2.A.1.2.3" etc.
SECTION_ID = r"(?:\d+(?:\.\d+)*|(?:\d+\.)?[A-Z](?:\.\d+)*)"

# Block start line: "<section> Indikative Aufschlüsselung ..."
RE_BLOCK_START = re.compile(
    rf"^\s*(?P<section>{SECTION_ID})\s+{SECTION_TITLE_GERMAN}\s*$",
    flags=re.MULTILINE
)

# Delimiter to find a possible next section if needed
RE_NEXT_SECTION = re.compile(rf"^\s*(?:{SECTION_ID})\s+[A-ZÄÖÜa-zäöü]", flags=re.MULTILINE)

# Context (explicit patterns; allow spaces & diacritics in funding/scope)
# 4-part: Priorität / Spezifisches Ziel / Funding / Scope
RE_CONTEXT_4 = re.compile(
    r"Priorität\s+(?P<prio>\d+)\s*/\s*Spezifisches\s+Ziel\s+(?P<ziel>[\dA-Z\.]+)\s*/\s*"
    r"(?P<funding>[A-Za-zÄÖÜäöüß\+\-][A-Za-zÄÖÜäöüß\+\-\s\.]*)\s*/\s*"
    r"(?P<scope>[A-Za-zÄÖÜäöüß0-9\-\s\.,/()]+)",
    flags=re.IGNORECASE
)

# 3-part: Priorität / Spezifisches Ziel / Funding    (Scope = empty)
RE_CONTEXT_3 = re.compile(
    r"Priorität\s+(?P<prio>\d+)\s*/\s*Spezifisches\s+Ziel\s+(?P<ziel>[\dA-Z\.]+)\s*/\s*"
    r"(?P<funding>[A-Za-zÄÖÜäöüß\+\-][A-Za-zÄÖÜäöüß\+\-\s\.]*)",
    flags=re.IGNORECASE
)

# Dimension header
RE_DIMENSION = re.compile(r"^\s*Tabelle\s+\d+\s*:\s*Dimension\s+(?P<dimension>.+?)\s*$", flags=re.MULTILINE)

# Table header (robust spacing)
RE_TABLE_HEADER = re.compile(r"^\s*Code\s+Beschreibung\s+Betrag\s+\(EUR\)\s*$", flags=re.MULTILINE)

# Row parsing
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

def _extract_context(block_text: str) -> Dict[str, Optional[str]]:
    """
    Accepts:
      - 'Priorität X / Spezifisches Ziel Y / EFRE / Übergangsregion'
      - 'Priorität 6 / Spezifisches Ziel JTF / Fonds JTF'
    Looks across up to 2 lines to handle an extra line break after the header.
    """
    ctx = {"Priorität": None, "Spezifisches Ziel": None, "Funding Programme": None, "Scope": None}

    # Non-empty lines for robustness (skip blank line that may precede the context)
    lines = [ln.strip() for ln in block_text.splitlines() if ln.strip() != ""]

    # Find the first line containing "Priorität"
    idx = None
    for i, ln in enumerate(lines):
        if "Priorität" in ln:
            idx = i
            break
    if idx is None:
        return ctx

    # Stitch up to two lines: current + next (some PDFs break after the second slash)
    candidate = lines[idx]
    if idx + 1 < len(lines):
        candidate = candidate + " / " + lines[idx + 1]

    # Try 4-part first
    m4 = RE_CONTEXT_4.search(candidate)
    if m4:
        ctx["Priorität"] = m4.group("prio")
        ctx["Spezifisches Ziel"] = m4.group("ziel")
        ctx["Funding Programme"] = m4.group("funding").strip()
        ctx["Scope"] = m4.group("scope").strip()
        return ctx

    # Fallback to 3-part (Scope empty)
    m3 = RE_CONTEXT_3.search(candidate)
    if m3:
        ctx["Priorität"] = m3.group("prio")
        ctx["Spezifisches Ziel"] = m3.group("ziel")
        ctx["Funding Programme"] = m3.group("funding").strip()
        ctx["Scope"] = None
        return ctx

    # If nothing matches, leave as None
    return ctx

def _rows_from_block(section_id: str, block_text: str) -> List[Dict[str, Union[str, float]]]:
    rows: List[Dict[str, Union[str, float]]] = []
    ctx = _extract_context(block_text)

    # Each dimension table
    for dim_match in RE_DIMENSION.finditer(block_text):
        dim_start = dim_match.end()
        dimension_label = dim_match.group("dimension").strip()

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

            # Start of a new code row?
            mcode = RE_CODE_LINE.match(ln)
            if mcode:
                current_code = mcode.group("code")
                rest = mcode.group("rest").strip()

                # If amount is on the same line, split it off
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

            # Amount-only line closes the row
            mamt = RE_AMOUNT_ONLY.match(ln)
            if mamt and current_code is not None:
                emit_row(mamt.group("amt"))
                i += 1
                continue

            # Otherwise: wrapped description line
            if current_code is not None:
                current_desc_parts.append(ln.strip())
            i += 1

    return rows

def parse_pdf_text(full_text: str) -> pd.DataFrame:
    """Parse complete PDF text into a DataFrame of rows."""
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
    df = df[ordered_cols]
    return df

def parse_pdf_filelike(file_like) -> pd.DataFrame:
    """Parse a single PDF (UploadedFile or file-like) into a DataFrame."""
    if hasattr(file_like, "seek"):
        try:
            file_like.seek(0)
        except Exception:
            pass
    text = _pdf_to_text(file_like)
    return parse_pdf_text(text)
