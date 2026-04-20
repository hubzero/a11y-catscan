# a11y-catscan

Multi-engine WCAG accessibility scanner. Crawls a website using
Playwright/Chromium and runs up to four accessibility engines on each
page, all sharing a single browser instance.

## Engines

| Engine | Flag | What it catches | License |
|--------|------|----------------|---------|
| [axe-core](https://github.com/dequelabs/axe-core) (Deque) | `--engine axe` | Color contrast, ARIA, labels, structure — the industry standard | MPL-2.0 |
| [Siteimprove Alfa](https://github.com/Siteimprove/alfa) | `--engine alfa` | Target size, landmarks, enhanced contrast — matches Siteimprove scanner | MIT |
| [IBM Equal Access](https://github.com/IBMa/equal-access) | `--engine ibm` | IBM Accessibility requirements, content landmarks, form validation | Apache-2.0 |
| [HTML_CodeSniffer](https://github.com/nickersk/HTML_CodeSniffer) (Squiz) | `--engine htmlcs` | WCAG parsing rules, heading structure, form submit buttons | BSD-3 |

Run one engine or combine them:

```bash
a11y-catscan.py                                     # axe-core only (default)
a11y-catscan.py --engine alfa                       # Siteimprove Alfa only
a11y-catscan.py --engine axe --engine alfa          # axe + Alfa
a11y-catscan.py --engine axe --engine ibm           # axe + IBM
a11y-catscan.py --engine all                        # all four engines
```

All engines share one Chromium process. Each finding is tagged with its
engine for attribution in reports.

### How the engines work

- **axe-core**, **IBM**, **HTML_CodeSniffer**: JavaScript injected into the
  page, runs in-browser, returns results instantly. Same architecture as
  the browser extensions.
- **Siteimprove Alfa**: Node.js subprocess connects to the shared Chromium
  via CDP (Chrome DevTools Protocol), runs its TypeScript rule engine on
  the already-loaded page. No second page load.

## Quick start

```bash
# Scan with defaults
a11y-catscan.py https://example.com/

# Scan 500 pages with multiple engines and LLM-friendly output
a11y-catscan.py --engine axe --engine alfa --max-pages 500 --llm https://example.com/

# Quick single-page check
a11y-catscan.py --page -q --summary-json https://example.com/fixed-page

# Parallel scanning with authentication
a11y-catscan.py --engine axe --engine alfa --workers 7 --max-pages 1000 https://example.com/
```

## Setup

Requires Python 3.8+ and Node.js 18+.

```bash
pip install playwright pyyaml
playwright install chromium
npm install                    # installs all engines from package.json
```

Copy `a11y-catscan.yaml.example` to `a11y-catscan.yaml` and edit for
your site. The config file is gitignored so each deployment keeps its
own settings without merge conflicts.

## Configuration

All settings in `a11y-catscan.yaml` can be overridden on the command line.

| Setting | CLI flag | Default | Description |
|---|---|---|---|
| `url` | positional arg | — | Starting URL to crawl |
| `level` | `--level` | `wcag21aa` | WCAG conformance level |
| `engine` | `--engine` | `axe` | One or more engines: `axe`, `alfa`, `ibm`, `htmlcs`, `all` |
| `max_pages` | `--max-pages` | 50 | Maximum pages to scan |
| `page_wait` | — | 1 | Seconds to wait after page load (with `--wait-until load`) |
| `wait_until` | `--wait-until` | `networkidle` | Page load strategy: `networkidle`, `load`, `domcontentloaded` |
| `save_every` | `--save-every` | 25 | Flush reports every N pages |
| `output_dir` | `--output-dir` | cwd | Report output directory |
| `exclude_paths` | `--exclude-path` | — | URL path prefixes to skip |
| `exclude_regex` | — | — | Regex patterns to skip |
| `exclude_query` | — | — | Query substrings to skip |
| `include_paths` | `--include-path` | — | Only scan URLs under these prefixes |
| `strip_query_params` | — | — | Query parameters to strip for URL deduplication |
| `niceness` | — | 10 | OS nice level (0–19) |
| `oom_score_adj` | — | 1000 | Linux OOM killer score (1000 = killed first) |
| `allowlist` | `--allowlist` | — | YAML file of known-acceptable incompletes |
| `ignore_robots` | `--ignore-robots` | false | Ignore robots.txt |
| `ignore_certificate_errors` | — | false | Accept self-signed TLS certs |
| `workers` | `--workers` | 1 | Parallel async browser pages |
| `restart_every` | — | 500 | Restart browser every N pages (prevents memory leaks) |
| `chromium_path` | — | — | Path to Chromium (uses Playwright's bundled Chromium by default) |

### WCAG level handling

Each engine maps `--level` to its native ruleset:

| Level | axe-core | Alfa | IBM | HTML_CodeSniffer |
|-------|----------|------|-----|------------------|
| A | `wcag2a, wcag21a` | Filters by SC level | `WCAG_2_1` (A rules) | `WCAG2A` |
| AA | `wcag2a, wcag2aa, wcag21a, wcag21aa` | Filters by SC level | `WCAG_2_1` | `WCAG2AA` |
| AAA | All tags | All rules | `WCAG_2_1` (all) | `WCAG2AAA` |

## Authentication

To scan authenticated pages, configure a login script:

```yaml
auth:
  login_script: login-hubzero.py
```

The login script receives a Playwright browser context and authenticates
via the browser UI. Features:

- **Session persistence**: Cookies saved to `.auth-state.json` after login.
  Subsequent scans restore the session instantly (4s vs 13s startup).
- **Expiry detection**: If the saved session is expired, falls back to
  running the login script automatically.
- **Mid-scan recovery**: If the session expires during a scan, all workers
  drain, the scanner re-authenticates, retests suspect URLs, and resumes.
- **Browser restart**: After every `restart_every` pages, the browser
  restarts to prevent memory leaks. Auth state is restored automatically.

## Analysis flags

After a scan, use these flags to analyze results from previous reports:

| Flag | Description |
|---|---|
| `--violations-from REPORT` | Extract and re-scan only pages with violations from a JSON/JSONL report |
| `--incompletes-from REPORT` | Extract and re-scan only pages with incompletes |
| `--group-by TYPE` | Print grouped summary: `rule`, `selector`, `color`, `reason`, `wcag` |
| `--diff PREV.jsonl` | Compare against a previous scan — show fixed/new/remaining |
| `--rescan PREV.jsonl` | Re-scan only pages that had issues |

```bash
# Group violations by color contrast pair
a11y-catscan.py --engine all --urls pages.txt --group-by color

# Rescan only pages that had violations
a11y-catscan.py --violations-from report.json --engine axe

# Show what changed since last scan
a11y-catscan.py --max-pages 500 --diff baseline.jsonl --llm https://example.com/
```

## Output files

Each scan produces:

| File | Description |
|---|---|
| `*.json` | Full results for every page (violations, incomplete, passes) |
| `*.html` | Human-readable report with summary cards, WCAG criteria table, per-page details |
| `*.jsonl` | Streaming results (one JSON object per line) — used for `--diff` and `--rescan` |
| `*.state.json` | Crawl state (queue + visited URLs) — used for `--resume` to continue later |
| `*.md` | LLM-optimized markdown summary (only with `--llm`) |

Each violation/incomplete in the report includes an `engine` field (`axe`,
`alfa`, `ibm`, `htmlcs`) so you can see which scanner found it.

## Scanning modes

| Flag | Description |
|---|---|
| `--crawl` | Crawl and discover pages from the starting URL (default) |
| `--page URL` | Scan only the given URL, no crawling — fast single-page verify |
| `--urls FILE` | Scan a specific list of URLs from a file (one per line) |
| `--rescan PREV.jsonl` | Re-scan only pages that had issues in a previous scan |
| `--resume STATE.json` | Resume a previous crawl from its saved state file |

## Performance

| Flag | Description |
|---|---|
| `--workers N` | Parallel async browser pages (default: 1). All workers share one Chromium process. |
| `--wait-until` | `networkidle` (default) adapts to each page. `load` uses fixed delay. |

Typical throughput:
- `--engine axe`: ~0.8s/page with 7 workers
- `--engine all`: ~5s/page with 7 workers (Alfa subprocess serializes)
- 39,000+ pages scanned in a single session with `--resume`

## Workflow: scan → fix → verify

```bash
# 1. Full baseline scan with all engines
a11y-catscan.py --engine all --max-pages 500 --llm https://example.com/

# 2. Group results to prioritize fixes
a11y-catscan.py --engine all --urls pages.txt --group-by reason

# 3. Fix issues, verify the specific page
a11y-catscan.py --page -q --summary-json https://example.com/fixed-page

# 4. Re-scan only pages that failed, compare against baseline
a11y-catscan.py --violations-from baseline.json --diff baseline.jsonl --llm

# 5. Large site? Scan in chunks and resume
a11y-catscan.py --max-pages 10000 --resume reports/scan.state.json

# 6. Suppress known limitations
echo '- rule: color-contrast
  url: /homepage
  reason: CKEditor toolbar gradient' >> allowlist.yaml
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No violations found |
| 1 | Violations found |
| 2 | Setup error (missing dependencies) |

## License

MIT — see [LICENSE](LICENSE).

Engine licenses: axe-core (MPL-2.0), Siteimprove Alfa (MIT),
IBM Equal Access (Apache-2.0), HTML_CodeSniffer (BSD-3).
