"""Tier 2: registry persistence and JSONL streaming.

The registry CRUD touches a JSON file on disk; iter_jsonl /
iter_deduped stream one page per line. All tests use tmp_path.
"""

import json
import os

import pytest

import registry
from engine_mappings import EARL_FAILED, EARL_CANTTELL


# ── Registry CRUD ──────────────────────────────────────────────

class TestRegistryCrud:
    def test_load_missing_returns_empty(self, tmp_path):
        path = str(tmp_path / 'nope.json')
        assert registry._load_registry(path) == {}

    def test_register_and_get_roundtrip(self, tmp_path):
        path = str(tmp_path / 'reg.json')
        entry = registry.register_scan(
            'baseline',
            report_paths={'json': '/r/scan.json',
                          'jsonl': '/r/scan.jsonl'},
            url='https://example.test/',
            engines=['axe', 'alfa'],
            summary={'pages': 10, 'failed': 3},
            registry_path=path)

        assert entry['url'] == 'https://example.test/'
        assert entry['engines'] == ['axe', 'alfa']
        assert entry['summary']['pages'] == 10
        assert entry['reports']['json'] == '/r/scan.json'

        # Persisted to disk
        with open(path) as f:
            on_disk = json.load(f)
        assert 'baseline' in on_disk

        # get_scan finds it
        assert registry.get_scan(
            'baseline', registry_path=path)['url'] == \
            'https://example.test/'

    def test_get_nonexistent_returns_none(self, tmp_path):
        path = str(tmp_path / 'reg.json')
        assert registry.get_scan('missing', registry_path=path) is None

    def test_list_scans_empty(self, tmp_path):
        path = str(tmp_path / 'reg.json')
        assert registry.list_scans(registry_path=path) == {}

    def test_list_scans_returns_all(self, tmp_path):
        path = str(tmp_path / 'reg.json')
        registry.register_scan('a', {}, registry_path=path)
        registry.register_scan('b', {}, registry_path=path)
        all_scans = registry.list_scans(registry_path=path)
        assert set(all_scans.keys()) == {'a', 'b'}

    def test_delete_existing(self, tmp_path):
        path = str(tmp_path / 'reg.json')
        registry.register_scan('a', {}, registry_path=path)
        removed = registry.delete_scan('a', registry_path=path)
        assert removed is not None
        assert registry.get_scan('a', registry_path=path) is None

    def test_delete_missing_returns_none(self, tmp_path):
        path = str(tmp_path / 'reg.json')
        assert registry.delete_scan('x', registry_path=path) is None

    def test_register_overwrites_same_name(self, tmp_path):
        path = str(tmp_path / 'reg.json')
        registry.register_scan(
            'a', {'json': 'first'}, registry_path=path)
        registry.register_scan(
            'a', {'json': 'second'}, registry_path=path)
        entry = registry.get_scan('a', registry_path=path)
        assert entry['reports']['json'] == 'second'

    def test_creates_parent_dir(self, tmp_path):
        # Registry path inside a subdir that doesn't yet exist
        path = str(tmp_path / 'nested' / 'dir' / 'reg.json')
        registry.register_scan('a', {}, registry_path=path)
        assert os.path.isfile(path)


# ── JSONL iteration ────────────────────────────────────────────

class TestIterJsonl:
    def test_iterates_records(self, tmp_path):
        path = tmp_path / 's.jsonl'
        path.write_text(
            json.dumps({'http://a/': {'failed': []}}) + '\n' +
            json.dumps({'http://b/': {'failed': []}}) + '\n')

        urls = [u for u, _ in registry.iter_jsonl(str(path))]
        assert urls == ['http://a/', 'http://b/']

    def test_skips_blank_lines(self, tmp_path):
        path = tmp_path / 's.jsonl'
        path.write_text(
            '\n' +
            json.dumps({'http://a/': {}}) + '\n' +
            '\n')

        records = list(registry.iter_jsonl(str(path)))
        assert len(records) == 1

    def test_skips_corrupt_lines(self, tmp_path):
        path = tmp_path / 's.jsonl'
        path.write_text(
            json.dumps({'http://a/': {}}) + '\n' +
            'NOT JSON\n' +
            json.dumps({'http://b/': {}}) + '\n')
        urls = [u for u, _ in registry.iter_jsonl(str(path))]
        assert urls == ['http://a/', 'http://b/']

    def test_empty_file(self, tmp_path):
        path = tmp_path / 'empty.jsonl'
        path.write_text('')
        assert list(registry.iter_jsonl(str(path))) == []


class TestIterDeduped:
    def test_applies_dedup(self, jsonl_factory, make_finding, make_page):
        # Two engines with same selector + SC — should collapse
        a = make_finding('color-contrast', EARL_FAILED, engine='axe',
                         tags=['sc-1.4.3'], selector='#x')
        b = make_finding('text_contrast_sufficient', EARL_FAILED,
                         engine='ibm',
                         tags=['sc-1.4.3'], selector='#x')
        page = make_page('http://a/', failed=[a, b])
        path = jsonl_factory([('http://a/', page)])

        records = list(registry.iter_deduped(path))
        assert len(records) == 1
        url, data = records[0]
        assert url == 'http://a/'
        assert len(data[EARL_FAILED]) == 1
        assert data[EARL_FAILED][0]['engine_count'] == 2
