# Security Policy

## Reporting a vulnerability

**Please do not open public issues for security bugs.** Use
GitHub's [private security advisory](https://github.com/hubzero/a11y-catscan/security/advisories/new)
flow — it gives us a private channel to triage, fix, and
coordinate disclosure before the bug becomes public knowledge.
A GitHub account is required (free); the form is the only
supported reporting channel.

When you report, please include:

- A clear description of the bug and its impact.
- Reproduction steps or a proof-of-concept (a minimal
  `a11y-catscan.yaml` + the URL or fixture HTML that triggers
  the problem is ideal).
- The a11y-catscan version / commit you tested against, plus
  Python and Node versions.
- The engine(s) involved (`axe`, `alfa`, `ibm`, `htmlcs`).
- Any thoughts on a fix or workaround if you have them.

We aim to acknowledge new reports within **3 working days**
and to ship a fix on a timeline proportional to severity
(typically 7–30 days for confirmed high-severity bugs;
lower-severity items move through normal release cadence).

## Scope

In scope:

- The `a11y-catscan.py` CLI and its supporting Python modules
  (`crawl.py`, `scanner.py`, `engines/*.py`, `mcp_server.py`,
  `registry.py`, `report_*.py`, `allowlist.py`,
  `cli_modes.py`, `crawl_utils.py`, `engine_mappings.py`,
  `results.py`, `report_io.py`).
- The MCP tool surface exposed under `--mcp` — input
  validation on `scan_page`, `analyze_report`, `find_issues`,
  `check_page`, `compare_scans`, `manage_scans`, etc.
- The login plugin loading mechanism (`scanner._setup_auth`)
  and the saved auth-state file (`.auth-state.json`).
- The crawl-state file (`*.state.json`) and the JSONL
  streaming format that `--diff` / `--rescan` / `--resume`
  consume.
- Bundled docs + tooling under `tools/` and `docs/`.

Out of scope:

- Issues in the bundled engine packages — axe-core, Siteimprove
  Alfa, IBM Equal Access, HTML_CodeSniffer.  Report those
  upstream to the respective projects.  We can adopt a fix
  once the upstream ships it.
- Issues in Playwright, Chromium, PyYAML, the `mcp` package,
  or any other third-party dependency listed in
  `pyproject.toml` / `package.json`.  Report those upstream;
  we'll bump the pin once a fix is available.
- Issues that require an attacker to control the
  `a11y-catscan.yaml` config file or the login plugin path on
  disk.  Both are user-supplied trusted inputs by design (the
  login plugin is loaded via `importlib.util.spec_from_file_location`
  and runs with the user's privileges — that's the documented
  extension mechanism, not a sandbox boundary).
- Issues in the sample login plugin `login-hubzero.py` — it's
  an example, not a supported component.
- Crashes or runaway memory on adversarial scan-target
  content where the underlying cause is the engine's parser
  or Chromium itself.

## What counts as a vulnerability

The most interesting bug classes for this project:

- **MCP tool injection** — paths where an MCP client can
  cause `scan_page` / `analyze_report` / etc. to read or
  write files outside the intended report directory, follow
  non-http(s) URLs, or exfiltrate filesystem content via the
  returned tool data.  The current `scan_page` URL validator
  rejects non-http(s) schemes; bypasses are in scope.
- **HTML injection in generated reports** — a rule `id`,
  `impact`, `selector`, `target`, `html` snippet, or other
  finding field reaching the HTML report's attribute or text
  context without being routed through `_esc()` or the
  `_safe_impact` whitelist.  The HTML report is opened
  locally in a browser; script execution from a foreign
  JSONL is the concern.
- **JSONL parse-time crashes** — `report_io.iter_jsonl` and
  `iter_report` are designed to skip malformed lines (corrupt
  JSON, non-dict shapes) without raising.  A crafted input
  that gets past those filters and crashes a downstream
  consumer is a real bug.
- **Allowlist bypass** — paths where a finding that should
  match an allowlist entry isn't suppressed, or vice versa.
  The engine + outcome filters are the trickiest part;
  matching a single-engine allowlist entry against a
  multi-engine deduped finding should not suppress.
- **Atomic-write breakage** — `_save_state` and `_save_registry`
  use temp-file + atomic-rename; a crash during save should
  never leave a zero-byte or half-written file in place.
- **Session-recovery loops** — the recovery cycle has a
  circuit breaker on persistent re-login failure.  A path
  that defeats the breaker and causes the crawl to loop
  indefinitely is in scope.
- **Resource leaks** — file handles, subprocesses, browser
  contexts, or asyncio tasks that aren't cleaned up across
  exception paths.

If you're not sure whether something qualifies, file the
report anyway and we'll triage it together.
