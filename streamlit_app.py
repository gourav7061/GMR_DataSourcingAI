import hashlib
import hmac
import os
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "tools"))
from common import REPORTS_DIR, UPLOAD_DIR, get_client, safe_filename, site_slug  # noqa: E402
from download_reports import discover, download, filter_years, open_site  # noqa: E402
from extract_financials import extract_one  # noqa: E402
from extract_non_gaap import extract_non_gaap_one  # noqa: E402
from generate_excel import SHEETS, build  # noqa: E402
from non_gaap_excel import EMPTY_MSG, NG_SHEETS, sheet_rows  # noqa: E402
from extract_segments import extract_segments_one  # noqa: E402
from segments_excel import EMPTY_MSG as SEG_EMPTY_MSG, SEGMENTS_SHEET, sheet_rows as segment_rows  # noqa: E402
from extract_guidance import extract_guidance_one  # noqa: E402
from guidance_excel import COLUMNS as GD_COLUMNS, EMPTY_MSG as GD_EMPTY_MSG, GUIDANCE_SHEET  # noqa: E402

UPLOAD_TYPES = ["pdf", "xlsx", "xlsm", "csv", "htm", "html"]
# the IR-URL mode needs a visible browser window, which cloud hosts (Linux, no display) can't open
URL_MODE_AVAILABLE = sys.platform == "win32" or sys.platform == "darwin" or bool(os.environ.get("DISPLAY"))


def secret(name: str) -> str | None:
    try:
        return st.secrets.get(name)
    except Exception:
        return None


st.set_page_config(page_title="IR Financials Extractor", layout="wide")

for key in ("ANTHROPIC_API_KEY",):
    if not os.environ.get(key) and secret(key):
        os.environ[key] = secret(key)

app_password = secret("APP_PASSWORD")
if not app_password and not URL_MODE_AVAILABLE:
    # hosted (Linux, no display): never run without a password, since every extraction spends API credit
    st.error("APP_PASSWORD is not set in this app's secrets, so the app is locked.")
    st.stop()
if app_password and not st.session_state.get("authenticated"):
    st.title("Financial statements extractor")
    entered = st.text_input("Password", type="password")
    if entered:
        if hmac.compare_digest(entered, app_password):
            st.session_state.authenticated = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()
st.title("Financial statements extractor")
st.caption("Extracts the income statement, balance sheet and cash flow, plus GAAP to non-GAAP reconciliations "
           "revenue segments and guidance, from quarterly and annual reports into one Excel workbook, "
           "with every period as a column.")

try:
    client = get_client()
except RuntimeError as e:
    st.error(str(e))
    st.stop()


class Log:
    def __init__(self, box):
        self.box, self.lines, self.lock = box, [], threading.Lock()

    def __call__(self, msg):
        with self.lock:
            self.lines.append(str(msg))

    def flush(self):
        with self.lock:
            lines, self.lines = self.lines, []
        for line in lines:
            self.box.write(line)


def run_extraction(docs: list[dict], company: str | None, reuse: bool, status, log: Log):
    """Extract every doc in parallel, build the workbook, store results in session state. company=None -> detect."""
    status.update(label=f"Extracting {len(docs)} reports with Claude...")
    progress = st.progress(0.0)
    ok, failed, cost = [], [], 0.0
    ng_ok, ng_failed = [], []
    seg_ok, seg_failed = [], []
    gd_ok, gd_failed = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(extract_one, d, client, not reuse, log): ("statements", d) for d in docs}
        futures.update({pool.submit(extract_non_gaap_one, d, client, not reuse, log): ("non_gaap", d)
                        for d in docs})
        futures.update({pool.submit(extract_segments_one, d, client, not reuse, log): ("segments", d)
                        for d in docs})
        futures.update({pool.submit(extract_guidance_one, d, client, not reuse, log): ("guidance", d)
                        for d in docs})
        for i, fut in enumerate(as_completed(futures), start=1):
            kind, d = futures[fut]
            name = d.get("file_name") or d["period_label"]
            try:
                r = fut.result()
                {"statements": ok, "non_gaap": ng_ok, "segments": seg_ok, "guidance": gd_ok}[kind].append(r)
                cost += r["cost_usd"]
            except Exception as e:
                if kind == "statements":
                    failed.append((name, str(e)))
                    log(f"FAILED {name}: {e}")
                elif kind == "non_gaap":
                    ng_failed.append((name, str(e)))
                    log(f"FAILED non-GAAP {name}: {e}")
                elif kind == "segments":
                    seg_failed.append((name, str(e)))
                    log(f"FAILED segments {name}: {e}")
                else:
                    gd_failed.append((name, str(e)))
                    log(f"FAILED guidance {name}: {e}")
            log.flush()
            progress.progress(i / len(futures))
    if not ok:
        raise RuntimeError("No report could be extracted.")
    # the guidance pass runs in parallel, so it can't see a new file's period yet: take it from the statements result
    periods = {r["source"]["file"]: r["source"] for r in ok}
    for g in gd_ok:
        s = periods.get(g["source"]["file"])
        if s and s.get("period_end"):
            g["source"].update(period_end=s["period_end"], period_label=s["period_label"])
    if not company:
        names = Counter(r["company"] for r in ok if r.get("company"))
        company = names.most_common(1)[0][0] if names else "Company"
        if len(names) > 1:
            log(f"Note: the files name more than one company ({', '.join(names)}); using '{company}'.")
            log.flush()
    path, merged = build(company, ok, non_gaap=ng_ok, segments=seg_ok, guidance=gd_ok)
    st.session_state.results = {"path": str(path), "merged": merged, "failed": failed, "ng_failed": ng_failed,
                                "seg_failed": seg_failed, "gd_failed": gd_failed, "cost": cost, "n": len(ok)}
    status.update(label=f"Done: {len(ok)} reports extracted, Claude cost ${cost:.2f}", state="complete")


def save_upload(f) -> tuple[Path, str]:
    data = f.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    folder = UPLOAD_DIR / digest[:12]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / safe_filename(f.name)
    if not path.exists():
        path.write_bytes(data)
    return path, digest


def show_results():
    res = st.session_state.get("results")
    if not res:
        return
    st.divider()
    path = Path(res["path"])
    st.download_button("Download Excel workbook", path.read_bytes(), file_name=path.name, type="primary",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    for label, err in res["failed"]:
        st.warning(f"{label} was not extracted: {err}")
    for label, err in res.get("ng_failed", []):
        st.warning(f"Non-GAAP measures from {label} were not extracted: {err}")
    for label, err in res.get("seg_failed", []):
        st.warning(f"Revenue segments from {label} were not extracted: {err}")
    for label, err in res.get("gd_failed", []):
        st.warning(f"Guidance from {label} was not extracted: {err}")
    tabs = st.tabs(list(SHEETS.values()) + list(NG_SHEETS.values()) + [SEGMENTS_SHEET, GUIDANCE_SHEET])
    gd_rows, gd_tab = res["merged"].get("guidance") or [], tabs[-1]
    if not gd_rows:
        gd_tab.info(GD_EMPTY_MSG)
    else:
        gd_tab.caption("One row per forward-looking statement and report, as published. "
                       "Rows of the same metric and period from different reports show how guidance changed.")
        gd_tab.dataframe(pd.DataFrame([[r.get(k) for _, k, _ in GD_COLUMNS] for r in gd_rows],
                                      columns=[h for h, _, _ in GD_COLUMNS]),
                         hide_index=True, use_container_width=True, height=600,
                         column_config={"Quote": st.column_config.TextColumn(width="large")})
    seg, seg_tab = res["merged"].get("segments"), tabs[len(SHEETS) + len(NG_SHEETS)]
    seg_rows = segment_rows(seg) if seg else []
    if not seg_rows:
        seg_tab.info(SEG_EMPTY_MSG)
    else:
        seg_tab.caption(f"Unit: {seg['unit_label']} (gross margins in %)")
        seg_tab.dataframe(pd.DataFrame(
            [["    " * r["level"] + r["label"]] + (r["values"] or [None] * len(seg["columns"])) for r in seg_rows],
            columns=["Line item"] + [f"{c['label']} ({c['period_end']})" for c in seg["columns"]],
        ), hide_index=True, use_container_width=True, height=600)
    ng = res["merged"].get("non_gaap")
    for kind, tab in zip(NG_SHEETS, tabs[len(SHEETS):]):
        rows = sheet_rows(ng, kind) if ng else []
        if not rows:
            tab.info(EMPTY_MSG)
            continue
        tab.caption(f"Unit: {ng['unit_label']} · Basis: {ng['gaap_basis'] or 'GAAP'}")
        df = pd.DataFrame(
            [["    " * r["level"] + r["label"]] + (r["values"] or [None] * len(ng["columns"])) for r in rows],
            columns=["Line item"] + [f"{c['label']} ({c['period_end']})" for c in ng["columns"]],
        )
        tab.dataframe(df, hide_index=True, use_container_width=True, height=600)
    for stype, tab in zip(SHEETS, tabs):
        data = res["merged"][stype]
        if not data["rows"]:
            tab.info("This statement was not found in the reports.")
            continue
        tab.caption(f"Unit: {data['unit_label']}")
        df = pd.DataFrame(
            [["    " * r["level"] + r["label"]] + r["values"] for r in data["rows"]],
            columns=["Line item"] + [f"{c['label']} ({c['period_end']})" for c in data["columns"]],
        )
        tab.dataframe(df, hide_index=True, use_container_width=True, height=600)


upload_tab, url_tab = st.tabs(["Upload files", "From IR page URL"])

with upload_tab:
    files = st.file_uploader("Upload reports (one company per batch)", type=UPLOAD_TYPES,
                             accept_multiple_files=True,
                             help="Results announcements, quarterly/interim/annual reports, 10-Q/10-K, "
                                  "as PDF, Excel (.xlsx), CSV or saved web pages (.htm/.html).")
    reuse_up = st.checkbox("Reuse previous extractions (free)", value=True, key="reuse_up")
    docs, seen = [], {}
    for f in files or []:
        path, digest = save_upload(f)
        if digest in seen:
            st.warning(f"'{f.name}' is the same file as '{seen[digest]}' and will be skipped.")
            continue
        seen[digest] = f.name
        docs.append({"period_label": f.name, "period_end": "", "doc_type": "uploaded", "href": None,
                     "file": str(path), "file_name": f.name})
    if docs:
        st.dataframe(pd.DataFrame([{"File": d["file_name"], "Size": f"{Path(d['file']).stat().st_size / 1024:,.0f} KB"}
                                   for d in docs]), hide_index=True, use_container_width=True)
        st.caption(f"{len(docs)} file{'s' if len(docs) != 1 else ''}. Rough cost: about \\$0.10-\\$0.40 per results "
                   f"announcement, more for full annual reports, plus about \\$0.05-\\$0.25 for non-GAAP "
                   f"reconciliations, \\$0.10-\\$0.30 for revenue segments and \\$0.10-\\$0.30 for guidance "
                   f"where present. "
                   f"Files extracted before are free. "
                   f"The period of each file is detected automatically.")
    if st.button("Extract", type="primary", disabled=not docs, key="extract_up"):
        with st.status("Starting...", expanded=True) as status:
            log = Log(status)
            try:
                run_extraction(docs, None, reuse_up, status, log)
            except Exception as e:
                log.flush()
                status.update(label=f"Failed: {e}", state="error")

def url_mode():
    url = st.text_input("Investor-relations page URL",
                        placeholder="https://ir.example.com/financial-information/quarterly-results")
    c1, c2, c3 = st.columns(3)
    years = c1.number_input("Most recent fiscal years (0 = all)", min_value=0, max_value=30, value=3)
    reuse_downloads = c2.checkbox("Reuse downloaded reports", value=True)
    reuse_extractions = c3.checkbox("Reuse previous extractions (free)", value=True)
    st.caption("Some sites block automated browsers; if so, a Chromium window opens briefly while the page loads.")

    if st.button("1. Find reports", type="primary", disabled=not url):
        with st.status("Reading the page...", expanded=True) as status:
            log = Log(status)
            try:
                result, _, cost = discover(url, client=client, log=log)
                log.flush()
                st.session_state.update(found=result, found_url=url)
                status.update(label=f"Found {len(result['documents'])} reports (classification cost ${cost:.3f})",
                              state="complete")
            except Exception as e:
                log.flush()
                status.update(label=f"Failed: {e}", state="error")

    found = st.session_state.get("found")
    if found and st.session_state.get("found_url") == url:
        st.subheader(found["company"])
        url_docs = filter_years(found["documents"], years or None)
        table = pd.DataFrame([{"Extract": True, "Period": d["period_label"], "Period end": d["period_end"],
                               "Type": d["doc_type"], "Link text": d["text"], "URL": d["href"]} for d in url_docs])
        edited = st.data_editor(table, hide_index=True, use_container_width=True, disabled=list(table.columns[1:]),
                                column_config={"URL": st.column_config.LinkColumn()})
        selected = [d for d, keep in zip(url_docs, edited["Extract"]) if keep]
        st.caption(f"{len(selected)} selected. Rough cost: about \\$0.10-\\$0.40 per results announcement, "
                   f"more for full annual reports, plus about \\$0.05-\\$0.25 for non-GAAP reconciliations "
                   f"\\$0.10-\\$0.30 for revenue segments and \\$0.10-\\$0.30 for guidance where present. "
                   f"Cached extractions are free.")

        if st.button("2. Download and extract", type="primary", disabled=not selected):
            with st.status("Downloading reports...", expanded=True) as status:
                log = Log(status)
                try:
                    with open_site(url, log) as page:
                        downloaded = download(page, selected, REPORTS_DIR / site_slug(url),
                                              force=not reuse_downloads, log=log)
                    log.flush()
                    ready = [d for d in downloaded if d.get("file")]
                    if not ready:
                        raise RuntimeError("No report could be downloaded.")
                    run_extraction(ready, found["company"], reuse_extractions, status, log)
                except Exception as e:
                    log.flush()
                    status.update(label=f"Failed: {e}", state="error")


with url_tab:
    if URL_MODE_AVAILABLE:
        url_mode()
    else:
        st.info("Finding reports from an IR page URL needs a visible browser window, so it only works when the app "
                "runs on your own computer. On this hosted version, download the reports and use the Upload files tab.")

show_results()
