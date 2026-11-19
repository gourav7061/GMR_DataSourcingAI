# Workflow: Extract Financial Statements from an Investor-Relations Page

## Objective
Given a company's investor-relations (IR) page URL, find its quarterly, interim and annual reports, download them, and build one Excel workbook with an Income Statement, Balance Sheet and Cash Flow sheet. Each sheet has all periods as columns.

## Inputs
- IR page URL, i.e. the page that lists results (e.g. `https://ir.mi.com/financial-information/quarterly-results`)
- Number of recent fiscal years to cover (default 3; 0 = everything on the page)
- Cache choices: reuse downloaded files, and reuse previous extractions

## Outputs
- `.tmp/output/<Company>_financials.xlsx` with the sheets Income Statement, Balance Sheet, Cash Flow and Sources. The Streamlit app also offers it as a download.
- Intermediates (disposable): `.tmp/reports/<site>/` (downloaded files + `manifest.json` + `documents.json`) and `.tmp/extractions/` (one JSON per file hash)

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
   - `tools/generate_excel.py` merges all extractions and writes the workbook.
3. **CLI alternative:**
   ```
   py tools/download_reports.py <url> --years 3
   py tools/extract_financials.py .tmp/reports/<site>/documents.json
   py tools/generate_excel.py .tmp/reports/<site>/extractions.json
   ```

## Merge rules (generate_excel.py)
- Column = (period end date, duration in months). When two reports cover the same column, the newest report wins, so restated figures are used.
- Row = normalized label + occurrence number within the statement. Rows that appear only in older reports are inserted after the row they followed there.
- Values are converted to the newest report's unit (thousand/million/billion). Per-share amounts and share counts are left unconverted.

## Error Handling
- **HTTP 403 / "Access Denied":** many IR sites sit behind Akamai and block headless browsers. The tool retries automatically with a visible Chromium window, which opens briefly. If that also fails, the site needs manual download, so drop the files in `.tmp/reports/<site>/` and edit `documents.json`.
- **Few or no candidate links:** the page may use year dropdowns or tabs that load content on demand. Use the page URL for a specific year if one exists, or extend `_expand_page()` for that site's pattern.
- **"Could not locate statement pages":** the PDF is likely scanned or uses unusual titles. PDFs of 120 pages or fewer are sent whole; larger ones are skipped to avoid cost.
- **A statement missing for a period:** some quarterly announcements omit the cash flow statement (e.g. Q1/Q3 in some markets). This is expected; the cell stays blank.
- **Wrong numbers:** delete the matching file in `.tmp/extractions/` (or untick "Reuse previous extractions") and re-run. If it recurs, tighten `SYSTEM` in `extract_financials.py` and bump `PROMPT_VERSION`.

## Notes and Constraints
- **Cost:** classification is about $0.01–0.05 per page scan. Extraction is roughly $0.10–0.40 per results announcement after page filtering, and more for full annual reports. Cached extractions are free. The app shows the actual cost after each run.
- **Playwright must stay in the main Streamlit thread;** only the Claude extraction calls run in parallel (4 workers).
- **Model:** `claude-opus-5-5` with server-side refusal fallback enabled (`fallbacks="default"`).

## Site notes
- **ir.mi.com (Xiaomi, HKEX 1810):** headless is blocked (Akamai), but a visible browser works. Each quarter has "Announcement" (full statements, about 46 pages, statements on about pages 30–34), "Presentation" (slides) and "Webcast". The Q4 announcement is the annual results. The quarterly page lists about 2018–present. Separate "Annual & Interim Reports" page exists. Clicking the nav menu's `aria-expanded` toggles navigates away, so `_expand_page` skips nav/header elements.
