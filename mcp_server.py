#!/usr/bin/env python3
"""
MCP server for a11y-catscan.

Exposes the scanner as Model Context Protocol tools that Claude Code
(or any MCP client) can call directly with structured parameters.

Tools are designed for quick operations that return in seconds:
  scan_page      — scan one URL (~3-17s depending on engines)
  analyze_report — group findings from a previous report (instant)
  list_engines   — show installed engines and versions (instant)
  lookup_wcag    — look up a WCAG Success Criterion (instant)

Site crawls are long-running — use the CLI or /wcag-audit skill.

Usage:
    python3 mcp_server.py
    python3 a11y-catscan.py --mcp

Configure in .mcp.json:
    {
        "mcpServers": {
            "wcag-audit": {
                "type": "stdio",
                "command": "python3",
                "args": ["/path/to/a11y-catscan/mcp_server.py"]
            }
        }
    }
"""

import json
import os
import re
import sys
import logging
from pathlib import Path
from urllib.parse import urlparse

# MCP server must log to stderr — stdout is reserved for the protocol.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('a11y-catscan-mcp')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from mcp.server.fastmcp import FastMCP

from engine_mappings import (
    SC_META, sc_name, IBM_SC_MAP,
    EARL_FAILED, EARL_CANTTELL,
    ARIA_CATEGORIES, BP_CATEGORIES)
from engines.axe import AXE_RULES, get_axe_version
from engines.alfa import ALFA_RULES
from engines.htmlcs import HTMLCS_SNIFFS
from engines.base import NODE_MODULES
from scanner import Scanner
from results import count_nodes

mcp = FastMCP("wcag-audit")


def _validate_scan_url(url):
    """Reject scan_page URLs whose scheme isn't http/https.

    The MCP tool's docstring promises http(s) only — without
    enforcement an MCP client (or a prompt-injection attack against
    the orchestrating LLM) could request `file://` or `chrome://`
    URLs and exfiltrate local content via the returned finding
    selectors / HTML snippets.  Returns an error message string if
    the URL should be rejected, or None if it's safe to scan.
    """
    if not isinstance(url, str) or not url:
        return 'url must be a non-empty string'
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return ("url scheme must be http or https "
                "(got: '{}')").format(parsed.scheme or '(empty)')
    if not parsed.netloc:
        return 'url must include a host'
    return None


@mcp.tool()
async def scan_page(
    url: str,
    engines: str = "all",
    level: str = "wcag21aa",
) -> str:
    """Scan a single page for WCAG accessibility issues.

    Launches a browser, runs up to four accessibility engines against
    one URL, and returns deduped findings with cross-engine confirmation.
    Takes 3-17 seconds depending on engines selected.

    Args:
        url: The page URL to scan (must start with http:// or https://)
        engines: Comma-separated engines: axe, alfa, ibm, htmlcs, or all (default: all)
        level: WCAG conformance level: wcag21aa (default), wcag21a, wcag21aaa, best

    Returns:
        JSON with url, clean (bool), failed/cantTell counts, deduped
        findings array with CSS selectors and engine attribution.
    """
    err = _validate_scan_url(url)
    if err:
        return json.dumps({'error': err, 'url': url})

    engine_list = (['axe', 'alfa', 'ibm', 'htmlcs'] if engines == 'all'
                   else [e.strip() for e in engines.split(',')])

    log.info('scan_page: %s (engines=%s, level=%s)', url, engines, level)

    async with Scanner(engines=engine_list, level=level) as scanner:
        result = await scanner.scan_page(url)

    if result.get('skipped'):
        return json.dumps({
            'url': url,
            'skipped': result['skipped'],
            'clean': True,
            'failed': 0,
            'cantTell': 0,
            'findings': [],
        }, indent=2)

    failed = count_nodes(result.get(EARL_FAILED, []))
    cant_tell = count_nodes(result.get(EARL_CANTTELL, []))

    # Flatten findings for the response
    findings = []
    for outcome in (EARL_FAILED, EARL_CANTTELL):
        for item in result.get(outcome, []):
            selector = ''
            html = ''
            if item.get('nodes'):
                node = item['nodes'][0]
                selector = (node.get('target', [''])[0]
                            if node.get('target') else '')
                html = node.get('html', '')[:200]
            findings.append({
                'outcome': outcome,
                'id': item.get('id', ''),
                'engines': item.get('engines', {}),
                'engine_count': item.get('engine_count', 1),
                'impact': item.get('impact', ''),
                'tags': item.get('tags', []),
                'description': item.get('description', ''),
                'selector': selector,
                'html': html,
            })

    return json.dumps({
        'url': result.get('url', url),
        'clean': failed == 0,
        'failed': failed,
        'cantTell': cant_tell,
        'elapsed': round(result.get('elapsed', 0), 1),
        'findings': findings,
    }, indent=2)


@mcp.tool()
async def analyze_report(
    report_path: str,
    group_by: str = "wcag",
) -> str:
    """Analyze a previous scan report by grouping findings.

    Groups deduped findings by WCAG SC, engine, best-practice category,
    or other criteria.  Use to prioritize remediation work.

    Args:
        report_path: Path to a .jsonl report file from a previous scan
        group_by: How to group: wcag, engine, bp, rule (default: wcag)

    Returns:
        JSON with grouped findings sorted by count, including SC names
        and example selectors.
    """
    if not os.path.exists(report_path):
        return json.dumps({'error': 'Report not found: ' + report_path})

    groups = {}
    try:
        with open(report_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                for url, data in obj.items():
                    for outcome in (EARL_FAILED, EARL_CANTTELL):
                        for item in data.get(outcome, []):
                            tags = item.get('tags', [])

                            match group_by:
                                case 'wcag':
                                    keys = [t for t in tags
                                            if t.startswith('sc-')]
                                case 'engine':
                                    engines_dict = item.get('engines', {})
                                    if engines_dict:
                                        keys = ['+'.join(
                                            sorted(engines_dict))]
                                    else:
                                        keys = [item.get('engine', '?')]
                                case 'bp':
                                    keys = [t for t in tags
                                            if t.startswith(
                                                ('bp-', 'aria-'))]
                                    if not keys:
                                        keys = [t for t in tags
                                                if t.startswith('sc-')]
                                case _:
                                    keys = [item.get('id', '?')]

                            if not keys:
                                keys = [item.get('id', 'unknown')]

                            for node in item.get('nodes', []):
                                sel = (node.get('target', [''])[0]
                                       if node.get('target')
                                       else '')
                                for key in keys:
                                    if key not in groups:
                                        groups[key] = {
                                            'count': 0,
                                            'pages': set(),
                                            'example_url': url,
                                            'example_selector': sel,
                                        }
                                    groups[key]['count'] += 1
                                    groups[key]['pages'].add(url)
    except Exception as e:
        return json.dumps({'error': str(e)})

    result = []
    for key, info in sorted(groups.items(),
                            key=lambda x: x[1]['count'],
                            reverse=True):
        sc = key.replace('sc-', '') if key.startswith('sc-') else ''
        result.append({
            'key': key,
            'sc_name': sc_name(sc) if sc else '',
            'count': info['count'],
            'pages': len(info['pages']),
            'example_url': info['example_url'],
            'example_selector': info['example_selector'],
        })

    return json.dumps({
        'group_by': group_by,
        'total_findings': sum(g['count'] for g in result),
        'groups': result,
    }, indent=2)


@mcp.tool()
async def list_engines() -> str:
    """List available accessibility engines, versions, and the tag system.

    Returns:
        JSON with engine details (name, version, rule count, install status)
        and the three-tier tag taxonomy (WCAG, ARIA, best-practice).
    """
    engines = []
    checks = [
        ('axe', 'axe-core (Deque)', get_axe_version(), len(AXE_RULES),
         'browser injection',
         os.path.join(NODE_MODULES, 'axe-core', 'axe.min.js')),
        ('ibm', 'IBM Equal Access', '4.0.16', 158,
         'browser injection',
         os.path.join(NODE_MODULES,
                      'accessibility-checker-engine', 'ace.js')),
        ('htmlcs', 'HTML_CodeSniffer', '2.5.1', len(HTMLCS_SNIFFS),
         'browser injection',
         os.path.join(NODE_MODULES,
                      'html_codesniffer', 'build', 'HTMLCS.js')),
        ('alfa', 'Siteimprove Alfa', '0.114.3', len(ALFA_RULES),
         'Node.js subprocess via CDP',
         os.path.join(NODE_MODULES,
                      '@siteimprove', 'alfa-rules')),
    ]
    for name, full, ver, rules, typ, path in checks:
        engines.append({
            'name': name,
            'full_name': full,
            'version': ver,
            'rules': rules,
            'type': typ,
            'installed': os.path.exists(path),
        })

    return json.dumps({
        'engines': engines,
        'tags': {
            'wcag': len(SC_META),
            'aria': list(ARIA_CATEGORIES.keys()),
            'bp': list(BP_CATEGORIES.keys()),
        },
    }, indent=2)


@mcp.tool()
async def lookup_wcag(sc: str) -> str:
    """Look up a WCAG Success Criterion by number.

    Returns the official name, conformance level, WCAG version, and
    which engines have rules that test this criterion.

    Args:
        sc: SC number like '1.4.3' or tag like 'sc-1.4.3'

    Returns:
        JSON with SC details and cross-engine rule mapping.
    """
    sc = sc.replace('sc-', '').strip()
    meta = SC_META.get(sc)
    if not meta:
        return json.dumps({
            'error': 'Unknown SC: ' + sc,
            'hint': 'Use format like 1.4.3 or 2.4.7',
        })

    level, version, name = meta

    tested_by = {}
    for rule_id, (desc, scs, is_bp) in AXE_RULES.items():
        if sc in scs:
            tested_by.setdefault('axe', []).append(rule_id)
    for rule_id, (desc, scs) in ALFA_RULES.items():
        if sc in scs:
            tested_by.setdefault('alfa', []).append(rule_id)
    for rule_id, scs in IBM_SC_MAP.items():
        if sc in scs:
            tested_by.setdefault('ibm', []).append(rule_id)

    return json.dumps({
        'sc': sc,
        'tag': 'sc-' + sc,
        'name': name,
        'level': level,
        'wcag_version': version,
        'tested_by': tested_by,
        'engine_count': len(tested_by),
    }, indent=2)


# ── Report analysis tools ────────────────────────────────────────

from registry import (
    register_scan, get_scan, list_scans, delete_scan,
    search_findings, page_status, diff_scans)


@mcp.tool()
async def find_issues(
    report: str,
    sc: str = "",
    url_pattern: str = "",
    selector_pattern: str = "",
    outcome: str = "",
    engine: str = "",
) -> str:
    """Search a scan report for specific accessibility findings.

    All filters are AND — a finding must match all specified filters.
    Omit a filter to not filter on that field.

    Args:
        report: Path to .jsonl report, or a registered scan name
        sc: WCAG SC filter, e.g. '1.4.3' (omit for all SCs)
        url_pattern: URL glob, e.g. '/groups/*' or '*forum*'
        selector_pattern: Element glob, e.g. '*table*' or '#nav*'
        outcome: 'failed' or 'cantTell' (omit for both)
        engine: Engine name, e.g. 'axe' (omit for all)

    Returns:
        JSON array of matching findings with URL, selector, tags,
        engine attribution.
    """
    jsonl_path = _resolve_report(report)
    if not jsonl_path:
        return json.dumps({'error': 'Report not found: ' + report})

    matches = search_findings(
        jsonl_path,
        sc=sc or None,
        url_pattern=url_pattern or None,
        selector_pattern=selector_pattern or None,
        outcome=outcome or None,
        engine=engine or None)

    return json.dumps({
        'report': jsonl_path,
        'filters': {k: v for k, v in {
            'sc': sc, 'url_pattern': url_pattern,
            'selector_pattern': selector_pattern,
            'outcome': outcome, 'engine': engine,
        }.items() if v},
        'count': len(matches),
        'findings': matches,
    }, indent=2)


@mcp.tool()
async def check_page(
    report: str,
    url: str,
) -> str:
    """Check the accessibility status of a specific page in a report.

    Shows whether the page is clean, what WCAG SCs are failing,
    how many engines agree on each finding, and the full findings list.

    Args:
        report: Path to .jsonl report, or a registered scan name
        url: The page URL to check (exact or path match)

    Returns:
        JSON with clean status, per-SC breakdown, engine agreement,
        and findings list.
    """
    jsonl_path = _resolve_report(report)
    if not jsonl_path:
        return json.dumps({'error': 'Report not found: ' + report})

    result = page_status(jsonl_path, url)
    return json.dumps(result, indent=2)


@mcp.tool()
async def compare_scans(
    old_report: str,
    new_report: str,
) -> str:
    """Compare two scan reports to see what changed.

    Shows fixed findings, new findings, and remaining findings
    with per-SC deltas.

    Args:
        old_report: Path to baseline .jsonl, or registered scan name
        new_report: Path to new .jsonl, or registered scan name

    Returns:
        JSON with summary (fixed/new/remaining counts), per-SC delta,
        and finding details.
    """
    old_path = _resolve_report(old_report)
    new_path = _resolve_report(new_report)
    if not old_path:
        return json.dumps({'error': 'Old report not found: ' + old_report})
    if not new_path:
        return json.dumps({'error': 'New report not found: ' + new_report})

    result = diff_scans(old_path, new_path)
    return json.dumps(result, indent=2)


@mcp.tool()
async def manage_scans(
    action: str = "list",
    name: str = "",
) -> str:
    """Manage the named scan registry.

    Args:
        action: 'list' (default), 'get', or 'delete'
        name: Scan name (required for 'get' and 'delete')

    Returns:
        JSON with scan details or the full registry.
    """
    match action:
        case 'list':
            scans = list_scans()
            entries = [{
                'name': sname,
                'timestamp': info.get('timestamp', ''),
                'url': info.get('url', ''),
                'engines': info.get('engines', []),
                'summary': info.get('summary', {}),
            } for sname, info in sorted(scans.items())]
            return json.dumps({'scans': entries}, indent=2)

        case 'get':
            if not name:
                return json.dumps({'error': 'name required for get'})
            scan = get_scan(name)
            if not scan:
                return json.dumps({'error': f'Scan not found: {name}'})
            return json.dumps({'name': name, **scan}, indent=2)

        case 'delete':
            if not name:
                return json.dumps(
                    {'error': 'name required for delete'})
            removed = delete_scan(name)
            if removed:
                return json.dumps({'deleted': name, 'ok': True})
            return json.dumps({'error': f'Scan not found: {name}'})

        case _:
            return json.dumps({'error': f'Unknown action: {action}'})


def _resolve_report(name_or_path):
    """Resolve a report reference to a JSONL path.

    Accepts:
      - A direct file path (must exist)
      - A registered scan name (looked up in registry)
      - A .json path (converted to .jsonl)
    """
    # Direct path
    if os.path.exists(name_or_path):
        if name_or_path.endswith('.json') and not name_or_path.endswith('.jsonl'):
            jsonl = name_or_path + 'l'
            if os.path.exists(jsonl):
                return jsonl
        return name_or_path

    # Try adding .jsonl
    if os.path.exists(name_or_path + '.jsonl'):
        return name_or_path + '.jsonl'

    # Registry lookup
    scan = get_scan(name_or_path)
    if scan:
        reports = scan.get('reports', {})
        jsonl = reports.get('jsonl', '')
        if jsonl and os.path.exists(jsonl):
            return jsonl
        # Try json → jsonl (with_suffix only swaps the last
        # extension, so paths whose directories contain '.json'
        # are handled correctly).
        jp = reports.get('json', '')
        if jp:
            jsonl = str(Path(jp).with_suffix('.jsonl'))
            if os.path.exists(jsonl):
                return jsonl

    return None


if __name__ == '__main__':
    mcp.run(transport='stdio')
