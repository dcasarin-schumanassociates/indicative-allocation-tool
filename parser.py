import re
from typing import List, Dict, Optional, Union, Callable
import io
import pandas as pd

# ------------------------ Debug utilities ------------------------------------

DEBUG: bool = False
_DEBUG_LOGS: List[str] = []

def set_debug(enabled: bool = True) -> None:
    """Enable/disable debug logs (prints to console and stores in buffer)."""
    global DEBUG, _DEBUG_LOGS
    DEBUG = enabled
    _DEBUG_LOGS = []  # reset buffer each time you toggle

def _dbg(msg: str) -> None:
    if DEBUG:
        _DEBUG_LOGS.append(msg)
        try:
            print(msg)
        except Exception:
            pass

def get_debug_log() -> str:
    """Return the accumulated debug log as a single string."""
    return "\n".join(_DEBUG_LOGS)

# ------------------------ PDF extraction -------------------------------------

def _pdf_to_text(file_like: Union[io.BytesIO, "UploadedFile"]) -> str:
    """Extract text from PDF using pdfplumber first, then pdfminer as fallback."""
    text_parts: List[str] = []
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(file_like) as pdf:
            for i, page in enumerate(pdf.pages):
                t = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
                _dbg(f"[pdfplumber] Page {i+1}: {len(t)} chars")
                text_parts.append(t)
    except Exception as e:
        _dbg(f"[pdfplumber] Failed: {e!r}. Falling back to pdfminer.")
        try:
            from pdfminer.high_level import extract_text  # type: ignore
            if hasattr(file_like, "seek"):
                try: file_like.seek(0)
                except Exception: pass
            text = extract_text(file_like) or ""
            _dbg(f"[pdfminer] Extracted {len(text)} chars total")
            return text
        except Exception as e2:
            _dbg(f"[pdfminer] Failed: {e2!r}")
            return ""
    full = "\n".join(text_parts)
    _dbg(f"[pdf] Total extracted chars: {len(full)}")
    return full

# ------------------------ Patterns -------------------------------------------

SECTION_ID = r"(?:\d+(?:\.\d+)*|(?:\d+\.)?[A-Z](?:\.\d+)*)"
SECTION_TITLE_GERMAN = r"Indikative Aufschlüsselung der Programmmittel \(EU\) nach Art der Intervention"

# Block start = section id + title
RE_BLOCK_START = re.compile(
    rf"^\s*(?P<section>{SECTION_ID})\s+{SECTION_TITLE_GERMAN}\s*$",
    flags=re.MULTILINE
)

# Inside blocks
RE_DIMENSION = re.compile(r"^\s*Tabelle\s+\d+\s*:\s*Dimension\s+(?P<dimension>.+?)\s*$", flags=re.MULTILINE)

# Two header styles:
RE_TABLE_HEADER_DESC = re.compile(r"^\s*Code\s+Beschreibung\s+Betrag\s+\(EUR\)\s*$", flags=re.MULTILINE)
RE_TABLE_HEADER_AMT  = re.compile(r"^\s*Code\s+Betrag\s+\(EUR\)\s*$", flags=re.MULTILINE)

RE_CODE_LINE = re.compile(r"^\s*(?P<code>\d{2,3})\s+(?P<rest>.+)$")
RE_AMOUNT_ONLY = re.compile(r"^\s*(?P<amt>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:EUR)?\s*$")
RE_AMOUNT_TRAILING = re.compile(r"(?P<amt>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:EUR)?\s*$")

PAGE_NUMBER_LIKE = re.compile(r"^\s*\d{1,3}\s*$")  # catches small ints like 1, 23, 125

# ------------------------ Helpers --------------------------------------------

def _norm_amount(s: str) -> float:
    s = s.strip().replace(".", "").replace(" ", "").replace("\u00A0", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")

def _looks_like_valid_amount(s: str) -> bool:
    """Accept as Betrag if it has thousand separators or numeric value >= 1000."""
    if "." in s:
        return True
    try:
        return float(s.replace(",", ".")) >= 1000
    except ValueError:
        return False

def _split_parts_by_slash(s: str) -> List[str]:
    s = s.replace("\u00A0", " ")
    return [p.strip() for p in s.split("/") if p.strip()]

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
    return bool(re.match(r"^\s*(Tabelle\s+\d+|Dimension\s+\d+|Code\s+Beschreibung\s+Betrag|Code\s+Betrag\s+\(EUR\))", s))

# ------------------------ Block extraction -----------------------------------

def _extract_blocks(full_text: str) -> List[Dict[str, str]]:
    """Extract section blocks: from section header to the next header."""
    blocks: List[Dict[str, str]] = []
    starts = list(RE_BLOCK_START.finditer(full_text))
    _dbg(f"[blocks] Found {len(starts)} section headers")
    if not starts:
        return []

    for idx, m in enumerate(starts):
        start_idx = m.start()  # keep section line in block
        section_id = m.group("section").strip()

        if idx + 1 < len(starts):
            end_idx = starts[idx + 1].start()
        else:
            end_idx = len(full_text)

        block_text = full_text[start_idx:end_idx].strip("\n")
        _dbg(f"[block] {section_id}: chars {start_idx}-{end_idx} (len={len(block_text)})")
        # Optionally show first/last line for quick inspection
        first_line = block_text.splitlines()[0] if block_text.splitlines() else ""
        last_line = block_text.splitlines()[-1] if block_text.splitlines() else ""
        _dbg(f"[block] first: {first_line[:120]}")
        _dbg(f"[block] last : {last_line[:120]}")
        blocks.append({"section": section_id, "text": block_text})

    return blocks

# ------------------------ Context extraction ---------------------------------

def _extract_context(block_text: str) -> Dict[str, Optional[str]]:
    ctx = {"Priorität": None, "Spezifisches Ziel": None, "Funding Programme": None, "Scope": ""}

    lines = [ln.strip().replace("\u00A0", " ") for ln in block_text.splitlines() if ln.strip()]
    idx = None
    for i, ln in enumerate(lines):
        if "Priorität" in ln:
            idx = i
            break
    if idx is None:
        _dbg("[context] No 'Priorität' line found in block")
        return ctx

    candidate = lines[idx]
    parts = _split_parts_by_slash(candidate)

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

    _dbg(f"[context] Priorität={ctx['Priorität']} Ziel={ctx['Spezifisches Ziel']} Funding={ctx['Funding Programme']} Scope={ctx['Scope']}")
    return ctx

# ------------------------ Row extraction -------------------------------------

def _find_table_header(local_text: str) -> Optional[Dict[str, Union[int, str]]]:
    """Return {'type': 'desc'|'amt', 'start': index_after_header} if a table header is found."""
    m1 = RE_TABLE_HEADER_DESC.search(local_text)
    if m1:
        return {"type": "desc", "start": m1.end()}
    m2 = RE_TABLE_HEADER_AMT.search(local_text)
    if m2:
        return {"type": "amt", "start": m2.end()}
    return None

def _rows_from_block(section_id: str, block_text: str) -> List[Dict[str, Union[str, float]]]:
    rows: List[Dict[str, Union[str, float]]] = []
    ctx = _extract_context(block_text)

    dims = list(RE_DIMENSION.finditer(block_text))
    _dbg(f"[rows] Section {section_id}: found {len(dims)} 'Tabelle ... Dimension ...' blocks")

    for di, dim_match in enumerate(dims, start=1):
        dim_start = dim_match.end()
        dimension_label = dim_match.group("dimension").strip()

        next_dim = RE_DIMENSION.search(block_text, pos=dim_start)
        local_end = next_dim.start() if next_dim else len(block_text)
        local_text = block_text[dim_start:local_end]

        header = _find_table_header(local_text)
        if not header:
            _dbg(f"[rows]  - Dimension '{dimension_label}': no table header found -> skip")
            continue

        header_type = header["type"]  # 'desc' or 'amt'
        local_start = int(header["start"])
        _dbg(f"[rows]  - Dimension '{dimension_label}': header='{header_type}' span={local_start}-{local_end}")

        lines = [ln.rstrip() for ln in local_text[local_start:].splitlines() if ln.strip() != ""]
        i = 0
        curr_code: Optional[str] = None
        desc_parts: List[str] = []
        pending_amt: Optional[str] = None

        def emit_row(reason: str):
            nonlocal curr_code, desc_parts, pending_amt
            if curr_code is None:
                return
            row = {
                "Indikative Aufschlüsselung (Section)": section_id,
                "Priorität": ctx.get("Priorität"),
                "Spezifisches Ziel": ctx.get("Spezifisches Ziel"),
                "Funding Programme": ctx.get("Funding Programme"),
                "Scope": ctx.get("Scope"),
                "Dimension": dimension_label,
                "Code": curr_code,
                "Beschreibung": _join_desc_parts(desc_parts),
                "Betrag (EUR)": _norm_amount(pending_amt) if pending_amt is not None else float("nan"),
            }
            rows.append(row)
            _dbg(f"[row]    emit (reason={reason}) code={curr_code} amt={pending_amt} desc='{row['Beschreibung'][:80]}'")
            curr_code = None
            desc_parts = []
            pending_amt = None

        while i < len(lines):
            ln = lines[i]

            # New code line => boundary for previous row
            mcode = RE_CODE_LINE.match(ln)
            if mcode:
                emit_row("new_code")
                curr_code = mcode.group("code")
                rest = mcode.group("rest").strip()

                trailing = RE_AMOUNT_TRAILING.search(rest)
                if trailing and _looks_like_valid_amount(trailing.group("amt")):
                    pending_amt = trailing.group("amt")
                    # Beschreibung (if present) is text before amount
                    before = rest[: trailing.start()].strip()
                    desc_parts = [before] if before else []
                else:
                    desc_parts = [rest] if rest else []
                    pending_amt = None
                i += 1
                continue

            # Amount-only line => remember it; do not emit yet
            mamt = RE_AMOUNT_ONLY.match(ln)
            if mamt and curr_code is not None:
                amt_str = mamt.group("amt")
                if _looks_like_valid_amount(amt_str):
                    pending_amt = amt_str
                    _dbg(f"[row]    amount-only line -> {amt_str}")
                else:
                    _dbg(f"[row]    small/suspicious number -> end row")
                    emit_row("small_number_boundary")
                i += 1
                continue

            # Lone page-number-like line => end current row (don’t treat as amount)
            if PAGE_NUMBER_LIKE.match(ln):
                _dbg("[row]    page-number-like line -> boundary")
                emit_row("page_number")
                i += 1
                continue

            # New header/table marker => boundary
            if _looks_like_new_table_marker(ln):
                _dbg("[row]    header-like marker -> boundary")
                emit_row("header_marker")
                i += 1
                continue

            # Otherwise it's a wrapped Beschreibung line
            if curr_code is not None:
                desc_parts.append(ln.strip())
            i += 1

        # End of this table: flush last row (even if amount missing)
        emit_row("table_end")

    _dbg(f"[rows] Section {section_id}: total rows extracted = {len(rows)}")
    return rows

# ------------------------ Public API -----------------------------------------

def parse_pdf_text(full_text: str) -> pd.DataFrame:
    blocks = _extract_blocks(full_text)
    all_rows: List[Dict[str, Union[str, float]]] = []
    for b in blocks:
        _dbg(f"[parse] Working on section {b['section']}")
        all_rows.extend(_rows_from_block(b["section"], b["text"]))
    df = pd.DataFrame(all_rows)
    _dbg(f"[parse] Grand total rows: {len(df)}")
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
