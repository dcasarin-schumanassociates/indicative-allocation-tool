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
2. Click **Parse PDFs** to extract rows (fresh parse each time).
3. Use the **Keep** checkboxes to choose rows to export.
4. Click **Download Excel** to export your selection.
    """
)

# EASY CACHE-BUSTER: increment this when you change parser logic
PARSER_VERSION = "v3.2"

# A manual re-parse button that ensures a clean state
col_a, col_b = st.columns([1,1])
with col_a:
    parse_clicked = st.button("Parse PDFs", type="primary")
with col_b:
    reparse_clicked = st.button("Re-parse (force fresh)")

# Optional: a tiny debug toggle to verify Funding/Scope and wrapped Beschreibung
show_debug = st.checkbox("Show debug context columns", value=False, help="Displays the parsed context to verify Funding/Scope and long descriptions.")

uploaded_files = st.file_uploader(
    "Upload PDF files",
    type=["pdf"],
    accept_multiple_files=True,
    help="You can upload multiple PDFs at once."
)

def parse_many(files: List[st.runtime.uploaded_file_manager.UploadedFile]) -> pd.DataFrame:
    all_rows = []
    for f in files:
        try:
            # IMPORTANT: read from the beginning every time
            if hasattr(f, "seek"):
                try:
                    f.seek(0)
                except Exception:
                    pass
            df = parse_pdf_filelike(f)
            if df is not None and not df.empty:
                all_rows.append(df)
        except Exception as e:
            st.warning(f"Failed to parse {f.name}: {e}")
    if not all_rows:
        return pd.DataFrame()
    out = pd.concat(all_rows, ignore_index=True)
    out.insert(0, "Keep", True)
    return out

if (parse_clicked or reparse_clicked) and uploaded_files:
    # Hard cache-bust: include parser version + file names + sizes in a throwaway state key
    st.session_state["cache_bust_key"] = (
        PARSER_VERSION,
        tuple((f.name, f.size) for f in uploaded_files),
        parse_clicked,  # toggling these will also change the key
        reparse_clicked
    )

    with st.spinner("Parsing PDFs..."):
        df = parse_many(uploaded_files)

    if df is None or df.empty:
        st.info("No rows extracted. Please verify the PDFs contain the expected sections.")
    else:
        # Optional debug visibility (helps verify Funding/Scope/long Beschreibung)
        if show_debug:
            dbg_cols = [c for c in [
                "Indikative Aufschlüsselung (Section)",
                "Priorität",
                "Spezifisches Ziel",
                "Funding Programme",
                "Scope",
                "Dimension",
                "Code",
                "Beschreibung",
                "Betrag (EUR)",
            ] if c in df.columns]
            st.expander("Parsed rows (debug view)").dataframe(df[["Keep"] + dbg_cols], use_container_width=True)

        st.subheader("Preview & Select")
        st.write("Use the **Keep** column to select rows for export. You can sort and filter as needed.")
        edited = st.data_editor(
            df,
            use_container_width=True,
            num_rows="dynamic",
            hide_index=True,
            key=f"preview_editor_{st.session_state.get('cache_bust_key')}",
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
