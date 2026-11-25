# Workflow: Extract Financial Statements from an Investor-Relations Page

## Objective
Given a company's investor-relations (IR) page URL, or uploaded reports, find its quarterly, interim and annual reports, download them, and build one Excel workbook with Income Statement, Balance Sheet and Cash Flow sheets, plus GAAP/non-GAAP reconciliations, revenue segments and a log of forward-looking guidance. Each data sheet has all periods as columns.

## Inputs
- IR page URL, i.e. the page that lists results (e.g. `https://ir.mi.com/financial-information/quarterly-results`)
- Number of recent fiscal years to cover (default 3; 0 = everything on the page)
- Cache choices: reuse downloaded files, and reuse previous extractions

## Outputs
- `.tmp/output/<Company>_financials.xlsx` with the sheets Income Statement, Balance Sheet, Cash Flow, GAAP Measures, Non-GAAP Measures, Revenue Segments, Guidance, Checks and Sources. The Streamlit app also offers it as a download.
  - **Checks:** automatic checks per file on the extracted statements: each statement found, assets = liabilities + equity, the activities add up to the net change in cash, opening + change = closing cash, and revenue − cost of sales = gross profit. FAIL or MISSING means verify against the report; n/a means the rows needed were not found. A first row flags uploads from different companies.
  - **GAAP Measures:** the as-reported line (and margin) of every measure the company reconciles to non-GAAP.
  - **Non-GAAP Measures:** a Summary block (each non-GAAP/adjusted line) and a Reconciliations block (GAAP line, then each adjustment, then the non-GAAP line, per measure).
  - **Revenue Segments:** one block per breakdown: "By business segment" (segments, with product lines nested under them), "By geography", and any other disclosed split. Each block has Revenue, Gross profit and Gross margin (%) sub-blocks where disclosed. Published figures only; nothing is calculated.
  - **Guidance:** a log with one row per forward-looking statement per report. Columns: Category (financial / operational / esg / other), Metric, Segment, Guided period, Type (range / point / minimum / maximum / growth / qualitative), Low, High, Unit, Basis, Status (as stated: raised / lowered / reiterated / achieved / new), Issued in, Report period end, Source file, Page, verbatim Quote. Rows are sorted so that the same target from different reports sits together, which shows the revision trail. The header row has an autofilter.
- Intermediates (disposable): `.tmp/reports/<site>/` (downloaded files + `manifest.json` + `documents.json`) and `.tmp/extractions/` (`<hash>_v1.json` for statements, `<hash>_nongaap_v1.json` for non-GAAP, `<hash>_segments_v1.json` for segments, `<hash>_guidance_v1.json` for guidance, `<hash>_pages_v1.json` for the AI page finder's result)

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
   - `tools/extract_financials.py` sends each report to Claude (`claude-opus-5-5`, effort `medium`) with a JSON schema. For PDFs, only the statement pages chosen by `tools/page_finder.py` are sent (see "Page finding" below). Afterwards `tools/checks.py` checks the result. If a statement is missing or its totals don't add up, the extraction is retried once with the AI page finder's pages added, and the better result is kept.
   - `tools/extract_non_gaap.py` runs in parallel as a separate second pass (both modes, always on). It sends only non-GAAP / alternative-performance-measure pages (max 30). A file with none gets no Claude call ($0). The statements pass is not affected by it; if it fails, the app shows a warning and still builds the workbook.
   - `tools/extract_segments.py` runs in parallel as a third pass (both modes, always on). It sends only pages that mention segments / revenue breakdowns **and** carry a table (at least 8 table figures), plus the next page, max 30 pages. A file with no such page gets no Claude call ($0). The other passes are not affected by it; a failure becomes a warning.
   - `tools/extract_guidance.py` runs in parallel as a fourth pass (both modes, always on). Guidance is narrative and scattered, so it sends the document's **text** with `[Page N]` markers, not a PDF. Documents up to 300k characters are sent whole (about $0.10 of input for a results announcement); longer ones send only pages with forward-looking wording plus their neighbours. A file with no forward-looking wording gets no Claude call ($0). The report finder in URL mode is not changed, so guidance comes from the reports it already picks. Upload transcripts or decks to cover those.
   - `tools/generate_excel.py` merges all extractions and writes the workbook (the non-GAAP sheets come from `tools/non_gaap_excel.py`, the segment sheet from `tools/segments_excel.py`, the guidance log from `tools/guidance_excel.py`).
3. **CLI alternative:**
   ```
   py tools/download_reports.py <url> --years 3
   py tools/extract_financials.py .tmp/reports/<site>/documents.json
   py tools/extract_non_gaap.py .tmp/reports/<site>/documents.json
   py tools/extract_segments.py .tmp/reports/<site>/documents.json
   py tools/extract_guidance.py .tmp/reports/<site>/documents.json
   py tools/generate_excel.py .tmp/reports/<site>/extractions.json --non-gaap .tmp/reports/<site>/non_gaap.json --segments .tmp/reports/<site>/segments.json --guidance .tmp/reports/<site>/guidance.json
   ```
   `--non-gaap`, `--segments` and `--guidance` are optional; without them the workbook leaves out those sheets.
4. **After code changes, restart the local app** (Ctrl+C, then `py -m streamlit run streamlit_app.py`). Streamlit re-runs `streamlit_app.py` on save but keeps the old `tools/` modules loaded, which causes errors like `build() got an unexpected keyword argument`.

## Page finding (page_finder.py), for any company
Every company lays its reports out differently, so pages are found by **content**, not by a fixed layout.
1. **Text clean-up.** pypdf text is normalised before matching:
   - Unicode NFKC turns ligature characters into letters.
   - Every hyphen variant becomes `-`; tabs and non-breaking spaces become spaces.
   - For matching only, `fi`/`fl` are folded to `f`, because pypdf often drops the ligature ("fnancial", "proft"; e.g. LVMH).
   - Titles are also matched with all spaces removed (letter-spaced slide titles).
2. **Rules (free).**
   - **Statements.** A page counts when a statement title sits in its first 6 or last 4 lines. If no titled page is found, pages with at least 4 typical line items and 15+ numbers count instead. Typical line items: total assets / equity / current liabilities, or operating / investing / financing activities, or revenue / cost of sales / profit before tax / per share.
     - Contents pages (including dotted-leader indexes) are ignored.
     - Consolidated sets rank above standalone / parent-company sets, and "extracted from" summaries rank lower.
     - Each statement page gets the next page; up to 2 more follow when they still look like the same statement (long bank statements).
     - Maximum 24 pages.
   - **Non-GAAP.** Strong wording (non-GAAP / non-IFRS, alternative performance measures, adjusted…, underlying…, core…, headline earnings, "excluding special items") counts on any page. Broad wording (EBITDA, free cash flow, net debt, organic growth, recurring operations) counts only on table pages with ≥2 mentions.
   - **Segments.** A page counts in either of two ways:
     - segment / breakdown wording plus at least 8 table figures;
     - on a dense table (20+ figures), "division" / "business unit" wording, or two or more region names (EMEA, Americas, Asia Pacific, Europe, China…). This catches companies that never say "segment", e.g. Sartorius' KPI tables with Bioprocess / Lab divisions and EMEA / Americas / Asia | Pacific.

     Grouped numbers in any style (1,234 / 1.234 / 1 234), decimals and 3+ digit numbers count; years don't.
3. **Claude Haiku 5.5 page finder (only when needed; about $0.005–0.03 per report, more for scans).** It runs when the report is scanned, not in English, or the rules can't find one of the three statements. It also runs on the statements retry.
   - **Text reports:** it reads the first part of every page's text and lists the pages per category. The result is cached as `<hash>_pages_v1.json` and shared by all four passes (one call per file).
   - **Scanned or unreadable pages:** a page counts as unreadable when its text is mostly not words, i.e. image-only or a scrambled font encoding (e.g. HSBC). Haiku reads those pages' images in parts of ≤100 pages.
4. **Non-English reports:** every pass adds "This report is in <language>. Write every row label … in English." Labels are translated so periods and reports line up. The guidance pass sends the whole text when it is short enough, because its keywords are English.
5. **Earlier empty results** (non-GAAP, segments, guidance) made with older rules are re-extracted once, when the page finder now finds candidate pages. New results are stamped `page_rules: <PAGE_RULES_VERSION>`, so an empty result made with the current rules is final and is never re-billed. Bump `PAGE_RULES_VERSION` in `page_finder.py` when a rule change should give earlier empty results one more look.

## Non-GAAP extraction rules (extract_non_gaap.py / non_gaap_excel.py)
- "GAAP" means whatever standard the company reports under (US GAAP, IFRS, HKFRS, Ind AS). The basis is recorded and shown on the sheets.
- Every reconciliation is normalized to rows = GAAP line, then adjustments, then non-GAAP line, and columns = periods. Many HK/IFRS reporters (e.g. Xiaomi) print one transposed table per period, with measures as rows and adjustments as columns; Claude transposes and combines these.
- Margins and per-share figures go in the same reconciliation as extra rows labelled "(as reported)" / "(non-IFRS)", flagged unscaled.
- When merging, GAAP and non-GAAP lines are matched by role and position within the measure, not by label, because wording drifts (e.g. "Profit for the period" vs "Profit for the period/year" in Q4 reports). Adjustments are matched by label. Newest report wins; units follow the newest report.

## Revenue segment rules (extract_segments.py / segments_excel.py)
- One breakdown per dimension per report. When the same split appears in both the management discussion and the segment note, Claude merges them into the most granular version (Xiaomi: the RMB'000 note figures are used over the MD&A's RMB-million tables).
- Printed "% of total revenue" and YoY columns, segment operating profit, cost of sales, non-financial KPIs (shipments, ASP, users) and narrative-only figures are skipped.
- If tables in one breakdown use different units, Claude converts them to one unit and says so in the unit label (e.g. a prior quarter shown only in RMB millions becomes rounded RMB'000).
- When merging, lines are matched by metric + label + occurrence, **not by parent**. So a line that moved under a new segment (Xiaomi's Smartphones moved under "Smartphone × AIoT" in 2025) keeps one continuous series, and its nesting follows the newest report. Total lines are matched by metric alone and labelled "Total". Renamed lines (e.g. "Mainland China" → "Chinese Mainland", "Others" → "Other related businesses") stay separate rows, as published.
- Check: each breakdown's revenue Total should equal Revenue on the Income Statement sheet for the same period.

## Guidance rules (extract_guidance.py / guidance_excel.py)
- **Captured:** every forward-looking statement about the company's own future, whether financial, operational, ESG or other, and whether numeric or qualitative.
- **Skipped:** historical results, third-party forecasts (IMF, Omdia), forward-looking-statement disclaimers and accounting estimates. Boilerplate such as "expected credit loss" and "expected useful life" is also ignored by the "does this file have any forward-looking wording" check.
- Values are copied as printed (no rescaling). A point value fills both Low and High, "exceed / at least" fills Low only, and "up to" fills High only.
- Claude is asked to reuse one short metric name per target (e.g. "EV deliveries") so revisions line up. Status is only filled when the report itself says raised, lowered, reiterated or achieved.
- **No merging:** every statement from every report is kept. "Issued in" is the report's period: from the IR page in URL mode, otherwise from the statements pass of the same file. As on the Sources sheet, a half-year report shows as "H1", because the longest period at that date wins.

## Merge rules (generate_excel.py)
- Column = (period end date, duration in months). When two reports cover the same column, the newest report wins, so restated figures are used.
- Row = normalized label + occurrence number within the statement. Rows that appear only in older reports are inserted after the row they followed there.
- Values are converted to the newest report's unit (thousand/million/billion). Per-share amounts and share counts are left unconverted.

## Error Handling
- **HTTP 403 / "Access Denied":** many IR sites sit behind Akamai and block headless browsers. The tool retries automatically with a visible Chromium window, which opens briefly. If that also fails, the site needs manual download, so drop the files in `.tmp/reports/<site>/` and edit `documents.json`.
- **Few or no candidate links:** the page may use year dropdowns or tabs that load content on demand. Use the page URL for a specific year if one exists, or extend `_expand_page()` for that site's pattern.
- **"This statement was not found" although the PDF has it:** run `py tools/page_finder.py <file>`. It prints the language, scanned/unreadable pages, and the pages chosen per category; this is free. Add `--ai` to also run the Haiku page finder, for about $0.01.
  - If the rules miss a layout, the AI page finder and the retry normally recover it; the Checks sheet shows MISSING otherwise.
  - A saved statements result with no statements at all is never reused.
  - Tesla decks taught the head/tail title check and the letter-spaced titles, and LVMH the ligature clean-up.
- **"Could not locate statement pages":** neither the rules nor the AI page finder found them, and the PDF has more than 120 pages, so sending it whole would be expensive. PDFs of 120 pages or fewer are sent whole.
- **Checks sheet shows FAIL:** compare the figure with the report. Common real causes:
  - a statement whose totals include rows the extraction split differently
  - a cash flow whose "net change" already includes the FX effect (handled), or excludes restricted cash
  - a bank layout without the usual totals
  If the extraction is wrong, delete the `<hash>_v1.json` cache and re-run.
- **Shell scripts that write Python code:** in this Git Bash setup, `\\b` inside a heredoc arrived as a backspace character in the written file, even with a quoted `'EOF'`. Use the Write/Edit tools for code containing backslashes.
- **A statement missing for a period:** some quarterly announcements omit the cash flow statement (e.g. Q1/Q3 in some markets). This is expected; the cell stays blank.
- **Wrong numbers:** delete the matching file in `.tmp/extractions/` (or untick "Reuse previous extractions") and re-run. If it recurs, tighten `SYSTEM` in `extract_financials.py` and bump `PROMPT_VERSION`. For non-GAAP figures, delete the `_nongaap_` file instead and bump `NON_GAAP_PROMPT_VERSION` in `extract_non_gaap.py`; this leaves the statement caches untouched.
- **Non-GAAP sheets empty:** expected for 10-Q/10-K, full annual reports and other filings without adjusted measures. Earnings releases and results announcements are where reconciliations usually appear.
- **A non-GAAP line split into two rows:** a measure was named differently across reports (the `measure` field). Tighten the naming rule in `extract_non_gaap.py` `SYSTEM` and bump the version.
- **Wrong or missing segment figures:** delete the `_segments_` cache file and re-run. If a segment table was missed, check that its page passes the filter: `select_segment_pages` in `extract_segments.py` (keywords, `MIN_TABLE_NUMBERS`). Prompt changes need `SEGMENTS_PROMPT_VERSION` bumped; the other caches are untouched.
- **Guidance missing or wrong:** delete the `_guidance_` cache file and re-run. If it recurs, tighten `SYSTEM` in `extract_guidance.py` and bump `GUIDANCE_PROMPT_VERSION`. To check a quote, search the PDF for it. Footnote markers can sit inside sentences ("…in August 2024 ¹⁹."), so an exact text search may miss it.
- **The same target shows under two metric names:** Claude named it differently in two reports. Filter the Guidance sheet on Metric, or tighten the naming rule in the prompt.
- **Segment Total doesn't match Income Statement revenue:** small differences are expected when a period was only available in a rounded unit (millions). Larger gaps mean a line was missed; re-extract that file.

## Notes and Constraints
- **Cost:** classification is about $0.01–0.05 per page scan. Extraction is roughly $0.10–0.40 per results announcement after page filtering, and more for full annual reports. The non-GAAP pass adds about $0.15 per Xiaomi announcement (12–16 pages sent), and $0 when no non-GAAP wording is found. The segment pass adds about $0.13–0.34 per Xiaomi announcement (9–27 pages sent; Q4/annual results are the largest). The guidance pass adds about $0.18–0.21 per Xiaomi announcement (whole text, about 100k characters). Cached extractions are free. The app shows the actual cost after each run, including all four passes.
- **Playwright must stay in the main Streamlit thread;** only the Claude extraction calls run in parallel (4 workers).
- **Model:** `claude-opus-5-5` with server-side refusal fallback enabled (`fallbacks="default"`). The page finder uses `claude-haiku-5-5` (effort `low`, no server-side fallback for Haiku), priced at $0.10 / $0.50 per million tokens up to 100K prompt tokens and $0.50 / $2.50 beyond (`common.usage_cost`).
- **Units:** `unit_multiplier` accepts 1, 10^3, 10^4 (万), 10^5 (lakh), 10^6, 10^7 (crore), 10^8 (億/亿) and 10^9. The merge converts by ratio, so mixed units across reports are fine.

## Site notes
- **Tesla quarterly "Update" decks (ir.tesla.com, about 31–33 slides):** statements are on slides about 25–29 (Statement of Operations, Balance Sheet, Statement of Cash Flows), each with 5 quarterly columns. The slide titles are letter-spaced, and on the operations and cash flow slides they extract at the bottom of the page. The Outlook slide mentions "strong balance sheet" and also matches the filter, which is harmless. About 6 pages and $0.20 per deck. GAAP/non-GAAP reconciliations are on slides about 28–31, including TTM tables.
- **ir.mi.com (Xiaomi, HKEX 1810):** headless is blocked (Akamai), but a visible browser works. Each quarter has "Announcement" (full statements, about 46 pages, statements on about pages 30–34), "Presentation" (slides) and "Webcast". The Q4 announcement is the annual results. The quarterly page lists about 2018–present. Separate "Annual & Interim Reports" page exists. Clicking the nav menu's `aria-expanded` toggles navigates away, so `_expand_page` skips nav/header elements. Non-IFRS: one reconciliation ("Profit for the period" plus net margin) with 5 adjustments (share-based comp, fair-value changes on investments, amortization of acquired intangibles, changes in value of financial liabilities to investors, income tax effects), printed as one transposed table per period, about 2 pages before the financial statements. Segments: since 2025 there are 2 segments ("Smartphone × AIoT" with Smartphones / IoT and lifestyle products / Internet services / Other related businesses, and "Smart EV, AI and other new initiatives"), with gross profit and margin in the MD&A. Until 2022 there were 4 flat segments (Smartphones, IoT and lifestyle products, Internet services, Others). Geography is Chinese Mainland vs rest of the world, in the notes (quarterly in Q1–Q3 announcements, full year only in Q4). Guidance: there is no formal financial guidance. Expect about 4–9 narrative items per announcement, mostly EV delivery targets (FY2024: 120k in the Q2 report, then 130k in Q3; FY2026: 550k), R&D spend (>RMB200bn over 2026–2030), buybacks and product launch dates, all in the Business Review section (pages 3–8).
