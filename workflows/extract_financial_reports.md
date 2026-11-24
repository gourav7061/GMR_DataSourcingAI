# Workflow: Extract Financial Statements from an Investor-Relations Page

## Objective
Given a company's investor-relations (IR) page URL, find its quarterly, interim and annual reports, download them, and build one Excel workbook with an Income Statement, Balance Sheet and Cash Flow sheet. Each sheet has all periods as columns.

## Inputs
- IR page URL, i.e. the page that lists results (e.g. `https://ir.mi.com/financial-information/quarterly-results`)
- Number of recent fiscal years to cover (default 3; 0 = everything on the page)
- Cache choices: reuse downloaded files, and reuse previous extractions

## Outputs
- `.tmp/output/<Company>_financials.xlsx` with the sheets Income Statement, Balance Sheet, Cash Flow, GAAP Measures, Non-GAAP Measures and Sources. The Streamlit app also offers it as a download.
  - **GAAP Measures:** the as-reported line (and margin) of every measure the company reconciles to non-GAAP.
  - **Non-GAAP Measures:** a Summary block (each non-GAAP/adjusted line) and a Reconciliations block (GAAP line, then each adjustment, then the non-GAAP line, per measure).
- Intermediates (disposable): `.tmp/reports/<site>/` (downloaded files + `manifest.json` + `documents.json`) and `.tmp/extractions/` (`<hash>_v1.json` for statements, `<hash>_nongaap_v1.json` for non-GAAP)

## Setup (once)
```
py -m pip install -r requirements.txt
py -m playwright install chromium
```
Add `ANTHROPIC_API_KEY=sk-ant-...` to `.env`.

## Steps — Upload mode (tab "Upload files")
1. Run `py -m streamlit run streamlit_app.py`, then drag in or browse to the reports. Upload one company per batch. Accepted types: PDF, .xlsx/.xlsm, .csv, .htm/.html. Old .xls must be re-saved as .xlsx.
2. Click **Extract**. Files are saved to `.tmp/uploads/<hash>/`; identical files are skipped with a warning.
3. No period or company input is needed. The period of each file is the latest column date Claude finds in its statements (at the same date the longest duration wins, e.g. FY over Q4). The company name is the one most files agree on. Both appear on the Sources sheet with the original file name.
4. Extractions are cached by file hash, so re-uploading the same file is free, including a file first fetched via URL mode.

## Steps — URL mode (tab "From IR page URL")
1. **Run the app:** `py -m streamlit run streamlit_app.py`, open the second tab, paste the URL, then click **1. Find reports**.
   - `tools/download_reports.py` opens the page in Chromium, collects every document-like link with its context (headings, link text, file name), and asks Claude (effort `low`, a few cents) to pick one statement-bearing document per period.
2. Review the table, untick anything unwanted, then click **2. Download and extract**.
   - Downloads go through the same browser session (cookies and bot checks), cached by URL.
   - `tools/extract_financials.py` sends each report to Claude (`claude-opus-5-5`, effort `medium`) with a JSON schema. For PDFs, only pages whose top lines carry a statement title (plus the page after each) are sent.
   - `tools/extract_non_gaap.py` runs in parallel as a separate second pass (both modes, always on). It sends only pages mentioning non-GAAP / non-IFRS / adjusted measures (full-page text search, max 30 pages). A file with no such wording gets no Claude call ($0). The statements pass is not affected by it; if it fails, the app shows a warning and still builds the workbook.
   - `tools/generate_excel.py` merges all extractions and writes the workbook (the non-GAAP sheets come from `tools/non_gaap_excel.py`).
3. **CLI alternative:**
   ```
   py tools/download_reports.py <url> --years 3
   py tools/extract_financials.py .tmp/reports/<site>/documents.json
   py tools/extract_non_gaap.py .tmp/reports/<site>/documents.json
   py tools/generate_excel.py .tmp/reports/<site>/extractions.json --non-gaap .tmp/reports/<site>/non_gaap.json
   ```
   Without `--non-gaap`, the workbook has only the original four sheets.

## Non-GAAP extraction rules (extract_non_gaap.py / non_gaap_excel.py)
- "GAAP" means whatever standard the company reports under (US GAAP, IFRS, HKFRS, Ind AS). The basis is recorded and shown on the sheets.
- Every reconciliation is normalized to rows = GAAP line, then adjustments, then non-GAAP line, and columns = periods. Many HK/IFRS reporters (e.g. Xiaomi) print one transposed table per period, with measures as rows and adjustments as columns; Claude transposes and combines these.
- Margins and per-share figures go in the same reconciliation as extra rows labelled "(as reported)" / "(non-IFRS)", flagged unscaled.
- When merging, GAAP and non-GAAP lines are matched by role and position within the measure, not by label, because wording drifts (e.g. "Profit for the period" vs "Profit for the period/year" in Q4 reports). Adjustments are matched by label. Newest report wins; units follow the newest report.

## Merge rules (generate_excel.py)
- Column = (period end date, duration in months). When two reports cover the same column, the newest report wins, so restated figures are used.
- Row = normalized label + occurrence number within the statement. Rows that appear only in older reports are inserted after the row they followed there.
- Values are converted to the newest report's unit (thousand/million/billion). Per-share amounts and share counts are left unconverted.

## Error Handling
- **HTTP 403 / "Access Denied":** many IR sites sit behind Akamai and block headless browsers. The tool retries automatically with a visible Chromium window, which opens briefly. If that also fails, the site needs manual download, so drop the files in `.tmp/reports/<site>/` and edit `documents.json`.
- **Few or no candidate links:** the page may use year dropdowns or tabs that load content on demand. Use the page URL for a specific year if one exists, or extend `_expand_page()` for that site's pattern.
- **"Could not locate statement pages":** the PDF is likely scanned or uses unusual titles. PDFs of 120 pages or fewer are sent whole; larger ones are skipped to avoid cost.
- **A statement missing for a period:** some quarterly announcements omit the cash flow statement (e.g. Q1/Q3 in some markets). This is expected; the cell stays blank.
- **Wrong numbers:** delete the matching file in `.tmp/extractions/` (or untick "Reuse previous extractions") and re-run. If it recurs, tighten `SYSTEM` in `extract_financials.py` and bump `PROMPT_VERSION`. For non-GAAP figures, delete the `_nongaap_` file instead and bump `NON_GAAP_PROMPT_VERSION` in `extract_non_gaap.py`; this leaves the statement caches untouched.
- **Non-GAAP sheets empty:** expected for 10-Q/10-K, full annual reports and other filings without adjusted measures. Earnings releases and results announcements are where reconciliations usually appear.
- **A non-GAAP line split into two rows:** a measure was named differently across reports (the `measure` field). Tighten the naming rule in `extract_non_gaap.py` `SYSTEM` and bump the version.

## Notes and Constraints
- **Cost:** classification is about $0.01–0.05 per page scan. Extraction is roughly $0.10–0.40 per results announcement after page filtering, and more for full annual reports. The non-GAAP pass adds about $0.15 per Xiaomi announcement (12–16 pages sent), and $0 when no non-GAAP wording is found. Cached extractions are free. The app shows the actual cost after each run, including both passes.
- **Playwright must stay in the main Streamlit thread;** only the Claude extraction calls run in parallel (4 workers).
- **Model:** `claude-opus-5-5` with server-side refusal fallback enabled (`fallbacks="default"`).

## Site notes
- **ir.mi.com (Xiaomi, HKEX 1810):** headless is blocked (Akamai), but a visible browser works. Each quarter has "Announcement" (full statements, about 46 pages, statements on about pages 30–34), "Presentation" (slides) and "Webcast". The Q4 announcement is the annual results. The quarterly page lists about 2018–present. Separate "Annual & Interim Reports" page exists. Clicking the nav menu's `aria-expanded` toggles navigates away, so `_expand_page` skips nav/header elements. Non-IFRS: one reconciliation ("Profit for the period" plus net margin) with 5 adjustments (share-based comp, fair-value changes on investments, amortization of acquired intangibles, changes in value of financial liabilities to investors, income tax effects), printed as one transposed table per period, about 2 pages before the financial statements.
