#!/usr/bin/env python3
"""
MCP server for a11y-catscan.

Exposes the scanner as Model Context Protocol tools that Claude Code
(or any MCP client) can call directly with structured parameters.

Usage:
    # Start as MCP server (stdio transport)
    python3 mcp_server.py

    # Or via the main script
    python3 a11y-catscan.py --mcp

Configure in .mcp.json or ~/.claude.json:
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
import sys
import logging

# MCP server must log to stderr — stdout is reserved for the protocol.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('a11y-catscan-mcp')

# Ensure we can import from the a11y-catscan directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from mcp.server.fastmcp import FastMCP

from engine_mappings import (
    SC_META, sc_name, sc_level,
    EARL_FAILED, EARL_CANTTELL, EARL_PASSED, EARL_INAPPLICABLE,
    ARIA_CATEGORIES, BP_CATEGORIES)
from engines.axe import AXE_RULES
from engines.alfa import ALFA_RULES
from engines.htmlcs import HTMLCS_SNIFFS

mcp = FastMCP("wcag-audit")


def _get_axe_version():
    """Read axe-core version from the JS file header."""
    import re
    axe_path = os.path.join(SCRIPT_DIR, 'node_modules', 'axe-core', 'axe.min.js')
    try:
        with open(axe_path) as f:
            m = re.search(r'axe v([\d.]+)', f.read(200))
            return m.group(1) if m else 'unknown'
    except Exception:
        return 'not installed'


@mcp.tool()
async def scan_page(
    url: str,
    engines: str = "all",
    level: str = "wcag21aa",
) -> str:
    """Scan a single page for WCAG accessibility issues.

    Runs the specified accessibility engines against one URL and returns
    deduped findings with cross-engine confirmation.

    Args:
        url: The page URL to scan (must start with http:// or https://)
        engines: Comma-separated engine names: axe, alfa, ibm, htmlcs, or all
        level: WCAG conformance level (wcag21a, wcag21aa, wcag21aaa, wcag22aa, best)

    Returns:
        JSON with failed/cantTell counts, deduped findings, and engine attribution.
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

    # The last line of stdout is the summary JSON
    lines = result.stdout.strip().split('\n')
    summary_line = lines[-1] if lines else '{}'
    try:
        summary = json.loads(summary_line)
    except (json.JSONDecodeError, ValueError):
        summary = {}

    # Find the report files from stderr (report paths are printed there)
    report_info = {}
    for line in result.stderr.split('\n') + result.stdout.split('\n'):
        if 'JSON report:' in line:
            report_info['json_report'] = line.split(':', 1)[-1].strip()
        elif 'HTML report:' in line:
            report_info['html_report'] = line.split(':', 1)[-1].strip()

    # Read the full JSON report for detailed findings
    findings = []
    json_path = report_info.get('json_report', '')
    if json_path and os.path.exists(json_path):
        try:
            # Use deduped iterator
            sys.path.insert(0, SCRIPT_DIR)
            # Import inline to avoid circular deps with main script
            exec_globals = {}
            exec("""
import json as _json
from engine_mappings import EARL_FAILED, EARL_CANTTELL

def _iter_jsonl(p):
    with open(p.replace('.json', '.jsonl')) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            for url, data in _json.loads(line).items():
                yield url, data
""", exec_globals)

            jsonl_path = json_path.replace('.json', '.jsonl')
            if os.path.exists(jsonl_path):
                for page_url, data in exec_globals['_iter_jsonl'](jsonl_path):
                    for outcome in (EARL_FAILED, EARL_CANTTELL):
                        for item in data.get(outcome, []):
                            findings.append({
                                'url': page_url,
                                'outcome': outcome,
                                'id': item.get('id', ''),
                                'engine': item.get('engine', ''),
                                'engines': item.get('engines', {}),
                                'description': item.get('description', ''),
                                'help': item.get('help', ''),
                                'impact': item.get('impact', ''),
                                'tags': item.get('tags', []),
                                'selector': (item.get('nodes', [{}])[0]
                                             .get('target', [''])[0]
                                             if item.get('nodes') else ''),
                                'html': (item.get('nodes', [{}])[0]
                                         .get('html', '')[:200]
                                         if item.get('nodes') else ''),
                            })
        except Exception as e:
            log.warning('Failed to read findings: %s', e)

    return json.dumps({
        'summary': summary,
        'reports': report_info,
        'findings': findings,
        'exit_code': result.returncode,
    }, indent=2)


@mcp.tool()
async def scan_site(
    url: str,
    engines: str = "axe",
    level: str = "wcag21aa",
    max_pages: int = 50,
    workers: int = 1,
) -> str:
    """Crawl and scan a website for WCAG accessibility issues.

    Discovers pages by following links from the starting URL, scanning
    each page with the specified engines.  Use for full site audits.

    Args:
        url: Starting URL to crawl from
        engines: Comma-separated engine names: axe, alfa, ibm, htmlcs, or all
        level: WCAG conformance level
        max_pages: Maximum number of pages to scan (default 50)
        workers: Parallel browser pages (default 1, try 7 for speed)

    Returns:
        JSON with page count, total findings, report file paths,
        and per-SC summary.
    """
    import subprocess
    cmd = [
        sys.executable, os.path.join(SCRIPT_DIR, 'a11y-catscan.py'),
        '--engine', engines,
        '--level', level,
        '--max-pages', str(max_pages),
        '--workers', str(workers),
        '-q', '--summary-json', '--llm',
        url
    ]
    log.info('scan_site: %s (engines=%s, max_pages=%d, workers=%d)',
             url, engines, max_pages, workers)
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=max_pages * 30,  # ~30s per page worst case
        cwd=SCRIPT_DIR)

    lines = result.stdout.strip().split('\n')
    summary_line = lines[-1] if lines else '{}'
    try:
        summary = json.loads(summary_line)
    except (json.JSONDecodeError, ValueError):
        summary = {}

    report_info = {}
    for line in result.stderr.split('\n') + result.stdout.split('\n'):
        if 'JSON report:' in line:
            report_info['json_report'] = line.split(':', 1)[-1].strip()
        elif 'HTML report:' in line:
            report_info['html_report'] = line.split(':', 1)[-1].strip()
        elif 'LLM report:' in line or '.md' in line:
            path = line.split(':', 1)[-1].strip() if ':' in line else ''
            if path.endswith('.md'):
                report_info['llm_report'] = path

    return json.dumps({
        'summary': summary,
        'reports': report_info,
        'exit_code': result.returncode,
    }, indent=2)


@mcp.tool()
async def analyze_report(
    report_path: str,
    group_by: str = "wcag",
) -> str:
    """Analyze a previous scan report by grouping findings.

    Args:
        report_path: Path to a .jsonl report file from a previous scan
        group_by: How to group: wcag, rule, selector, color, reason, level, engine, bp

    Returns:
        JSON with grouped findings, counts, and examples.
    """
    import subprocess
    if not os.path.exists(report_path):
        return json.dumps({'error': 'Report not found: ' + report_path})

    # We need a URL to satisfy the positional arg — use a dummy
    # and feed the JSONL via --urls wouldn't work, use the report directly
    # Actually --group-by works on the scan output, so we need to run
    # a scan that reads from the report. But we can just parse the JSONL.

    findings_by_group = {}
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
                                if not keys:
                                    keys = [item.get('id', 'unknown')]
                            elif group_by == 'engine':
                                engines = item.get('engines', {})
                                if engines:
                                    keys = ['+'.join(sorted(engines))]
                                else:
                                    keys = [item.get('engine', '?')]
                            elif group_by == 'bp':
                                keys = [t for t in tags
                                        if t.startswith('bp-')]
                                if not keys:
                                    keys = [t for t in tags
                                            if t.startswith('wcag-')]
                                if not keys:
                                    keys = [item.get('id', '?')]
                            else:
                                keys = [item.get('id', 'unknown')]

                            for node in item.get('nodes', []):
                                for key in keys:
                                    if key not in findings_by_group:
                                        findings_by_group[key] = {
                                            'count': 0,
                                            'pages': set(),
                                            'example_url': url,
                                            'example_selector': (
                                                node.get('target', [''])[0]
                                                if node.get('target')
                                                else ''),
                                        }
                                    findings_by_group[key]['count'] += 1
                                    findings_by_group[key]['pages'].add(url)
    except Exception as e:
        return json.dumps({'error': str(e)})

    # Convert sets to counts for JSON serialization
    groups = []
    for key, info in sorted(
            findings_by_group.items(),
            key=lambda x: x[1]['count'], reverse=True):
        sc = key.replace('wcag-', '') if key.startswith('wcag-') else ''
        groups.append({
            'key': key,
            'sc_name': sc_name(sc) if sc else '',
            'count': info['count'],
            'pages': len(info['pages']),
            'example_url': info['example_url'],
            'example_selector': info['example_selector'],
        })

    return json.dumps({
        'group_by': group_by,
        'total_findings': sum(g['count'] for g in groups),
        'total_groups': len(groups),
        'groups': groups,
    }, indent=2)


@mcp.tool()
async def list_engines() -> str:
    """List available accessibility engines and their versions.

    Returns:
        JSON with engine names, versions, rule counts, and installation status.
    """
    engines = []

    # axe-core
    axe_ver = _get_axe_version()
    engines.append({
        'name': 'axe',
        'full_name': 'axe-core (Deque)',
        'version': axe_ver,
        'rules': len(AXE_RULES),
        'installed': axe_ver != 'not installed',
        'type': 'browser injection',
    })

    # IBM
    ace_path = os.path.join(SCRIPT_DIR, 'node_modules',
                            'accessibility-checker-engine', 'ace.js')
    engines.append({
        'name': 'ibm',
        'full_name': 'IBM Equal Access',
        'version': '4.0.16',
        'rules': 158,
        'installed': os.path.exists(ace_path),
        'type': 'browser injection',
    })

    # HTMLCS
    htmlcs_path = os.path.join(SCRIPT_DIR, 'node_modules',
                               'html_codesniffer', 'build', 'HTMLCS.js')
    engines.append({
        'name': 'htmlcs',
        'full_name': 'HTML_CodeSniffer',
        'version': '2.5.1',
        'rules': len(HTMLCS_SNIFFS),
        'installed': os.path.exists(htmlcs_path),
        'type': 'browser injection',
    })

    # Alfa
    alfa_path = os.path.join(SCRIPT_DIR, 'node_modules',
                             '@siteimprove', 'alfa-rules')
    engines.append({
        'name': 'alfa',
        'full_name': 'Siteimprove Alfa',
        'version': '0.114.3',
        'rules': len(ALFA_RULES),
        'installed': os.path.isdir(alfa_path),
        'type': 'Node.js subprocess via CDP',
    })

    return json.dumps({
        'engines': engines,
        'tag_system': {
            'wcag_tags': len(SC_META),
            'aria_categories': list(ARIA_CATEGORIES.keys()),
            'bp_categories': list(BP_CATEGORIES.keys()),
        },
    }, indent=2)


@mcp.tool()
async def lookup_wcag(sc: str) -> str:
    """Look up a WCAG Success Criterion by number.

    Args:
        sc: Success Criterion number like '1.4.3' or 'wcag-1.4.3'

    Returns:
        JSON with SC name, level, WCAG version, and which engines test it.
    """
    sc = sc.replace('wcag-', '').strip()
    meta = SC_META.get(sc)
    if not meta:
        return json.dumps({'error': 'Unknown SC: ' + sc,
                           'hint': 'Use format like 1.4.3 or 2.4.7'})

    level, version, name = meta

    # Which engines test this SC?
    tested_by = {}
    for rule_id, (desc, scs, is_bp) in AXE_RULES.items():
        if sc in scs:
            tested_by.setdefault('axe', []).append(rule_id)
    for rule_id, (desc, scs) in ALFA_RULES.items():
        if sc in scs:
            tested_by.setdefault('alfa', []).append(rule_id)
    from engine_mappings import IBM_SC_MAP
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
