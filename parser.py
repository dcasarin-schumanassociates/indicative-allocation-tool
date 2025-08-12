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

# Table headers:
RE_TABLE_HEADER_3 = re.compile(r"^\s*Code\s+Beschreibung\s+Betrag\s+\(EUR\)\s*$", flags=re.MULTILINE)
RE_TABLE_HEADER_2 = re.compile(r"^\s*Code\s+Betrag\s+\(EUR\)\s*$", flags=re.MULTILINE)
# Heuristic: any header line that contains both "Code" and "Betrag"
RE_TABLE_HEADER_ANY = re.compile(r"^\s*Code\b.*\bBetrag\b.*$", flags=re.MULTILINE | re.IGNORECASE)

# Allow 2–3 digit codes; description must begin with a letter or "(" (filters "90 %")
RE_CODE_LINE = re.compile(r"^\s*(?P<code>\d{2,3})\s+(?P<rest>[A-Za-zÄÖÜäöüß(].+)$")

# Amounts (amount-only line, or trailing at end of line)
RE_AMOUNT_ONLY = re.compile(r"^\s*(?P<amt>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:EUR)?\s*$")
RE_AMOUNT_TRAILING = re.compile(r"(?P<amt>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:EUR)?\s*$")

# Inline section header (to cut dimension scan if a new section starts)
RE_SECTION_HEADER_INLINE = re.compile(rf"^\s*(?:{SECTION_ID})\s+[A-ZÄÖÜa-zäöü]", flags=re.MULTILINE)

# --- Helpers -----------------------------------------------------------------

def _norm_amount(s: str) -> float:
    s = s.strip().replace(".", "").replace(" ", "").replace("\u00A0", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")

def _is_page_number_line(s: str) -> bool:
    # Pure page number like "56"
    return bool(re.fullmatch(r"\d{1,4}", s.strip()))

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
    """
    Robust context:
      - Split by '/'
      - Stitch next line only if needed
      - Scope = "" when missing (3-part case)
    """
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
    return bool(re.match(r"^\s*(Tabelle\s+\d+|Dimension\s+\d+|Code\b.*Betrag|Code\s+Beschreibung\s+Betrag)", s, flags=re.IGNORECASE))

# --- Row extraction -----------------------------------------------------------

def _rows_from_block(section_id: str, block_text: str) -> List[Dict[str, Union[str, float]]]:
    rows: List[Dict[str, Union[str, float]]] = []
    ctx = _extract_context(block_text)

    for dim_match in RE_DIMENSION.finditer(block_text):
        dim_start = dim_match.end()
        dimension_label = dim_match.group("dimension").strip()

        # end of this dimension = next "Tabelle ..." or next section header
        next_dim = RE_DIMENSION.search(block_text, pos=dim_start)
        next_hdr = RE_SECTION_HEADER_INLINE.search(block_text, pos=dim_start)
        cut_points = [p.start() for p in [next_dim, next_hdr] if p]
        local_end = min(cut_points) if cut_points else len(block_text)
        local_text = block_text[dim_start:local_end]

        # 1) Prefer explicit headers
        header_match = RE_TABLE_HEADER_3.search(local_text) or RE_TABLE_HEADER_2.search(local_text) or RE_TABLE_HEADER_ANY.search(local_text)
        if header_match:
            local_start = header_match.end()
        else:
            # 2) Fallback: start at first code line that has an amount within the next few lines
            m_any_code = RE_CODE_LINE.search(local_text)
            if not m_any_code:
                continue
            # lookahead window for amount
            after = local_text[m_any_code.start():]
            lines_tmp = [ln.strip() for ln in after.splitlines() if ln.strip()]
            has_amount_nearby = False
            for k in range(min(10, len(lines_tmp))):
                ln_k = lines_tmp[k]
                if RE_AMOUNT_TRAILING.search(ln_k) or RE_AMOUNT_ONLY.match(ln_k):
                    amt = (RE_AMOUNT_TRAILING.search(ln_k) or RE_AMOUNT_ONLY.match(ln_k)).group("amt")
                    if "." in amt or "," in amt:
                        has_amount_nearby = True
                        break
                if _looks_like_new_table_marker(ln_k):
                    break
            if not has_amount_nearby:
                continue
            # start parsing from that code line
            local_start = m_any_code.start()

        snippet = local_text[local_start:].strip("\n")
        if not snippet:
            continue

        # Lines within the table; keep non-empty but skip page numbers
        raw_lines = [ln.rstrip() for ln in snippet.splitlines() if ln.strip() != ""]
        lines = [ln for ln in raw_lines if not _is_page_number_line(ln)]
        i = 0

        current_code = None
        current_desc_parts: List[str] = []
        pending_amount: Optional[str] = None

        def emit_now():
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

            # New code row?
            mcode = RE_CODE_LINE.match(ln)
            if mcode:
                # If we had a complete row already, flush
                if current_code is not None and pending_amount is not None:
                    emit_now()
                # Start new row
                current_code = mcode.group("code")
                rest = mcode.group("rest").strip()

                # Amount on same line? -> capture & EMIT IMMEDIATELY
                trailing = RE_AMOUNT_TRAILING.search(rest)
                if trailing:
                    amt = trailing.group("amt")
                    if "." in amt or "," in amt:
                        pending_amount = amt
                        desc_part = rest[: trailing.start()].strip()
                        current_desc_parts = [desc_part] if desc_part else []
                        emit_now()  # immediate emit avoids over-capturing after the amount
                    else:
                        # Not a real amount
                        pending_amount = None
                        current_desc_parts = [rest] if rest else []
                else:
                    current_desc_parts = [rest] if rest else []
                    pending_amount = None
                i += 1
                continue

            # Amount-only line -> capture & EMIT IMMEDIATELY
            mamt = RE_AMOUNT_ONLY.match(ln)
            if mamt and current_code is not None:
                amt = mamt.group("amt")
                if "." in amt or "," in amt:
                    pending_amount = amt
                    emit_now()
                i += 1
                continue

            # New header/table marker or next section => boundary (emit if complete)
            if _looks_like_new_table_marker(ln) or RE_SECTION_HEADER_INLINE.match(ln):
                if current_code is not None and pending_amount is not None:
                    emit_now()
                else:
                    # incomplete row -> drop
                    current_code = None
                    current_desc_parts = []
                    pending_amount = None
                i += 1
                continue

            # Otherwise continuation of Beschreibung
            if current_code is not None:
                current_desc_parts.append(ln.strip())
            i += 1

        # End of this table: nothing to do (we already emitted at amount)
        # If there is an incomplete row, we drop it silently.

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

