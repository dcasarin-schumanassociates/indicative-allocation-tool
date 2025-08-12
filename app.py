import io
from typing import List, Tuple
import streamlit as st
import pandas as pd
from parser import parse_pdf_filelike

st.set_page_config(page_title="Indicative Allocation Extractor", layout="wide")

st.title("Indicative Allocation Extractor")
st.caption("Upload programme PDFs, preview the extracted rows, run quality checks, select what to keep, and export to Excel.")

st.sidebar.header("Instructions")
st.sidebar.markdown(
    """
1. **Upload** one or more programme PDFs.
2. Click **Parse PDFs** to extract rows.
3. Use **Quality checks** to flag abnormally long descriptions.
4. Use the **Keep** checkboxes to choose rows for export.
5. Optionally **sort** the preview and export using the controls.
6. Click **Download Excel** to export your selection.
    """
)

uploaded_files = st.file_uploader(
    "Upload PDF files",
    type=["pdf"],
    accept_multiple_files=True,
    help="You can upload multiple PDFs at once."
)

parse_clicked = st.button("Parse PDFs", type="primary", disabled=(len(uploaded_files) == 0))


def _parse_many(files: List[st.runtime.uploaded_file_manager.UploadedFile]) -> pd.DataFrame:
    all_rows = []
    for f in files:
        try:
            df = parse_pdf_filelike(f)
            if df is not None and not df.empty:
                all_rows.append(df)
        except Exception as e:
            st.warning(f"Failed to parse {f.name}: {e}")
    if not all_rows:
        return pd.DataFrame()
    out = pd.concat(all_rows, ignore_index=True)
    out.insert(0, "Keep", True)  # default keep all
    return out


# ---------- Persist parsed DF ----------
if parse_clicked and uploaded_files:
    with st.spinner("Parsing PDFs..."):
        df = _parse_many(uploaded_files)
    if df.empty:
        st.info("No rows extracted. Please verify the PDFs contain the expected sections.")
    else:
        st.session_state.parsed_df = df

if "parsed_df" in st.session_state and not st.session_state.parsed_df.empty:
    df = st.session_state.parsed_df.copy()

    # ---------- QUALITY CHECKS: Abnormally long Beschreibung ----------
    st.subheader("Quality checks")
    st.write("Flag unusually long **Beschreibung** entries and optionally exclude them from the preview/export.")

    # Compute description length (persist for export if desired)
    if "Beschreibung" not in df.columns:
        df["Beschreibung"] = ""
    desc_len = df["Beschreibung"].fillna("").astype(str).str.len()
    df["Beschreibung Länge"] = desc_len  # keep a visible column for transparency

    # Controls
    qc_cols = st.columns([1, 1, 1.2, 1])
    with qc_cols[0]:
        mode = st.selectbox("Detection mode", ["IQR-based (recommended)", "Fixed cap"])
    with qc_cols[1]:
        if mode.startswith("IQR"):
            k = st.number_input("IQR multiplier (k)", min_value=0.5, max_value=5.0, value=1.5, step=0.5)
        else:
            k = None
    with qc_cols[2]:
        if mode.startswith("Fixed"):
            fixed_cap = st.number_input("Length cap (characters)", min_value=50, max_value=10000, value=600, step=50)
        else:
            fixed_cap = None
    with qc_cols[3]:
        hide_flagged = st.checkbox("Hide flagged rows (preview & export)", value=True)

    # Compute threshold & flags
    if mode.startswith("IQR"):
        q1, q3 = desc_len.quantile(0.25), desc_len.quantile(0.75)
        iqr = max(q3 - q1, 1.0)
        threshold = int(q3 + (k or 1.5) * iqr)
        method_label = f"IQR: Q1={int(q1)}, Q3={int(q3)}, IQR={int(iqr)}, k={k} → threshold={threshold}"
    else:
        threshold = int(fixed_cap or 600)
        method_label = f"Fixed cap → threshold={threshold}"

    df["Flag: Long Beschreibung"] = df["Beschreibung Länge"] > threshold

    # Summary
    flagged_count = int(df["Flag: Long Beschreibung"].sum())
    total_count = len(df)
    st.info(f"Detection: {method_label} — Flagged {flagged_count} of {total_count} rows "
            f"({(flagged_count/total_count*100 if total_count else 0):.1f}%).")

    # Show flagged rows for review
    with st.expander("Show flagged rows (review / QA)", expanded=False):
        flagged = df[df["Flag: Long Beschreibung"]].copy()
        st.dataframe(flagged, use_container_width=True, hide_index=True)

    # Apply hiding to working DataFrame for preview/export
    working_df = df[~df["Flag: Long Beschreibung"]].copy() if hide_flagged else df.copy()

    # ---------- SORTING CONTROLS ----------
    st.subheader("Sorting")
    st.write("Click column headers in the table to sort ad-hoc, or use these controls for reproducible multi-column sorting.")

    sortable_cols = [c for c in working_df.columns if c != "Keep"]

    sort_cols = st.multiselect(
        "Sort by column(s)",
        options=sortable_cols,
        default=st.session_state.get("sort_cols", [])
    )
    st.session_state.sort_cols = sort_cols

    if "sort_dirs" not in st.session_state:
        st.session_state.sort_dirs = {}
    for col in sort_cols:
        if col not in st.session_state.sort_dirs:
            st.session_state.sort_dirs[col] = True
    for col in list(st.session_state.sort_dirs.keys()):
        if col not in sort_cols:
            del st.session_state.sort_dirs[col]

    sort_dirs = []
    if sort_cols:
        st_cols = st.columns(len(sort_cols))
        for i, col in enumerate(sort_cols):
            with st_cols[i]:
                st.session_state.sort_dirs[col] = st.toggle(
                    f"↑ Asc for “{col}”",
                    value=st.session_state.sort_dirs[col],
                    key=f"asc_{col}"
                )
                sort_dirs.append(st.session_state.sort_dirs[col])

    apply_sort_to_export = st.checkbox(
        "Apply sorting to exported Excel",
        value=st.session_state.get("apply_sort_to_export", True)
    )
    st.session_state.apply_sort_to_export = apply_sort_to_export

    # ---------- PREVIEW ----------
    st.subheader("Preview & Select")
    st.write("Use the **Keep** column to select rows for export.")

    preview_df = working_df.copy()
    if sort_cols:
        preview_df = preview_df.sort_values(
            by=sort_cols,
            ascending=sort_dirs,
            kind="stable",
            ignore_index=True
        )

    edited = st.data_editor(
        preview_df,
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        key="preview_editor",
        column_config={
            "Keep": st.column_config.CheckboxColumn("Keep"),
            "Betrag (EUR)": st.column_config.NumberColumn("Betrag (EUR)", step=1, format="%.2f"),
        }
    )

    # ---------- EXPORT ----------
    to_export = edited[edited["Keep"] == True].drop(columns=["Keep"]) if "Keep" in edited.columns else edited

    if apply_sort_to_export and sort_cols:
        valid_sort_cols = [c for c in sort_cols if c in to_export.columns]
        if valid_sort_cols:
            to_export = to_export.sort_values(
                by=valid_sort_cols,
                ascending=[st.session_state.sort_dirs[c] for c in valid_sort_cols],
                kind="stable",
                ignore_index=True
            )

    st.divider()
    st.subheader("Export")

    # Offer both datasets: clean export and (optionally) flagged rows for audit
    col_a, col_b = st.columns(2)
    with col_a:
        if to_export.empty:
            st.warning("No rows selected. Tick at least one row to enable export.")
        else:
            buf_xlsx = io.BytesIO()
            with pd.ExcelWriter(buf_xlsx, engine="openpyxl") as writer:
                to_export.to_excel(writer, index=False, sheet_name="Extract")
            buf_xlsx.seek(0)
            st.download_button(
                label="Download Excel (cleaned)",
                data=buf_xlsx,
                file_name="indicative_allocations_clean.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    with col_b:
        flagged_only = df[df["Flag: Long Beschreibung"]].drop(columns=["Keep"], errors="ignore")
        if not flagged_only.empty:
            buf_csv = flagged_only.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="Download flagged rows (CSV)",
                data=buf_csv,
                file_name="flagged_long_beschreibung.csv",
                mime="text/csv",
            )

else:
    if not parse_clicked:
        st.info("Upload one or more PDFs, then click **Parse PDFs**.")
