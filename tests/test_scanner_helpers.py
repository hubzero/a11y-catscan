"""Tier 1: pure helpers in scanner.py.

count_nodes and dedup_page operate on plain dicts — no browser, no IO.
WCAG_LEVELS is a static lookup table that the engines depend on.
"""

import pytest

from scanner import count_nodes, dedup_page, WCAG_LEVELS, DEFAULT_LEVEL
from engine_mappings import (
    EARL_FAILED, EARL_CANTTELL, EARL_PASSED, EARL_INAPPLICABLE)


# ── count_nodes ────────────────────────────────────────────────

class TestCountNodes:
    def test_empty_list(self):
        assert count_nodes([]) == 0

    def test_single_finding_one_node(self):
        items = [{'nodes': [{'target': ['a']}]}]
        assert count_nodes(items) == 1

    def test_single_finding_multi_node(self):
        items = [{'nodes': [{'target': ['a']},
                            {'target': ['b']},
                            {'target': ['c']}]}]
        assert count_nodes(items) == 3

    def test_many_findings(self):
        items = [
            {'nodes': [{}, {}]},
            {'nodes': [{}]},
            {'nodes': []},
            {'nodes': [{}, {}, {}, {}]},
        ]
        assert count_nodes(items) == 7

    def test_finding_without_nodes_key(self):
        # Defensive: missing nodes treated as zero
        assert count_nodes([{'id': 'x'}]) == 0


# ── WCAG_LEVELS ────────────────────────────────────────────────

class TestWcagLevels:
    def test_default_level_in_table(self):
        assert DEFAULT_LEVEL in WCAG_LEVELS

    @pytest.mark.parametrize('level', [
        'wcag2a', 'wcag2aa', 'wcag2aaa',
        'wcag21a', 'wcag21aa', 'wcag21aaa',
        'wcag22a', 'wcag22aa', 'wcag22aaa',
        'best',
    ])
    def test_all_levels_have_tags_and_label(self, level):
        info = WCAG_LEVELS[level]
        assert 'tags' in info
        assert 'label' in info
        assert isinstance(info['tags'], list)
        assert info['tags']
        assert info['label']

    def test_higher_levels_include_lower_tags(self):
        # AA must include all A tags; AAA all AA tags
        a = set(WCAG_LEVELS['wcag21a']['tags'])
        aa = set(WCAG_LEVELS['wcag21aa']['tags'])
        aaa = set(WCAG_LEVELS['wcag21aaa']['tags'])
        assert a.issubset(aa)
        assert aa.issubset(aaa)

    def test_best_includes_best_practice_tag(self):
        assert 'best-practice' in WCAG_LEVELS['best']['tags']


# ── dedup_page ─────────────────────────────────────────────────

class TestDedupPage:
    def test_empty_page(self, make_page):
        page = make_page('https://example.test/')
        result = dedup_page(page)
        assert result['url'] == 'https://example.test/'
        assert result[EARL_FAILED] == []
        assert result[EARL_CANTTELL] == []

    def test_single_engine_passthrough(self, make_finding, make_page):
        finding = make_finding('color-contrast', EARL_FAILED,
                               engine='axe',
                               tags=['sc-1.4.3', 'wcag2aa'],
                               selector='#a')
        page = make_page('https://example.test/', failed=[finding])
        result = dedup_page(page)
        assert len(result[EARL_FAILED]) == 1
        item = result[EARL_FAILED][0]
        assert item['engine_count'] == 1
        assert 'axe' in item['engines']
        assert item['engines']['axe']['rule'] == 'color-contrast'
        # primary_tag becomes the id after dedup
        assert item['id'] == 'sc-1.4.3'

    def test_two_engines_same_selector_and_sc_merge(
            self, make_finding, make_page):
        # axe and IBM both report SC 1.4.3 on the same selector — should
        # collapse into one finding with engine_count=2.
        a = make_finding('color-contrast', EARL_FAILED,
                         engine='axe', tags=['sc-1.4.3'],
                         selector='#a', impact='serious')
        b = make_finding('text_contrast_sufficient', EARL_FAILED,
                         engine='ibm', tags=['sc-1.4.3'],
                         selector='#a', impact='critical')
        page = make_page('u', failed=[a, b])
        result = dedup_page(page)

        assert len(result[EARL_FAILED]) == 1
        item = result[EARL_FAILED][0]
        assert item['engine_count'] == 2
        assert {'axe', 'ibm'} == set(item['engines'].keys())
        # Highest impact wins on merge
        assert item['impact'] == 'critical'

    def test_different_selectors_stay_separate(
            self, make_finding, make_page):
        a = make_finding('color-contrast', EARL_FAILED,
                         engine='axe', tags=['sc-1.4.3'],
                         selector='#a')
        b = make_finding('color-contrast', EARL_FAILED,
                         engine='axe', tags=['sc-1.4.3'],
                         selector='#b')
        page = make_page('u', failed=[a, b])
        result = dedup_page(page)
        assert len(result[EARL_FAILED]) == 2
        selectors = {
            item['nodes'][0]['target'][0] for item in result[EARL_FAILED]}
        assert selectors == {'#a', '#b'}

    def test_failed_and_cant_tell_stay_separate(
            self, make_finding, make_page):
        # Same selector + tag but different outcomes — keep separate.
        a = make_finding('r', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#a')
        b = make_finding('r', EARL_CANTTELL, engine='ibm',
                         tags=['sc-1.4.3'], selector='#a')
        page = make_page('u', failed=[a], cant_tell=[b])
        result = dedup_page(page)
        assert len(result[EARL_FAILED]) == 1
        assert len(result[EARL_CANTTELL]) == 1

    def test_aria_and_bp_tags_become_primary(
            self, make_finding, make_page):
        # Findings without sc- tags but with aria-* primary tag
        f = make_finding('aria-valid-attr', EARL_FAILED,
                         engine='axe',
                         tags=['aria-valid-attrs'], selector='#x')
        page = make_page('u', failed=[f])
        result = dedup_page(page)
        assert result[EARL_FAILED][0]['id'] == 'aria-valid-attrs'

    def test_passed_and_inapplicable_pass_through(
            self, make_finding, make_page):
        passed = make_finding('p', EARL_PASSED, tags=['sc-1.1.1'])
        inapp = make_finding('i', EARL_INAPPLICABLE, tags=['sc-1.1.1'])
        page = make_page('u', passed=[passed], inapplicable=[inapp])
        result = dedup_page(page)
        # passed/inapplicable copied verbatim — not deduped
        assert result[EARL_PASSED] == [passed]
        assert result[EARL_INAPPLICABLE] == [inapp]

    def test_message_extracted_from_any_check(
            self, make_finding, make_page):
        f = make_finding('color-contrast', EARL_FAILED,
                         engine='axe', tags=['sc-1.4.3'],
                         message='not enough contrast')
        page = make_page('u', failed=[f])
        result = dedup_page(page)
        item = result[EARL_FAILED][0]
        assert item['nodes'][0]['any'][0]['message'] == \
            'not enough contrast'

    def test_preserves_url_and_metadata(self, make_finding, make_page):
        page = make_page('https://example.test/x')
        page['http_status'] = 200
        page['timestamp'] = '2026-04-30T00:00:00'
        result = dedup_page(page)
        assert result['url'] == 'https://example.test/x'
        assert result['http_status'] == 200
        assert result['timestamp'] == '2026-04-30T00:00:00'

    def test_multi_tag_finding_creates_one_entry_per_primary_tag(
            self, make_finding, make_page):
        # A finding with both an SC and an ARIA tag becomes two
        # deduped entries (per dedup_page's primary_tags logic).
        f = make_finding('aria-valid-attr', EARL_FAILED, engine='axe',
                         tags=['sc-4.1.2', 'aria-valid-attrs'],
                         selector='#x')
        page = make_page('u', failed=[f])
        result = dedup_page(page)
        ids = sorted(item['id'] for item in result[EARL_FAILED])
        assert ids == ['aria-valid-attrs', 'sc-4.1.2']
