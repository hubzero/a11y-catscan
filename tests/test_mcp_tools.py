"""Tier 3: MCP server tool functions.

The MCP tools registered via @mcp.tool() are still plain async
functions — we call them directly (no FastMCP transport needed) and
assert on the JSON they return. Each tool is a thin wrapper over the
registry/scanner functions already covered, so these tests verify the
tool-level wiring: argument plumbing, error paths, and the JSON
shape downstream consumers depend on.
"""

import json
import os

import pytest

from engine_mappings import EARL_FAILED, EARL_CANTTELL


@pytest.fixture
def mcp_server_module():
    """Import mcp_server lazily so failures show clearly."""
    import mcp_server
    return mcp_server


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Redirect registry.DEFAULT_REGISTRY_PATH to a tmp file.

    The MCP `manage_scans` tool calls list_scans() / get_scan() /
    delete_scan() without an explicit path, so they fall through to
    the module-level default. Patching that lets each test work
    against a clean registry file.
    """
    import registry
    fake = str(tmp_path / 'scans.json')
    monkeypatch.setattr(registry, 'DEFAULT_REGISTRY_PATH', fake)
    return fake


@pytest.fixture
def populated_jsonl(jsonl_factory, make_finding, make_page):
    """Same shape as the search-findings fixture, kept local for MCP."""
    home_v = make_finding('color-contrast', EARL_FAILED, engine='axe',
                          tags=['sc-1.4.3'], selector='#hero')
    home_v_ibm = make_finding('text_contrast_sufficient',
                              EARL_FAILED, engine='ibm',
                              tags=['sc-1.4.3'], selector='#hero')
    admin_ct = make_finding('aria-allowed-attr', EARL_CANTTELL,
                             engine='axe',
                             tags=['aria-valid-attrs'],
                             selector='#nav')
    docs_v = make_finding('color-contrast', EARL_FAILED, engine='axe',
                          tags=['sc-1.4.3'], selector='.btn')

    home = make_page('https://example.test/home',
                     failed=[home_v, home_v_ibm])
    admin = make_page('https://example.test/admin/users',
                      cant_tell=[admin_ct])
    docs = make_page('https://example.test/docs/intro',
                     failed=[docs_v])
    return jsonl_factory([
        ('https://example.test/home', home),
        ('https://example.test/admin/users', admin),
        ('https://example.test/docs/intro', docs),
    ])


# ── lookup_wcag ────────────────────────────────────────────────

class TestLookupWcag:
    async def test_known_sc(self, mcp_server_module):
        out = await mcp_server_module.lookup_wcag('1.4.3')
        data = json.loads(out)
        assert data['sc'] == '1.4.3'
        assert data['tag'] == 'sc-1.4.3'
        assert data['level'] == 'AA'
        assert data['wcag_version'] == '2.0'
        assert data['name'] == 'Contrast (Minimum)'
        # Multiple engines test SC 1.4.3
        assert 'axe' in data['tested_by']
        assert data['engine_count'] >= 1

    async def test_with_sc_prefix(self, mcp_server_module):
        out = await mcp_server_module.lookup_wcag('sc-2.1.1')
        data = json.loads(out)
        assert data['sc'] == '2.1.1'
        assert data['name'] == 'Keyboard'

    async def test_unknown_sc(self, mcp_server_module):
        out = await mcp_server_module.lookup_wcag('99.99.99')
        data = json.loads(out)
        assert 'error' in data
        assert 'hint' in data


# ── list_engines ───────────────────────────────────────────────

class TestListEngines:
    async def test_lists_four_engines(self, mcp_server_module):
        out = await mcp_server_module.list_engines()
        data = json.loads(out)
        names = [e['name'] for e in data['engines']]
        assert names == ['axe', 'ibm', 'htmlcs', 'alfa']
        for entry in data['engines']:
            assert 'version' in entry
            assert 'rules' in entry
            assert 'installed' in entry  # bool

    async def test_includes_tag_taxonomy(self, mcp_server_module):
        out = await mcp_server_module.list_engines()
        data = json.loads(out)
        assert 'wcag' in data['tags']
        assert isinstance(data['tags']['aria'], list)
        assert isinstance(data['tags']['bp'], list)

    def test_get_axe_version_handles_missing_file(
            self, monkeypatch, tmp_path):
        # If axe.min.js is unreadable (e.g. node_modules missing),
        # get_axe_version should swallow the IOError and return
        # the 'not installed' sentinel instead of crashing.
        # Patch the cached path + reset the version cache so the
        # next call re-reads through the missing path.
        from engines import axe as axe_mod
        monkeypatch.setattr(
            axe_mod, 'AXE_JS_PATH',
            str(tmp_path / 'nope.min.js'))
        monkeypatch.setattr(axe_mod, '_AXE_VERSION', None)
        assert axe_mod.get_axe_version() == 'not installed'


# ── analyze_report ─────────────────────────────────────────────

class TestAnalyzeReport:
    async def test_missing_report_returns_error(
            self, mcp_server_module, tmp_path):
        out = await mcp_server_module.analyze_report(
            str(tmp_path / 'nope.jsonl'))
        data = json.loads(out)
        assert 'error' in data

    async def test_group_by_wcag(
            self, mcp_server_module, populated_jsonl):
        out = await mcp_server_module.analyze_report(
            populated_jsonl, group_by='wcag')
        data = json.loads(out)
        assert data['group_by'] == 'wcag'
        # The fixture has SC 1.4.3 on home (axe+ibm) and docs (axe)
        keys = [g['key'] for g in data['groups']]
        assert 'sc-1.4.3' in keys
        # SC name resolved
        sc_group = next(g for g in data['groups']
                         if g['key'] == 'sc-1.4.3')
        assert sc_group['sc_name'] == 'Contrast (Minimum)'

    async def test_group_by_engine(
            self, mcp_server_module, populated_jsonl):
        out = await mcp_server_module.analyze_report(
            populated_jsonl, group_by='engine')
        data = json.loads(out)
        assert data['group_by'] == 'engine'
        assert data['groups']  # at least one group

    async def test_group_by_bp_falls_back_to_sc(
            self, mcp_server_module, populated_jsonl):
        # group_by='bp' looks for bp-/aria- tags first, falls back to
        # sc-* when neither is present.  The fixture has the cantTell
        # entry tagged aria-valid-attrs and the failed entries tagged
        # sc-1.4.3 — both keys should appear.
        out = await mcp_server_module.analyze_report(
            populated_jsonl, group_by='bp')
        data = json.loads(out)
        keys = [g['key'] for g in data['groups']]
        assert 'aria-valid-attrs' in keys
        assert 'sc-1.4.3' in keys

    async def test_group_by_rule_uses_id(
            self, mcp_server_module, populated_jsonl):
        # Any value other than wcag/engine/bp falls through to using
        # item['id'] (rule id) as the group key.
        out = await mcp_server_module.analyze_report(
            populated_jsonl, group_by='rule')
        data = json.loads(out)
        keys = [g['key'] for g in data['groups']]
        assert 'color-contrast' in keys
        assert 'aria-allowed-attr' in keys

    async def test_handles_corrupt_jsonl_line(
            self, mcp_server_module, tmp_path):
        # A corrupt JSON line should surface as an error rather than
        # crash the tool.
        bad = tmp_path / 'bad.jsonl'
        bad.write_text('{not valid json}\n')
        out = await mcp_server_module.analyze_report(str(bad))
        data = json.loads(out)
        assert 'error' in data

    async def test_group_by_engine_with_dedup_engines_dict(
            self, mcp_server_module, tmp_path):
        # Deduped findings carry an `engines` dict (engine name -> rule
        # id from that engine).  group_by='engine' should join them
        # into a 'axe+ibm' style key for these multi-engine findings.
        # JSONL lines with blank lines in between also exercise the
        # blank-line skip in the loop.
        from engine_mappings import EARL_FAILED
        path = tmp_path / 'deduped.jsonl'
        page_data = {
            'https://example.test/p': {
                'url': 'https://example.test/p',
                'timestamp': '2026-04-30T10:00:00',
                'http_status': 200,
                EARL_FAILED: [{
                    'id': 'color-contrast',
                    'engine': 'axe',
                    'engines': {
                        'axe': 'color-contrast',
                        'ibm': 'text_contrast_sufficient',
                    },
                    'engine_count': 2,
                    'tags': ['sc-1.4.3'],
                    'impact': 'serious',
                    'description': 'merged',
                    'nodes': [{'target': ['#hero'],
                               'html': '<p>x</p>'}],
                }],
                'cantTell': [], 'passed': [], 'inapplicable': [],
            },
        }
        path.write_text(
            '\n' + json.dumps(page_data) + '\n\n')
        out = await mcp_server_module.analyze_report(
            str(path), group_by='engine')
        data = json.loads(out)
        keys = [g['key'] for g in data['groups']]
        # Multi-engine key joined with '+'
        assert any('+' in k for k in keys), (
            'Expected joined engines key, got {}'.format(keys))


# ── find_issues ────────────────────────────────────────────────

class TestFindIssues:
    async def test_no_filters_returns_all(
            self, mcp_server_module, populated_jsonl):
        out = await mcp_server_module.find_issues(populated_jsonl)
        data = json.loads(out)
        # 2 failed (home merged, docs) + 1 cantTell
        assert data['count'] == 3

    async def test_filter_by_sc(
            self, mcp_server_module, populated_jsonl):
        out = await mcp_server_module.find_issues(
            populated_jsonl, sc='1.4.3')
        data = json.loads(out)
        assert data['count'] == 2
        assert data['filters']['sc'] == '1.4.3'

    async def test_filter_by_engine(
            self, mcp_server_module, populated_jsonl):
        out = await mcp_server_module.find_issues(
            populated_jsonl, engine='ibm')
        data = json.loads(out)
        # Only the home page's contrast finding has IBM
        assert data['count'] == 1

    async def test_missing_report(
            self, mcp_server_module, tmp_path):
        out = await mcp_server_module.find_issues(
            str(tmp_path / 'nope.jsonl'))
        data = json.loads(out)
        assert 'error' in data


# ── check_page ─────────────────────────────────────────────────

class TestCheckPage:
    async def test_failing_page(
            self, mcp_server_module, populated_jsonl):
        out = await mcp_server_module.check_page(
            populated_jsonl, 'https://example.test/home')
        data = json.loads(out)
        assert data['found'] is True
        assert data['clean'] is False
        # SC breakdown populated
        assert '1.4.3' in data['sc_breakdown']

    async def test_clean_page(
            self, mcp_server_module, populated_jsonl):
        out = await mcp_server_module.check_page(
            populated_jsonl,
            'https://example.test/admin/users')
        data = json.loads(out)
        # Only cantTell, no failed → clean
        assert data['clean'] is True

    async def test_url_not_in_report(
            self, mcp_server_module, populated_jsonl):
        out = await mcp_server_module.check_page(
            populated_jsonl, 'https://example.test/missing')
        data = json.loads(out)
        assert data['found'] is False

    async def test_missing_report(
            self, mcp_server_module, isolated_registry):
        # An unresolvable report name should surface a Report-not-found
        # error before trying to read the file.
        out = await mcp_server_module.check_page(
            '/nope/does-not-exist.jsonl', 'https://example.test/p')
        data = json.loads(out)
        assert 'error' in data
        assert 'not found' in data['error'].lower()


# ── compare_scans ──────────────────────────────────────────────

class TestCompareScans:
    async def test_diff_via_mcp(
            self, mcp_server_module, jsonl_factory,
            make_finding, make_page):
        a = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#a')
        b = make_finding('label', EARL_FAILED, engine='axe',
                         tags=['sc-4.1.2'], selector='#b')
        old = jsonl_factory(
            [('https://example.test/p',
              make_page('https://example.test/p', failed=[a]))],
            name='old.jsonl')
        new = jsonl_factory(
            [('https://example.test/p',
              make_page('https://example.test/p', failed=[b]))],
            name='new.jsonl')

        out = await mcp_server_module.compare_scans(old, new)
        data = json.loads(out)
        assert data['summary']['fixed'] == 1
        assert data['summary']['new'] == 1
        assert data['summary']['remaining'] == 0

    async def test_missing_old(
            self, mcp_server_module, jsonl_factory):
        new = jsonl_factory([], name='new.jsonl')
        out = await mcp_server_module.compare_scans(
            '/nonexistent/old.jsonl', new)
        data = json.loads(out)
        assert 'error' in data

    async def test_missing_new(
            self, mcp_server_module, jsonl_factory):
        old = jsonl_factory([], name='old.jsonl')
        out = await mcp_server_module.compare_scans(
            old, '/nonexistent/new.jsonl')
        data = json.loads(out)
        assert 'error' in data


# ── manage_scans ───────────────────────────────────────────────

class TestManageScans:
    async def test_list_empty(
            self, mcp_server_module, isolated_registry):
        out = await mcp_server_module.manage_scans(action='list')
        data = json.loads(out)
        assert data['scans'] == []

    async def test_list_with_entries(
            self, mcp_server_module, isolated_registry):
        # Register two scans via the registry directly, then list
        # via the MCP tool.
        import registry
        registry.register_scan(
            'baseline', {'jsonl': '/r/a.jsonl'},
            url='https://example.test/',
            engines=['axe'],
            registry_path=isolated_registry)
        registry.register_scan(
            'post-fix', {'jsonl': '/r/b.jsonl'},
            url='https://example.test/',
            engines=['axe', 'ibm'],
            registry_path=isolated_registry)

        out = await mcp_server_module.manage_scans(action='list')
        data = json.loads(out)
        names = [s['name'] for s in data['scans']]
        assert names == ['baseline', 'post-fix']

    async def test_get_existing(
            self, mcp_server_module, isolated_registry):
        import registry
        registry.register_scan(
            'baseline', {'jsonl': '/r/a.jsonl'},
            url='https://example.test/',
            registry_path=isolated_registry)

        out = await mcp_server_module.manage_scans(
            action='get', name='baseline')
        data = json.loads(out)
        assert data['name'] == 'baseline'
        assert data['url'] == 'https://example.test/'

    async def test_get_missing(
            self, mcp_server_module, isolated_registry):
        out = await mcp_server_module.manage_scans(
            action='get', name='missing')
        data = json.loads(out)
        assert 'error' in data

    async def test_get_requires_name(
            self, mcp_server_module, isolated_registry):
        out = await mcp_server_module.manage_scans(action='get')
        data = json.loads(out)
        assert 'error' in data
        assert 'name' in data['error'].lower()

    async def test_delete_existing(
            self, mcp_server_module, isolated_registry):
        import registry
        registry.register_scan(
            'tmp', {}, registry_path=isolated_registry)

        out = await mcp_server_module.manage_scans(
            action='delete', name='tmp')
        data = json.loads(out)
        assert data.get('ok') is True
        # And it's actually gone
        assert registry.get_scan(
            'tmp', registry_path=isolated_registry) is None

    async def test_delete_missing(
            self, mcp_server_module, isolated_registry):
        out = await mcp_server_module.manage_scans(
            action='delete', name='nope')
        data = json.loads(out)
        assert 'error' in data

    async def test_delete_requires_name(
            self, mcp_server_module, isolated_registry):
        out = await mcp_server_module.manage_scans(action='delete')
        data = json.loads(out)
        assert 'error' in data
        assert 'name' in data['error'].lower()

    async def test_unknown_action(
            self, mcp_server_module, isolated_registry):
        out = await mcp_server_module.manage_scans(action='dance')
        data = json.loads(out)
        assert 'error' in data


# ── _resolve_report ────────────────────────────────────────────

class TestResolveReport:
    def test_direct_path(self, mcp_server_module, tmp_path):
        path = tmp_path / 'r.jsonl'
        path.write_text('')
        assert mcp_server_module._resolve_report(str(path)) == \
            str(path)

    def test_json_to_jsonl_sibling(
            self, mcp_server_module, tmp_path):
        # Pass a .json path; resolver should prefer the .jsonl sibling
        json_path = tmp_path / 'r.json'
        json_path.write_text('{}')
        jsonl_path = tmp_path / 'r.jsonl'
        jsonl_path.write_text('')
        resolved = mcp_server_module._resolve_report(str(json_path))
        assert resolved == str(jsonl_path)

    def test_appends_jsonl_extension(
            self, mcp_server_module, tmp_path):
        target = tmp_path / 'r.jsonl'
        target.write_text('')
        # Pass without extension — should find r.jsonl
        resolved = mcp_server_module._resolve_report(
            str(tmp_path / 'r'))
        assert resolved == str(target)

    def test_registry_lookup(
            self, mcp_server_module, isolated_registry, tmp_path):
        import registry
        jsonl = tmp_path / 'baseline.jsonl'
        jsonl.write_text('')
        registry.register_scan(
            'baseline', {'jsonl': str(jsonl)},
            registry_path=isolated_registry)

        resolved = mcp_server_module._resolve_report('baseline')
        assert resolved == str(jsonl)

    def test_returns_none_for_unknown(
            self, mcp_server_module, isolated_registry):
        assert mcp_server_module._resolve_report(
            '/no/such/path') is None
        assert mcp_server_module._resolve_report('not-a-scan') is None

    def test_registry_json_to_jsonl_fallback(
            self, mcp_server_module, isolated_registry, tmp_path):
        # If the registry entry only has a 'json' key (no 'jsonl'),
        # _resolve_report converts it by replacing the suffix and
        # checking that sibling file exists.
        import registry
        jsonl = tmp_path / 'r.jsonl'
        jsonl.write_text('')
        registry.register_scan(
            'name-only-json',
            {'json': str(tmp_path / 'r.json')},
            registry_path=isolated_registry)
        resolved = mcp_server_module._resolve_report('name-only-json')
        assert resolved == str(jsonl)


# ── scan_page URL validation (no browser needed) ──────────────

class TestScanPageUrlValidation:
    """The MCP scan_page tool docstring promises http(s) URLs only.
    Without enforcement an MCP client could request file:// or
    chrome:// URLs and exfiltrate local content via the returned
    finding selectors / HTML snippets.  The validator returns an
    error JSON instead of launching a browser.
    """

    async def test_rejects_file_scheme(self, mcp_server_module):
        out = await mcp_server_module.scan_page('file:///etc/passwd')
        data = json.loads(out)
        assert 'error' in data
        assert 'http' in data['error'].lower()

    async def test_rejects_chrome_scheme(self, mcp_server_module):
        out = await mcp_server_module.scan_page('chrome://settings')
        data = json.loads(out)
        assert 'error' in data

    async def test_rejects_empty_url(self, mcp_server_module):
        out = await mcp_server_module.scan_page('')
        data = json.loads(out)
        assert 'error' in data

    async def test_rejects_url_without_host(self, mcp_server_module):
        out = await mcp_server_module.scan_page('http:///path')
        data = json.loads(out)
        assert 'error' in data
        assert 'host' in data['error'].lower()

    def test_validator_accepts_http(self, mcp_server_module):
        assert mcp_server_module._validate_scan_url(
            'http://example.test/p') is None

    def test_validator_accepts_https(self, mcp_server_module):
        assert mcp_server_module._validate_scan_url(
            'https://example.test/p') is None


# ── scan_page (MCP wrapper) ───────────────────────────────────

import socket as _socket
import threading as _threading
import time as _time
from functools import partial as _partial
from http.server import (
    ThreadingHTTPServer as _ThreadingHTTPServer,
    SimpleHTTPRequestHandler as _SimpleHTTPRequestHandler,
)
from pathlib import Path as _Path

_PROJECT_ROOT = _Path(__file__).resolve().parent.parent
_FIXTURES = _Path(__file__).resolve().parent / 'fixtures'
_AXE_JS = _PROJECT_ROOT / 'node_modules' / 'axe-core' / 'axe.min.js'


class _QuietHandler(_SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass


@pytest.fixture
def mcp_fixture_site():
    """Local HTTP server serving tests/fixtures/ — used by the
    scan_page MCP tests.  scan_page validates http(s) only, so
    tests can't use file:// URIs anymore.
    """
    handler = _partial(_QuietHandler, directory=str(_FIXTURES))
    server = _ThreadingHTTPServer(('127.0.0.1', 0), handler)
    port = server.server_address[1]
    thread = _threading.Thread(
        target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            try:
                with _socket.create_connection(
                        ('127.0.0.1', port), timeout=0.2):
                    break
            except OSError:
                _time.sleep(0.05)
        yield 'http://127.0.0.1:{}'.format(port)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.browser
@pytest.mark.skipif(
    not _AXE_JS.is_file(),
    reason='node_modules not installed')
class TestScanPage:
    """The MCP scan_page tool is a thin wrapper that flattens
    Scanner.scan_page output into a JSON shape consumers expect.
    Drive it against the local fixtures to lock in the field layout.
    """

    async def test_clean_page_returns_clean_true(
            self, mcp_server_module, mcp_fixture_site):
        out = await mcp_server_module.scan_page(
            mcp_fixture_site + '/clean.html', engines='axe')
        data = json.loads(out)
        assert data['clean'] is True
        assert data['failed'] == 0

    async def test_known_bad_page_returns_findings(
            self, mcp_server_module, mcp_fixture_site):
        out = await mcp_server_module.scan_page(
            mcp_fixture_site + '/known_bad.html', engines='axe')
        data = json.loads(out)
        assert data['clean'] is False
        assert data['failed'] > 0
        # Each finding has the consumer-facing fields populated
        for f in data['findings']:
            assert 'id' in f
            assert 'outcome' in f
            assert 'selector' in f

    async def test_skipped_page_returns_skipped_field(
            self, mcp_server_module, mcp_fixture_site, tmp_path):
        # A tiny HTML payload (under the 100-byte threshold) is
        # rejected up front by Scanner — the wrapper preserves the
        # `skipped` reason in its JSON response.  Drop a tiny file
        # next to the served fixtures so the fixture HTTP server
        # can serve it via http://.
        tiny = _FIXTURES / 'tiny_for_mcp.html'
        tiny.write_text('<html></html>')
        try:
            out = await mcp_server_module.scan_page(
                mcp_fixture_site + '/tiny_for_mcp.html',
                engines='axe')
            data = json.loads(out)
            assert data.get('skipped')
            assert data['clean'] is True
            assert data['failed'] == 0
            assert data['findings'] == []
        finally:
            if tiny.is_file():
                tiny.unlink()
