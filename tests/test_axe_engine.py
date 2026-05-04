"""Tier 1: pure helpers in engines/axe.py.

The actual scan() requires a live browser, but the tag normalizer
is pure and worth testing on its own.
"""

import pytest

from engines.axe import _normalize_axe_tags, AXE_RULES, AXE_OUTCOME_MAP
from engine_mappings import (
    EARL_FAILED, EARL_CANTTELL, EARL_PASSED, EARL_INAPPLICABLE)


class TestNormalizeAxeTags:
    @pytest.mark.parametrize('axe_tag, expected', [
        ('wcag143', 'sc-1.4.3'),
        ('wcag111', 'sc-1.1.1'),
        ('wcag412', 'sc-4.1.2'),
        ('wcag258', 'sc-2.5.8'),
    ])
    def test_wcag_sc_normalized(self, axe_tag, expected):
        assert _normalize_axe_tags([axe_tag]) == [expected]

    @pytest.mark.parametrize('level_tag', [
        'wcag2a', 'wcag2aa', 'wcag2aaa',
        'wcag21a', 'wcag21aa', 'wcag22aa',
    ])
    def test_level_tags_dropped(self, level_tag):
        # Level tags are internal to axe and not useful downstream
        assert _normalize_axe_tags([level_tag]) == []

    def test_other_tags_passed_through(self):
        out = _normalize_axe_tags(['cat.color', 'best-practice', 'ACT'])
        assert out == ['cat.color', 'best-practice', 'ACT']

    def test_mixed_tags(self):
        out = _normalize_axe_tags(
            ['wcag143', 'wcag2aa', 'cat.color', 'best-practice'])
        assert 'sc-1.4.3' in out
        assert 'wcag2aa' not in out
        assert 'cat.color' in out
        assert 'best-practice' in out

    def test_empty_input(self):
        assert _normalize_axe_tags([]) == []


class TestAxeRulesTable:
    def test_axe_rules_nonempty(self):
        assert len(AXE_RULES) > 50

    def test_each_entry_is_three_tuple(self):
        for rule_id, entry in AXE_RULES.items():
            assert len(entry) == 3
            description, scs, is_bp = entry
            assert isinstance(description, str)
            assert isinstance(scs, list)
            assert isinstance(is_bp, bool)

    def test_best_practice_rules_have_no_sc(self):
        for rule_id, (_desc, scs, is_bp) in AXE_RULES.items():
            if is_bp:
                assert scs == [], (
                    'BP rule {} should have no SC mapping but got {}'
                    .format(rule_id, scs))


class TestAxeOutcomeMap:
    def test_categories_map_to_earl(self):
        assert AXE_OUTCOME_MAP['violations'] == EARL_FAILED
        assert AXE_OUTCOME_MAP['incomplete'] == EARL_CANTTELL
        assert AXE_OUTCOME_MAP['passes'] == EARL_PASSED
        assert AXE_OUTCOME_MAP['inapplicable'] == EARL_INAPPLICABLE
