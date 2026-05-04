# Changelog

## 2026-05-04

### Added
- `pyproject.toml` with Python 3.12 runtime metadata, pinned runtime
  dependency ranges, and a dev extra for pytest / coverage / audit
  tooling. The CLI remains the executable `a11y-catscan.py` script
  rather than a console-entry-point package because it is meant to run
  beside its `node_modules/` and bundled engine assets.
- Focused test suite around the split modules: crawl loop behavior,
  signal handling, report streaming, registry/search/diff helpers,
  MCP tools, engine mappings, browser lifecycle, auth lifecycle, and
  normalizer behavior. The suite now exercises the library API and the
  CLI-import path instead of treating the project as one monolithic
  script.
- Session-expiry recovery for authenticated crawls. When a login plugin
  reports `session_active=False`, in-flight workers drain, the scanner
  re-authenticates, suspect URLs are tested serially, logout traps are
  banned, and safe suspects are requeued for a normal scan.
- Recovery circuit breaker. If re-login fails during recovery, suspect
  URLs are banned and further recovery is disabled for that run so the
  crawl does not loop forever on every subsequent page.
- MCP hardening:
  - `scan_page` rejects non-HTTP schemes and, by default, private,
    loopback, link-local, reserved, multicast, and unspecified targets.
    This prevents the MCP surface from becoming an SSRF primitive.
  - Report references are resolved through report-shaped files
    (`.json` / `.jsonl`) or the scan registry instead of arbitrary local
    paths.
  - Scanner infrastructure errors are surfaced as `clean: false` tool
    responses rather than clean-looking skipped pages.

### Changed
- `a11y-catscan.py` was reduced back to a CLI coordinator. The browser
  lifecycle, crawling, URL utilities, reports, registry, allowlist, and
  result helpers now live in focused modules:
  - `scanner.py`: reusable async scanner and auth lifecycle.
  - `crawl.py`: crawl frontier, worker sliding window, signal handling,
    streaming writes, browser restarts.
  - `crawl_utils.py`: URL normalization, robots.txt, HTTP probes,
    cookie loading, browser cleanup.
  - `report_*.py`: HTML, LLM, grouped, diff, and streaming report
    concerns.
  - `registry.py`: named scans plus search / page-status / structured
    diff for CLI and MCP consumers.
- Codebase modernized to Python 3.12 idioms: native generic type
  annotations, `match` where it clarifies dispatch, dataclasses for
  running totals, and narrower helper modules instead of broad
  underscore alias layers.
- The crawl loop now keeps a single JSONL file handle open for the run.
  Results are still written by one main-loop writer only; worker tasks
  return complete page records. This preserves the invariant that each
  JSONL line is one complete page result with no interleaving.
- Report flushing uses streaming JSONL reads to build final JSON / HTML
  outputs. Large scans stay bounded in memory; the final JSON is a
  convenience product, not the source of truth.
- Engine startup is fail-closed when all selected engines fail to start.
  A missing `node_modules/`, broken browser launch, or bad engine setup
  no longer produces a clean-looking scan.
- `--cleanup` now targets Playwright-launched Chromium processes rather
  than every same-user process whose command name contains `chrome`.
- `--name` / `--output` is restricted to a basename. Output names cannot
  use absolute paths or `../` traversal to escape `output_dir`.
- LLM markdown report examples now escape Markdown fence delimiters in
  scanned HTML snippets. This keeps page content from breaking out of
  the example block and becoming instructions to the downstream LLM.

### Fixed
- Five correctness / exception-handling issues from the refactor arc:
  report path suffix handling, corrupt-line tolerance, state-file
  writes, skipped-page accounting, and subprocess/browser shutdown
  noise paths.
- Session plugin exceptions during `check_session` now emit a warning
  instead of silently disabling session-expiry detection.
- After `restart_browser()`, failed re-login no longer leaves the
  scanner pointing at a closed authenticated context.
- Alfa browser restart no longer starts Alfa twice. Alfa owns the
  Playwright browser server; restart connects Python to that server and
  skips Alfa in the second engine-start loop.
- LLM report incompletes render an `(unknown)` bucket when deduped
  findings no longer carry axe's `data.messageKey`. The section no
  longer disappears for valid `cantTell` results.
- Committed `cookies.json` was removed from the repository. The file
  was already ignored but had been tracked; any sessions contained in
  earlier history should still be revoked if the repository was shared.

### Hygiene
- `compileall`, full pytest, and npm audit were run after the hardening
  pass:

      python3.12 -m compileall -q .
      .venv/bin/pytest -q      # 368 passed
      npm audit --omit=dev     # found 0 vulnerabilities

- Test ownership was tightened around fixtures and local HTTP servers:
  generated fixture files live under `tmp_path`, registry writes are
  isolated, and browser tests are marked separately from pure helpers.
- Playwright shutdown noise is filtered in both production crawl runs
  and the pytest harness, but real task/browser exceptions remain
  visible.

## 2026-04-21

### Added
- `Scanner` class: reusable async library API for one-page scans. It
  owns Playwright startup/shutdown, engine startup, authentication
  context, per-page tab creation, element resolution, and structured
  page results. The CLI crawl loop and MCP server now share this path.
- MCP stdio server mode (`a11y-catscan.py --mcp` / `mcp_server.py`) for
  quick tool calls:
  - `scan_page`
  - `analyze_report`
  - `list_engines`
  - `lookup_wcag`
  - `find_issues`
  - `check_page`
  - `compare_scans`
  - `manage_scans`
- Named scan registry under `reports/scans.json`. Completed scans can
  be listed, looked up by name, searched, checked by page URL, compared
  structurally, and deleted from the registry without deleting report
  files.
- Human-friendly search aliases for WCAG Success Criteria. Search
  accepts canonical SC numbers, `sc-*` tags, and known slug aliases for
  common criteria.

### Changed
- CLI crawl loop now uses `Scanner` instead of inline browser/engine
  code. The CLI owns crawling and persistence; the scanner owns
  "navigate this page and normalize engine findings."
- Alfa architecture was hardened: the Node.js subprocess launches
  Chromium via Playwright `launchServer()` and returns a GUID-protected
  WebSocket endpoint. Python connects to that endpoint; there is no open
  CDP debug port.
- Alfa rule filtering now respects both WCAG level and WCAG version.
  `wcag21aa` does not include WCAG 2.2 criteria simply because they are
  AA.
- Best-practice and ARIA findings are separated from strict WCAG
  failures in summary counts. `--level best` explicitly opts best
  practices into the compliance total.
- The normalized Success Criterion tag prefix changed from `wcag-` to
  `sc-` to distinguish success criteria from WCAG version / level tags.
- Engine-aware allowlist matching. A single-engine allowlist entry will
  not suppress a multi-engine confirmed finding.
- IBM landmark / ARIA naming findings were reclassified from WCAG into
  the ARIA bucket where IBM's native mapping would otherwise inflate
  WCAG totals.

### Fixed
- Alfa selector resolution handles text-node targets, id/class/tag
  fallbacks, and iterator materialization correctly. Findings now point
  at useful CSS selectors instead of unstable or empty targets.
- Alfa `cantTell` messages are extracted from ACT expectations and
  preserved in normalized results.
- Alfa page discovery no longer relies solely on CDP page identity; the
  subprocess can navigate in its own context when needed.
- Playwright / Alfa shutdown cleanup suppresses harmless
  `TargetClosedError` / `Event loop is closed` traces during process
  teardown.

### Removed
- `scan_site` was removed from the MCP surface. Full crawls are
  intentionally CLI work: they are long-running, write multiple output
  files, and benefit from signal handling and resume state. MCP tools
  stay quick and bounded.
- Dead code, stale comments, deprecated `aria-usage` category remnants,
  and temporary refactor aliases were pruned.

## 2026-04-20

### Added
- Multi-engine architecture:
  - `axe`: axe-core browser injection.
  - `alfa`: Siteimprove Alfa via Node.js / Playwright.
  - `ibm`: IBM Equal Access browser injection.
  - `htmlcs`: HTML_CodeSniffer browser injection.
- `engines/` package with one implementation module per scanner plus a
  shared `Engine` base class and factory.
- `engine_mappings.py`: WCAG Success Criterion metadata, IBM rule
  mappings, best-practice categories, ARIA categories, HTMLCS code
  parsing, and EARL outcome constants.
- EARL outcome normalization across engines:
  - `failed`
  - `cantTell`
  - `passed`
  - `inapplicable`
- Cross-engine deduplication. Findings that share
  `(selector, primary_tag, outcome)` merge into one finding with
  `engines` attribution and the highest impact preserved.
- `--group-by` modes for `level` and `engine`, extending existing
  grouping by rule, selector, color, reason, WCAG, and best-practice
  category.
- `--level best`: WCAG 2.1 AA plus best-practice checks.
- Comma-separated `--engine` syntax (`--engine axe,alfa,ibm`) plus
  `--engine all`.

### Changed
- Project renamed to `a11y-catscan`; remaining `axe-spyder`
  references were removed from YAML and lockfile metadata.
- Report consumers switched to `iter_deduped()` so HTML, LLM, group,
  search, page-status, and diff views all read a consistent
  cross-engine shape.
- JSONL write path documents and enforces the single-writer invariant:
  one complete page result per line, written from the main event loop.
- URL / element normalization moved toward engine-independent data:
  engine-native CSS selectors, XPath, and Alfa target descriptions are
  resolved against the live DOM into stable CSS selectors.

### Fixed
- HTML_CodeSniffer engine support was added and normalized to the same
  EARL / SC tag vocabulary as the other engines.
- Strict WCAG filtering prevents best-practice-only rules from being
  counted as compliance failures unless requested.
- Multiple-engine CLI parsing was simplified after an initial repeatable
  flag design; comma-separated values became the stable surface.

## 2026-04-19 and earlier

### Added
- Original axe-based crawler and report generator: Playwright/Chromium
  navigation, same-origin URL discovery, robots.txt support, skip
  extension filtering, HTML/JSON output, quiet/verbose modes, and
  single-page verification.
- JSONL streaming format and resume-oriented crawl state. Long scans can
  be interrupted, flushed, and resumed without holding all results in
  memory.
- Allowlist YAML for known-acceptable incompletes, initially rule/url/
  target based and later extended with engine/outcome filters.
- LLM-oriented markdown summary for feeding concise scan findings into
  remediation workflows.
