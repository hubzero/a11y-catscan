"""Tier 3: explicit coverage for multi-primary-tag splitting.

scanner.dedup_page documents that a single finding carrying multiple
primary tags (sc-*, aria-*, bp-*) becomes multiple deduped entries —
one per primary tag — so the same issue can be filtered both by WCAG
and by ARIA category. These tests pin that behavior.
"""

import pytest

from scanner import dedup_page
from engine_mappings import EARL_FAILED


def test_finding_with_sc_and_aria_splits_into_two(
        make_finding, make_page):
    finding = make_finding(
        'aria-valid-attr', EARL_FAILED, engine='axe',
        tags=['sc-4.1.2', 'aria-valid-attrs', 'wcag2a'],
        selector='#x')
    page = make_page('u', failed=[finding])
    result = dedup_page(page)

    assert len(result[EARL_FAILED]) == 2
    ids = sorted(item['id'] for item in result[EARL_FAILED])
    assert ids == ['aria-valid-attrs', 'sc-4.1.2']

    # Both deduped entries reference the same single source engine
    for item in result[EARL_FAILED]:
        assert item['engine_count'] == 1
        assert 'axe' in item['engines']


def test_finding_with_only_bp_tag_uses_bp_as_primary(
        make_finding, make_page):
    finding = make_finding(
        'region', EARL_FAILED, engine='axe',
        tags=['bp-landmarks', 'best-practice'], selector='#main')
    page = make_page('u', failed=[finding])
    result = dedup_page(page)

    assert len(result[EARL_FAILED]) == 1
    assert result[EARL_FAILED][0]['id'] == 'bp-landmarks'


def test_finding_with_no_primary_tag_falls_back_to_id(
        make_finding, make_page):
    # No sc-/aria-/bp- tag: dedup falls back to the engine's rule id
    finding = make_finding('weird-rule', EARL_FAILED, engine='axe',
                           tags=['cat.unknown'], selector='#x')
    page = make_page('u', failed=[finding])
    result = dedup_page(page)

    assert len(result[EARL_FAILED]) == 1
    assert result[EARL_FAILED][0]['id'] == 'weird-rule'
