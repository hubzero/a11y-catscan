"""Tier 2: CLI script's JSONL/JSON iteration helpers.

The CLI exposes _iter_jsonl, _iter_report (auto-detect JSON vs JSONL),
_iter_deduped (apply cross-engine dedup on the fly), and
_extract_urls_from_report. Each is loaded via the `cli` fixture.
"""

import json

import pytest

from engine_mappings import EARL_FAILED


# ── _iter_jsonl ────────────────────────────────────────────────

class TestIterJsonl:
    def test_yields_records(self, cli, tmp_path):
        path = tmp_path / 's.jsonl'
        path.write_text(
            json.dumps({'http://a/': {'failed': []}}) + '\n' +
            json.dumps({'http://b/': {'failed': []}}) + '\n')
        urls = [u for u, _ in cli.iter_jsonl(str(path))]
        assert urls == ['http://a/', 'http://b/']

    def test_skips_corrupt_lines_with_warning(self, cli, tmp_path,
                                                capsys):
        path = tmp_path / 's.jsonl'
        path.write_text(
            json.dumps({'http://a/': {}}) + '\n' +
            'not json\n' +
            json.dumps({'http://b/': {}}) + '\n')
        urls = [u for u, _ in cli.iter_jsonl(str(path))]
        assert urls == ['http://a/', 'http://b/']
        captured = capsys.readouterr()
        # CLI emits a warning to stderr for corrupt lines
        assert 'corrupt' in captured.err.lower() or \
            'corrupt' in captured.out.lower()

    def test_skips_blank_lines(self, cli, tmp_path):
        path = tmp_path / 's.jsonl'
        path.write_text(
            '\n\n' +
            json.dumps({'http://a/': {}}) + '\n')
        records = list(cli.iter_jsonl(str(path)))
        assert len(records) == 1


# ── _iter_report (auto-detect) ────────────────────────────────

class TestIterReport:
    def test_jsonl_auto_detected(self, cli, tmp_path):
        path = tmp_path / 'r.jsonl'
        path.write_text(
            json.dumps({'http://a/': {'failed': []}}) + '\n')
        records = list(cli.iter_report(str(path)))
        assert len(records) == 1
        assert records[0][0] == 'http://a/'

    def test_json_auto_detected(self, cli, tmp_path):
        # JSON file: a top-level dict of url → page_data
        path = tmp_path / 'r.json'
        path.write_text(json.dumps({
            'http://a/': {'failed': []},
            'http://b/': {'failed': []},
        }))
        records = sorted(cli.iter_report(str(path)))
        urls = [u for u, _ in records]
        assert urls == ['http://a/', 'http://b/']

    def test_falls_back_to_jsonl_on_invalid_json(self, cli, tmp_path):
        # Starts with '{' so iter_report tries JSON, fails, then
        # falls back to JSONL parsing.
        path = tmp_path / 'r.jsonl'
        path.write_text(
            json.dumps({'http://a/': {}}) + '\n' +
            json.dumps({'http://b/': {}}) + '\n')
        records = list(cli.iter_report(str(path)))
        assert {u for u, _ in records} == {'http://a/', 'http://b/'}


# ── _iter_deduped ─────────────────────────────────────────────

class TestIterDeduped:
    def test_applies_cross_engine_merge(
            self, cli, jsonl_factory, make_finding, make_page):
        a = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        b = make_finding('text_contrast_sufficient', EARL_FAILED,
                         engine='ibm', tags=['sc-1.4.3'],
                         selector='#x')
        page = make_page('http://a/', failed=[a, b])
        path = jsonl_factory([('http://a/', page)])

        records = list(cli.iter_deduped(path))
        assert len(records) == 1
        url, data = records[0]
        assert len(data[EARL_FAILED]) == 1
        assert data[EARL_FAILED][0]['engine_count'] == 2


# ── _extract_urls_from_report ─────────────────────────────────

class TestExtractUrlsFromReport:
    def test_returns_only_urls_with_failures(
            self, cli, tmp_path, make_finding, make_page):
        f = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        clean = make_page('http://clean/')
        broken = make_page('http://broken/', failed=[f])
        path = tmp_path / 'r.jsonl'
        path.write_text(
            json.dumps({'http://clean/': clean}) + '\n' +
            json.dumps({'http://broken/': broken}) + '\n')

        urls = cli.extract_urls_from_report(str(path))
        assert urls == ['http://broken/']

    def test_supports_cant_tell_filter(
            self, cli, tmp_path, make_finding, make_page):
        from engine_mappings import EARL_CANTTELL
        ct = make_finding('aria-allowed-attr', EARL_CANTTELL,
                          engine='axe',
                          tags=['aria-valid-attrs'],
                          selector='#nav')
        page = make_page('http://review/', cant_tell=[ct])
        path = tmp_path / 'r.jsonl'
        path.write_text(
            json.dumps({'http://review/': page}) + '\n')

        urls = cli.extract_urls_from_report(
            str(path), which=EARL_CANTTELL)
        assert urls == ['http://review/']

    def test_empty_report_returns_empty(self, cli, tmp_path):
        path = tmp_path / 'r.jsonl'
        path.write_text('')
        assert cli.extract_urls_from_report(str(path)) == []
