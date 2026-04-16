# axe-spider

WCAG accessibility scanner that crawls a website using Selenium/Chromium
and runs [axe-core](https://github.com/dequelabs/axe-core) checks on
each page, producing HTML and JSON reports.

## Quick start

```bash
# Scan with defaults from config
python3 axe-spider.py

# Scan a specific URL
python3 axe-spider.py https://example.com/

# Scan 200 pages at WCAG 2.2 AA
python3 axe-spider.py --level wcag22aa --max-pages 200 https://example.com/

# Restrict to a section
python3 axe-spider.py --include-path /docs https://example.com/
```

## Setup

Requires Python 3.6+, Selenium, and Chromium/Chrome + ChromeDriver.

```bash
pip install selenium
```

Copy `axe-spider.yaml.example` to `axe-spider.yaml` and edit for your
site. The config file is gitignored so each deployment keeps its own
settings without merge conflicts.

## Configuration

All settings in `axe-spider.yaml` can be overridden on the command line.

| Setting | CLI flag | Default | Description |
|---------|----------|---------|-------------|
| `url` | positional arg | — | Starting URL to crawl |
| `level` | `--level` | `wcag21aa` | WCAG conformance level |
| `max_pages` | `--max-pages` | 50 | Maximum pages to scan |
| `page_wait` | — | 1 | Seconds to wait after page load |
| `save_every` | `--save-every` | 25 | Flush reports every N pages |
| `output_dir` | `--output-dir` | cwd | Report output directory |
| `exclude_paths` | `--exclude-path` | — | URL path prefixes to skip |
| `exclude_regex` | — | — | Regex patterns to skip |
| `exclude_query` | — | — | Query substrings to skip |
| `chromium_path` | — | `/usr/bin/chromium-browser` | Path to Chrome/Chromium |
| `chromedriver_path` | — | `/usr/bin/chromedriver` | Path to ChromeDriver |

## Features

- Breadth-first crawl with automatic link discovery
- WCAG 2.0 / 2.1 / 2.2 level presets (A through AAA)
- HTML report with impact breakdown, per-page details, rule summaries
- Incomplete (needs-review) reporting alongside violations
- Incremental save — partial results survive if the scan is killed
- HTTP pre-check skips error pages' links to prevent crawl fan-out
- Same-origin redirect detection (catches login walls)
- Skips non-HTML responses, empty pages, and binary downloads
- Configurable via YAML — no code changes needed per site

## License

MIT — see [LICENSE](LICENSE). Bundled axe-core is MPL-2.0.
