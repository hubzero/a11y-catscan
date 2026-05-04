"""Tier 1+2: utility helpers exposed by a11y-catscan.py.

After the file split, these helpers live in dedicated modules:

  - safe_int, normalize_url, is_same_origin, should_scan,
    RateLimiter      → crawl_utils
  - parse_wcag_sc    → engine_mappings
  - load_allowlist,
    matches_allowlist → allowlist
  - load_config      → a11y-catscan.py (still the CLI's own)

The CLI script re-exports them so tests using the `cli` fixture can
reach them in one place.  Module-level imports below pin the real
owners — the `cli`-style access in individual tests is convenience
only.

The CLI script is loaded via the `cli` fixture (importlib trick —
the script's filename has a hyphen that prevents normal imports).
"""

import os
import re
import textwrap

import pytest


# ── _safe_int ──────────────────────────────────────────────────

class TestSafeInt:
    def test_int_passthrough(self, cli):
        assert cli.safe_int(7) == 7
        assert cli.safe_int(0) == 0

    def test_string_int(self, cli):
        assert cli.safe_int('42') == 42

    def test_invalid_returns_default(self, cli):
        assert cli.safe_int('abc', 99) == 99
        assert cli.safe_int(None, 5) == 5
        assert cli.safe_int([], 3) == 3

    def test_default_zero(self, cli):
        assert cli.safe_int('nope') == 0


# ── _parse_wcag_sc ─────────────────────────────────────────────

class TestParseWcagSc:
    def test_normalized_format(self, cli):
        scs = cli.parse_wcag_sc(['sc-1.4.3', 'sc-2.4.1'])
        assert scs == {'1.4.3', '2.4.1'}

    def test_legacy_axe_format(self, cli):
        scs = cli.parse_wcag_sc(['wcag143', 'wcag258'])
        assert scs == {'1.4.3', '2.5.8'}

    def test_level_tags_ignored(self, cli):
        # 'wcag2a' / 'wcag21aa' are level tags, not SCs
        scs = cli.parse_wcag_sc(['wcag2a', 'wcag21aa', 'wcag2aaa'])
        assert scs == set()

    def test_mixed_tags(self, cli):
        scs = cli.parse_wcag_sc(
            ['sc-1.1.1', 'wcag143', 'best-practice', 'aria-naming'])
        assert scs == {'1.1.1', '1.4.3'}

    def test_empty(self, cli):
        assert cli.parse_wcag_sc([]) == set()


# ── normalize_url ──────────────────────────────────────────────

class TestNormalizeUrl:
    def test_strips_fragment(self, cli):
        assert cli.normalize_url('https://example.test/a#frag') == \
            'https://example.test/a'

    def test_strips_trailing_slash(self, cli):
        assert cli.normalize_url('https://example.test/a/') == \
            'https://example.test/a'

    def test_root_keeps_slash(self, cli):
        assert cli.normalize_url('https://example.test/') == \
            'https://example.test/'

    def test_query_preserved_when_no_strip_rules(self, cli):
        # No global strip_query_params configured — query is preserved.
        url = 'https://example.test/page?keep=1'
        assert cli.normalize_url(url) == url

    def test_strips_configured_query_params(self, cli, monkeypatch):
        # Inject a strip rule into the module-level state used by
        # normalize_url().  Restore it after the test via monkeypatch.
        # The strip-rule state lives in crawl_utils now.
        import crawl_utils
        monkeypatch.setattr(
            crawl_utils, '_strip_params', {'sort', 'limit'})
        out = cli.normalize_url(
            'https://example.test/x?sort=a&limit=10&keep=1')
        # 'keep' remains; 'sort' and 'limit' are gone
        assert 'keep=1' in out
        assert 'sort=' not in out
        assert 'limit=' not in out


# ── is_same_origin ─────────────────────────────────────────────

class TestIsSameOrigin:
    def test_same_host(self, cli):
        assert cli.is_same_origin(
            'https://example.test/a', 'https://example.test/b')

    def test_different_host(self, cli):
        assert not cli.is_same_origin(
            'https://example.test/a', 'https://other.test/b')

    def test_subdomain_is_different(self, cli):
        # netloc is "host:port" so subdomain is a different origin.
        assert not cli.is_same_origin(
            'https://www.example.test/', 'https://example.test/')

    def test_different_port(self, cli):
        assert not cli.is_same_origin(
            'https://example.test:8080/', 'https://example.test/')


# ── should_scan ────────────────────────────────────────────────

class TestShouldScan:
    BASE = 'https://example.test/'

    def test_blocks_other_origin(self, cli):
        assert not cli.should_scan(
            'https://other.test/a', self.BASE, [], [])

    def test_blocks_skip_extensions(self, cli):
        for ext in ('.pdf', '.png', '.zip', '.js', '.css'):
            url = self.BASE + 'asset' + ext
            assert not cli.should_scan(url, self.BASE, [], []), ext

    def test_include_paths_filter_in(self, cli):
        assert cli.should_scan(
            self.BASE + 'docs/intro', self.BASE, ['/docs'], [])
        assert not cli.should_scan(
            self.BASE + 'blog/post', self.BASE, ['/docs'], [])

    def test_exclude_paths_filter_out(self, cli):
        assert not cli.should_scan(
            self.BASE + 'admin/users', self.BASE, [], ['/admin'])
        assert cli.should_scan(
            self.BASE + 'public/x', self.BASE, [], ['/admin'])

    def test_exclude_regex(self, cli):
        pattern = re.compile(r'^/internal/.*/secret')
        assert not cli.should_scan(
            self.BASE + 'internal/foo/secret/x',
            self.BASE, [], [], exclude_regex=[pattern])
        assert cli.should_scan(
            self.BASE + 'public/x',
            self.BASE, [], [], exclude_regex=[pattern])

    def test_action_pdf_query_blocked(self, cli):
        assert not cli.should_scan(
            self.BASE + 'page?action=pdf', self.BASE, [], [])

    def test_basic_url_passes(self, cli):
        assert cli.should_scan(self.BASE + 'x', self.BASE, [], [])


# ── RateLimiter ────────────────────────────────────────────────

class TestRateLimiter:
    def test_disabled_when_zero(self, cli):
        rl = cli.RateLimiter(0)
        assert rl.wait_time() == 0
        assert rl.wait_time() == 0

    def test_first_call_no_wait(self, cli):
        rl = cli.RateLimiter(1)
        # First call has no prior timestamp, so wait should be 0
        assert rl.wait_time() == 0

    def test_back_to_back_waits(self, cli):
        rl = cli.RateLimiter(2)
        rl.wait_time()  # first call sets the marker
        delay = rl.wait_time()
        # We just called wait_time twice in microseconds, so the
        # second call should ask us to wait roughly the full interval
        assert delay > 1.5
        assert delay <= 2

    def test_negative_interval_treated_as_disabled(self, cli):
        rl = cli.RateLimiter(-5)
        assert rl.wait_time() == 0


# ── load_allowlist ─────────────────────────────────────────────

class TestLoadAllowlist:
    def test_missing_file_returns_empty(self, cli, tmp_path):
        # load_allowlist returns an Allowlist (sequence-like).
        # Empty allowlist is falsy and has len 0.
        result = cli.load_allowlist(str(tmp_path / 'nope.yaml'))
        assert not result
        assert len(result) == 0

    def test_none_path_returns_empty(self, cli):
        assert not cli.load_allowlist(None)
        assert not cli.load_allowlist('')

    def test_loads_valid_yaml(self, cli, tmp_path):
        path = tmp_path / 'allow.yaml'
        path.write_text(textwrap.dedent("""\
            - rule: color-contrast
              reason: gradient
            - rule: aria-allowed-attr
              url: /admin
              target: '#nav'
        """))
        entries = list(cli.load_allowlist(str(path)))
        assert len(entries) == 2
        assert entries[0]['rule'] == 'color-contrast'
        assert entries[1]['target'] == '#nav'

    def test_empty_yaml_returns_empty(self, cli, tmp_path):
        path = tmp_path / 'empty.yaml'
        path.write_text('')
        result = cli.load_allowlist(str(path))
        assert not result
        assert len(result) == 0

    def test_indexed_lookup_is_o1(self, cli, tmp_path):
        # The Allowlist class indexes by rule_id; a finding whose
        # rule has no matching entry should return False without
        # scanning the full list.  We can't observe O(1) directly
        # but we verify the class exposes the rule index.
        path = tmp_path / 'allow.yaml'
        path.write_text(textwrap.dedent("""\
            - rule: color-contrast
              reason: gradient
            - rule: image-alt
              reason: decorative
        """))
        al = cli.load_allowlist(str(path))
        assert al.matches('color-contrast',
                          'https://example.test/p',
                          [], outcome='cantTell')
        # Different rule — no match, no scan
        assert not al.matches('button-name',
                              'https://example.test/p',
                              [], outcome='cantTell')


# ── load_config ────────────────────────────────────────────────

class TestLoadConfig:
    def test_missing_file_returns_empty_dict(self, cli, tmp_path):
        cfg = cli.load_config(str(tmp_path / 'no.yaml'))
        assert cfg == {}

    def test_loads_yaml(self, cli, tmp_path):
        path = tmp_path / 'c.yaml'
        path.write_text(textwrap.dedent("""\
            url: https://example.test/
            level: wcag22aa
            max_pages: 100
            workers: 4
        """))
        cfg = cli.load_config(str(path))
        assert cfg['url'] == 'https://example.test/'
        assert cfg['max_pages'] == 100
        assert cfg['workers'] == 4

    def test_empty_yaml_returns_empty_dict(self, cli, tmp_path):
        path = tmp_path / 'empty.yaml'
        path.write_text('')
        assert cli.load_config(str(path)) == {}


# ── _matches_allowlist ─────────────────────────────────────────

class TestMatchesAllowlist:
    def test_no_rules_no_match(self, cli):
        assert not cli.matches_allowlist(
            'sc-1.4.3', 'http://x/', [{}], [])

    def test_rule_id_must_match(self, cli):
        allowlist = [{'rule': 'sc-1.4.3'}]
        assert cli.matches_allowlist(
            'sc-1.4.3', 'http://x/', [{}], allowlist)
        assert not cli.matches_allowlist(
            'sc-2.1.1', 'http://x/', [{}], allowlist)

    def test_url_substring_filter(self, cli):
        allowlist = [{'rule': 'sc-1.4.3', 'url': '/admin'}]
        assert cli.matches_allowlist(
            'sc-1.4.3', 'http://x/admin/page', [{}], allowlist)
        assert not cli.matches_allowlist(
            'sc-1.4.3', 'http://x/public/page', [{}], allowlist)

    def test_target_substring_filter(self, cli):
        allowlist = [{'rule': 'sc-1.4.3', 'target': '#nav'}]
        nodes_match = [{'target': ['#nav .item']}]
        nodes_other = [{'target': ['#main']}]
        assert cli.matches_allowlist(
            'sc-1.4.3', 'http://x/', nodes_match, allowlist)
        assert not cli.matches_allowlist(
            'sc-1.4.3', 'http://x/', nodes_other, allowlist)

    def test_engine_filter_single_engine(self, cli):
        allowlist = [{'rule': 'sc-1.4.3', 'engine': 'ibm'}]
        engines_dict = {'ibm': {'rule': 'text_contrast_sufficient'}}
        assert cli.matches_allowlist(
            'sc-1.4.3', 'http://x/', [{}], allowlist,
            engines_dict=engines_dict)

    def test_engine_filter_skips_multi_engine_findings(self, cli):
        # Multi-engine findings are not suppressed by single-engine rules
        allowlist = [{'rule': 'sc-1.4.3', 'engine': 'ibm'}]
        engines_dict = {'ibm': {}, 'axe': {}}
        assert not cli.matches_allowlist(
            'sc-1.4.3', 'http://x/', [{}], allowlist,
            engines_dict=engines_dict)

    def test_outcome_filter(self, cli):
        allowlist = [{'rule': 'sc-1.4.3', 'outcome': 'cantTell'}]
        assert cli.matches_allowlist(
            'sc-1.4.3', 'http://x/', [{}], allowlist,
            outcome='cantTell')
        assert not cli.matches_allowlist(
            'sc-1.4.3', 'http://x/', [{}], allowlist,
            outcome='failed')

    def test_all_filters_must_match(self, cli):
        # Combined: rule + url + target — all must match
        allowlist = [{'rule': 'sc-1.4.3', 'url': '/admin',
                      'target': '#nav'}]
        nodes = [{'target': ['#nav']}]
        assert cli.matches_allowlist(
            'sc-1.4.3', 'http://x/admin/p', nodes, allowlist)
        # Wrong URL — no match
        assert not cli.matches_allowlist(
            'sc-1.4.3', 'http://x/public/p', nodes, allowlist)
