# a11y-catscan -- design

a11y-catscan is a multi-engine WCAG accessibility scanner. It crawls
HTML pages with Playwright/Chromium, runs one or more accessibility
engines against each page, normalizes their findings into one shared
EARL-shaped result model, and streams reports to disk as it goes.

The central design target is practical audit work on large, real sites:

- A scan can run for thousands of pages without holding all findings in
  memory.
- Partial results survive process termination.
- A failed browser, engine, or auth session is visible instead of
  quietly producing a clean-looking report.
- The same page-scan code path is reusable from the CLI crawl loop, the
  MCP server, tests, and small one-off verification scripts.

The scanner is not a replacement for manual WCAG judgment. It is an
evidence collector: automated failures are prioritized, `cantTell`
items are surfaced for review, and engine attribution is preserved so
auditors can decide whether a finding is broadly confirmed or tool
specific.

## Non-goals

- Full WCAG certification. Automated engines cannot verify every WCAG
  requirement and cannot reason about intent.
- Scanning every resource type. Scope is HTML/XHTML pages rendered in a
  browser. PDFs, Office documents, videos, audio, and other media are
  explicitly out of scope.
- A general browser automation framework. The scanner exposes enough
  hooks for login and session checking, but page interaction beyond
  authentication belongs in site-specific plugins or pre-scan setup.
- A long-running MCP crawl service. Full crawls write files, handle
  signals, and may run for hours; they stay in the CLI. MCP tools are
  quick, bounded analysis and one-page scan helpers.

## Execution surfaces

There are three supported ways into the codebase:

| Surface | Entry point | Responsibility |
|---------|-------------|----------------|
| CLI | `./a11y-catscan.py` | Parse operator flags, load YAML config, select scan/analysis mode, write final summaries, register completed scans |
| Library | `scanner.Scanner` | Start browser + engines, scan one page, normalize selectors, manage auth context |
| MCP | `mcp_server.py` or `a11y-catscan.py --mcp` | Expose bounded tools for one-page scanning, report analysis, registry lookup, and WCAG metadata |

The CLI imports the same public functions used by tests and external
integrations. Some names are intentionally re-exported from
`a11y-catscan.py` so older scripts that imported the single-file tool
continue to work after the module split.

## Source layout

| File | Responsibility |
|------|----------------|
| `a11y-catscan.py` | CLI entry point. Defines flags, loads config, dispatches analysis modes, resolves crawl settings, invokes `crawl_and_scan`, prints final summaries, registers scans, and starts MCP mode when `--mcp` is present |
| `scanner.py` | Reusable async scanner. Owns Playwright lifecycle, engine construction/start/stop, authenticated browser context, login plugin loading, saved storage state, per-page navigation, element resolution, browser restart, and session checks |
| `crawl.py` | Crawl engine. Owns URL frontier, visited set, worker sliding window, shared rate limiter, robots integration, incremental JSONL writes, report flushing, signal handlers, resume state, browser restart cadence, and auth recovery cycle |
| `crawl_utils.py` | Browser-free crawl helpers: URL normalization, strip-query rules, same-origin checks, robots.txt loading, `should_scan`, HEAD/GET HTTP probe, cookie loading, process cleanup, numeric parsing |
| `results.py` | Page-result helpers: node counting, `RunningTotals`, and cross-engine deduplication by `(selector, primary_tag, outcome)` |
| `allowlist.py` | YAML allowlist loader and indexed matcher. Filters known-acceptable findings by normalized rule id, URL substring, target substring, engine, and outcome. Also classifies deduped page totals |
| `engine_mappings.py` | WCAG Success Criterion metadata, SC aliases, engine rule mappings, best-practice categories, ARIA categories, HTMLCS rule-code parsing, and shared EARL constants |
| `engines/base.py` | Engine interface and normalized result schema documentation |
| `engines/axe.py` | axe-core adapter: loads `axe.min.js`, injects it into the page, runs `axe.run`, normalizes tags and best-practice / ARIA categories |
| `engines/alfa.py` | Python side of Siteimprove Alfa integration. Starts `alfa-engine.mjs`, connects to its browser server, serializes scans through an async lock, forwards cookies, and normalizes Alfa results |
| `alfa-engine.mjs` | Node.js Alfa subprocess. Launches Chromium through Playwright `launchServer`, filters Alfa ACT rules by WCAG level/version, scans pages in isolated contexts, resolves selectors in the DOM, and returns newline-delimited JSON |
| `engines/ibm.py` | IBM Equal Access adapter: injects `ace.js`, runs IBM's checker, maps policy/confidence pairs to EARL outcomes and normalized tags |
| `engines/htmlcs.py` | HTML_CodeSniffer adapter: injects `HTMLCS.js`, runs a WCAG standard, maps ERROR/WARNING messages to failed/cantTell |
| `engines/__init__.py` | Engine registry and `make_engine` factory with engine-specific kwargs |
| `report_io.py` | Streaming JSON / JSONL readers plus `iter_deduped` and URL extraction helpers |
| `report_html.py` | Streaming human-readable HTML report generator. Aggregates stats in one pass, renders per-page details in a second pass |
| `report_llm.py` | Token-efficient Markdown report for LLM-assisted remediation. Groups violations and incompletes, includes representative HTML snippets, and points to full reports |
| `report_group.py` | `--group-by` terminal summary printer |
| `report_diff.py` | CLI text diff between two JSONL scans |
| `registry.py` | Named scan registry plus search, page status, and structured diff utilities used by CLI and MCP |
| `cli_modes.py` | No-scan CLI modes: cleanup, list scans, page status, search, audit help |
| `mcp_server.py` | FastMCP tool definitions and MCP-specific safety checks |
| `login-hubzero.py` | Example login plugin implementing the scanner auth contract |

## Result model

Every engine returns a list of normalized findings:

```python
{
    "id": "color-contrast",
    "engine": "axe",
    "outcome": "failed",
    "description": "...",
    "help": "...",
    "helpUrl": "...",
    "impact": "serious",
    "tags": ["sc-1.4.3"],
    "nodes": [{
        "target": ["#main"],
        "html": "<main>...</main>",
        "any": [{"message": "..."}],
    }],
}
```

Outcomes use W3C EARL names:

| Outcome | Meaning |
|---------|---------|
| `failed` | Automated check found a definite failure |
| `cantTell` | Automated check is inconclusive; manual review needed |
| `passed` | Rule passed |
| `inapplicable` | Rule does not apply |

Tags are normalized across engines:

| Prefix | Meaning | Example |
|--------|---------|---------|
| `sc-` | WCAG Success Criterion | `sc-1.4.3` |
| `aria-` | ARIA-specific category | `aria-valid-attrs` |
| `bp-` | Best-practice category | `bp-landmarks` |

The `sc-` prefix is deliberate. Native engine tags such as
`wcag2aa`, `wcag21aa`, and `wcag143` mix WCAG version, level, and
criterion identifiers. The normalized `sc-*` vocabulary names only
Success Criteria, which keeps reports and search filters unambiguous.

## Engine model

All engines share the same Playwright browser when possible, but they
do not all run the same way.

| Engine | Mechanism | Notes |
|--------|-----------|-------|
| axe-core | Browser injection | Loads `node_modules/axe-core/axe.min.js`, calls `axe.run(document, opts)` |
| IBM Equal Access | Browser injection | Loads `accessibility-checker-engine/ace.js`, calls `new ace.Checker().check` |
| HTML_CodeSniffer | Browser injection | Loads `html_codesniffer/build/HTMLCS.js`, calls `HTMLCS.process` |
| Siteimprove Alfa | Node.js subprocess | `alfa-engine.mjs` launches a Playwright browser server and runs Alfa ACT rules |

Alfa is special because it is a TypeScript ecosystem. Instead of
opening an unauthenticated debug port, the subprocess launches Chromium
with Playwright `launchServer()` and sends Python the GUID-bearing
WebSocket endpoint over stdout. The GUID acts as a bearer token.
Python connects to that endpoint and uses the same browser server for
its own page work. Alfa scans are serialized by an `asyncio.Lock`
because the subprocess protocol is one request/response stream.

If every selected engine fails to start, scanning fails closed. This
matters for audit integrity: missing dependencies, broken browser
binaries, and bad engine configuration should be setup errors, not
"clean" reports.

## Page scan lifecycle

`Scanner.scan_page(url, extract_links=False, dedup=True)` performs the
unit of work the rest of the system builds on:

1. Require `Scanner.start()` to have launched Playwright and started at
   least one engine.
2. Open a fresh page from the authenticated context when auth is
   configured; otherwise open a new browser page.
3. Navigate with the configured `wait_until` strategy and timeout.
4. Reject browser responses that are missing, HTTP 4xx/5xx, non-HTML,
   too small, or DOMs whose rendered document is not HTML.
5. Run each started engine against the page.
6. Normalize engine-native element references through a live-DOM
   resolver. CSS selectors, IBM XPath, and Alfa tag/attribute hints are
   converted into deterministic CSS selectors when possible.
7. Optionally extract HTTP links for the crawl frontier.
8. Attach `session_active` when a login plugin provides
   `is_logged_in(page)`.
9. Close the page in a `finally` block.
10. Return a page-result dict, or a structured skipped result with a
    reason.

Engine scan failures are intentionally local to that engine's result
list; a single noisy engine should not abort a page when other engines
still produced useful data. Startup failure of all engines is different
and is treated as fatal.

## Crawl lifecycle

`crawl_and_scan()` owns long-running site traversal. Its data structures
are deliberately plain:

- `queue`: URLs waiting to be scanned.
- `visited`: normalized URLs already claimed by the frontier.
- `_logout_urls`: URLs proven to trigger logout and banned from future
  visits.
- `pending`: active asyncio tasks in the worker sliding window.
- `running_totals`: summary counters updated as pages are written.

The main loop keeps at most `workers` scan tasks in flight. Worker tasks
never write report files directly; they return complete page results.
The main loop consumes finished tasks, updates counters, writes one
JSONL line, and enqueues discovered links. That single-writer shape is
important enough to be documented at the write site because consumers
assume a line is one complete page and never half of two interleaved
pages.

### URL filtering

URLs are normalized before they enter the frontier:

- Fragment removed.
- Path trailing slash normalized.
- Configured query parameters stripped globally or by path regex.

`should_scan()` then applies:

1. Same-origin check.
2. Static non-HTML extension skip list.
3. Include path prefixes.
4. Exclude path prefixes.
5. Exclude regex list.
6. Known non-HTML query patterns.
7. robots.txt, unless disabled.

The HTTP HEAD/GET probe in `crawl_utils.http_status()` is a cheap
pre-browser filter for obvious non-HTML or error responses. It sends
configured auth cookies when available so authenticated pages do not
look like redirects to login.

### Streaming persistence

JSONL is the primary write format:

```json
{"https://example.test/page": {"url": "...", "failed": [], "...": "..."}}
```

The crawler opens the `.jsonl` once and appends one line per scanned
page. Each line is flushed immediately so signal-triggered snapshots
and concurrent readers see complete records.

Periodic and final flushes convert JSONL into:

- `.json`: one large JSON object for compatibility and inspection.
- `.html`: human-readable report.
- `.md`: optional LLM summary.
- `.state.json`: crawl queue / visited / logout ban state for resume.

The JSON and HTML products are derived artifacts. If a process dies
mid-run, JSONL plus state are the recovery surface.

### Signals

The crawl loop installs:

- `SIGTERM` / `SIGINT`: set an interrupted flag, flush reports, save
  state, then let the async loop wind down.
- `SIGUSR1`: flush and save a snapshot without stopping.

Signal handlers avoid directly exiting the interpreter. They set state
that the async loop observes; this keeps Playwright cleanup predictable
and avoids orphaned browser processes where possible.

## Authentication model

Authentication is plugin-based. A login script is a Python module with:

```python
async def login(context, config) -> bool: ...
```

Optional hooks:

```python
async def is_logged_in(page) -> bool: ...
async def init_from_context(context) -> None: ...
exclude_paths = ["/logout"]
```

The scanner stores Playwright `storage_state` in `.auth-state.json`.
Startup first tries saved state, verifies it by loading the configured
start URL, and falls back to `login()` when the state is missing or no
longer valid.

During a crawl, `is_logged_in(page)` is checked after successful page
scans. If the session is lost:

1. The suspect URL is recorded.
2. Recovery mode is entered.
3. In-flight workers drain.
4. The scanner re-authenticates.
5. Suspects are tested serially.
6. URLs that immediately break the session are added to `_logout_urls`.
7. Safe suspects are requeued for a real scan.

If re-login fails, recovery is disabled for the rest of the run. That
is intentionally imperfect but bounded: a broken auth plugin should not
trap the crawler in an infinite recovery loop.

## Report consumers

All report consumers read JSONL through `report_io`:

- `iter_jsonl()` tolerates blank and corrupt lines, warning to stderr.
- `iter_report()` auto-detects JSON vs JSONL.
- `iter_deduped()` applies cross-engine dedup per page.

Dedup is done on read rather than at write time. Raw JSONL preserves
engine-native findings and keeps the on-disk format stable for existing
tools. Consumers that need presentation-level findings use
`iter_deduped()`.

### HTML report

`report_html.generate_html_report()` makes two streaming passes:

1. Aggregate totals, impact counts, WCAG criteria, rule summary, and
   incomplete summary.
2. Render per-page details and clean page list.

All page-sourced strings are escaped before entering HTML. Impact
values are whitelisted before being used in CSS class names.

### LLM report

`report_llm.generate_llm_report()` is intentionally compact. It groups
violations by normalized rule id and incompletes by axe `messageKey`
when available. Deduped findings may no longer have `messageKey`, so
they fall into an `(unknown)` bucket rather than disappearing.

HTML snippets from scanned pages are emitted inside fenced code blocks,
with fence delimiters escaped. This is not a complete prompt-injection
solution, but it prevents the most direct "break out of the code block"
failure mode when the report is pasted into an LLM.

## MCP safety model

The MCP server is a local tool surface that may be driven by an LLM
client. It is therefore treated as a security boundary even though it
runs on the user's machine.

`scan_page` validates:

- URL is a non-empty string.
- Scheme is `http` or `https`.
- Host is present.
- Host does not resolve to private, loopback, link-local, reserved,
  multicast, or unspecified IP space.

The private-address restriction can be disabled with
`A11Y_CATSCAN_MCP_ALLOW_PRIVATE=1`, intended for trusted local tests or
explicit operator use.

Report-analysis tools resolve direct paths only when they look like
report files (`.json` / `.jsonl`), or through the scan registry. This
keeps prompt-injected report requests from becoming arbitrary local
file reads.

## Security notes

- `cookies.json`, `.auth-state.json`, local config, reports, virtualenvs,
  and tool caches are gitignored. Tracked credentials are a release
  blocker; if a cookie file ever lands in history, revoke the sessions
  as well as removing the file from the current tree.
- `yaml.safe_load()` is used for config and allowlist files.
- Subprocess execution is list-based, not shell-based.
- Browser cleanup tracks launched browser PIDs and child Chromium
  processes. Manual `--cleanup` is intentionally narrowed to
  Playwright-looking Chromium processes.
- Output basenames are validated so `--name` cannot write outside the
  configured output directory.
- MCP `scan_page` returns scanner setup errors as errors, not as clean
  skipped pages.

## Testing model

The suite is split between pure helpers and browser-backed tests:

- Pure tests cover URL normalization, allowlist matching, report IO,
  registry/search/diff, grouped summaries, CLI argument behavior, and
  MCP tool JSON shapes.
- Browser tests use Playwright and local HTTP fixture servers to verify
  real engine execution, link extraction, auth plugin behavior, browser
  restart, and one-page MCP scans.
- Signal tests exercise flush/state behavior under SIGTERM and SIGUSR1.

The project-local command is:

```bash
.venv/bin/pytest -q
```

In sandboxed environments, browser and local-socket tests may require
running outside the filesystem/network sandbox because they bind
`127.0.0.1` and launch Chromium.

## Style

Python source follows the existing local style rather than an external
formatter profile:

- Python 3.12 syntax is allowed and expected.
- Keep modules focused and browser-free where possible.
- Prefer small helper functions over inline ad hoc parsing when the
  behavior is shared by CLI, MCP, and tests.
- Comments explain invariants and failure-mode choices, not obvious
  assignments.
- Streaming readers/writers are preferred for scan-size-dependent data.
- Error handling should make audit integrity explicit: a failed setup
  is not a clean page; a page-specific scan failure is a skipped page
  with a reason.

The useful local checks are:

```bash
python3.12 -m compileall -q .
.venv/bin/pytest -q
npm audit --omit=dev
```
