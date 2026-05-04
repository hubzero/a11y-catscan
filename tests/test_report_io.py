"""Tests for `report_io.py` — JSONL/JSON streaming readers.

`iter_jsonl` reads JSONL line-by-line, `iter_report` auto-detects
JSON vs JSONL, `iter_deduped` applies cross-engine dedup on the
fly, and `extract_urls_from_report` filters to URLs whose page has
findings.

Tests import the helpers directly from `report_io` rather than via
the CLI's re-export layer, so removing a re-export from
`a11y-catscan.py` won't silently break these tests.
"""

import json

import pytest

from engine_mappings import EARL_FAILED
from report_io import (
    extract_urls_from_report,
    iter_deduped,
    iter_jsonl,
    iter_report,
)


# ── iter_jsonl ─────────────────────────────────────────────────

class TestIterJsonl:
    def test_yields_records(self, tmp_path):
        path = tmp_path / 's.jsonl'
        path.write_text(
            json.dumps({'http://a/': {'failed': []}}) + '\n' +
            json.dumps({'http://b/': {'failed': []}}) + '\n')
        urls = [u for u, _ in iter_jsonl(str(path))]
        assert urls == ['http://a/', 'http://b/']

    def test_skips_corrupt_lines_with_warning(self, tmp_path,
                                                capsys):
        path = tmp_path / 's.jsonl'
        path.write_text(
            json.dumps({'http://a/': {}}) + '\n' +
            'not json\n' +
            json.dumps({'http://b/': {}}) + '\n')
        urls = [u for u, _ in iter_jsonl(str(path))]
        assert urls == ['http://a/', 'http://b/']
        captured = capsys.readouterr()
        # CLI emits a warning to stderr for corrupt lines
        assert 'corrupt' in captured.err.lower() or \
            'corrupt' in captured.out.lower()

    def test_skips_blank_lines(self, tmp_path):
        path = tmp_path / 's.jsonl'
        path.write_text(
            '\n\n' +
            json.dumps({'http://a/': {}}) + '\n')
        records = list(iter_jsonl(str(path)))
        assert len(records) == 1

    def test_skips_non_object_json_lines(self, tmp_path,
                                         capsys):
        # Valid JSON that isn't a {url: data} object — e.g. a
        # list, a literal, or null — must be skipped with a
        # warning instead of crashing on .items().
        path = tmp_path / 's.jsonl'
        path.write_text(
            json.dumps({'http://a/': {}}) + '\n' +
            json.dumps([1, 2, 3]) + '\n' +
            'true\n' +
            json.dumps({'http://b/': {}}) + '\n')
        urls = [u for u, _ in iter_jsonl(str(path))]
        assert urls == ['http://a/', 'http://b/']
        err = capsys.readouterr().err.lower()
        assert 'not an object' in err or 'corrupt' in err


# ── _iter_report (auto-detect) ────────────────────────────────

class TestIterReport:
    def test_jsonl_auto_detected(self, tmp_path):
        path = tmp_path / 'r.jsonl'
        path.write_text(
            json.dumps({'http://a/': {'failed': []}}) + '\n')
        records = list(iter_report(str(path)))
        assert len(records) == 1
        assert records[0][0] == 'http://a/'

    def test_json_auto_detected(self, tmp_path):
        # JSON file: a top-level dict of url → page_data
        path = tmp_path / 'r.json'
        path.write_text(json.dumps({
            'http://a/': {'failed': []},
            'http://b/': {'failed': []},
        }))
        records = sorted(iter_report(str(path)))
        urls = [u for u, _ in records]
        assert urls == ['http://a/', 'http://b/']

    def test_falls_back_to_jsonl_on_invalid_json(self, tmp_path):
        # Starts with '{' so iter_report tries JSON, fails, then
        # falls back to JSONL parsing.
        path = tmp_path / 'r.jsonl'
        path.write_text(
            json.dumps({'http://a/': {}}) + '\n' +
            json.dumps({'http://b/': {}}) + '\n')
        records = list(iter_report(str(path)))
        assert {u for u, _ in records} == {'http://a/', 'http://b/'}


# ── _iter_deduped ─────────────────────────────────────────────

class TestIterDeduped:
    def test_applies_cross_engine_merge(
            self, jsonl_factory, make_finding, make_page):
        a = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        b = make_finding('text_contrast_sufficient', EARL_FAILED,
                         engine='ibm', tags=['sc-1.4.3'],
                         selector='#x')
        page = make_page('http://a/', failed=[a, b])
        path = jsonl_factory([('http://a/', page)])

        records = list(iter_deduped(path))
        assert len(records) == 1
        url, data = records[0]
        assert len(data[EARL_FAILED]) == 1
        assert data[EARL_FAILED][0]['engine_count'] == 2


# ── _extract_urls_from_report ─────────────────────────────────

class TestExtractUrlsFromReport:
    def test_returns_only_urls_with_failures(
            self, tmp_path, make_finding, make_page):
        f = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        clean = make_page('http://clean/')
        broken = make_page('http://broken/', failed=[f])
        path = tmp_path / 'r.jsonl'
        path.write_text(
            json.dumps({'http://clean/': clean}) + '\n' +
            json.dumps({'http://broken/': broken}) + '\n')

        urls = extract_urls_from_report(str(path))
        assert urls == ['http://broken/']

    def test_supports_cant_tell_filter(
            self, tmp_path, make_finding, make_page):
        from engine_mappings import EARL_CANTTELL
        ct = make_finding('aria-allowed-attr', EARL_CANTTELL,
                          engine='axe',
                          tags=['aria-valid-attrs'],
                          selector='#nav')
        page = make_page('http://review/', cant_tell=[ct])
        path = tmp_path / 'r.jsonl'
        path.write_text(
            json.dumps({'http://review/': page}) + '\n')

        urls = extract_urls_from_report(
            str(path), which=EARL_CANTTELL)
        assert urls == ['http://review/']

    def test_empty_report_returns_empty(self, tmp_path):
        path = tmp_path / 'r.jsonl'
        path.write_text('')
        assert extract_urls_from_report(str(path)) == []
