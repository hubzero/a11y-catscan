"""Tier 3: registry.diff_scans against synthetic baselines.

Verifies the fixed/new/remaining bucketing and per-SC delta accounting
that the CLI's --diff and the MCP server expose.
"""

import pytest

import registry
from engine_mappings import EARL_FAILED


def _scan(jsonl_factory, make_finding, make_page, name, findings):
    """Build a single-page jsonl with the given findings on /home."""
    page = make_page('https://example.test/home', failed=findings)
    return jsonl_factory(
        [('https://example.test/home', page)], name=name)


class TestDiffScans:
    def test_no_changes(self, jsonl_factory, make_finding, make_page):
        f = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#a')
        old = _scan(jsonl_factory, make_finding, make_page,
                    'old.jsonl', [f])
        new = _scan(jsonl_factory, make_finding, make_page,
                    'new.jsonl', [f])

        diff = registry.diff_scans(old, new)
        assert diff['summary']['fixed'] == 0
        assert diff['summary']['new'] == 0
        assert diff['summary']['remaining'] == 1
        assert diff['improved'] is False
        assert diff['clean'] is False

    def test_all_fixed_is_clean(self, jsonl_factory,
                                  make_finding, make_page):
        old_f = make_finding(
            'color-contrast', EARL_FAILED, engine='axe',
            tags=['sc-1.4.3'], selector='#a')
        old = _scan(jsonl_factory, make_finding, make_page,
                    'old.jsonl', [old_f])
        # Empty new scan
        new = jsonl_factory(
            [('https://example.test/home',
              make_page('https://example.test/home'))],
            name='new.jsonl')

        diff = registry.diff_scans(old, new)
        assert diff['summary']['fixed'] == 1
        assert diff['summary']['new'] == 0
        assert diff['summary']['remaining'] == 0
        assert diff['improved'] is True
        assert diff['clean'] is True

    def test_new_regression(self, jsonl_factory,
                              make_finding, make_page):
        old = jsonl_factory(
            [('https://example.test/home',
              make_page('https://example.test/home'))],
            name='old.jsonl')
        new_f = make_finding(
            'color-contrast', EARL_FAILED, engine='axe',
            tags=['sc-1.4.3'], selector='#a')
        new = _scan(jsonl_factory, make_finding, make_page,
                    'new.jsonl', [new_f])

        diff = registry.diff_scans(old, new)
        assert diff['summary']['fixed'] == 0
        assert diff['summary']['new'] == 1
        assert diff['summary']['remaining'] == 0
        assert diff['improved'] is False
        assert diff['clean'] is False

    def test_mixed_change(self, jsonl_factory,
                           make_finding, make_page):
        # Old: A and B failing.
        # New: A still failing (remaining), B fixed, C newly broken.
        a = make_finding('color-contrast', EARL_FAILED,
                         engine='axe',
                         tags=['sc-1.4.3'], selector='#a')
        b = make_finding('color-contrast', EARL_FAILED,
                         engine='axe',
                         tags=['sc-1.4.3'], selector='#b')
        c = make_finding('label', EARL_FAILED, engine='axe',
                         tags=['sc-4.1.2'], selector='#c')

        old = _scan(jsonl_factory, make_finding, make_page,
                    'old.jsonl', [a, b])
        new = _scan(jsonl_factory, make_finding, make_page,
                    'new.jsonl', [a, c])

        diff = registry.diff_scans(old, new)
        assert diff['summary']['fixed'] == 1   # b
        assert diff['summary']['new'] == 1     # c
        assert diff['summary']['remaining'] == 1  # a

    def test_per_sc_delta_populated(self, jsonl_factory,
                                      make_finding, make_page):
        # Two SCs: one improved, one regressed.
        before = [
            make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#a'),
        ]
        after = [
            make_finding('label', EARL_FAILED, engine='axe',
                         tags=['sc-4.1.2'], selector='#z'),
        ]
        old = _scan(jsonl_factory, make_finding, make_page,
                    'old.jsonl', before)
        new = _scan(jsonl_factory, make_finding, make_page,
                    'new.jsonl', after)

        diff = registry.diff_scans(old, new)
        sc = diff['sc_delta']
        assert sc['sc-1.4.3']['fixed'] == 1
        assert sc['sc-1.4.3']['new'] == 0
        assert sc['sc-4.1.2']['fixed'] == 0
        assert sc['sc-4.1.2']['new'] == 1
        assert sc['sc-1.4.3']['name'] == 'Contrast (Minimum)'
