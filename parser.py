import re
from typing import Iterable, List, Dict, Optional, Union
import io

import pandas as pd

# We prefer pdfplumber for structured text; if unavailable, we fall back to pdfminer.six
def _pdf_to_text(file_like: Union[io.BytesIO, "UploadedFile"]) -> str:
    text_parts: List[str] = []
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(file_like) as pdf:
            for page in pdf.pages:
                # Extract simple text (layout=False for robustness)
                t = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
                text_parts.append(t)
    except Exception:
        # Fallback to pdfminer.six high-level extract_text
        try:
            from pdfminer.high_level import extract_text  # type: ignore
            # pdfminer expects a file-like object at position 0
            if hasattr(file_like, "seek"):
                file_like.seek(0)
            text = extract_text(file_like)
            return text or ""
        except Exception:
            # Final fallback: empty string
            return ""
    return "\n".join(text_parts)

# Regex patterns
SECTION_TITLE_GERMAN = r"Indikative Aufschlüsselung der Programmmittel \(EU\) nach Art der Intervention"

SECTION_ID = r"(?:\d+(?:\.\d+)*|(?:\d+\.)?[A-Z](?:\.\d+)*)"

RE_BLOCK_START = re.compile(
    rf"^\s*(?P<section>{SECTION_ID})\s+{SECTION_TITLE_GERMAN}\s*$",
    flags=re.MULTILINE
)

# A conservative "new section header" detector to delimit blocks.
RE_NEXT_SECTION = re.compile(rf"^\s*(?:{SECTION_ID})\s+[A-ZÄÖÜa-zäöü]", flags=re.MULTILINE)

# Context line: "Priorität 1 / Spezifisches Ziel 1.2 / EFRE / Übergangsregion"
RE_CONTEXT = re.compile(
    r"Priorität\s+(?P<prio>\d+)\s*/\s*Spezifisches\s+Ziel\s+(?P<ziel>[\d\.A-Z]+)\s*/\s*(?P<funding>[A-Za-zÄÖÜäöüß\+\-]+)\s*/\s*(?P<scope>[^\n]+)"
)

# "Tabelle 1: Dimension 1 – Interventionsbereich"
RE_DIMENSION = re.compile(
    r"^\s*Tabelle\s+\d+\s*:\s*Dimension\s+(?P<dimension>.+?)\s*$",
    flags=re.MULTILINE
)

# Table header line: "Code Beschreibung Betrag (EUR)" (robust spacing)
RE_TABLE_HEADER = re.compile(
    r"^\s*Code\s+Beschreibung\s+Betrag\s+\(EUR\)\s*$",
    flags=re.MULTILINE
)

# Code line (first line of a row)
RE_CODE_LINE = re.compile(r"^\s*(?P<code>\d{2,3})\s+(?P<rest>.+)$")

# Pure amount line "15.000.000" or "25.000.000,00"
RE_AMOUNT_ONLY = re.compile(r"^\s*(?P<amt>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*$")

# Amount at end of line (handle one-line rows if present)
RE_AMOUNT_TRAILING = re.compile(r"(?P<amt>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*$")

def _norm_amount(s: str) -> float:
    # Convert European formatted string to float (e.g. "15.000.000,00" -> 15000000.00)
    s = s.strip()
    s = s.replace(".", "").replace(" ", "")
    s = s.replace("\u00A0", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")

def _extract_blocks(full_text: str) -> List[Dict[str, Union[str, int]]]:
    """Return a list of blocks with 'section' and 'text' for each target section."""
    blocks: List[Dict[str, str]] = []

    # Find all block starts
    starts = list(RE_BLOCK_START.finditer(full_text))
    if not starts:
        return []

    for idx, m in enumerate(starts):
        start_idx = m.end()
        section_id = m.group("section").strip()

        # End is either the next section start or a generic new section header
        if idx + 1 < len(starts):
            end_idx = starts[idx + 1].start()
        else:
            # try to find any later section header
            nxt = RE_NEXT_SECTION.search(full_text, pos=start_idx)
            end_idx = nxt.start() if nxt else len(full_text)

        block_text = full_text[start_idx:end_idx].strip("\n")
        blocks.append({"section": section_id, "text": block_text})
    return blocks

def _extract_context(block_text: str) -> Dict[str, Optional[str]]:
    ctx = {"Priorität": None, "Spezifisches Ziel": None, "Funding Programme": None, "Scope": None}
    m = RE_CONTEXT.search(block_text)
    if m:
        ctx["Priorität"] = m.group("prio")
        ctx["Spezifisches Ziel"] = m.group("ziel")
        ctx["Funding Programme"] = m.group("funding")
        ctx["Scope"] = m.group("scope").strip()
    return ctx

def _rows_from_block(section_id: str, block_text: str) -> List[Dict[str, Union[str, float]]]:
    rows: List[Dict[str, Union[str, float]]] = []
    ctx = _extract_context(block_text)

    # Iterate over each "Tabelle ... Dimension ..." segment within the block
    for dim_match in RE_DIMENSION.finditer(block_text):
        dim_start = dim_match.end()
        dimension_label = dim_match.group("dimension").strip()

        # The local text runs until next "Tabelle ..." or end-of-block
        next_dim = RE_DIMENSION.search(block_text, pos=dim_start)
        local_end = next_dim.start() if next_dim else len(block_text)
        local_text = block_text[dim_start:local_end]

        # Find the table header; start processing lines after it
        th = RE_TABLE_HEADER.search(local_text)
        if not th:
            # Some docs may omit the header; try a heuristic: first code line
            local_start = 0
        else:
            local_start = th.end()

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
                # If we were in the middle of a row without having seen an amount,
                # we do NOT emit (invalid/incomplete); start a new row.
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

            # Amount-only line that closes the current row
            mamt = RE_AMOUNT_ONLY.match(ln)
            if mamt and current_code is not None:
                emit_row(mamt.group("amt"))
                i += 1
                continue

            # Otherwise it's a wrapped description line -> append to Beschreibung
            if current_code is not None:
                current_desc_parts.append(ln.strip())
            # If no current row, ignore stray lines
            i += 1

    return rows

def parse_pdf_text(full_text: str, source_name: Optional[str] = None) -> pd.DataFrame:
    """Parse complete PDF text into a DataFrame of rows."""
    blocks = _extract_blocks(full_text)
    all_rows: List[Dict[str, Union[str, float]]] = []
    for b in blocks:
        all_rows.extend(_rows_from_block(b["section"], b["text"]))
    df = pd.DataFrame(all_rows)
    if df.empty:
        return df

    # Reorder columns for consistent output
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

    # Add optional provenance
    if source_name:
        df.insert(1, "Source File", source_name)

    return df

def parse_pdf_filelike(file_like, source_name: Optional[str] = None) -> pd.DataFrame:
    """Parse a single PDF (UploadedFile or file-like) into a DataFrame."""
    # Ensure we start reading from the beginning every time
    if hasattr(file_like, "seek"):
        try:
            file_like.seek(0)
        except Exception:
            pass
    text = _pdf_to_text(file_like)
    return parse_pdf_text(text, source_name=source_name)
