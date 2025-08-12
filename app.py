import io
from typing import List
import streamlit as st
import pandas as pd
from parser import parse_pdf_filelike

st.set_page_config(page_title="Indicative Allocation Extractor", layout="wide")

st.title("Indicative Allocation Extractor")
st.caption("Upload programme PDFs, preview the extracted rows, select what to keep, and export to Excel.")

st.sidebar.header("Instructions")
st.sidebar.markdown(
    """
1. **Upload** one or more programme PDFs.
2. Click **Parse PDFs** to extract rows.
3. Use the **Keep** checkboxes to choose rows for export.
4. Optionally **sort** the preview and export using the controls below.
5. Click **Download Excel** to export your selection.
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

if parse_clicked:
    with st.spinner("Parsing PDFs..."):
        df = _parse_many(uploaded_files)

    if df is None or df.empty:
        st.info("No rows extracted. Please verify the PDFs contain the expected sections.")
    else:
        # ---------- SORTING CONTROLS ----------
        st.subheader("Sorting")
        st.write("Click column headers in the table to sort ad-hoc, or use these controls for reproducible multi-column sorting.")

        sortable_cols = [c for c in df.columns if c != "Keep"]

        # Remember sort columns in session
        sort_cols = st.multiselect(
            "Sort by column(s)",
            options=sortable_cols,
            default=st.session_state.get("sort_cols", [])
        )
        st.session_state.sort_cols = sort_cols

        # Initialise sort directions if not present
        if "sort_dirs" not in st.session_state:
            st.session_state.sort_dirs = {}

        # Add missing sort_dirs entries for new columns
        for col in sort_cols:
            if col not in st.session_state.sort_dirs:
                st.session_state.sort_dirs[col] = True  # default ascending

        # Remove directions for deselected columns
        for col in list(st.session_state.sort_dirs.keys()):
            if col not in sort_cols:
                del st.session_state.sort_dirs[col]

        # Always render toggle area if sort_cols is not empty
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
        preview_df = df.copy()
        if sort_cols:
            preview_df = preview_df.sort_values(
                by=sort_cols,
                ascending=sort_dirs,
                kind="stable",
                ignore_index=True
            )

        st.subheader("Preview & Select")
        st.write("Use the **Keep** column to select rows for export.")

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

        if to_export.empty:
            st.warning("No rows selected. Tick at least one row to enable export.")
        else:
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                to_export.to_excel(writer, index=False, sheet_name="Extract")
            buffer.seek(0)
            st.download_button(
                label="Download Excel",
                data=buffer,
                file_name="indicative_allocations.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
else:
    st.info("Upload one or more PDFs, then click **Parse PDFs**.")

