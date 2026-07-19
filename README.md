# Site Scraper

![Architecture diagram](architecture.png)

Opens a site in real Chrome (via Playwright, with stealth patches and
human-like typing/scrolling), then saves a screenshot, the HTML, and a
report on whether Cloudflare/Akamai were detected.

## Install

```bash
pip install playwright playwright-stealth
playwright install chrome
```

Run as a regular (non-root) user — root forces `--no-sandbox`, which is
itself a detectable flag.

## Usage

```bash
python site_scraper.py --url https://example.com
python site_scraper.py --url https://example.com --keyword "search term"
python site_scraper.py --url https://example.com --save-assets
```

No `--url`? You'll be prompted for one.

## Flags

| Flag | Does |
|---|---|
| `--url` | Site to open |
| `--keyword` | Search term to type and submit |
| `--selector` | CSS selector for the search box, if auto-detect fails |
| `--output-dir` | Where run folders go |
| `--save-assets` | Also save assets, page sections, and subpages |
| `--no-sections` | With `--save-assets`, skip section screenshots |
| `--no-subpages` | With `--save-assets`, skip subpage crawling |
| `--max-subpages N` | Cap on subpages crawled |
| `--headed` | Show the browser window |
| `--log-level` | DEBUG / INFO / WARNING / ERROR |

Defaults for anything not passed live in `config.py`.

## Output

```
output/<slug>_<timestamp>/
├── screenshot.png
├── page.html
├── cdn_report.txt
├── assets/       # --save-assets only
├── sections/     # --save-assets only
└── subpages/     # --save-assets only
    └── 01_<slug>/screenshot.png, page.html, sections/
```

`cdn_report.txt` flags Cloudflare/Akamai based on response headers, cookies,
and challenge-page text — a heuristic, not a guarantee.
