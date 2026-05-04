"""Tier 1: pure functions in engine_mappings.

Covers the rule/SC lookup tables that every engine relies on for
normalized tagging. These have no I/O and no async — fast, hermetic.
"""

import pytest

from engine_mappings import (
    EARL_FAILED, EARL_CANTTELL, EARL_PASSED, EARL_INAPPLICABLE,
    EARL_TO_DISPLAY,
    SC_META, SC_SLUGS, SC_NUM_TO_SLUG,
    sc_level, sc_name, sc_slug, resolve_sc,
    ibm_rule_to_sc, ibm_rule_to_tags, htmlcs_code_to_sc,
    aria_category, bp_category,
    AXE_BP_MAP, IBM_BP_MAP, AXE_ARIA_MAP, IBM_ARIA_MAP,
    BP_CATEGORIES, ARIA_CATEGORIES,
)


# ── EARL constants ─────────────────────────────────────────────

class TestEarlConstants:
    def test_earl_values_match_w3c(self):
        assert EARL_FAILED == 'failed'
        assert EARL_CANTTELL == 'cantTell'
        assert EARL_PASSED == 'passed'
        assert EARL_INAPPLICABLE == 'inapplicable'

    def test_display_map_covers_all_outcomes(self):
        for outcome in (EARL_FAILED, EARL_CANTTELL,
                        EARL_PASSED, EARL_INAPPLICABLE):
            assert outcome in EARL_TO_DISPLAY


# ── SC metadata ────────────────────────────────────────────────

class TestScMeta:
    @pytest.mark.parametrize('sc, expected_level, expected_version', [
        ('1.4.3', 'AA', '2.0'),
        ('1.1.1', 'A', '2.0'),
        ('2.5.8', 'AA', '2.2'),
        ('1.3.4', 'AA', '2.1'),
        ('2.4.13', 'AAA', '2.2'),
    ])
    def test_sc_level_known(self, sc, expected_level, expected_version):
        assert sc_level(sc) == (expected_level, expected_version)

    def test_sc_level_unknown_returns_question_marks(self):
        assert sc_level('99.99.99') == ('?', '?')
        assert sc_level('') == ('?', '?')

    @pytest.mark.parametrize('sc, expected', [
        ('1.4.3', 'Contrast (Minimum)'),
        ('1.1.1', 'Non-text Content'),
        ('4.1.2', 'Name, Role, Value'),
    ])
    def test_sc_name_known(self, sc, expected):
        assert sc_name(sc) == expected

    def test_sc_name_unknown_returns_empty(self):
        assert sc_name('99.99.99') == ''

    def test_sc_meta_keys_match_versions(self):
        # Every SC should declare a version of 2.0, 2.1, or 2.2
        valid_versions = {'2.0', '2.1', '2.2'}
        for sc, (level, version, name) in SC_META.items():
            assert version in valid_versions, (
                'SC {} has invalid version {}'.format(sc, version))
            assert level in {'A', 'AA', 'AAA'}, (
                'SC {} has invalid level {}'.format(sc, level))
            assert name, 'SC {} missing name'.format(sc)


# ── Slug ↔ SC number conversion ────────────────────────────────

class TestSlugConversion:
    def test_sc_slug_known(self):
        assert sc_slug('1.4.3') == 'contrast-minimum'
        assert sc_slug('2.1.1') == 'keyboard'
        assert sc_slug('4.1.2') == 'name-role-value'

    def test_sc_slug_unknown(self):
        assert sc_slug('99.99.99') == ''

    def test_slugs_and_num_to_slug_are_inverses(self):
        for slug, num in SC_SLUGS.items():
            assert SC_NUM_TO_SLUG[num] == slug


# ── resolve_sc ─────────────────────────────────────────────────

class TestResolveSc:
    def test_direct_number(self):
        assert resolve_sc('1.4.3') == '1.4.3'

    def test_exact_slug(self):
        assert resolve_sc('contrast-minimum') == '1.4.3'
        assert resolve_sc('keyboard') == '2.1.1'

    def test_unique_prefix_match(self):
        # 'reflow' is a unique slug — no other SC slug starts with 'ref'
        assert resolve_sc('ref') == '1.4.10'
        # 'parsing' is unique — no other SC slug starts with 'par'
        assert resolve_sc('par') == '4.1.1'

    def test_ambiguous_prefix_returns_none(self):
        # 'contrast' matches both 1.4.3 and 1.4.6 — ambiguous
        assert resolve_sc('contrast') is None

    def test_unknown_returns_none(self):
        assert resolve_sc('not-a-real-thing') is None

    def test_strips_whitespace(self):
        assert resolve_sc('  1.4.3  ') == '1.4.3'


# ── IBM rule mapping ───────────────────────────────────────────

class TestIbmRuleMapping:
    def test_known_rule_to_sc(self):
        assert ibm_rule_to_sc('html_lang_exists') == ['3.1.1']
        assert ibm_rule_to_sc('text_contrast_sufficient') == ['1.4.3']

    def test_multi_sc_rule(self):
        result = ibm_rule_to_sc('a_text_purpose')
        assert '2.4.4' in result
        assert '4.1.2' in result

    def test_unknown_rule_returns_empty(self):
        assert ibm_rule_to_sc('not_a_real_rule') == []

    def test_to_tags_prepends_sc(self):
        tags = ibm_rule_to_tags('html_lang_exists')
        assert tags == ['sc-3.1.1']

    def test_to_tags_handles_unknown(self):
        assert ibm_rule_to_tags('bogus') == []


# ── HTML_CodeSniffer code parsing ──────────────────────────────

class TestHtmlcsCodeToSc:
    @pytest.mark.parametrize('code, expected', [
        ('WCAG2AA.Principle1.Guideline1_4.1_4_3.G18', '1.4.3'),
        ('WCAG2AA.Principle2.Guideline2_4.2_4_2.H25', '2.4.2'),
        ('WCAG2AAA.Principle1.Guideline1_4.1_4_6', '1.4.6'),
    ])
    def test_extracts_sc(self, code, expected):
        assert htmlcs_code_to_sc(code) == expected

    def test_no_match_returns_none(self):
        assert htmlcs_code_to_sc('NoSCInThisCode') is None
        assert htmlcs_code_to_sc('') is None


# ── ARIA / best-practice categories ────────────────────────────

class TestAriaCategory:
    def test_axe_aria_rule(self):
        assert aria_category('axe', 'aria-valid-attr') == 'valid-attrs'
        assert aria_category('axe', 'aria-required-children') == \
            'required-structure'
        assert aria_category('axe', 'aria-hidden-body') == 'hidden'

    def test_ibm_aria_rule(self):
        assert aria_category('ibm', 'aria_role_valid') == 'valid-roles'
        assert aria_category('ibm', 'aria_attribute_required') == \
            'required-states'

    def test_alfa_aria_rule(self):
        assert aria_category('alfa', 'sia-r18') == 'valid-attrs'

    def test_unknown_engine(self):
        assert aria_category('htmlcs', 'anything') is None
        assert aria_category('unknown', 'sia-r18') is None

    def test_unknown_rule(self):
        assert aria_category('axe', 'not-an-aria-rule') is None

    def test_categories_in_known_taxonomy(self):
        valid = set(ARIA_CATEGORIES)
        for rule, cat in AXE_ARIA_MAP.items():
            assert cat in valid, '{} -> {}'.format(rule, cat)
        for rule, cat in IBM_ARIA_MAP.items():
            assert cat in valid


class TestBpCategory:
    def test_axe_bp_rule(self):
        assert bp_category('axe', 'region') == 'landmarks'
        assert bp_category('axe', 'heading-order') == 'headings'
        assert bp_category('axe', 'tabindex') == 'keyboard'

    def test_ibm_bp_rule(self):
        assert bp_category('ibm', 'aria_content_in_landmark') == \
            'landmarks'

    def test_unknown_returns_none(self):
        assert bp_category('axe', 'color-contrast') is None  # WCAG-mapped
        assert bp_category('htmlcs', 'anything') is None

    def test_categories_in_known_taxonomy(self):
        valid = set(BP_CATEGORIES)
        for rule, cat in AXE_BP_MAP.items():
            assert cat in valid, '{} -> {}'.format(rule, cat)
        for rule, cat in IBM_BP_MAP.items():
            assert cat in valid
