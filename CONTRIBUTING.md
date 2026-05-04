# Contributing to a11y-catscan

Thanks for your interest. The project is small and the workflow is
deliberately low-ceremony.

## Quick start

```sh
git clone https://github.com/hubzero/a11y-catscan.git
cd a11y-catscan
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
npm install
pytest -m "not browser"
```

The `pytest -m "not browser"` smoke check finishes in under 10 s
and runs ~285 hermetic tests.  The full suite (368 tests, ~70 s)
needs Chromium and the bundled engines; drop the `-m` flag to
include browser-driven crawl, scanner, signal-handler, and MCP
end-to-end tests.

## Reporting bugs and asking questions

- **Bugs**: open a GitHub issue with reproduction steps. A
  minimal `a11y-catscan.yaml` snippet plus the URL or fixture
  HTML that triggers the problem is the gold standard.  Engine
  attribution helps too — note whether the bug surfaces with
  one engine or all four.
- **Security bugs**: do not open public issues. See
  [SECURITY.md](SECURITY.md).
- **Questions / feature ideas**: GitHub Discussions is fine for
  open-ended things; issues are fine for things you want
  tracked.

## Submitting a pull request

1. Fork → branch from `main`.
2. Make the change.  Keep it focused — one concern per PR is
   easier to review than three concerns bundled.
3. **Run the test suite**:
   ```sh
   pytest
   ```
   PRs are gated on CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
   Locally-green tests + clean CI is the baseline; flake-on-CI-only
   happens occasionally and is a conversation, not an automatic
   block.  `tests/test_signal_handlers.py` is the historical flake
   suspect — it spawns subprocesses and sends signals.
4. **Rebuild the docs** if you touched `docs-src/*.md` or
   `site-src/*`:
   ```sh
   pip install -r tools/requirements-docs.txt
   python tools/build_site.py
   git add docs/
   ```
   The rendered HTML lives in `docs/` and is what GitHub Pages
   serves.  CI doesn't currently fail on stale `docs/`, but a
   PR that ships a `docs-src/` change without the matching
   `docs/` rebuild will look unfinished.
5. **Commit messages**: imperative, focused on the *why* when
   it isn't obvious from the diff.  The git log shows the
   style; copy what you see.  No "fix typo" PRs are too small
   to be welcome.
6. **Open the PR**.  Describe what changed and what you
   tested.  Screenshots / log excerpts when relevant.

## Where things live

The CLI script `a11y-catscan.py` (hyphenated, so it isn't
importable as a module — runs as `./a11y-catscan.py`) is a thin
shell over a dozen focused modules:

- `crawl.py` — async crawl loop, sliding-window worker pool,
  signal handlers, recovery cycle.
- `scanner.py` — Playwright Scanner class, browser lifecycle,
  per-page scan orchestration.
- `engines/` — `axe.py`, `alfa.py`, `ibm.py`, `htmlcs.py` plus
  the `Engine` base class and `make_engine` factory.  Each
  engine wraps a third-party rule pack from `node_modules/`.
- `engine_mappings.py` — pure data: WCAG SC metadata, IBM rule
  → SC table, ARIA / best-practice category maps, the EARL
  StrEnum.
- `results.py` — `count_nodes`, `dedup_page`, `RunningTotals`
  dataclass.  No browser dependency.
- `report_io.py` — JSONL streaming readers (`iter_jsonl`,
  `iter_report`, `iter_deduped`, `extract_urls_from_report`).
- `report_html.py`, `report_llm.py`, `report_group.py`,
  `report_diff.py` — output generators.
- `crawl_utils.py` — URL/HTTP utilities, `RateLimiter`,
  `should_scan`, `http_status`, browser-PID atexit cleanup.
- `allowlist.py` — indexed `Allowlist` class with rule-id
  lookup, plus the legacy plain-list compatibility path.
- `cli_modes.py` — analysis-mode handlers (`--cleanup`,
  `--list-scans`, `--page-status`, `--search`, `--help-audit`).
- `mcp_server.py` — MCP tool layer for Claude Code.
- `registry.py` — named-scan registry, search, page-status,
  structured diff.

Tests in `tests/`; reusable fixtures and factories in
`tests/conftest.py` and `tests/fixtures/`.  Test markers:
`@pytest.mark.browser` for Chromium-required tests; everything
else is hermetic.  Layout details in
[`docs-src/troubleshooting.md`](docs-src/troubleshooting.md).

`CHANGELOG.md` is date-organized and gets an entry per
notable PR.  `DESIGN.md` is current-state architecture; update
it when you change behavior, not just when you ship a feature.

## Code style

- Python 3.12, idiomatic.  f-strings (not `.format()`),
  `pathlib.Path` for filesystem work where it clarifies
  (`with_suffix` over `.replace('.json', ...)`),
  `match/case` for non-trivial dispatches, `@dataclass` for
  passing structured state, type hints on public API surfaces.
- `pyupgrade --py312-plus` is the mechanical baseline; run it
  if you're adding code in older style by reflex.
- Comments explain *why* when the code can't say it itself.
  Don't narrate what `if visited:` does; do explain why we
  skip the check_session call when the plugin raised once
  already (circuit breaker).
- No mutable default arguments.  Trip the linter on this one
  so we never relearn it.
- Engine plugins follow the `Engine` base class contract —
  `start(browser)`, `scan(page) → list[finding-dict]`,
  `stop()`.  Adding a fifth engine is meant to be a single
  new file in `engines/` plus one entry in `ENGINES` /
  `_EXTRA_KWARGS` in `engines/__init__.py`.

## License

By contributing, you agree your contribution is licensed under
the MIT License (see [LICENSE](LICENSE)).
