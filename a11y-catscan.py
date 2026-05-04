#!/usr/bin/env python3
"""
a11y-catscan - WCAG accessibility scanner using axe-core, Playwright, and Chromium.

Crawls a website and runs axe-core accessibility checks on each page,
producing HTML and JSON reports.

Usage:
    a11y-catscan.py [OPTIONS] START_URL

Examples:
    # Full crawl scan
    a11y-catscan.py https://example.com/
    a11y-catscan.py --max-pages 500 --llm https://example.com/

    # Quick single-page check after a fix
    a11y-catscan.py --page -q --summary-json https://example.com/fixed-page

    # Re-scan only pages that failed previously
    a11y-catscan.py --rescan previous.jsonl --diff previous.jsonl --llm

    # Check just contrast issues
    a11y-catscan.py --page --rule color-contrast https://example.com/page

    # Scan a specific list of URLs
    a11y-catscan.py --urls pages.txt --llm

Exit codes: 0 = no failures, 1 = failures found.
"""

import argparse
import atexit
import json
import os
import re
import sys
import time

from engine_mappings import (
    SC_META, sc_level, sc_name, parse_wcag_sc,
    EARL_FAILED, EARL_CANTTELL, EARL_PASSED, EARL_INAPPLICABLE)

# Engine classes and Scanner
from engines import AxeEngine, IbmEngine, HtmlcsEngine, AlfaEngine
from engines.axe import get_axe_version
from scanner import (
    Scanner, WCAG_LEVELS, DEFAULT_LEVEL, HTML_TYPES)
from results import count_nodes, dedup_page
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

# Required dependencies.  Catch ImportError here rather than letting
# Python's traceback confuse users who haven't installed them.
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed.", file=sys.stderr)
    print("  Install it with:  pip install pyyaml", file=sys.stderr)
    sys.exit(2)

# All supporting files (config, node_modules) live alongside this script.
# This lets the tool work as a self-contained directory you can clone
# and run from anywhere without installation.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NODE_MODULES = os.path.join(SCRIPT_DIR, 'node_modules')
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, 'a11y-catscan.yaml')

# URL / HTTP / rate-limit utilities live in crawl_utils.py.  Import
# the public names so existing call sites keep working.
import crawl_utils
from crawl_utils import (
    SKIP_EXTENSIONS,
    RateLimiter,
    load_cookies,
    normalize_url,
    is_same_origin,
    load_robots_txt,
    should_scan,
    http_status,
)

# WCAG_LEVELS and DEFAULT_LEVEL imported from scanner.py


# Re-export the helper modules' public surface so tests and external
# integrations that drive the CLI by importing this script continue
# to find them all in one place.  The underscore-prefixed alias
# layer this used to carry was a refactor artefact and is gone.
from allowlist import (load_allowlist, matches_allowlist,
                       classify_page)
from crawl import crawl_and_scan  # noqa: F401
from crawl_utils import safe_int, register_browser_pid, cleanup_browsers
from report_io import (iter_jsonl, iter_report, iter_deduped,
                       extract_urls_from_report)
from report_html import generate_html_report  # noqa: F401
from report_llm import generate_llm_report  # noqa: F401
from report_group import group_results
from report_diff import print_diff  # noqa: F401


def load_config(config_path=None):
    """Load site configuration from YAML file.

    Returns a dict with config values.  Missing keys get sensible defaults.
    """
    config = {}
    path = config_path or DEFAULT_CONFIG_PATH
    if os.path.exists(path):
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    return config


def main():
    parser = argparse.ArgumentParser(
        description='Scan a website for WCAG accessibility violations using axe-core.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('url', nargs='?', default=None,
                        help='Starting URL to scan')
    parser.add_argument('--config', default=None,
                        help='Path to YAML config file (default: a11y-catscan.yaml alongside script)')
    parser.add_argument('--level', default=None,
                        choices=sorted(WCAG_LEVELS.keys()),
                        help='WCAG conformance level (default: wcag21aa)')
    parser.add_argument('--max-pages', type=int, default=None,
                        help='Maximum pages to scan (default: 50)')
    parser.add_argument('--tags', default=None,
                        help='Comma-separated axe-core tags (overrides --level)')
    parser.add_argument('--include-path', action='append', default=None,
                        help='Only scan URLs starting with this prefix (repeatable)')
    parser.add_argument('--exclude-path', action='append', default=None,
                        help='Skip URLs starting with this prefix (repeatable, adds to config)')
    parser.add_argument('--no-default-excludes', action='store_true',
                        help='Ignore exclude_paths from config file')
    parser.add_argument('--ignore-robots', action='store_true',
                        help='Ignore robots.txt (by default, disallowed paths are skipped)')
    parser.add_argument('--name', '--output', default=None, dest='output',
                        help='Job name used as the basename for all output files '
                             '(default: a11y-catscan-YYYY-MM-DD-HHMMSS)')
    parser.add_argument('--output-dir', default=None,
                        help='Output directory (default: from config or current directory)')
    parser.add_argument('--allowlist', default=None,
                        help='YAML file of known-acceptable incompletes to suppress')
    parser.add_argument('--workers', type=int, default=None,
                        help='Number of parallel browser instances (default: 1). '
                             'Each uses ~200-500MB RAM. Rate limits are shared.')
    parser.add_argument('--wait-until', default=None,
                        choices=['networkidle', 'load', 'domcontentloaded', 'commit'],
                        help='Page load strategy (default: networkidle). '
                             'networkidle waits for no network activity for 500ms. '
                             'load uses the traditional load event + page_wait delay.')
    parser.add_argument('--engine', default=None,
                        metavar='ENGINE[,ENGINE,...]',
                        help='Accessibility engines, comma-separated (default: axe). '
                             'Engines: axe, alfa, ibm, htmlcs, all. '
                             'Example: --engine axe,alfa')
    parser.add_argument('--save-every', type=int, default=None,
                        help='Flush reports every N pages (default: 25). '
                             'Partial results survive if the scan is killed.')
    parser.add_argument('--diff', default=None, metavar='PREV.jsonl',
                        help='Compare against a previous scan JSONL and show what changed')
    parser.add_argument('--urls', default=None, metavar='FILE',
                        help='Scan URLs from a file (one per line) instead of crawling')
    parser.add_argument('--rescan', default=None, metavar='PREV.jsonl',
                        help='Re-scan only pages that had violations or incompletes in a previous scan')
    parser.add_argument('--violations-from', default=None, metavar='REPORT',
                        help='Extract and re-scan only pages with violations from a previous JSON or JSONL report')
    parser.add_argument('--incompletes-from', default=None, metavar='REPORT',
                        help='Extract and re-scan only pages with incompletes from a previous JSON or JSONL report')
    parser.add_argument('--group-by', default=None,
                        choices=['rule', 'selector', 'color', 'reason',
                                 'wcag', 'level', 'engine', 'bp'],
                        help='After scanning, print a grouped summary. '
                             'rule: by axe rule ID. selector: by CSS selector pattern. '
                             'color: by foreground/background color pair. '
                             'reason: by incomplete reason category. '
                             'wcag: by WCAG success criterion.')
    parser.add_argument('--resume', default=None, metavar='STATE.json',
                        help='Resume a previous crawl from its saved state file')
    parser.add_argument('--rule', action='append', default=None,
                        help='Only run specific axe rules (repeatable, e.g. --rule color-contrast)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--page', action='store_true',
                       help='Scan only the given URL (no crawling). Fast single-page verify.')
    group.add_argument('--crawl', action='store_true', default=True,
                       help='Crawl and discover pages from the starting URL (default).')
    parser.add_argument('--llm', action='store_true',
                        help='Generate a compact markdown summary optimized for LLM context')
    parser.add_argument('--summary-json', action='store_true',
                        help='Print a one-line JSON summary to stdout (machine-parseable)')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Suppress per-page progress, only show final summary')
    parser.add_argument('--help-audit', action='store_true',
                        help='Print a guide for using this tool to perform a WCAG audit')
    parser.add_argument('--cleanup', action='store_true',
                        help='Kill orphaned chromium processes from previous runs and exit')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show detailed rule/node counts for pages with issues')

    # Report analysis flags (no scan needed — operate on previous reports)
    report_group = parser.add_argument_group('report analysis')
    report_group.add_argument('--list-scans', action='store_true',
                              help='List all registered scan names and exit')
    report_group.add_argument('--page-status', default=None, metavar='URL',
                              help='Check a specific URL in the latest scan '
                                   '(or use --name to specify which scan)')
    report_group.add_argument('--search', default=None, metavar='SC_OR_PATTERN',
                              help='Search findings in a report. Prefix with '
                                   'sc: for WCAG SC (sc:1.4.3), url: for URL '
                                   'pattern (url:/groups/*), sel: for selector '
                                   '(sel:*table*), or engine: (engine:axe)')

    args = parser.parse_args()

    config = load_config(args.config)

    # Analysis-mode dispatch — these handlers all sys.exit().
    from cli_modes import (
        cmd_cleanup, cmd_list_scans, cmd_page_status,
        cmd_search, cmd_help_audit)
    if args.cleanup:
        cmd_cleanup()
    if args.list_scans:
        cmd_list_scans()
    if args.page_status:
        cmd_page_status(args, config)
    if args.search:
        cmd_search(args, config)
    if args.help_audit:
        cmd_help_audit()

    # Load config
    config = load_config(args.config)

    # Resolve URL: command line > config > error
    url = args.url or config.get('url')
    if not url:
        parser.error('No URL specified. Provide a URL argument or set "url" in config.')
    if not url.startswith('http'):
        url = 'https://' + url

    # Resolve tags/level
    tags = None
    if args.tags:
        tags = [t.strip() for t in args.tags.split(',')]
    level = args.level or config.get('level', DEFAULT_LEVEL)
    level_info = WCAG_LEVELS.get(level, {})
    level_label = level_info.get('label', 'Custom') if not args.tags else 'Custom tags'

    # Load URL list from file or previous scan
    seed_urls = None
    if args.rescan:
        if not os.path.exists(args.rescan):
            parser.error(f'Rescan file not found: {args.rescan}')
        seed_urls = []
        for prev_url, prev_data in iter_jsonl(args.rescan):
            if prev_data.get(EARL_FAILED) or prev_data.get(EARL_CANTTELL):
                seed_urls.append(prev_url)
        if not seed_urls:
            print("No failures in previous scan — nothing to rescan.")
            sys.exit(0)
        print(f"Rescanning {len(seed_urls)} pages with previous failures")
    if args.violations_from:
        if not os.path.exists(args.violations_from):
            parser.error(f'Report not found: {args.violations_from}')
        seed_urls = extract_urls_from_report(args.violations_from, EARL_FAILED)
        if not seed_urls:
            print("No failures in previous report — nothing to rescan.")
            sys.exit(0)
        print(f"Rescanning {len(seed_urls)} pages with previous failures")
        if not url:
            url = seed_urls[0]
    if args.incompletes_from:
        if not os.path.exists(args.incompletes_from):
            parser.error(f'Report not found: {args.incompletes_from}')
        seed_urls = extract_urls_from_report(args.incompletes_from, EARL_CANTTELL)
        if not seed_urls:
            print("No incompletes in previous report — nothing to rescan.")
            sys.exit(0)
        print(f"Rescanning {len(seed_urls)} pages with previous incompletes")
        if not url:
            url = seed_urls[0]
    if args.urls:
        if not os.path.exists(args.urls):
            parser.error(f'URL file not found: {args.urls}')
        with open(args.urls) as f:
            seed_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        if not seed_urls:
            parser.error(f'No URLs found in {args.urls}')
        if not url:
            url = seed_urls[0]

    # Resolve max pages
    if args.page:
        max_pages = 1
    elif seed_urls:
        max_pages = len(seed_urls)
    else:
        max_pages = args.max_pages or safe_int(config.get('max_pages', 50), 50)

    # Resolve exclude paths: config defaults + CLI additions
    exclude_paths = []
    if not args.no_default_excludes:
        exclude_paths.extend(config.get('exclude_paths', []))
    if args.exclude_path:
        for p in args.exclude_path:
            if p not in exclude_paths:
                exclude_paths.append(p)

    # Resolve include paths: CLI only (config can set defaults)
    include_paths = args.include_path or config.get('include_paths')

    # Resolve exclude regex from config
    exclude_regex = None
    regex_list = config.get('exclude_regex', [])
    if regex_list and not args.no_default_excludes:
        exclude_regex = []
        for pattern in regex_list:
            try:
                exclude_regex.append(re.compile(pattern))
            except re.error as e:
                print(f"WARNING: invalid exclude_regex '{pattern}': {e}",
                      file=sys.stderr)

    # Query parameters to strip from URLs during normalization.
    # This deduplicates sort/filter/pagination variants of the same
    # page.  Entries can be plain strings (global) or dicts with
    # path + params for path-conditional stripping.
    strip_list = config.get('strip_query_params', [])
    if isinstance(strip_list, str):
        strip_list = [s.strip() for s in strip_list.split(',')]
    strip_global = set()
    strip_path_rules = []
    for entry in strip_list:
        if isinstance(entry, str):
            strip_global.add(entry)
        elif isinstance(entry, dict) and 'path' in entry:
            params = entry.get('querystring', entry.get('params', []))
            if isinstance(params, str):
                params = [p.strip() for p in params.split(',')]
            try:
                strip_path_rules.append(
                    (re.compile(entry['path']), set(params)))
            except re.error as e:
                print("WARNING: invalid strip_query_params path "
                      "regex '{}': {}".format(entry['path'], e),
                      file=sys.stderr)
    crawl_utils.configure_strip_rules(strip_global, strip_path_rules)

    # Resolve output
    save_every = args.save_every or safe_int(config.get('save_every', 25), 25)

    # Workers: number of parallel browser instances
    if args.workers:
        config['workers'] = args.workers
    if args.wait_until:
        config['wait_until'] = args.wait_until
    if args.engine:
        engines = []
        for e in args.engine.split(','):
            e = e.strip()
            if e == 'all':
                engines = ['axe', 'alfa', 'ibm', 'htmlcs']
                break
            elif e in ('axe', 'alfa', 'ibm', 'htmlcs'):
                engines.append(e)
            else:
                parser.error("Unknown engine: {}. "
                    "Choose from: axe, alfa, ibm, htmlcs, all"
                    .format(e))
        config['engines'] = engines
    basename = args.output or 'a11y-catscan-{}'.format(datetime.now().strftime('%Y-%m-%d-%H%M%S'))
    if (os.path.isabs(basename)
            or basename != os.path.basename(basename)
            or basename in ('', '.', '..')):
        parser.error('--name/--output must be a filename, not a path')
    output_dir = args.output_dir or config.get('output_dir', os.getcwd())
    os.makedirs(output_dir, exist_ok=True)

    # Load allowlist
    allowlist_path = args.allowlist or config.get('allowlist')
    allowlist = load_allowlist(allowlist_path) if allowlist_path else []
    if allowlist:
        print(f"Allowlist: {len(allowlist)} entries from {allowlist_path}")

    # Load robots.txt unless told to ignore it.
    # By default we respect robots.txt — it's polite and often excludes
    # the same paths we'd want to skip anyway (admin, API, login, etc.).
    ignore_robots = args.ignore_robots or config.get('ignore_robots') in (
        True, 'true', 'yes', '1')
    robots_parser = None
    if not ignore_robots:
        robots_parser = load_robots_txt(url)
        if robots_parser and not args.quiet:
            print("Respecting robots.txt (use --ignore-robots to override)")

    html_path = os.path.join(output_dir, basename + '.html')
    json_path = os.path.join(output_dir, basename + '.json')

    # Load saved crawl state for --resume
    resume_state = None
    if args.resume:
        try:
            with open(args.resume) as f:
                resume_state = json.load(f)
            if not args.quiet:
                print(f"Resuming from: {args.resume}")
        except Exception as e:
            print(f"ERROR: cannot load state file: {e}",
                  file=sys.stderr)
            sys.exit(2)

    (scanned, jsonl_path, wall_time, total_page_time,
     totals) = crawl_and_scan(
        url,
        max_pages=max_pages,
        tags=tags,
        rules=args.rule,
        level=args.level,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        exclude_regex=exclude_regex,
        verbose=args.verbose,
        quiet=args.quiet,
        config=config,
        json_path=json_path,
        html_path=html_path,
        save_every=save_every,
        level_label=level_label,
        allowlist=allowlist,
        seed_urls=seed_urls,
        robots_parser=robots_parser,
        resume_state=resume_state,
    )

    # Final reports already flushed by crawl_and_scan
    print(f"\nJSON report: {json_path}")
    print(f"HTML report: {html_path}")

    if args.llm and jsonl_path and os.path.exists(jsonl_path):
        llm_path = os.path.join(output_dir, basename + '.md')
        generate_llm_report(jsonl_path, llm_path, url,
                            level_label=level_label, allowlist=allowlist,
                            config=config)
        print(f"LLM report: {llm_path}")

    # Summary totals were accumulated during the crawl by _write_page;
    # no second pass over the JSONL needed.  --level controls whether
    # bp-*/aria-* failures count toward the compliance number.
    scan_level_used = args.level or config.get('level', DEFAULT_LEVEL)
    include_bp_in_count = (scan_level_used == 'best')
    total_wcag_failed = totals.wcag
    total_aria_failed = totals.aria
    total_bp_failed = totals.bp
    total_incomplete = totals.incomplete
    violation_rules = totals.rules

    # Compliance count: WCAG only, unless --level best
    compliance_failed = total_wcag_failed
    if include_bp_in_count:
        compliance_failed += total_bp_failed

    throughput = (wall_time / scanned) if scanned else 0
    print("\nScan complete: {} pages in {:.1f}s ({:.1f}s/page)".format(
        scanned, wall_time, throughput))
    print(f"  WCAG failed: {total_wcag_failed} node(s)")
    if total_aria_failed:
        print(f"  ARIA: {total_aria_failed} node(s)")
    if total_bp_failed:
        print(f"  Best practice: {total_bp_failed} node(s)")
    print("  Can't tell: {} node(s) needing manual review".format(
        total_incomplete))

    if args.summary_json:
        summary = {
            'pages': scanned,
            EARL_FAILED: compliance_failed,
            'wcag_failed': total_wcag_failed,
            'aria_failed': total_aria_failed,
            'bp_failed': total_bp_failed,
            EARL_CANTTELL: total_incomplete,
            'rules': sorted(violation_rules),
            'clean': compliance_failed == 0,
        }
        print(json.dumps(summary))

    # Register scan in the named scan registry
    if basename and jsonl_path:
        from registry import register_scan
        report_paths = {
            'json': json_path,
            'jsonl': jsonl_path,
            'html': html_path,
        }
        register_scan(
            name=basename,
            report_paths=report_paths,
            url=url,
            engines=config.get('engines', ['axe']),
            summary={
                'pages': scanned,
                EARL_FAILED: compliance_failed,
                EARL_CANTTELL: total_incomplete,
                'clean': compliance_failed == 0,
            })

    # Diff against previous scan
    if args.diff and jsonl_path and os.path.exists(jsonl_path):
        if os.path.exists(args.diff):
            print(f"\nDiff vs {args.diff}:")
            print_diff(args.diff, jsonl_path, allowlist=allowlist)
        else:
            print(f"\nWARNING: diff file not found: {args.diff}")

    # Group-by summary
    if args.group_by and jsonl_path and os.path.exists(jsonl_path):
        group_results(jsonl_path, args.group_by, allowlist=allowlist)

    # Exit code: 0 = clean, 1 = violations found
    if compliance_failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    if '--mcp' in sys.argv:
        # Start as MCP server (Model Context Protocol) for Claude Code.
        # Exposes scan_page, scan_site, analyze_report, list_engines,
        # lookup_wcag as structured tools over stdio transport.
        from mcp_server import mcp as _mcp_server
        _mcp_server.run(transport='stdio')
    else:
        main()
