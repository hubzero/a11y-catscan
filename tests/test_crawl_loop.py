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


@pytest.fixture
def fixture_site():
    """Start a threaded HTTP server hosting tests/fixtures/site/."""
    handler = partial(_QuietHandler, directory=str(SITE))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Wait briefly for the listener to be ready
        for _ in range(50):
            try:
                with socket.create_connection(
                        ('127.0.0.1', port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        yield 'http://127.0.0.1:{}'.format(port)
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


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Same as in test_main_modes — keep registry writes contained."""
    import registry
    fake = str(tmp_path / 'scans.json')
    monkeypatch.setattr(registry, 'DEFAULT_REGISTRY_PATH', fake)
    return fake


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
            fixture_site, isolated_registry):
        # Copy the seeded known-bad fixture into the served directory
        # so the fixture HTTP server can respond to the request.
        bad_src = (Path(__file__).resolve().parent
                   / 'fixtures' / 'known_bad.html')
        bad_in_site = SITE / 'known_bad.html'
        bad_in_site.write_text(bad_src.read_text())
        try:
            monkeypatch.setattr('sys.argv', [
                'a11y-catscan.py',
                '--page',
                '-q',
                '--summary-json',
                '--output-dir', str(tmp_path),
                '--engine', 'axe',
                fixture_site + '/known_bad.html',
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
        finally:
            if bad_in_site.is_file():
                bad_in_site.unlink()

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
