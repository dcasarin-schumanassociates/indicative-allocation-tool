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
3. Use the **Keep** checkboxes to choose rows to export.
4. Click **Download Excel** to export your selection.
    """
)

uploaded_files = st.file_uploader(
    "Upload PDF files",
    type=["pdf"],
    accept_multiple_files=True,
    help="You can upload multiple PDFs at once."
)

parse_clicked = st.button("Parse PDFs", type="primary", disabled=(len(uploaded_files) == 0))

@st.cache_data(show_spinner=False)
def _parse_many(files: List[st.runtime.uploaded_file_manager.UploadedFile]) -> pd.DataFrame:
    all_rows = []
    for f in files:
        try:
            df = parse_pdf_filelike(f, source_name=f.name)
            if df is not None and not df.empty:
                all_rows.append(df)
        except Exception as e:
            st.warning(f"Failed to parse {f.name}: {e}")
    if not all_rows:
        return pd.DataFrame()
    out = pd.concat(all_rows, ignore_index=True)
    # Default selection = keep all
    out.insert(0, "Keep", True)
    return out

if parse_clicked:
    with st.spinner("Parsing PDFs..."):
        df = _parse_many(uploaded_files)

    if df is None or df.empty:
        st.info("No rows extracted. Please verify the PDFs contain the expected sections.")
    else:
        st.subheader("Preview & Select")
        st.write("Use the **Keep** column to select rows for export. You can sort and filter as needed.")

        # Interactive editor with a checkbox column
        edited = st.data_editor(
            df,
            use_container_width=True,
            num_rows="dynamic",
            hide_index=True,
            key="preview_editor",
            column_config={
                "Keep": st.column_config.CheckboxColumn("Keep"),
                "Betrag (EUR)": st.column_config.NumberColumn("Betrag (EUR)", step=1, format="%.2f"),
            }
        )

        to_export = edited[edited["Keep"] == True].drop(columns=["Keep"]) if "Keep" in edited.columns else edited

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
