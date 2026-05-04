# Getting started

a11y-catscan crawls a website with Playwright/Chromium and runs one or
more accessibility engines against each rendered HTML page. This page
gets you from a fresh checkout to a first report.

## Prerequisites

You need:

- Python 3.12 or newer.
- Node.js 18 or newer.
- A browser environment supported by Playwright.
- Enough memory for Chromium. Each parallel worker can cost hundreds of
  megabytes on JavaScript-heavy pages.

The project keeps Python and Node dependencies separate:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
npm install
python -m playwright install chromium
```

If you are using the checked-in development environment, the local
pytest command is:

```sh
.venv/bin/pytest -q
```

## First scan

Run a small crawl with the default engine, axe-core:

```sh
./a11y-catscan.py --max-pages 25 https://example.com/
```

The default output basename is timestamped:

```text
a11y-catscan-YYYY-MM-DD-HHMMSS.json
a11y-catscan-YYYY-MM-DD-HHMMSS.jsonl
a11y-catscan-YYYY-MM-DD-HHMMSS.html
```

Open the HTML report first. It is the easiest place to see total
violations, criteria, affected pages, selectors, and representative
HTML snippets.

## Run all engines

The scanner can run four engines:

```sh
./a11y-catscan.py --engine all --max-pages 50 https://example.com/
```

Or choose a subset:

```sh
./a11y-catscan.py --engine axe,alfa https://example.com/
./a11y-catscan.py --engine axe,ibm,htmlcs https://example.com/
```

All engines report into the same result model. Findings include engine
attribution so you can tell whether a result came from one tool or was
confirmed by several.

## Quick page verification

Use page mode after a code fix:

```sh
./a11y-catscan.py --page -q --summary-json https://example.com/fixed-page
```

`--page` does not crawl links. `-q` keeps output quiet. `--summary-json`
prints a machine-readable one-line summary suitable for scripts.

## Generate an LLM summary

The full JSON report can be far too large for an LLM prompt. Use
`--llm` to generate a compact Markdown summary:

```sh
./a11y-catscan.py --engine all --max-pages 100 --llm https://example.com/
```

The `.md` file groups recurring failures by rule and page pattern. It
is designed for remediation planning, not as the legal audit artifact.
Keep the full HTML and JSONL reports for traceability.

## Respect robots.txt

robots.txt is respected by default. Use `--ignore-robots` only when you
have permission to scan paths that the site asks crawlers to avoid:

```sh
./a11y-catscan.py --ignore-robots https://example.com/
```

For production or shared infrastructure, prefer a scoped include path
instead of simply ignoring robots:

```sh
./a11y-catscan.py --include-path /docs --include-path /help https://example.com/
```

## Where to next

- Configure a real site: [configuration](../configuration/index.html).
- Learn scan modes: [scan workflows](../scan-workflows/index.html).
- Scan logged-in pages: [authentication](../authentication/index.html).
- Interpret output: [reports](../reports/index.html).
