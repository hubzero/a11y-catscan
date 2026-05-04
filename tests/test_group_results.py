"""Tier 2: CLI's _group_results stdout report.

_group_results streams a JSONL report and prints a grouped summary
table. The function returns nothing — its contract is the printed
output — so we capture stdout and assert against the captured text.
"""

import pytest

from engine_mappings import EARL_FAILED, EARL_CANTTELL


@pytest.fixture
def grouped_jsonl(jsonl_factory, make_finding, make_page):
    """A JSONL with a deterministic mix of findings for grouping tests."""
    p1 = make_page(
        'https://example.test/home',
        failed=[
            make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3', 'wcag2aa'],
                         selector='#hero',
                         message='foreground color: #777, background '
                                 'color: #fff, ratio 3.5:1'),
            make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='.btn',
                         message='Element has insufficient '
                                 'contrast on a background gradient'),
        ])
    p2 = make_page(
        'https://example.test/docs',
        failed=[
            make_finding('label', EARL_FAILED, engine='axe',
                         tags=['sc-4.1.2'], selector='input.search',
                         message='Form element missing label'),
            make_finding('region', EARL_FAILED, engine='axe',
                         tags=['bp-landmarks', 'best-practice'],
                         selector='div.outer',
                         message='Element not in landmark'),
        ])
    return jsonl_factory([
        ('https://example.test/home', p1),
        ('https://example.test/docs', p2),
    ])


class TestGroupResults:
    def test_group_by_rule(self, cli, capsys, grouped_jsonl):
        cli.group_results(grouped_jsonl, 'rule')
        out = capsys.readouterr().out
        # Header indicates how many groups
        assert 'Grouped by rule' in out
        # _group_results runs over *deduped* findings, so the displayed
        # rule id is the primary tag (sc-/aria-/bp-), not the engine
        # rule id. The fixture's three primary tags should all appear.
        assert 'sc-1.4.3' in out
        assert 'sc-4.1.2' in out
        assert 'bp-landmarks' in out

    def test_group_by_wcag(self, cli, capsys, grouped_jsonl):
        cli.group_results(grouped_jsonl, 'wcag')
        out = capsys.readouterr().out
        assert 'Grouped by wcag' in out
        # The SC numbers from sc-* tags appear
        assert '1.4.3' in out
        assert '4.1.2' in out

    def test_group_by_color(self, cli, capsys, grouped_jsonl):
        cli.group_results(grouped_jsonl, 'color')
        out = capsys.readouterr().out
        # Findings with foreground/background info get parsed into
        # the "fg on bg" key; the gradient one is "(no color info)"
        assert '#777 on #fff' in out

    def test_group_by_reason_extracts_gradient(
            self, cli, capsys, grouped_jsonl):
        cli.group_results(grouped_jsonl, 'reason')
        out = capsys.readouterr().out
        assert 'background gradient' in out

    def test_group_by_level(self, cli, capsys, grouped_jsonl):
        cli.group_results(grouped_jsonl, 'level')
        out = capsys.readouterr().out
        # SC 1.4.3 is WCAG 2.0 AA; 4.1.2 is WCAG 2.0 A
        assert 'WCAG 2.0 AA' in out
        assert 'WCAG 2.0 A' in out
        # Best-practice findings get a separate bucket
        assert 'Best Practice' in out

    def test_group_by_engine(self, cli, capsys, grouped_jsonl):
        cli.group_results(grouped_jsonl, 'engine')
        out = capsys.readouterr().out
        assert 'Grouped by engine' in out
        # Single-engine findings show 'axe'
        assert 'axe' in out

    def test_group_by_bp(self, cli, capsys, grouped_jsonl):
        cli.group_results(grouped_jsonl, 'bp')
        out = capsys.readouterr().out
        assert 'Grouped by bp' in out
        # bp-landmarks is the only bp-* tag in the fixture
        assert 'landmarks' in out

    def test_unknown_group_falls_back_to_rule(
            self, cli, capsys, grouped_jsonl):
        cli.group_results(grouped_jsonl, 'something-unknown')
        out = capsys.readouterr().out
        # Falls through to the default branch — keys are deduped rule
        # ids (i.e. the primary tags after dedup_page).
        assert 'sc-1.4.3' in out

    def test_empty_jsonl_prints_zero_groups(
            self, cli, capsys, jsonl_factory):
        empty = jsonl_factory([])
        cli.group_results(empty, 'rule')
        out = capsys.readouterr().out
        assert '0 nodes in 0 groups' in out
