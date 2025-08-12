PAGE_NUMBER_LIKE = re.compile(r"^\s*\d{1,3}\s*$")  # catches small ints like 1, 23, 125

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
            # Only accept as Betrag if >= 1000 or has thousand sep
            if "." in amt_str or _norm_amount(amt_str) >= 1000:
                pending_amount = amt_str
                desc_part = rest[: trailing.start()].strip()
                current_desc_parts = [desc_part] if desc_part else []
            else:
                # Probably page number or wrong match — treat as part of Beschreibung
                current_desc_parts = [rest]
                pending_amount = None
        else:
            current_desc_parts = [rest] if rest else []
            pending_amount = None
        i += 1
        continue

    # Amount-only line => remember; do not emit yet
    mamt = RE_AMOUNT_ONLY.match(ln)
    if mamt and current_code is not None:
        amt_str = mamt.group("amt")
        if "." in amt_str or _norm_amount(amt_str) >= 1000:
            pending_amount = amt_str
        else:
            # Looks too small => likely not a Betrag => end row
            emit_if_complete()
        i += 1
        continue

    # Lone page-number-like line => treat as boundary
    if PAGE_NUMBER_LIKE.match(ln):
        emit_if_complete()
        i += 1
        continue

    # New header/table marker => boundary
    if _looks_like_new_table_marker(ln):
        emit_if_complete()
        i += 1
        continue

    # Otherwise it's a wrapped Beschreibung line
    if current_code is not None:
        current_desc_parts.append(ln.strip())
    i += 1
