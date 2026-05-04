"""Tier 3: search_findings and page_status against a synthetic JSONL.

Verifies the AND-of-filters semantics, dedup application, and the
sc/url/selector/engine/outcome filter dimensions used by the MCP
server and CLI's `--search` flag.
"""

import pytest

import registry
from engine_mappings import EARL_FAILED, EARL_CANTTELL


@pytest.fixture
def populated_jsonl(jsonl_factory, make_finding, make_page):
    """A JSONL file with three pages and a mix of failures & cantTells.

    Layout:
      /home          — axe color-contrast failed on #hero,
                       ibm text_contrast_sufficient on same #hero (multi-engine)
      /admin/users   — axe aria-allowed-attr cantTell on #nav
      /docs/intro    — axe color-contrast failed on .button (different page)
    """
    home = make_page(
        'https://example.test/home',
        failed=[
            make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3', 'wcag2aa'],
                         selector='#hero',
                         html='<div id="hero">Banner</div>'),
            make_finding('text_contrast_sufficient', EARL_FAILED,
                         engine='ibm',
                         tags=['sc-1.4.3'],
                         selector='#hero',
                         html='<div id="hero">Banner</div>'),
        ])
    admin = make_page(
        'https://example.test/admin/users',
        cant_tell=[
            # Single primary tag so dedup_page produces one entry.
            make_finding('aria-allowed-attr', EARL_CANTTELL,
                         engine='axe',
                         tags=['aria-valid-attrs'],
                         selector='#nav',
                         html='<nav id="nav"></nav>'),
        ])
    docs = make_page(
        'https://example.test/docs/intro',
        failed=[
            make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'],
                         selector='.button',
                         html='<a class="button"></a>'),
        ])
    return jsonl_factory([
        ('https://example.test/home', home),
        ('https://example.test/admin/users', admin),
        ('https://example.test/docs/intro', docs),
    ])


# ── search_findings ────────────────────────────────────────────

class TestSearchFindings:
    def test_no_filters_returns_all_failed_and_cant_tell(
            self, populated_jsonl):
        # 2 failed (home dedups two engines into one + docs) + 1 cantTell
        results = registry.search_findings(populated_jsonl)
        outcomes = [r['outcome'] for r in results]
        assert outcomes.count(EARL_FAILED) == 2
        assert outcomes.count(EARL_CANTTELL) == 1

    def test_filter_by_sc_number(self, populated_jsonl):
        results = registry.search_findings(populated_jsonl, sc='1.4.3')
        assert len(results) == 2
        for r in results:
            assert 'sc-1.4.3' in r['tags']

    def test_filter_by_sc_with_prefix(self, populated_jsonl):
        # 'sc-1.4.3' should be auto-stripped to '1.4.3'
        results = registry.search_findings(
            populated_jsonl, sc='sc-1.4.3')
        assert len(results) == 2

    def test_filter_by_sc_slug(self, populated_jsonl):
        # slug → SC resolution
        results = registry.search_findings(
            populated_jsonl, sc='contrast-minimum')
        assert len(results) == 2

    def test_filter_by_url_pattern(self, populated_jsonl):
        results = registry.search_findings(
            populated_jsonl, url_pattern='/admin/*')
        assert len(results) == 1
        assert '/admin/' in results[0]['url']

    def test_filter_by_selector_pattern(self, populated_jsonl):
        results = registry.search_findings(
            populated_jsonl, selector_pattern='#nav*')
        assert len(results) == 1
        assert results[0]['selector'] == '#nav'

    def test_filter_by_outcome_failed_only(self, populated_jsonl):
        results = registry.search_findings(
            populated_jsonl, outcome=EARL_FAILED)
        assert len(results) == 2
        assert all(r['outcome'] == EARL_FAILED for r in results)

    def test_filter_by_outcome_cant_tell_only(self, populated_jsonl):
        results = registry.search_findings(
            populated_jsonl, outcome=EARL_CANTTELL)
        assert len(results) == 1
        assert results[0]['outcome'] == EARL_CANTTELL

    def test_filter_by_engine(self, populated_jsonl):
        # axe contributes to all 3 findings (home merge has axe);
        # ibm only contributes to the home merge.
        axe_results = registry.search_findings(
            populated_jsonl, engine='axe')
        ibm_results = registry.search_findings(
            populated_jsonl, engine='ibm')
        assert len(axe_results) == 3
        assert len(ibm_results) == 1

    def test_combined_filters_are_anded(self, populated_jsonl):
        # SC=1.4.3 AND url=/docs/* should leave only the docs/intro one
        results = registry.search_findings(
            populated_jsonl, sc='1.4.3', url_pattern='/docs/*')
        assert len(results) == 1
        assert '/docs/' in results[0]['url']

    def test_no_match(self, populated_jsonl):
        results = registry.search_findings(
            populated_jsonl, sc='9.9.9')
        assert results == []

    def test_dedup_off_yields_more_findings_for_multi_engine(
            self, populated_jsonl):
        # With dedup off, the home page's two engines stay separate
        no_dedup = registry.search_findings(
            populated_jsonl, sc='1.4.3', dedup=False)
        with_dedup = registry.search_findings(
            populated_jsonl, sc='1.4.3', dedup=True)
        assert len(no_dedup) > len(with_dedup)

    def test_engine_count_reflects_merge(self, populated_jsonl):
        # Home page's contrast finding is multi-engine after dedup
        home_findings = [
            r for r in registry.search_findings(populated_jsonl,
                                                sc='1.4.3')
            if '/home' in r['url']]
        assert len(home_findings) == 1
        assert home_findings[0]['engine_count'] == 2


# ── page_status ────────────────────────────────────────────────

class TestPageStatus:
    def test_clean_page_when_only_cant_tell(self, populated_jsonl):
        # /admin/users has only a cantTell, no failed → clean=True
        st = registry.page_status(
            populated_jsonl, 'https://example.test/admin/users')
        assert st['found'] is True
        assert st['clean'] is True
        assert st['failed'] == 0
        assert st['cantTell'] == 1

    def test_failing_page(self, populated_jsonl):
        st = registry.page_status(
            populated_jsonl, 'https://example.test/home')
        assert st['found'] is True
        assert st['clean'] is False
        assert st['failed'] == 1  # deduped from 2 engines
        assert st['sc_breakdown']['1.4.3']['failed'] == 1

    def test_engine_agreement(self, populated_jsonl):
        st = registry.page_status(
            populated_jsonl, 'https://example.test/home')
        # The single failed finding has engine_count=2
        assert st['engine_agreement'].get(2, 0) == 1

    def test_url_not_in_report(self, populated_jsonl):
        st = registry.page_status(
            populated_jsonl, 'https://example.test/missing')
        assert st['found'] is False

    def test_path_match_when_exact_url_missing(self, populated_jsonl):
        # Same path, different base → page_status falls back to
        # path-only match
        st = registry.page_status(
            populated_jsonl, 'https://other.test/home')
        assert st['found'] is True
        # Returned url is the actual one in the report
        assert st['url'] == 'https://example.test/home'

    def test_findings_list_populated(self, populated_jsonl):
        st = registry.page_status(
            populated_jsonl, 'https://example.test/home')
        assert len(st['findings']) == 1
        assert st['findings'][0]['selector'] == '#hero'
