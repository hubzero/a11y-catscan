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

Site crawls (--max-pages, --workers) are long-running and should be
run via the CLI, not MCP.  Use the /wcag-audit skill or Bash tool.

Usage:
    python3 mcp_server.py                  # start server
    python3 a11y-catscan.py --mcp          # same thing

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
    SC_META, sc_name, sc_level, IBM_SC_MAP,
    EARL_FAILED, EARL_CANTTELL,
    ARIA_CATEGORIES, BP_CATEGORIES)
from engines.axe import AXE_RULES
from engines.alfa import ALFA_RULES
from engines.htmlcs import HTMLCS_SNIFFS

mcp = FastMCP("wcag-audit")


def _get_axe_version():
    """Read axe-core version from the JS file header."""
    axe_path = os.path.join(SCRIPT_DIR, 'node_modules', 'axe-core', 'axe.min.js')
    try:
        with open(axe_path) as f:
            m = re.search(r'axe v([\d.]+)', f.read(200))
            return m.group(1) if m else 'unknown'
    except Exception:
        return 'not installed'


def _read_jsonl_findings(jsonl_path):
    """Read findings from a JSONL file, return flat list of findings."""
    findings = []
    if not os.path.exists(jsonl_path):
        return findings
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            for page_url, data in obj.items():
                for outcome in (EARL_FAILED, EARL_CANTTELL):
                    for item in data.get(outcome, []):
                        selector = ''
                        html = ''
                        if item.get('nodes'):
                            node = item['nodes'][0]
                            selector = (node.get('target', [''])[0]
                                        if node.get('target') else '')
                            html = node.get('html', '')[:200]
                        findings.append({
                            'url': page_url,
                            'outcome': outcome,
                            'id': item.get('id', ''),
                            'engine': item.get('engine', ''),
                            'engines': item.get('engines', {}),
                            'engine_count': item.get('engine_count', 1),
                            'description': item.get('description', ''),
                            'help': item.get('help', ''),
                            'impact': item.get('impact', ''),
                            'tags': item.get('tags', []),
                            'selector': selector,
                            'html': html,
                        })
    return findings


@mcp.tool()
async def scan_page(
    url: str,
    engines: str = "all",
    level: str = "wcag21aa",
) -> str:
    """Scan a single page for WCAG accessibility issues.

    Runs up to four accessibility engines against one URL and returns
    deduped findings with cross-engine confirmation.  Takes 3-17s
    depending on engines selected.

    Args:
        url: The page URL to scan (must start with http:// or https://)
        engines: Comma-separated engines: axe, alfa, ibm, htmlcs, or all (default: all)
        level: WCAG conformance level: wcag21aa (default), wcag21a, wcag21aaa, best

    Returns:
        JSON with summary counts, deduped findings array with CSS selectors
        and engine attribution, and paths to full report files.
    """
    import subprocess
    cmd = [
        sys.executable, os.path.join(SCRIPT_DIR, 'a11y-catscan.py'),
        '--engine', engines,
        '--level', level,
        '--page', '-q', '--summary-json',
        url
    ]
    log.info('scan_page: %s (engines=%s, level=%s)', url, engines, level)
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, cwd=SCRIPT_DIR)

    # Parse summary JSON from the last line of output
    output = result.stdout + result.stderr
    summary = {}
    for line in reversed(output.strip().split('\n')):
        try:
            summary = json.loads(line)
            break
        except (json.JSONDecodeError, ValueError):
            continue

    # Extract report paths from output
    reports = {}
    for line in output.split('\n'):
        if 'JSON report:' in line:
            reports['json'] = line.split('JSON report:')[-1].strip()
        elif 'HTML report:' in line:
            reports['html'] = line.split('HTML report:')[-1].strip()

    # Read detailed findings from the JSONL
    findings = []
    json_path = reports.get('json', '')
    if json_path:
        jsonl_path = json_path.replace('.json', '.jsonl')
        findings = _read_jsonl_findings(jsonl_path)

    return json.dumps({
        'url': url,
        'clean': summary.get('clean', False),
        'failed': summary.get(EARL_FAILED, 0),
        'cantTell': summary.get(EARL_CANTTELL, 0),
        'findings': findings,
        'reports': reports,
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

                            if group_by == 'wcag':
                                keys = [t for t in tags
                                        if t.startswith('wcag-')]
                            elif group_by == 'engine':
                                engines = item.get('engines', {})
                                if engines:
                                    keys = ['+'.join(sorted(engines))]
                                else:
                                    keys = [item.get('engine', '?')]
                            elif group_by == 'bp':
                                keys = [t for t in tags
                                        if t.startswith(('bp-', 'aria-'))]
                                if not keys:
                                    keys = [t for t in tags
                                            if t.startswith('wcag-')]
                            else:
                                keys = [item.get('id', '?')]

                            if not keys:
                                keys = [item.get('id', 'unknown')]

                            for node in item.get('nodes', []):
                                sel = (node.get('target', [''])[0]
                                       if node.get('target') else '')
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
                            key=lambda x: x[1]['count'], reverse=True):
        sc = key.replace('wcag-', '') if key.startswith('wcag-') else ''
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
        ('axe', 'axe-core (Deque)', _get_axe_version(), len(AXE_RULES),
         'browser injection',
         os.path.join(SCRIPT_DIR, 'node_modules', 'axe-core', 'axe.min.js')),
        ('ibm', 'IBM Equal Access', '4.0.16', 158,
         'browser injection',
         os.path.join(SCRIPT_DIR, 'node_modules',
                      'accessibility-checker-engine', 'ace.js')),
        ('htmlcs', 'HTML_CodeSniffer', '2.5.1', len(HTMLCS_SNIFFS),
         'browser injection',
         os.path.join(SCRIPT_DIR, 'node_modules',
                      'html_codesniffer', 'build', 'HTMLCS.js')),
        ('alfa', 'Siteimprove Alfa', '0.114.3', len(ALFA_RULES),
         'Node.js subprocess via CDP',
         os.path.join(SCRIPT_DIR, 'node_modules',
                      '@siteimprove', 'alfa-rules')),
    ]
    for name, full, ver, rules, typ, path in checks:
        installed = os.path.exists(path)
        engines.append({
            'name': name,
            'full_name': full,
            'version': ver,
            'rules': rules,
            'type': typ,
            'installed': installed,
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
        sc: SC number like '1.4.3' or tag like 'wcag-1.4.3'

    Returns:
        JSON with SC details and cross-engine rule mapping.
    """
    sc = sc.replace('wcag-', '').strip()
    meta = SC_META.get(sc)
    if not meta:
        return json.dumps({
            'error': 'Unknown SC: ' + sc,
            'hint': 'Use format like 1.4.3 or 2.4.7',
            'available': len(SC_META),
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
        'tag': 'wcag-' + sc,
        'name': name,
        'level': level,
        'wcag_version': version,
        'tested_by': tested_by,
        'engine_count': len(tested_by),
    }, indent=2)


if __name__ == '__main__':
    mcp.run(transport='stdio')
