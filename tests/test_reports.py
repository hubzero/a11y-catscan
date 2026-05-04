"""Tier 3: HTML / LLM-markdown report generators and CLI diff_scans.

These functions stream a JSONL of scan results and write a derived
report file. We feed each one a synthetic JSONL and assert structural
properties (file is created, contains expected sections, references
the seeded findings) — no exact-string matching that would be brittle.
"""

import json

import pytest

from engine_mappings import EARL_FAILED, EARL_CANTTELL


# ── generate_html_report ──────────────────────────────────────

class TestGenerateHtmlReport:
    def test_writes_file_with_expected_sections(
            self, cli, tmp_path, jsonl_factory,
            make_finding, make_page):
        # Two pages, one with a violation, one with cantTell
        v = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3', 'wcag2aa'], selector='#a',
                         html='<span id="a">low contrast</span>',
                         message='Element has insufficient contrast')
        ct = make_finding('aria-valid-attr', EARL_CANTTELL,
                          engine='axe',
                          tags=['aria-valid-attrs'],
                          selector='#nav',
                          html='<nav id="nav"></nav>',
                          message='Suspect aria attribute')
        page_v = make_page('https://example.test/x', failed=[v])
        page_ct = make_page('https://example.test/y', cant_tell=[ct])
        jsonl = jsonl_factory(
            [('https://example.test/x', page_v),
             ('https://example.test/y', page_ct)])

        out = tmp_path / 'report.html'
        cli.generate_html_report(
            jsonl, str(out), 'https://example.test/',
            level_label='WCAG 2.1 Level AA')

        assert out.is_file()
        body = out.read_text()
        # Structural assertions
        assert '<!DOCTYPE html>' in body
        assert '<html' in body
        # Mentions the start URL we fed in
        assert 'https://example.test/' in body
        # Mentions the seeded rule + page URL somewhere
        assert 'color-contrast' in body
        assert 'https://example.test/x' in body

    def test_runs_on_empty_jsonl(self, cli, tmp_path, jsonl_factory):
        # Empty JSONL → still produces a valid file
        empty = jsonl_factory([])
        out = tmp_path / 'empty.html'
        cli.generate_html_report(
            empty, str(out), 'https://example.test/')
        assert out.is_file()
        assert out.read_text().startswith('<!DOCTYPE html>')

    def test_allowlist_marks_findings_suppressed(
            self, cli, tmp_path, jsonl_factory,
            make_finding, make_page):
        # cantTell that matches an allowlist entry should be counted
        # as suppressed rather than reported.
        ct = make_finding('aria-valid-attrs', EARL_CANTTELL,
                          engine='axe',
                          tags=['aria-valid-attrs'],
                          selector='#nav')
        page = make_page('https://example.test/p', cant_tell=[ct])
        jsonl = jsonl_factory([('https://example.test/p', page)])

        out = tmp_path / 'r.html'
        cli.generate_html_report(
            jsonl, str(out), 'https://example.test/',
            allowlist=[{'rule': 'aria-valid-attrs'}])
        body = out.read_text().lower()
        assert 'suppress' in body  # report mentions suppression


# ── generate_llm_report ───────────────────────────────────────

class TestGenerateLlmReport:
    def test_writes_markdown_with_expected_sections(
            self, cli, tmp_path, jsonl_factory,
            make_finding, make_page):
        v = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='.btn',
                         html='<a class="btn">Go</a>')
        page = make_page('https://example.test/home', failed=[v])
        jsonl = jsonl_factory([('https://example.test/home', page)])

        out = tmp_path / 'report.md'
        report = cli.generate_llm_report(
            jsonl, str(out), 'https://example.test/',
            level_label='WCAG 2.1 Level AA', config={})

        # Function returns the markdown it just wrote
        assert isinstance(report, str)
        assert out.is_file()
        text = out.read_text()
        assert text == report

        # Major sections
        assert text.startswith('# a11y-catscan')
        assert '## Violations' in text
        assert '## Detailed reports' in text
        # The seeded violation appears in markdown
        assert 'color-contrast' in text
        # WCAG SC tag is rendered
        assert '1.4.3' in text

    def test_clean_run_says_violations_none(
            self, cli, tmp_path, jsonl_factory, make_page):
        page = make_page('https://example.test/p')
        jsonl = jsonl_factory([('https://example.test/p', page)])

        out = tmp_path / 'r.md'
        text = cli.generate_llm_report(
            jsonl, str(out), 'https://example.test/', config={})
        assert '## Violations: NONE' in text

    def test_uses_custom_instructions_file(
            self, cli, tmp_path, jsonl_factory,
            make_finding, make_page):
        instr = tmp_path / 'instr.md'
        instr.write_text('### Site-specific guidance\n'
                         'Templates live in app/views/.\n')
        page = make_page('https://example.test/p')
        jsonl = jsonl_factory([('https://example.test/p', page)])

        out = tmp_path / 'r.md'
        text = cli.generate_llm_report(
            jsonl, str(out), 'https://example.test/',
            config={'llm_instructions': str(instr)})
        assert 'Site-specific guidance' in text
        assert 'Templates live in app/views/' in text

    def test_default_instructions_when_no_config_path(
            self, cli, tmp_path, jsonl_factory, make_page):
        page = make_page('https://example.test/p')
        jsonl = jsonl_factory([('https://example.test/p', page)])
        out = tmp_path / 'r.md'
        text = cli.generate_llm_report(
            jsonl, str(out), 'https://example.test/', config={})
        # The default fallback writes a generic Instructions section
        assert '## Instructions' in text

    def test_incompletes_loop_runs_for_canttell(
            self, cli, tmp_path, jsonl_factory,
            make_finding, make_page):
        # generate_llm_report iterates cantTell findings and tries to
        # bucket them by node.any[].data.messageKey.  dedup_page
        # collapses the any/all/none structure into a flat 'message'
        # string before the report sees it — so the bucketer ends up
        # with no keys.  This still exercises the cantTell loop body
        # (allowlist check, page tracking) without crashing.
        ct = make_finding('aria-valid-attrs', EARL_CANTTELL,
                          engine='axe', selector='#nav',
                          html='<nav role="x"></nav>')
        page = make_page('https://example.test/home',
                         cant_tell=[ct])
        jsonl = jsonl_factory(
            [('https://example.test/home', page)])

        out = tmp_path / 'r.md'
        text = cli.generate_llm_report(
            jsonl, str(out), 'https://example.test/', config={})
        # Section header is always rendered
        assert '## Incompletes' in text

    def test_incompletes_none_when_no_canttell(
            self, cli, tmp_path, jsonl_factory,
            make_finding, make_page):
        v = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        page = make_page('https://example.test/p', failed=[v])
        jsonl = jsonl_factory([('https://example.test/p', page)])
        out = tmp_path / 'r.md'
        text = cli.generate_llm_report(
            jsonl, str(out), 'https://example.test/', config={})
        # When there are no cantTell entries, the report says so
        assert '## Incompletes: NONE' in text


# ── CLI diff_scans (the printing variant) ─────────────────────

class TestCliDiffScans:
    def test_no_changes_prints_nothing_changed(
            self, cli, capsys, jsonl_factory,
            make_finding, make_page):
        v = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#a')
        page = make_page('https://example.test/p', failed=[v])
        old = jsonl_factory(
            [('https://example.test/p', page)], name='old.jsonl')
        new = jsonl_factory(
            [('https://example.test/p', page)], name='new.jsonl')

        fixed, added = cli.print_diff(old, new)
        out = capsys.readouterr().out
        assert fixed == 0
        assert added == 0
        # Either "REMAINING" or "No changes" depending on whether
        # the function shows the remaining bucket; both indicate
        # no regressions.
        assert 'REMAINING' in out or 'No changes' in out

    def test_fixed_and_new_counted(
            self, cli, capsys, jsonl_factory,
            make_finding, make_page):
        a = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#a')
        b = make_finding('label', EARL_FAILED, engine='axe',
                         tags=['sc-4.1.2'], selector='#b')

        old_page = make_page('https://example.test/p', failed=[a])
        new_page = make_page('https://example.test/p', failed=[b])

        old = jsonl_factory(
            [('https://example.test/p', old_page)], name='old.jsonl')
        new = jsonl_factory(
            [('https://example.test/p', new_page)], name='new.jsonl')

        fixed, added = cli.print_diff(old, new)
        out = capsys.readouterr().out
        assert fixed == 1   # color-contrast gone
        assert added == 1   # label new
        assert 'FIXED' in out
        assert 'NEW' in out
        assert 'color-contrast' in out
        assert 'label' in out
