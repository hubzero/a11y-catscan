"""Shared pytest fixtures and path setup.

The project root contains a CLI script with a hyphen in its name
(`a11y-catscan.py`), which can't be imported with a normal `import`.
We expose it as the `cli` fixture by loading it via importlib.
"""

import asyncio
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

import pytest


# ── Suppress Playwright shutdown noise ───────────────────────────
# When a test's pytest-asyncio loop closes, leftover Playwright
# Futures (TargetClosedError / Browser closed) are GC'd and asyncio
# routes the warning through the 'asyncio' logger and through stderr.
# The CLI's crawl_and_scan installs the same suppression for
# production runs (commit aae81e8); here we apply it suite-wide so
# test output stays clean.

class _SuppressTargetClosed(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if ('TargetClosedError' in msg
                or 'Browser closed' in msg
                or 'Event loop is closed' in msg):
            return False
        return True


logging.getLogger('asyncio').addFilter(_SuppressTargetClosed())


# Also wrap sys.unraisablehook to drop the same shutdown errors when
# they bubble up through Future.__del__ instead of through logging.
_orig_unraisablehook = sys.unraisablehook


def _filtered_unraisablehook(unraisable):
    exc = unraisable.exc_value
    text = '{}: {}'.format(type(exc).__name__, exc) if exc else ''
    if ('TargetClosedError' in text
            or 'Browser closed' in text
            or 'Event loop is closed' in text):
        return
    _orig_unraisablehook(unraisable)


sys.unraisablehook = _filtered_unraisablehook

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Make project modules importable from tests.
sys.path.insert(0, str(PROJECT_ROOT))


def _load_cli_module():
    """Load a11y-catscan.py as a module despite the hyphen in the name.

    Done lazily because the module imports playwright and other deps;
    we want a clear failure when those are missing rather than at
    collection time.
    """
    spec = importlib.util.spec_from_file_location(
        'a11y_catscan_cli', str(PROJECT_ROOT / 'a11y-catscan.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope='session')
def cli():
    """The a11y-catscan.py CLI module, loaded once per test session."""
    return _load_cli_module()


# ── Synthetic JSONL fixture data ────────────────────────────────

def _make_finding(rule_id, outcome, *, engine='axe', tags=None,
                  selector='div.x', html='<div class="x"></div>',
                  message='', impact='serious'):
    """Build one normalized finding dict matching engines/base.py format."""
    return {
        'id': rule_id,
        'engine': engine,
        'outcome': outcome,
        'description': '{} description'.format(rule_id),
        'help': '{} help'.format(rule_id),
        'helpUrl': 'https://example.test/{}'.format(rule_id),
        'impact': impact,
        'tags': list(tags or []),
        'nodes': [{
            'target': [selector],
            'html': html,
            'any': [{'message': message}] if message else [],
        }],
    }


def _page_record(url, *, failed=None, cant_tell=None, passed=None,
                 inapplicable=None):
    """Build one page-level result dict (pre-dedup)."""
    return {
        'url': url,
        'timestamp': '2026-04-30T10:00:00',
        'http_status': 200,
        'failed': list(failed or []),
        'cantTell': list(cant_tell or []),
        'passed': list(passed or []),
        'inapplicable': list(inapplicable or []),
    }


@pytest.fixture
def make_finding():
    """Factory for building normalized finding dicts."""
    return _make_finding


@pytest.fixture
def make_page():
    """Factory for building page-result dicts."""
    return _page_record


@pytest.fixture
def jsonl_factory(tmp_path):
    """Build a temporary JSONL report file from a list of (url, page_data)."""
    def _build(records, name='scan.jsonl'):
        path = tmp_path / name
        with open(path, 'w') as f:
            for url, data in records:
                f.write(json.dumps({url: data}) + '\n')
        return str(path)
    return _build


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Redirect registry.DEFAULT_REGISTRY_PATH to a tmp file.

    Several tests register/list/delete scans through the registry
    or the MCP `manage_scans` tool — this keeps each test's writes
    contained so they don't pollute the developer's reports/scans.json.
    """
    import registry
    fake = str(tmp_path / 'scans.json')
    monkeypatch.setattr(registry, 'DEFAULT_REGISTRY_PATH', fake)
    return fake
