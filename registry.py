"""
Scan registry and report utilities for a11y-catscan.

Provides:
  - Named scan registry (save, load, list, delete)
  - Report search (filter findings by SC, URL, selector, engine)
  - Page status (is a URL clean in a given report?)
  - Structured scan diff (fixed/new/remaining as JSON)

Used by both the CLI and MCP server.
"""

import fnmatch
import json
import os
import re
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

from engine_mappings import (
    sc_name, sc_level, resolve_sc,
    EARL_FAILED, EARL_CANTTELL)
from report_io import iter_jsonl, iter_deduped
from results import dedup_page, count_nodes

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REGISTRY_PATH = os.path.join(SCRIPT_DIR, 'reports', 'scans.json')


# ── Scan Registry ────────────────────────────────────────────────

def _load_registry(path=None):
    path = path or DEFAULT_REGISTRY_PATH
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save_registry(data, path=None):
    """Atomically write the registry to disk.

    Writes to a sibling temp file and `os.replace`s into place so a
    crash mid-write can never produce a zero-byte registry — losing
    every prior named scan would be a nasty UX regression.
    """
    path = path or DEFAULT_REGISTRY_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        # Leave the existing registry in place; clean up the tmp.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register_scan(
    name: str,
    report_paths: dict[str, str],
    url: str | None = None,
    engines: list[str] | None = None,
    summary: dict | None = None,
    registry_path: str | None = None,
) -> dict:
    """Register a completed scan by name.

    Args:
        name: Human-readable scan name (e.g. 'baseline', 'post-fix')
        report_paths: Dict of report file paths
            {'json': '...', 'jsonl': '...', 'html': '...', 'md': '...'}
        url: Starting URL that was scanned
        engines: List of engine names used
        summary: Summary dict (pages, failed, cantTell, clean)
    """
    reg = _load_registry(registry_path)
    reg[name] = {
        'timestamp': datetime.now().isoformat(),
        'url': url or '',
        'engines': engines or [],
        'reports': report_paths,
        'summary': summary or {},
    }
    _save_registry(reg, registry_path)
    return reg[name]


def get_scan(name: str, registry_path: str | None = None) -> dict | None:
    """Look up a named scan.  Returns the registry entry or None."""
    reg = _load_registry(registry_path)
    return reg.get(name)


def list_scans(registry_path: str | None = None) -> dict[str, dict]:
    """List all registered scans.  Returns dict of name → entry."""
    return _load_registry(registry_path)


def delete_scan(
    name: str,
    registry_path: str | None = None,
) -> dict | None:
    """Remove a scan from the registry (doesn't delete files)."""
    reg = _load_registry(registry_path)
    removed = reg.pop(name, None)
    if removed:
        _save_registry(reg, registry_path)
    return removed


# ── Search Findings ──────────────────────────────────────────────

def search_findings(
    jsonl_path: str,
    sc: str | None = None,
    url_pattern: str | None = None,
    selector_pattern: str | None = None,
    outcome: str | None = None,
    engine: str | None = None,
    dedup: bool = True,
) -> list[dict]:
    """Search a JSONL report for matching findings.

    All filters are AND — a finding must match all specified filters.

    Args:
        jsonl_path: Path to .jsonl report file
        sc: WCAG SC filter, e.g. '1.4.3' or 'sc-1.4.3'
        url_pattern: URL glob pattern, e.g. '/groups/*' or '*forum*'
        selector_pattern: CSS selector glob, e.g. '*table*'
        outcome: 'failed' or 'cantTell'
        engine: Engine name filter, e.g. 'axe'
        dedup: Apply cross-engine dedup (default True)

    Returns:
        List of matching findings with url context.
    """
    # Normalize SC filter — accepts number (1.4.3), slug
    # (contrast-minimum), or tag (sc-1.4.3)
    if sc:
        sc = sc.replace('sc-', '')
        resolved = resolve_sc(sc)
        if resolved:
            sc = resolved
        sc_tag = 'sc-' + sc

    iterator = iter_deduped(jsonl_path) if dedup else iter_jsonl(jsonl_path)
    matches = []

    for page_url, data in iterator:
        # URL filter
        if url_pattern:
            path = urlparse(page_url).path
            if not fnmatch.fnmatch(path, url_pattern):
                continue

        outcomes = []
        if outcome:
            outcomes = [outcome]
        else:
            outcomes = [EARL_FAILED, EARL_CANTTELL]

        for out in outcomes:
            for item in data.get(out, []):
                # SC filter
                if sc:
                    tags = item.get('tags', [])
                    if sc_tag not in tags:
                        continue

                # Engine filter
                if engine:
                    engines_dict = item.get('engines', {})
                    item_engine = item.get('engine', '')
                    if engine not in engines_dict and engine != item_engine:
                        continue

                # Selector filter
                if selector_pattern:
                    sel_match = False
                    for node in item.get('nodes', []):
                        sel = (node.get('target', [''])[0]
                               if node.get('target') else '')
                        html = node.get('html', '')
                        if (fnmatch.fnmatch(sel, selector_pattern)
                                or fnmatch.fnmatch(html, selector_pattern)):
                            sel_match = True
                            break
                    if not sel_match:
                        continue

                # Build match entry
                selector = ''
                html = ''
                if item.get('nodes'):
                    node = item['nodes'][0]
                    selector = (node.get('target', [''])[0]
                                if node.get('target') else '')
                    html = node.get('html', '')[:200]

                matches.append({
                    'url': page_url,
                    'outcome': out,
                    'id': item.get('id', ''),
                    'engines': item.get('engines', {}),
                    'engine_count': item.get('engine_count', 1),
                    'impact': item.get('impact', ''),
                    'tags': item.get('tags', []),
                    'description': item.get('description', ''),
                    'help': item.get('help', ''),
                    'selector': selector,
                    'html': html,
                })

    return matches


# ── Page Status ──────────────────────────────────────────────────

def page_status(
    jsonl_path: str,
    url: str,
    dedup: bool = True,
) -> dict:
    """Check the status of a specific URL in a report.

    Args:
        jsonl_path: Path to .jsonl report file
        url: The URL to check (exact match or path match)

    Returns:
        Dict with clean (bool), failed/cantTell counts,
        per-SC breakdown, engine agreement, and findings list.
    """
    iterator = iter_deduped(jsonl_path) if dedup else iter_jsonl(jsonl_path)

    # Find the page — try exact match first, then path match
    page_data = None
    page_url = None
    target_path = urlparse(url).path

    for purl, pdata in iterator:
        if purl == url:
            page_data = pdata
            page_url = purl
            break
        if urlparse(purl).path == target_path:
            page_data = pdata
            page_url = purl
            # Don't break — might find exact match later

    if page_data is None:
        return {'found': False, 'url': url, 'error': 'URL not in report'}

    failed = page_data.get(EARL_FAILED, [])
    cant_tell = page_data.get(EARL_CANTTELL, [])

    # Per-SC breakdown
    sc_breakdown = {}
    for item in failed + cant_tell:
        for tag in item.get('tags', []):
            if tag.startswith('sc-'):
                sc_id = tag.replace('sc-', '')
                if sc_id not in sc_breakdown:
                    sc_breakdown[sc_id] = {
                        'name': sc_name(sc_id),
                        'failed': 0,
                        'cantTell': 0,
                    }
                if item.get('outcome') == EARL_FAILED:
                    sc_breakdown[sc_id]['failed'] += 1
                else:
                    sc_breakdown[sc_id]['cantTell'] += 1

    # Engine agreement summary
    engine_counts = Counter()
    for item in failed + cant_tell:
        ec = item.get('engine_count', 1)
        engine_counts[ec] += 1

    # Flatten findings
    findings = []
    for item in failed + cant_tell:
        selector = ''
        if item.get('nodes'):
            selector = (item['nodes'][0].get('target', [''])[0]
                        if item['nodes'][0].get('target') else '')
        findings.append({
            'outcome': item.get('outcome', ''),
            'id': item.get('id', ''),
            'impact': item.get('impact', ''),
            'engines': item.get('engines', {}),
            'selector': selector,
        })

    return {
        'found': True,
        'url': page_url,
        'clean': len(failed) == 0,
        'failed': count_nodes(failed),
        'cantTell': count_nodes(cant_tell),
        'sc_breakdown': sc_breakdown,
        'engine_agreement': dict(engine_counts),
        'findings': findings,
    }


# ── Structured Diff ──────────────────────────────────────────────

def diff_scans(old_jsonl: str, new_jsonl: str) -> dict:
    """Compare two scans and return structured results.

    Args:
        old_jsonl: Path to baseline .jsonl report
        new_jsonl: Path to new .jsonl report

    Returns:
        Dict with fixed, new, remaining findings, per-SC deltas,
        and summary counts.
    """
    def _finding_keys(jsonl_path):
        """Extract {(url_path, sc_tag, selector): finding_info}."""
        keys = {}
        for url, data in iter_deduped(jsonl_path):
            path = urlparse(url).path
            for item in data.get(EARL_FAILED, []):
                for tag in item.get('tags', []):
                    if tag.startswith('sc-'):
                        selector = ''
                        if item.get('nodes'):
                            selector = (
                                item['nodes'][0].get('target', [''])[0]
                                if item['nodes'][0].get('target')
                                else '')
                        key = (path, tag, selector)
                        keys[key] = {
                            'url_path': path,
                            'sc': tag,
                            'selector': selector,
                            'id': item.get('id', ''),
                            'impact': item.get('impact', ''),
                            'engines': item.get('engines', {}),
                        }
        return keys

    old = _finding_keys(old_jsonl)
    new = _finding_keys(new_jsonl)

    old_set = set(old.keys())
    new_set = set(new.keys())

    fixed_keys = old_set - new_set
    new_keys = new_set - old_set
    remaining_keys = old_set & new_set

    fixed = [old[k] for k in sorted(fixed_keys)]
    new_findings = [new[k] for k in sorted(new_keys)]
    remaining = [new[k] for k in sorted(remaining_keys)]

    # Per-SC delta
    sc_delta = {}
    for k in fixed_keys:
        sc = k[1]
        sc_delta.setdefault(sc, {'fixed': 0, 'new': 0, 'remaining': 0})
        sc_delta[sc]['fixed'] += 1
    for k in new_keys:
        sc = k[1]
        sc_delta.setdefault(sc, {'fixed': 0, 'new': 0, 'remaining': 0})
        sc_delta[sc]['new'] += 1
    for k in remaining_keys:
        sc = k[1]
        sc_delta.setdefault(sc, {'fixed': 0, 'new': 0, 'remaining': 0})
        sc_delta[sc]['remaining'] += 1

    # Add SC names
    for sc_tag, delta in sc_delta.items():
        sc_id = sc_tag[3:]  # strip 'sc-'
        delta['name'] = sc_name(sc_id)

    return {
        'summary': {
            'fixed': len(fixed),
            'new': len(new_findings),
            'remaining': len(remaining),
            'old_total': len(old),
            'new_total': len(new),
        },
        'improved': len(fixed) > len(new_findings),
        'clean': len(new) == 0,
        'fixed': fixed,
        'new': new_findings,
        'remaining': remaining,
        'sc_delta': sc_delta,
    }
