<p align="center">
  <img src="docs/assets/logo-a11y-catscan.svg" alt="a11y-catscan" width="180">
</p>

<p align="center">
  <strong>Multi-engine accessibility scans that survive real crawls.</strong>
</p>

<p align="center">
  <a href="https://github.com/hubzero/a11y-catscan/actions/workflows/ci.yml"><img src="https://github.com/hubzero/a11y-catscan/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://hubzero.github.io/a11y-catscan/"><img src="https://img.shields.io/badge/docs-Pages-blue" alt="docs"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="https://playwright.dev/"><img src="https://img.shields.io/badge/Playwright-1.59-2EAD33?logo=playwright&logoColor=white" alt="Playwright 1.59"></a>
  <a href="https://www.w3.org/TR/WCAG22/"><img src="https://img.shields.io/badge/WCAG-2.2-005A9C" alt="WCAG 2.2"></a>
  <a href="docs-src/mcp.md"><img src="https://img.shields.io/badge/MCP-server-444" alt="MCP server"></a>
  <a href="#whats-shipped"><img src="https://img.shields.io/badge/status-beta-orange" alt="status: beta"></a>
</p>

a11y-catscan crawls a website with Playwright and runs four
accessibility engines — axe-core, Siteimprove Alfa, IBM Equal
Access, and HTML_CodeSniffer — sharing one Chromium instance.
Findings are deduped across engines, streamed to JSONL/HTML/JSON
reports, and exposed as MCP tools so an LLM can analyze them
directly.

**Status: beta.** Production-shaped, exercising in dev; recovery
cycle and worker pool work end-to-end on multi-thousand-page
authenticated crawls. Architecture and per-module design notes
live in [DESIGN.md](DESIGN.md). Site handbook is rendered to
GitHub Pages from `docs-src/`; see the [documentation index](#documentation)
below.

## What's shipped

- **Four scan engines.** axe-core (Deque), Siteimprove Alfa
  (ACT-rules native), IBM Equal Access, HTML_CodeSniffer. Run
  one or combine them — `--engine axe,alfa,ibm,htmlcs` — all
  sharing one Chromium so a multi-engine scan isn't 4× the
  page loads. Each finding carries an `engine` attribution.
- **Cross-engine dedup.** Findings sharing
  `(selector, primary-tag, outcome)` collapse into one entry
  with `engines: {axe: ..., ibm: ...}` and per-engine impact
  upgraded to the worst severity. EARL outcomes
  (`failed` / `cantTell` / `passed` / `inapplicable`) are the
  internal vocabulary.
- **Streaming reports.** JSONL is written one page per line so
  memory stays flat across 5000-page crawls; HTML and the
  LLM-friendly markdown summary stream from disk on demand.
- **Sliding-window async crawler.** N-worker pool with one
  Chromium, periodic browser restart for memory hygiene
  (`restart_every`), atomic state save (`--resume`), graceful
  shutdown on SIGTERM/SIGINT, on-demand snapshot via SIGUSR1.
- **Authenticated scans with mid-scan session recovery.** A
  Python login plugin authenticates once, the saved session
  state shortcuts subsequent starts, and if the session expires
  mid-crawl the scanner drains workers, re-logs-in, bans
  detected logout-trap URLs, and resumes. Persistent re-login
  failure trips a circuit breaker so the crawl exits instead
  of looping.
- **Allowlist with engine + outcome filters.** YAML allowlist
  suppresses known-acceptable findings by rule, URL, target,
  engine, and outcome — all AND'd. O(1) average lookup via a
  rule-id index.
- **MCP server.** `--mcp` exposes
  `scan_page` / `analyze_report` / `find_issues` / `check_page`
  / `compare_scans` / `manage_scans` / `lookup_wcag` /
  `list_engines` as Claude Code tools. URL-scheme validated to
  http(s).
- **Diff and rescan workflows.** `--diff PREV.jsonl` shows
  fixed/new/remaining findings; `--rescan PREV.jsonl` re-scans
  only pages that previously had issues; `--violations-from`
  / `--incompletes-from` extract specific URL sets from prior
  reports.
- **Group-by analysis.** `--group-by {rule, selector, color,
  reason, wcag, level, engine, bp}` prints a sorted summary
  with per-group page counts and one example.
- **Niceness + OOM-resistance.** Defaults to `nice 10` and
  `oom_score_adj=1000` so the scanner doesn't starve
  production services on shared hosts.

## Quick start

Requires Python 3.12 and Node.js 18+.

```sh
pip install -e .              # installs playwright, pyyaml, mcp
playwright install chromium
npm install                   # bundles the four engines
```

Scan one URL:

```sh
./a11y-catscan.py --page https://example.com/
```

Crawl with all four engines, write LLM-friendly report:

```sh
./a11y-catscan.py --engine all --max-pages 500 --llm \
    https://example.com/
```

Compare against last week's baseline:

```sh
./a11y-catscan.py --diff baseline.jsonl --max-pages 500 \
    https://example.com/
```

Full setup walkthrough in [`docs-src/getting-started.md`](docs-src/getting-started.md).

## Documentation

Site handbook (rendered to
[hubzero.github.io/a11y-catscan](https://hubzero.github.io/a11y-catscan/)
from these sources):

| Topic | Source |
|---|---|
| Getting started — install, first scan, exit codes | [`docs-src/getting-started.md`](docs-src/getting-started.md) |
| Configuration — every YAML setting + CLI override | [`docs-src/configuration.md`](docs-src/configuration.md) |
| Scan workflows — crawl, page, urls, rescan, diff, resume | [`docs-src/scan-workflows.md`](docs-src/scan-workflows.md) |
| Reports — JSON, JSONL, HTML, LLM markdown formats | [`docs-src/reports.md`](docs-src/reports.md) |
| Authentication — login plugin, session recovery, logout traps | [`docs-src/authentication.md`](docs-src/authentication.md) |
| MCP server — tool surface for Claude Code | [`docs-src/mcp.md`](docs-src/mcp.md) |
| Troubleshooting | [`docs-src/troubleshooting.md`](docs-src/troubleshooting.md) |
| FAQ | [`docs-src/faq.md`](docs-src/faq.md) |

Internal references:

- [DESIGN.md](DESIGN.md) — current-state design specification
- [CHANGELOG.md](CHANGELOG.md) — date-organized log of changes

## Engines

| Engine | Flag | Type | License |
|---|---|---|---|
| [axe-core](https://github.com/dequelabs/axe-core) (Deque) | `--engine axe` | Browser injection (default) | MPL-2.0 |
| [Siteimprove Alfa](https://github.com/Siteimprove/alfa) | `--engine alfa` | Node.js subprocess via CDP | MIT |
| [IBM Equal Access](https://github.com/IBMa/equal-access) | `--engine ibm` | Browser injection | Apache-2.0 |
| [HTML_CodeSniffer](https://github.com/squizlabs/HTML_CodeSniffer) | `--engine htmlcs` | Browser injection | BSD-3 |

`--engine all` runs all four; engines that aren't listed are
skipped. axe-core, IBM, and HTML_CodeSniffer inject JavaScript
into the live page and run in-browser. Alfa's TypeScript engine
runs as a Node.js subprocess and connects to the shared Chromium
via CDP — no second page load.

## Local development

The full test suite runs against the bundled fixtures:

```sh
pip install -e '.[dev]'
pytest                       # 368 tests, ~70s with browser
pytest -m "not browser"      # 285 fast tests, <10s
```

Coverage is configured in `pyproject.toml`; see
[`tests/`](tests/) for the layout (`test_engine_normalizers.py`,
`test_crawl_loop.py`, `test_mcp_tools.py`, etc.).

## License

MIT. See [LICENSE](LICENSE).

Engine licenses: axe-core (MPL-2.0), Siteimprove Alfa (MIT),
IBM Equal Access (Apache-2.0), HTML_CodeSniffer (BSD-3). The
four engines are vendored via npm and ship under their own
licenses; this repo wraps them.
