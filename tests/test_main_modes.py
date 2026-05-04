"""Tier 3: CLI main() argparse and analysis-mode dispatch.

main() reads sys.argv, loads config, then routes to one of several
modes: --cleanup, --help-audit, --list-scans, --page-status, --search,
or a scan/diff. Each mode calls sys.exit() with a status code, so
tests wrap the call in pytest.raises(SystemExit) and read stdout via
capsys.

These cover the analysis surfaces — flags that don't need a browser.
Browser-driven scan modes (--page, --crawl, --urls) are exercised
through the crawl-loop tests.
"""

import json
import os

import pytest


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Redirect the scan registry to a tmp file."""
    import registry
    fake = str(tmp_path / 'scans.json')
    monkeypatch.setattr(registry, 'DEFAULT_REGISTRY_PATH', fake)
    return fake


def _run_main(cli, monkeypatch, argv):
    """Drive cli.main() with a fake sys.argv. Returns the SystemExit
    code (0 / 1 / 2). Wraps the boilerplate that every test below
    needs."""
    monkeypatch.setattr('sys.argv', ['a11y-catscan.py'] + argv)
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    code = excinfo.value.code
    return code if code is not None else 0


# ── --help-audit ──────────────────────────────────────────────

class TestHelpAudit:
    def test_prints_audit_guide(self, cli, monkeypatch, capsys):
        code = _run_main(cli, monkeypatch, ['--help-audit'])
        out = capsys.readouterr().out
        assert code == 0
        # Sanity — the printed guide mentions the major workflow words
        assert 'WCAG' in out
        assert 'AUDIT' in out.upper()
        assert 'a11y-catscan' in out


# ── --cleanup ─────────────────────────────────────────────────

class TestCleanup:
    def test_runs_and_reports_count(self, cli, monkeypatch, capsys):
        code = _run_main(cli, monkeypatch, ['--cleanup'])
        out = capsys.readouterr().out
        assert code == 0
        assert 'orphaned' in out.lower()
        # Number of killed processes is reported
        assert 'Killed' in out


# ── --list-scans ──────────────────────────────────────────────

class TestListScans:
    def test_empty_registry(
            self, cli, monkeypatch, capsys, isolated_registry):
        code = _run_main(cli, monkeypatch, ['--list-scans'])
        out = capsys.readouterr().out
        assert code == 0
        assert 'No registered scans' in out

    def test_with_entries(
            self, cli, monkeypatch, capsys, isolated_registry):
        import registry
        registry.register_scan(
            'baseline',
            {'jsonl': '/tmp/scan.jsonl'},
            url='https://example.test/',
            engines=['axe', 'ibm'],
            summary={'pages': 42, EARL: 'failed'} if False else
                    {'pages': 42, 'failed': 5},
            registry_path=isolated_registry)
        code = _run_main(cli, monkeypatch, ['--list-scans'])
        out = capsys.readouterr().out
        assert code == 0
        assert 'baseline' in out
        assert '42 pages' in out


# Helper imported lazily to dodge the EARL-line trick above
EARL = None  # noqa: E305 — sentinel only


# ── --search ───────────────────────────────────────────────────

class TestSearchMode:
    def test_searches_named_scan(
            self, cli, monkeypatch, capsys, tmp_path,
            isolated_registry, jsonl_factory,
            make_finding, make_page):
        from engine_mappings import EARL_FAILED
        # Set up a JSONL and register it as a named scan
        f = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        page = make_page('https://example.test/p', failed=[f])
        jsonl = jsonl_factory(
            [('https://example.test/p', page)],
            name='baseline.jsonl')

        import registry
        registry.register_scan(
            'baseline', {'jsonl': jsonl},
            url='https://example.test/',
            registry_path=isolated_registry)

        code = _run_main(
            cli, monkeypatch,
            ['--name', 'baseline', '--search', 'sc:1.4.3'])
        out = capsys.readouterr().out
        assert code == 0
        assert "matching 'sc:1.4.3'" in out
        assert 'sc-1.4.3' in out

    def test_no_report_found(
            self, cli, monkeypatch, capsys, isolated_registry):
        code = _run_main(
            cli, monkeypatch, ['--search', 'sc:1.4.3'])
        out = capsys.readouterr().out
        assert code == 1
        assert 'No scan report found' in out

    def test_bare_sc_number_treated_as_sc(
            self, cli, monkeypatch, capsys, tmp_path,
            isolated_registry, jsonl_factory,
            make_finding, make_page):
        from engine_mappings import EARL_FAILED
        f = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        page = make_page('https://example.test/p', failed=[f])
        jsonl = jsonl_factory(
            [('https://example.test/p', page)], name='b.jsonl')
        import registry
        registry.register_scan(
            'b', {'jsonl': jsonl},
            registry_path=isolated_registry)

        # No 'sc:' prefix — but '1.4.3' looks like an SC number
        code = _run_main(
            cli, monkeypatch,
            ['--name', 'b', '--search', '1.4.3'])
        out = capsys.readouterr().out
        assert code == 0
        assert 'sc-1.4.3' in out

    def test_search_url_prefix(
            self, cli, monkeypatch, capsys, tmp_path,
            isolated_registry, jsonl_factory,
            make_finding, make_page):
        # url:<glob> filters to pages whose URL matches the pattern.
        from engine_mappings import EARL_FAILED
        f = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        home = make_page('https://example.test/home', failed=[f])
        about = make_page('https://example.test/about', failed=[f])
        jsonl = jsonl_factory([
            ('https://example.test/home', home),
            ('https://example.test/about', about),
        ], name='b.jsonl')
        import registry
        registry.register_scan(
            'b', {'jsonl': jsonl},
            registry_path=isolated_registry)
        code = _run_main(
            cli, monkeypatch,
            ['--name', 'b', '--search', 'url:*about*'])
        out = capsys.readouterr().out
        assert code == 0
        # Filter narrowed results to /about
        assert 'about' in out

    def test_search_engine_prefix(
            self, cli, monkeypatch, capsys, tmp_path,
            isolated_registry, jsonl_factory,
            make_finding, make_page):
        from engine_mappings import EARL_FAILED
        f = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        page = make_page('https://example.test/p', failed=[f])
        jsonl = jsonl_factory(
            [('https://example.test/p', page)], name='b.jsonl')
        import registry
        registry.register_scan(
            'b', {'jsonl': jsonl},
            registry_path=isolated_registry)
        code = _run_main(
            cli, monkeypatch,
            ['--name', 'b', '--search', 'engine:axe'])
        out = capsys.readouterr().out
        assert code == 0
        # Search prints results as: [outcome] <tag> — <engine> — <sel>
        assert 'axe' in out
        assert "'engine:axe'" in out

    def test_search_selector_prefix(
            self, cli, monkeypatch, capsys, tmp_path,
            isolated_registry, jsonl_factory,
            make_finding, make_page):
        from engine_mappings import EARL_FAILED
        f = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#hero')
        page = make_page('https://example.test/p', failed=[f])
        jsonl = jsonl_factory(
            [('https://example.test/p', page)], name='b.jsonl')
        import registry
        registry.register_scan(
            'b', {'jsonl': jsonl},
            registry_path=isolated_registry)
        code = _run_main(
            cli, monkeypatch,
            ['--name', 'b', '--search', 'sel:*hero*'])
        out = capsys.readouterr().out
        assert code == 0
        assert '#hero' in out

    def test_search_outcome_prefix(
            self, cli, monkeypatch, capsys, tmp_path,
            isolated_registry, jsonl_factory,
            make_finding, make_page):
        from engine_mappings import EARL_FAILED, EARL_CANTTELL
        v = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        ct = make_finding('aria-valid-attrs', EARL_CANTTELL,
                          engine='axe', tags=['aria-valid-attrs'],
                          selector='#nav')
        page = make_page('https://example.test/p',
                         failed=[v], cant_tell=[ct])
        jsonl = jsonl_factory(
            [('https://example.test/p', page)], name='b.jsonl')
        import registry
        registry.register_scan(
            'b', {'jsonl': jsonl},
            registry_path=isolated_registry)
        code = _run_main(
            cli, monkeypatch,
            ['--name', 'b', '--search', 'outcome:cantTell'])
        out = capsys.readouterr().out
        assert code == 0
        # Only the cantTell finding shows up
        assert 'aria-valid-attrs' in out


# ── --page-status ──────────────────────────────────────────────

class TestPageStatusMode:
    def test_clean_page_exits_zero(
            self, cli, monkeypatch, capsys, isolated_registry,
            jsonl_factory, make_page):
        # No failures → clean → exit 0
        page = make_page('https://example.test/p')
        jsonl = jsonl_factory(
            [('https://example.test/p', page)], name='c.jsonl')
        import registry
        registry.register_scan(
            'c', {'jsonl': jsonl},
            registry_path=isolated_registry)

        code = _run_main(
            cli, monkeypatch,
            ['--name', 'c', '--page-status',
             'https://example.test/p'])
        out = capsys.readouterr().out
        assert code == 0
        assert 'CLEAN' in out

    def test_failing_page_exits_one(
            self, cli, monkeypatch, capsys, isolated_registry,
            jsonl_factory, make_finding, make_page):
        from engine_mappings import EARL_FAILED
        f = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        page = make_page('https://example.test/p', failed=[f])
        jsonl = jsonl_factory(
            [('https://example.test/p', page)], name='b.jsonl')
        import registry
        registry.register_scan(
            'b', {'jsonl': jsonl},
            registry_path=isolated_registry)

        code = _run_main(
            cli, monkeypatch,
            ['--name', 'b', '--page-status',
             'https://example.test/p'])
        out = capsys.readouterr().out
        assert code == 1
        assert 'FAILING' in out

    def test_url_not_in_report_exits_one(
            self, cli, monkeypatch, capsys, isolated_registry,
            jsonl_factory, make_page):
        page = make_page('https://example.test/p')
        jsonl = jsonl_factory(
            [('https://example.test/p', page)], name='c.jsonl')
        import registry
        registry.register_scan(
            'c', {'jsonl': jsonl},
            registry_path=isolated_registry)

        code = _run_main(
            cli, monkeypatch,
            ['--name', 'c', '--page-status',
             'https://example.test/missing'])
        out = capsys.readouterr().out
        assert code == 1
        assert 'not found' in out.lower()

    def test_no_report_exits_one(
            self, cli, monkeypatch, capsys, isolated_registry):
        code = _run_main(
            cli, monkeypatch,
            ['--page-status', 'https://example.test/'])
        out = capsys.readouterr().out
        assert code == 1
        assert 'No scan report' in out


# ── argparse validation ───────────────────────────────────────

class TestSeedFileModes:
    """--rescan / --violations-from / --incompletes-from / --urls
    early-exit and validation paths. These don't hit the browser —
    main() reads the seed file and either errors out or short-circuits
    before crawl_and_scan would launch Chromium.
    """

    # main() requires a URL argument before validating seed files,
    # so each test passes a placeholder URL — the seed-file logic
    # short-circuits before any browser would be launched.
    URL = 'https://example.test/'

    def test_rescan_missing_file_errors(
            self, cli, monkeypatch, capsys, tmp_path):
        code = _run_main(
            cli, monkeypatch,
            [self.URL, '--rescan', str(tmp_path / 'nope.jsonl')])
        # parser.error → exit 2
        assert code == 2
        err = capsys.readouterr().err
        assert 'Rescan file not found' in err

    def test_rescan_clean_report_exits_zero(
            self, cli, monkeypatch, capsys, tmp_path,
            jsonl_factory, make_page):
        # Previous scan had no failures or incompletes → main()
        # prints a "nothing to rescan" line and exits 0.
        page = make_page('https://example.test/p')
        prev = jsonl_factory(
            [('https://example.test/p', page)],
            name='prev.jsonl')
        code = _run_main(
            cli, monkeypatch, [self.URL, '--rescan', prev])
        out = capsys.readouterr().out
        assert code == 0
        assert 'nothing to rescan' in out.lower()

    def test_violations_from_missing_file_errors(
            self, cli, monkeypatch, capsys, tmp_path):
        code = _run_main(
            cli, monkeypatch,
            [self.URL, '--violations-from',
             str(tmp_path / 'nope.jsonl')])
        assert code == 2
        err = capsys.readouterr().err
        assert 'Report not found' in err

    def test_violations_from_clean_report_exits_zero(
            self, cli, monkeypatch, capsys, tmp_path,
            jsonl_factory, make_page):
        page = make_page('https://example.test/p')
        prev = jsonl_factory(
            [('https://example.test/p', page)],
            name='prev.jsonl')
        code = _run_main(
            cli, monkeypatch,
            [self.URL, '--violations-from', prev])
        out = capsys.readouterr().out
        assert code == 0
        assert 'no failures' in out.lower()

    def test_incompletes_from_clean_report_exits_zero(
            self, cli, monkeypatch, capsys, tmp_path,
            jsonl_factory, make_page):
        page = make_page('https://example.test/p')
        prev = jsonl_factory(
            [('https://example.test/p', page)],
            name='prev.jsonl')
        code = _run_main(
            cli, monkeypatch,
            [self.URL, '--incompletes-from', prev])
        out = capsys.readouterr().out
        assert code == 0
        assert 'no incompletes' in out.lower()

    def test_urls_missing_file_errors(
            self, cli, monkeypatch, capsys, tmp_path):
        code = _run_main(
            cli, monkeypatch,
            [self.URL, '--urls', str(tmp_path / 'nope.txt')])
        assert code == 2
        err = capsys.readouterr().err
        assert 'URL file not found' in err

    def test_urls_empty_file_errors(
            self, cli, monkeypatch, capsys, tmp_path):
        empty = tmp_path / 'urls.txt'
        # Only blank lines and a comment — should be treated as empty
        empty.write_text('\n# comment\n   \n')
        code = _run_main(
            cli, monkeypatch,
            [self.URL, '--urls', str(empty)])
        assert code == 2
        err = capsys.readouterr().err
        assert 'No URLs found' in err


class TestArgparseValidation:
    def test_invalid_level_rejected(self, cli, monkeypatch, capsys):
        code = _run_main(
            cli, monkeypatch,
            ['--level', 'wcag99zz', 'https://example.test/'])
        # argparse exits 2 on invalid choice
        assert code == 2
        err = capsys.readouterr().err
        assert 'invalid choice' in err.lower()

    def test_mutually_exclusive_page_and_crawl(
            self, cli, monkeypatch, capsys):
        # --page and --crawl are mutually exclusive
        code = _run_main(
            cli, monkeypatch,
            ['--page', '--crawl', 'https://example.test/'])
        assert code == 2
        err = capsys.readouterr().err
        assert 'not allowed' in err.lower() or \
            'mutually exclusive' in err.lower()

    def test_unknown_engine_rejected(
            self, cli, monkeypatch, capsys, tmp_path):
        # main() builds the engines list manually (not via argparse
        # choices) so unknown engines surface a parser.error → exit 2.
        code = _run_main(
            cli, monkeypatch,
            ['--engine', 'fakey-mc-fakeface',
             '--output-dir', str(tmp_path),
             'https://example.test/'])
        assert code == 2
        err = capsys.readouterr().err
        assert 'unknown engine' in err.lower()

    def test_resume_missing_state_file_errors(
            self, cli, monkeypatch, capsys, tmp_path):
        # --resume <path> with a non-existent path → exit 2 with an
        # ERROR line on stderr.  The state-load happens before any
        # browser launch, so this is a fast path to test.
        code = _run_main(
            cli, monkeypatch,
            ['--resume', str(tmp_path / 'no.state.json'),
             '--output-dir', str(tmp_path),
             'https://example.test/'])
        assert code == 2
        err = capsys.readouterr().err
        assert 'cannot load state file' in err.lower()

    def test_resume_invalid_json_errors(
            self, cli, monkeypatch, capsys, tmp_path):
        # A state file that exists but isn't valid JSON also errors
        # out before launching anything.
        bad = tmp_path / 'bad.state.json'
        bad.write_text('{not json')
        code = _run_main(
            cli, monkeypatch,
            ['--resume', str(bad),
             '--output-dir', str(tmp_path),
             'https://example.test/'])
        assert code == 2
        err = capsys.readouterr().err
        assert 'cannot load state file' in err.lower()
