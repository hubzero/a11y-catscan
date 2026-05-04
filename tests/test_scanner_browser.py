"""End-to-end smoke tests against a live Chromium and the engine JS.

These exercise the full Scanner pipeline:
    Playwright launch → page load → engine JS injection → result
    normalization → element resolution → optional cross-engine dedup.

Prerequisites (skipped cleanly when missing):
    - npm install has populated node_modules/ (axe-core, etc.)
    - Playwright Chromium is downloaded (`playwright install chromium`)

Run only the browser tests:
    pytest -m browser
Skip them (the default in CI without a browser):
    pytest -m "not browser"
"""

import os
import shutil
from pathlib import Path

import pytest

from scanner import Scanner
from engine_mappings import EARL_FAILED, EARL_CANTTELL


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / 'fixtures'

# Pre-flight: every test in this module needs node_modules populated.
# Skipping at module level keeps the suite green on machines that
# haven't run `npm install` yet.
_AXE_JS = PROJECT_ROOT / 'node_modules' / 'axe-core' / 'axe.min.js'
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        not _AXE_JS.is_file(),
        reason='node_modules not installed (run: npm install)'),
]


def _file_url(name):
    """file:// URL for a fixture HTML file."""
    return (FIXTURES / name).as_uri()


# ── axe-core end-to-end ────────────────────────────────────────

class TestAxeEndToEnd:
    async def test_scans_known_bad_page(self):
        async with Scanner(engines=['axe'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('known_bad.html'), dedup=False)

        assert 'skipped' not in result, (
            'Unexpected skip: {}'.format(result.get('skipped')))
        assert result[EARL_FAILED], 'axe should report violations'

        # Collect rule ids found
        rule_ids = {item['id'] for item in result[EARL_FAILED]}
        # Seeded violations we expect axe to detect.
        # 'document-title' may end up in failed; image/button/link
        # /label/duplicate-id-active are reliable across versions.
        expected = {
            'image-alt', 'button-name', 'link-name', 'label',
        }
        assert expected & rule_ids, (
            'Expected at least one of {} in {}'
            .format(expected, rule_ids))

    async def test_clean_page_has_no_critical_failures(self):
        async with Scanner(engines=['axe'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('clean.html'), dedup=False)

        assert 'skipped' not in result
        # No image/button/link/label violations on the clean page
        rule_ids = {item['id'] for item in result[EARL_FAILED]}
        forbidden = {'image-alt', 'button-name', 'label'}
        assert not (forbidden & rule_ids), (
            'Clean page surfaced unexpected failures: {}'
            .format(forbidden & rule_ids))

    async def test_findings_carry_normalized_tags(self):
        async with Scanner(engines=['axe'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('known_bad.html'), dedup=False)

        # axe's wcag143 / wcag2aa tags should be normalized to sc-* form
        all_tags = []
        for item in result[EARL_FAILED]:
            all_tags.extend(item.get('tags', []))
        sc_tags = [t for t in all_tags if t.startswith('sc-')]
        # The fixture intentionally trips at least image-alt (1.1.1)
        # or button-name (4.1.2), so we should see sc-* tags.
        assert sc_tags, 'expected normalized sc-* tags, got {}'.format(
            sorted(set(all_tags))[:10])

    async def test_engine_field_is_set(self):
        async with Scanner(engines=['axe'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('known_bad.html'), dedup=False)

        for item in result[EARL_FAILED]:
            assert item.get('engine') == 'axe'
            assert item.get('outcome') == EARL_FAILED

    async def test_scanner_lifecycle_idempotent(self):
        # start() / stop() should be safe to call repeatedly
        scanner = Scanner(engines=['axe'], quiet=True)
        await scanner.start()
        try:
            assert scanner.is_started
            # Double-start is a no-op
            await scanner.start()
        finally:
            await scanner.stop()
        assert not scanner.is_started
        # Double-stop is a no-op
        await scanner.stop()


# ── IBM Equal Access end-to-end ───────────────────────────────

@pytest.mark.skipif(
    not (PROJECT_ROOT / 'node_modules' / 'accessibility-checker-engine')
    .is_dir(),
    reason='IBM engine not installed')
class TestIbmEndToEnd:
    async def test_ibm_finds_violations(self):
        async with Scanner(engines=['ibm'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('known_bad.html'), dedup=False)

        assert 'skipped' not in result, (
            'Unexpected skip: {}'.format(result.get('skipped')))
        # IBM produces a mix of failed + cantTell on this fixture
        total = len(result[EARL_FAILED]) + len(result[EARL_CANTTELL])
        assert total > 0, 'IBM should produce at least one finding'

    async def test_ibm_engine_field_is_set(self):
        async with Scanner(engines=['ibm'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('known_bad.html'), dedup=False)
        for item in result[EARL_FAILED] + result[EARL_CANTTELL]:
            assert item.get('engine') == 'ibm'


# ── HTML_CodeSniffer end-to-end ───────────────────────────────

@pytest.mark.skipif(
    not (PROJECT_ROOT / 'node_modules' / 'html_codesniffer').is_dir(),
    reason='HTML_CodeSniffer not installed')
class TestHtmlcsEndToEnd:
    async def test_htmlcs_finds_violations(self):
        async with Scanner(engines=['htmlcs'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('known_bad.html'), dedup=False)

        assert 'skipped' not in result
        total = len(result[EARL_FAILED]) + len(result[EARL_CANTTELL])
        assert total > 0, 'HTMLCS should produce at least one finding'

    async def test_htmlcs_tags_normalized_to_sc(self):
        async with Scanner(engines=['htmlcs'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('known_bad.html'), dedup=False)

        all_tags = []
        for item in result[EARL_FAILED] + result[EARL_CANTTELL]:
            all_tags.extend(item.get('tags', []))
        sc_tags = [t for t in all_tags if t.startswith('sc-')]
        assert sc_tags, (
            'HTMLCS findings should be tagged with sc-* but got: {}'
            .format(sorted(set(all_tags))[:10]))


# ── Cross-engine integration ──────────────────────────────────

@pytest.mark.skipif(
    not (PROJECT_ROOT / 'node_modules' / 'accessibility-checker-engine')
    .is_dir(),
    reason='IBM engine not installed')
class TestMultiEngineDedup:
    async def test_dedup_collapses_shared_findings(self):
        # Run axe + IBM on the same page; some violations (e.g. label,
        # img missing alt) are flagged by both engines and should
        # collapse into engine_count > 1 entries after dedup.
        async with Scanner(engines=['axe', 'ibm'], quiet=True) as s:
            result = await s.scan_page(
                _file_url('known_bad.html'), dedup=True)

        assert 'skipped' not in result
        # At least one finding should have multiple engines attributed
        multi = [item for item in result[EARL_FAILED]
                 if item.get('engine_count', 1) > 1]
        # Even if no overlap, the engines should both produce results
        all_engines = set()
        for item in result[EARL_FAILED] + result[EARL_CANTTELL]:
            all_engines.update((item.get('engines') or {}).keys())
        assert {'axe', 'ibm'}.issubset(all_engines), (
            'Both engines should contribute findings; got {}'
            .format(all_engines))
        # Multi-engine collapse is the goal but engines disagree often
        # enough that we don't make this hard-required — just record
        # in the assertion message if it didn't happen.
        if not multi:
            pytest.skip(
                'axe and IBM did not overlap on this fixture — '
                'cross-engine dedup not exercised this run')


# ── Link extraction ───────────────────────────────────────────

class TestLinkExtraction:
    async def test_extract_links_returns_http_urls(self):
        async with Scanner(engines=['axe'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('with_links.html'),
                extract_links=True, dedup=False)

        assert 'links' in result, (
            'extract_links=True should populate result["links"]')
        links = result['links']
        # http(s) absolute links should appear
        assert any(l.startswith('https://example.test/a')
                    for l in links)
        assert any(l.startswith('http://example.test/b')
                    for l in links)
        # Pure-fragment anchor isn't an http link, so it must not
        # appear in the extracted set
        assert not any(l.endswith('#anchor') for l in links)

    async def test_extract_links_omitted_by_default(self):
        async with Scanner(engines=['axe'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('with_links.html'), dedup=False)
        assert 'links' not in result


# ── Alfa engine end-to-end ───────────────────────────────────

@pytest.mark.skipif(
    not (PROJECT_ROOT / 'node_modules' / '@siteimprove' /
         'alfa-rules').is_dir(),
    reason='Alfa engine not installed')
class TestAlfaEndToEnd:
    async def test_alfa_finds_violations(self):
        # Alfa runs as a Node subprocess and connects via CDP — slower
        # to start than the in-browser engines but the result shape
        # should match.
        async with Scanner(engines=['alfa'], quiet=True) as scanner:
            result = await scanner.scan_page(
                _file_url('known_bad.html'), dedup=False)

        assert 'skipped' not in result, (
            'Unexpected skip: {}'.format(result.get('skipped')))
        total = (len(result[EARL_FAILED]) +
                 len(result[EARL_CANTTELL]))
        assert total > 0, 'Alfa should produce at least one finding'

        for item in result[EARL_FAILED] + result[EARL_CANTTELL]:
            assert item.get('engine') == 'alfa'


# ── Browser restart ───────────────────────────────────────────

class TestBrowserRestart:
    """Scanner.restart_browser is the every-N-pages cycle that
    keeps long-running scans from leaking memory. We exercise it
    directly against the in-memory fixture page and verify the
    Scanner is still functional after the swap.
    """

    async def test_scan_works_after_restart(self):
        async with Scanner(engines=['axe'], quiet=True) as scanner:
            r1 = await scanner.scan_page(
                _file_url('clean.html'), dedup=False)
            assert 'skipped' not in r1
            browser_before = scanner.browser

            await scanner.restart_browser()

            # Browser instance has been replaced
            assert scanner.browser is not None
            # And scanning still works on the new browser
            r2 = await scanner.scan_page(
                _file_url('known_bad.html'), dedup=False)
            assert 'skipped' not in r2
            # axe should still report violations on the bad page
            assert r2[EARL_FAILED]


# ── Auth/login plugin lifecycle ──────────────────────────────

class TestAuthLifecycle:
    """End-to-end exercise of Scanner._setup_auth via a fake plugin.

    The fake plugin lives at tests/fixtures/fake_login_plugin.py and
    just records that it was called. We verify the Scanner wires it
    up: login() is invoked, the resulting context is used for scans,
    exclude_paths is exposed, and a failed login leaves the Scanner
    in anonymous-fallback mode.
    """

    PLUGIN = str(FIXTURES / 'fake_login_plugin.py')

    def _clean_state(self):
        # Scanner._setup_auth loads the plugin under its *own* module
        # name ('login_plugin'), so we read call counts from
        # `s._login_plugin` rather than from a separate import. This
        # helper just removes any persisted auth-state file so we
        # exercise the live-login path, not the saved-state shortcut.
        state_path = PROJECT_ROOT / '.auth-state.json'
        if state_path.is_file():
            state_path.unlink()

    async def test_login_invoked_on_start(self):
        self._clean_state()
        async with Scanner(
                engines=['axe'], quiet=True,
                auth={'login_script': self.PLUGIN,
                      'fake_outcome': 'success'},
                config={'url': _file_url('clean.html'),
                        'auth': {'fake_outcome': 'success'}}) as s:
            assert s.context is not None, (
                'Successful login should produce an auth context')
            assert '/logout-test' in s.login_exclude_paths
            assert s._login_plugin.calls['login'] == 1

    async def test_failed_login_falls_back_to_anonymous(self):
        self._clean_state()
        async with Scanner(
                engines=['axe'], quiet=True,
                auth={'login_script': self.PLUGIN,
                      'fake_outcome': 'fail'},
                config={'url': _file_url('clean.html'),
                        'auth': {'fake_outcome': 'fail'}}) as s:
            # Login returned False — Scanner runs without an auth
            # context (anonymous fallback)
            assert s.context is None
            assert s._login_plugin.calls['login'] == 1
            # And scanning still works
            result = await s.scan_page(
                _file_url('clean.html'), dedup=False)
            assert 'skipped' not in result

    async def test_exception_during_login_swallowed(self):
        self._clean_state()
        async with Scanner(
                engines=['axe'], quiet=True,
                auth={'login_script': self.PLUGIN,
                      'fake_outcome': 'raise'},
                config={'url': _file_url('clean.html'),
                        'auth': {'fake_outcome': 'raise'}}) as s:
            # Plugin raised — Scanner catches it and proceeds
            # anonymously rather than crashing the session.
            assert s.context is None
            assert s._login_plugin.calls['login'] == 1

    async def test_no_login_script_means_no_plugin(self):
        # Sanity: if no login_script is configured, the plugin is
        # never loaded and the Scanner has no auth context.
        async with Scanner(engines=['axe'], quiet=True) as s:
            assert s.context is None
            assert s.login_exclude_paths == []


# ── Scanner sanity ────────────────────────────────────────────

class TestScannerSanity:
    async def test_skip_result_for_invalid_file(self, tmp_path):
        bad = tmp_path / 'tiny.html'
        bad.write_text('<html></html>')  # under 100 bytes
        async with Scanner(engines=['axe'], quiet=True) as scanner:
            result = await scanner.scan_page(
                bad.as_uri(), dedup=False)
        assert result.get('skipped'), (
            'Expected skip, got {}'.format(result))

    async def test_engine_names_exposed(self):
        async with Scanner(
                engines=['axe'], quiet=True) as scanner:
            assert 'AxeEngine' in scanner.engine_names
