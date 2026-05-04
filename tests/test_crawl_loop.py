"""End-to-end test of the CLI's crawl_and_scan loop.

Spins up a tiny local HTTP server hosting three linked HTML pages and
runs `crawl_and_scan(...)` against it. Asserts the crawler:
  - follows links from index.html to /page-a.html and /page-b.html
  - stops at the same-origin boundary (skips the off-site anchor)
  - writes a JSONL file with one line per page scanned
  - returns a final page count matching what landed in the JSONL

This is a long-ish test (browser launch + 3 page loads ≈ 5-10s)
and depends on node_modules + a real Chromium, so it carries the
'browser' marker.
"""

import json
import os
import socket
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITE = Path(__file__).resolve().parent / 'fixtures' / 'site'

_AXE_JS = PROJECT_ROOT / 'node_modules' / 'axe-core' / 'axe.min.js'

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        not _AXE_JS.is_file(),
        reason='node_modules not installed (run: npm install)'),
]


class _QuietHandler(SimpleHTTPRequestHandler):
    """Suppress per-request access logging — keeps test output clean."""
    def log_message(self, *args, **kwargs):
        pass


def _serve_dir(directory):
    """Spawn a 127.0.0.1 ThreadingHTTPServer for the given dir.

    Returns (base_url, server, thread).  Caller is responsible for
    `server.shutdown()` + `server.server_close()`.
    """
    handler = partial(_QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    # Wait briefly for the listener to be ready
    for _ in range(50):
        try:
            with socket.create_connection(
                    ('127.0.0.1', port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return f'http://127.0.0.1:{port}', server, thread


@pytest.fixture
def fixture_site():
    """Start a threaded HTTP server hosting tests/fixtures/site/."""
    base_url, server, _ = _serve_dir(SITE)
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def tmp_fixture_site(tmp_path):
    """Start a threaded HTTP server hosting `tmp_path`.

    Use this when a test needs to serve files it generates so the
    shared tests/fixtures/site/ directory stays untouched (and so
    parallel runs don't race each other writing into it).
    """
    base_url, server, _ = _serve_dir(tmp_path)
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()


def _read_jsonl_urls(path):
    urls = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            urls.extend(obj.keys())
    return urls


# isolated_registry fixture defined in tests/conftest.py


class TestCrawlLoop:
    def test_crawls_and_writes_jsonl(
            self, cli, tmp_path, fixture_site):
        json_path = str(tmp_path / 'scan.json')
        page_count, jsonl_path, wall_time, page_time, _t = \
            cli.crawl_and_scan(
                start_url=fixture_site + '/index.html',
                max_pages=10,
                level='wcag21aa',
                quiet=True,
                config={
                    'engine': 'axe',
                    # Disable nice / oom adjustments — they need
                    # privileges some CI environments don't grant.
                    'niceness': 0,
                    'oom_score_adj': 0,
                    'workers': 1,
                },
                json_path=json_path,
                save_every=0)

        # Should have reached all three pages
        assert page_count == 3, (
            'Expected 3 scanned pages, got {}'.format(page_count))

        # JSONL exists with one line per page
        assert os.path.isfile(jsonl_path)
        urls = _read_jsonl_urls(jsonl_path)
        assert len(urls) == 3
        # Index + both linked pages, all on the local origin
        paths = sorted(u.split(fixture_site, 1)[1] for u in urls)
        assert '/index.html' in paths
        assert '/page-a.html' in paths
        assert '/page-b.html' in paths
        # Off-site link from index.html should not have been scanned
        for u in urls:
            assert u.startswith(fixture_site), (
                'Crawler followed off-origin URL: {}'.format(u))

    def test_seed_urls_disables_link_following(
            self, cli, tmp_path, fixture_site):
        # When seed_urls is provided, the crawler scans only those
        # URLs without following discovered links.
        json_path = str(tmp_path / 'scan.json')
        page_count, jsonl_path, _wall, _ptime, _t = cli.crawl_and_scan(
            start_url=fixture_site + '/index.html',
            seed_urls=[fixture_site + '/page-a.html'],
            max_pages=10,
            level='wcag21aa',
            quiet=True,
            config={'engine': 'axe', 'niceness': 0,
                    'oom_score_adj': 0, 'workers': 1},
            json_path=json_path,
            save_every=0)

        assert page_count == 1
        urls = _read_jsonl_urls(jsonl_path)
        assert urls == [fixture_site + '/page-a.html']

    def test_max_pages_caps_the_crawl(
            self, cli, tmp_path, fixture_site):
        json_path = str(tmp_path / 'scan.json')
        page_count, jsonl_path, _w, _p, _t = cli.crawl_and_scan(
            start_url=fixture_site + '/index.html',
            max_pages=1,  # only the start URL
            level='wcag21aa',
            quiet=True,
            config={'engine': 'axe', 'niceness': 0,
                    'oom_score_adj': 0, 'workers': 1},
            json_path=json_path,
            save_every=0)

        assert page_count == 1
        urls = _read_jsonl_urls(jsonl_path)
        assert urls == [fixture_site + '/index.html']

    def test_rescan_only_revisits_pages_with_findings(
            self, cli, tmp_path, fixture_site):
        # Step 1: do a normal crawl. It produces a JSONL where every
        # page is "clean" (the fixture pages have no seeded WCAG
        # failures), so _extract_urls_from_report returns nothing
        # for failed; we synthesize one by hand.
        from engine_mappings import EARL_FAILED
        prev = tmp_path / 'prev.jsonl'
        # Synthesize a previous-scan JSONL where only page-a.html
        # had a violation. --rescan should pick only that URL.
        prev.write_text(
            json.dumps({
                fixture_site + '/page-a.html': {
                    EARL_FAILED: [{'id': 'image-alt',
                                    'nodes': [{'target': ['#x']}]}],
                },
                fixture_site + '/page-b.html': {EARL_FAILED: []},
            }) + '\n')

        # Use _extract_urls_from_report + crawl_and_scan(seed_urls)
        # to mirror what the CLI's --rescan path does.
        urls = cli.extract_urls_from_report(str(prev))
        assert urls == [fixture_site + '/page-a.html']

        json_path = str(tmp_path / 'scan.json')
        page_count, jsonl_path, _w, _p, _t = cli.crawl_and_scan(
            start_url=fixture_site + '/index.html',
            seed_urls=urls,
            max_pages=10,
            level='wcag21aa',
            quiet=True,
            config={'engine': 'axe', 'niceness': 0,
                    'oom_score_adj': 0, 'workers': 1},
            json_path=json_path,
            save_every=0)

        # Only the previously-failing URL got rescanned
        assert page_count == 1
        scanned = _read_jsonl_urls(jsonl_path)
        assert scanned == [fixture_site + '/page-a.html']

    def test_restart_every_triggers_browser_restart(
            self, cli, tmp_path, fixture_site, monkeypatch):
        # restart_every=2 with 3 pages crawled should trip the
        # mid-crawl browser swap exactly once. We spy on
        # Scanner.restart_browser to confirm the call site fires.
        from scanner import Scanner

        call_count = {'n': 0}
        original = Scanner.restart_browser

        async def _counting_restart(self):
            call_count['n'] += 1
            await original(self)

        monkeypatch.setattr(
            Scanner, 'restart_browser', _counting_restart)

        json_path = str(tmp_path / 'scan.json')
        page_count, jsonl_path, _w, _p, _t = cli.crawl_and_scan(
            start_url=fixture_site + '/index.html',
            max_pages=10,
            level='wcag21aa',
            quiet=True,
            config={'engine': 'axe', 'niceness': 0,
                    'oom_score_adj': 0, 'workers': 1,
                    'restart_every': 2},
            json_path=json_path,
            save_every=0)

        assert page_count == 3
        # 3 pages with restart_every=2 → restart fires after the 2nd
        assert call_count['n'] >= 1, (
            'restart_browser should fire when crossing '
            'restart_every threshold')

    def test_main_page_mode_with_summary_json(
            self, cli, monkeypatch, capsys, tmp_path,
            fixture_site, isolated_registry):
        # Drive the CLI through main() in --page mode against the
        # fixture server's /page-a.html — axe-core should report it
        # clean, so --summary-json prints {"clean": true, ...} and
        # main() exits 0.
        monkeypatch.setattr('sys.argv', [
            'a11y-catscan.py',
            '--page',
            '-q',
            '--summary-json',
            '--output-dir', str(tmp_path),
            '--engine', 'axe',
            fixture_site + '/page-a.html',
        ])
        # main() only sys.exit(1) on violations; the clean path just
        # returns. Accept either.
        try:
            cli.main()
            exit_code = 0
        except SystemExit as e:
            exit_code = e.code or 0
        assert exit_code == 0

        out = capsys.readouterr().out
        # The summary JSON is printed as the last meaningful line
        # of stdout. Find it and parse.
        json_lines = [
            ln for ln in out.splitlines()
            if ln.startswith('{') and ln.endswith('}')]
        assert json_lines, (
            'Expected a JSON summary line in: {!r}'.format(out))
        summary = json.loads(json_lines[-1])
        assert summary['pages'] == 1
        assert summary['clean'] is True
        assert summary['failed'] == 0

    def test_main_diff_against_prior_scan(
            self, cli, monkeypatch, capsys, tmp_path,
            fixture_site, isolated_registry):
        # Build a synthetic prior scan JSONL where /page-a.html had
        # a violation. Run main() in --page mode against the live
        # fixture (where /page-a.html is clean), with --diff pointing
        # at the prior. The diff output should mention the FIXED
        # finding.
        from engine_mappings import EARL_FAILED
        prior = tmp_path / 'prev.jsonl'
        prior.write_text(json.dumps({
            fixture_site + '/page-a.html': {
                EARL_FAILED: [{
                    'id': 'image-alt',
                    'tags': ['sc-1.1.1'],
                    'nodes': [{'target': ['#x'], 'html': '<img>'}],
                }],
            },
        }) + '\n')

        monkeypatch.setattr('sys.argv', [
            'a11y-catscan.py',
            '--page',
            '-q',
            '--output-dir', str(tmp_path),
            '--engine', 'axe',
            '--diff', str(prior),
            fixture_site + '/page-a.html',
        ])
        try:
            cli.main()
        except SystemExit:
            pass

        out = capsys.readouterr().out
        # main() prints "Diff vs <path>:" before invoking diff_scans
        assert 'Diff vs' in out
        # diff_scans prints FIXED for issues that disappeared
        assert 'FIXED' in out
        assert 'image-alt' in out

    def test_main_diff_warns_on_missing_diff_file(
            self, cli, monkeypatch, capsys, tmp_path,
            fixture_site, isolated_registry):
        # --diff path doesn't exist → main() prints a WARNING line
        # but still completes the scan normally.
        monkeypatch.setattr('sys.argv', [
            'a11y-catscan.py',
            '--page',
            '-q',
            '--output-dir', str(tmp_path),
            '--engine', 'axe',
            '--diff', str(tmp_path / 'nope.jsonl'),
            fixture_site + '/page-a.html',
        ])
        try:
            cli.main()
        except SystemExit:
            pass
        out = capsys.readouterr().out
        assert 'diff file not found' in out

    def test_main_page_mode_failing_page_exits_one(
            self, cli, monkeypatch, capsys, tmp_path,
            tmp_fixture_site, isolated_registry):
        # Serve the seeded known-bad fixture from tmp_path (not the
        # shared tests/fixtures/site/) so parallel test runs don't
        # race writing into the repo.
        bad_src = (Path(__file__).resolve().parent
                   / 'fixtures' / 'known_bad.html')
        (tmp_path / 'known_bad.html').write_text(bad_src.read_text())
        monkeypatch.setattr('sys.argv', [
            'a11y-catscan.py',
            '--page',
            '-q',
            '--summary-json',
            '--output-dir', str(tmp_path),
            '--engine', 'axe',
            tmp_fixture_site + '/known_bad.html',
        ])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        # Violations → exit 1
        assert excinfo.value.code == 1

        out = capsys.readouterr().out
        json_lines = [
            ln for ln in out.splitlines()
            if ln.startswith('{') and ln.endswith('}')]
        assert json_lines
        summary = json.loads(json_lines[-1])
        assert summary['pages'] == 1
        assert summary['clean'] is False
        assert summary['failed'] > 0

    def test_multi_worker_crawl_visits_each_page_once(
            self, cli, tmp_path, fixture_site):
        # workers=3 with 3 pages should produce exactly one JSONL
        # line per page (no duplicates from race conditions in the
        # visited set), and all three pages should be reached.
        json_path = str(tmp_path / 'scan.json')
        page_count, jsonl_path, _w, _p, _t = cli.crawl_and_scan(
            start_url=fixture_site + '/index.html',
            max_pages=10,
            level='wcag21aa',
            quiet=True,
            config={'engine': 'axe', 'niceness': 0,
                    'oom_score_adj': 0, 'workers': 3},
            json_path=json_path,
            save_every=0)

        assert page_count == 3
        urls = _read_jsonl_urls(jsonl_path)
        # No duplicates — each URL appears exactly once
        assert len(urls) == len(set(urls)) == 3
        paths = sorted(u.split(fixture_site, 1)[1] for u in urls)
        assert paths == ['/index.html', '/page-a.html', '/page-b.html']

    def test_resume_state_continues_from_saved_queue(
            self, cli, tmp_path, fixture_site):
        # Start a crawl from page-a.html and pre-seed it with the
        # other two pages already in the queue, simulating a
        # previously-saved --resume state file.
        json_path = str(tmp_path / 'scan.json')
        resume_state = {
            'queue': [
                fixture_site + '/page-a.html',
                fixture_site + '/page-b.html',
            ],
            'visited': [fixture_site + '/index.html'],
        }
        page_count, jsonl_path, _w, _p, _t = cli.crawl_and_scan(
            start_url=fixture_site + '/index.html',
            max_pages=10,
            level='wcag21aa',
            quiet=True,
            config={'engine': 'axe', 'niceness': 0,
                    'oom_score_adj': 0, 'workers': 1},
            json_path=json_path,
            save_every=0,
            resume_state=resume_state)

        # Should scan exactly the two queued pages — the visited
        # set already contains index.html so it isn't rescanned.
        assert page_count == 2
        urls = _read_jsonl_urls(jsonl_path)
        # index.html shouldn't appear; page-a and page-b should
        assert sorted(urls) == sorted([
            fixture_site + '/page-a.html',
            fixture_site + '/page-b.html',
        ])

    def test_worker_scan_exception_doesnt_halt_crawl(
            self, cli, tmp_path, fixture_site, monkeypatch):
        # If a worker task raises mid-scan the drain handler
        # should swallow it and the remaining pages should still
        # complete.  Patches Scanner.scan_page to raise on
        # /page-a.html only; verifies index.html and page-b.html
        # land in the JSONL.
        from scanner import Scanner

        original = Scanner.scan_page
        target = fixture_site + '/page-a.html'

        async def _flaky(self, url, *args, **kwargs):
            if url == target:
                raise RuntimeError('synthetic worker error')
            return await original(self, url, *args, **kwargs)

        monkeypatch.setattr(Scanner, 'scan_page', _flaky)

        json_path = str(tmp_path / 'scan.json')
        page_count, jsonl_path, _w, _p, _t = cli.crawl_and_scan(
            start_url=fixture_site + '/index.html',
            max_pages=10,
            level='wcag21aa',
            quiet=True,
            verbose=True,  # exercise the verbose-warning branch
            config={'engine': 'axe', 'niceness': 0,
                    'oom_score_adj': 0, 'workers': 1},
            json_path=json_path,
            save_every=0)

        # index.html scans, page-a.html raises (excluded), page-b.html
        # scans — final JSONL has 2 lines, page-a.html absent.
        urls = _read_jsonl_urls(jsonl_path)
        assert target not in urls
        assert fixture_site + '/index.html' in urls
        assert fixture_site + '/page-b.html' in urls

    def test_unknown_level_exits_with_error(
            self, cli, tmp_path):
        # A bogus level name should print an ERROR and sys.exit(1)
        # before any browser is launched — covers the early
        # validation in crawl_and_scan.  No fixture_site needed.
        with pytest.raises(SystemExit) as excinfo:
            cli.crawl_and_scan(
                start_url='https://example.test/',
                level='wcag99zz',  # not in WCAG_LEVELS
                quiet=True,
                config={'engine': 'axe', 'niceness': 0,
                        'oom_score_adj': 0, 'workers': 1},
                json_path=str(tmp_path / 'scan.json'),
                save_every=0)
        assert excinfo.value.code == 1

    def test_session_recovery_bans_logout_trap(
            self, cli, tmp_path, fixture_site, monkeypatch):
        # End-to-end exercise of the recovery cycle.  The
        # session_expiry_plugin treats /page-a.html as a logout
        # trap — every visit to it makes is_logged_in return
        # False.  The crawl should:
        #   1. Scan index.html (session OK)
        #   2. Scan page-a.html, detect logout, mark suspect,
        #      enter recovery
        #   3. Drain workers, re-login
        #   4. Re-test page-a.html: still triggers logout → ban
        #   5. Re-login again, exit recovery
        #   6. Resume crawl, scan page-b.html successfully
        # Final JSONL has index + page-b but NOT page-a.
        plugin_path = str(
            Path(__file__).resolve().parent
            / 'fixtures' / 'session_expiry_plugin.py')
        monkeypatch.setenv('A11Y_TEST_LOGOUT_TRAPS', 'page-a')
        # Reset plugin call counts (the module is imported under
        # a fresh name by Scanner._setup_auth so the counters in
        # session_expiry_plugin.calls won't actually match the
        # plugin instance loaded by Scanner — verifying side-
        # effects via the JSONL output is what matters).
        from tests.fixtures import session_expiry_plugin
        session_expiry_plugin.reset()

        json_path = str(tmp_path / 'scan.json')
        page_count, jsonl_path, _w, _p, _t = cli.crawl_and_scan(
            start_url=fixture_site + '/index.html',
            max_pages=10,
            level='wcag21aa',
            quiet=True,
            verbose=True,
            config={
                'engine': 'axe', 'niceness': 0,
                'oom_score_adj': 0, 'workers': 1,
                'auth': {'login_script': plugin_path},
            },
            json_path=json_path,
            save_every=0)

        # Final JSONL should contain index + page-b, NOT page-a
        # (which got banned as a logout trap).
        urls = _read_jsonl_urls(jsonl_path)
        assert fixture_site + '/page-a.html' not in urls
        assert fixture_site + '/index.html' in urls
        assert fixture_site + '/page-b.html' in urls

        # State file should record page-a as a banned logout URL.
        state_path = json_path.replace('.json', '.state.json')
        if os.path.isfile(state_path):
            with open(state_path) as f:
                state = json.load(f)
            assert any('page-a' in u
                       for u in state.get('logout_urls', []))

    def test_recovery_circuit_breaker_on_relogin_failure(
            self, cli, tmp_path, fixture_site, monkeypatch,
            capsys):
        # If re-login fails inside recovery, the breaker disables
        # further recovery attempts for the rest of the run —
        # otherwise every subsequent page would re-trigger
        # recovery, which would re-fail relogin, and the crawl
        # would never exit.  Configure the plugin so:
        #   - /page-a.html is a logout trap (kills session)
        #   - The first login() (initial) succeeds
        #   - The second login() (the recovery relogin) fails
        # Expected behavior: page-a triggers recovery, relogin
        # fails, breaker fires, suspect URLs banned, scan
        # continues without further recovery and exits.
        plugin_path = str(
            Path(__file__).resolve().parent
            / 'fixtures' / 'session_expiry_plugin.py')
        monkeypatch.setenv('A11Y_TEST_LOGOUT_TRAPS', 'page-a')
        monkeypatch.setenv('A11Y_TEST_LOGIN_FAILS_AFTER', '1')
        from tests.fixtures import session_expiry_plugin
        session_expiry_plugin.reset()

        json_path = str(tmp_path / 'scan.json')
        # The crawl must terminate within a reasonable time —
        # if the breaker is broken, this test will hang until
        # pytest times out.
        page_count, jsonl_path, _w, _p, _t = cli.crawl_and_scan(
            start_url=fixture_site + '/index.html',
            max_pages=10,
            level='wcag21aa',
            quiet=True,
            config={
                'engine': 'axe', 'niceness': 0,
                'oom_score_adj': 0, 'workers': 1,
                'auth': {'login_script': plugin_path},
            },
            json_path=json_path,
            save_every=0)

        # Crawl exited (didn't loop).  page-a was banned.
        urls = _read_jsonl_urls(jsonl_path)
        assert fixture_site + '/page-a.html' not in urls

    def test_check_session_exception_surfaces_warning(
            self, cli, tmp_path, fixture_site, monkeypatch,
            capsys):
        # If is_logged_in() raises, Scanner should surface a
        # WARNING to stderr (always, not just under verbose) so
        # a plugin bug doesn't silently disable session
        # detection across the whole scan.
        plugin_path = str(
            Path(__file__).resolve().parent
            / 'fixtures' / 'session_expiry_plugin.py')
        monkeypatch.setenv('A11Y_TEST_IS_LOGGED_IN_RAISES', '1')
        from tests.fixtures import session_expiry_plugin
        session_expiry_plugin.reset()

        json_path = str(tmp_path / 'scan.json')
        cli.crawl_and_scan(
            start_url=fixture_site + '/index.html',
            max_pages=2,
            level='wcag21aa',
            quiet=True,
            config={
                'engine': 'axe', 'niceness': 0,
                'oom_score_adj': 0, 'workers': 1,
                'auth': {'login_script': plugin_path},
            },
            json_path=json_path,
            save_every=0)

        err = capsys.readouterr().err
        assert 'check_session raised' in err
        assert 'simulated is_logged_in failure' in err
