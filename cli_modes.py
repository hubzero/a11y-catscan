"""Analysis-mode handlers for the CLI.

These functions implement the no-scan-needed modes that operate on
previously-saved reports:

  - cmd_cleanup     — kill orphaned chromium processes
  - cmd_list_scans  — list registered scans
  - cmd_page_status — check whether a URL is clean in the latest scan
  - cmd_search      — search findings by SC / URL / selector / engine
  - cmd_help_audit  — print the audit guide

Each handler exits via sys.exit() with an appropriate status code
(0 = success, 1 = expected failure, 2 = usage error).
"""

import os
import re
import signal
import subprocess
import sys

from engine_mappings import EARL_FAILED


def cmd_cleanup():
    """--cleanup: kill orphaned chromium processes and exit."""
    killed = 0
    try:
        result = subprocess.run(
            ['ps', '-u', str(os.getuid()), '-o', 'pid,comm'],
            capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                try:
                    pid = int(parts[0])
                    comm = parts[1]
                    if pid != os.getpid() and 'chrome' in comm:
                        os.kill(pid, signal.SIGKILL)
                        killed += 1
                except (ValueError, ProcessLookupError,
                        PermissionError):
                    pass
    except Exception:
        pass
    print(f"Killed {killed} orphaned browser process(es).")
    sys.exit(0)


def cmd_list_scans():
    """--list-scans: print the registered scan registry."""
    from registry import list_scans
    scans = list_scans()
    if not scans:
        print("No registered scans.")
    else:
        print(f"{len(scans)} registered scan(s):\n")
        for sname, info in sorted(scans.items()):
            summary = info.get('summary', {})
            status = ('clean' if summary.get('clean')
                      else '{} failed'.format(
                          summary.get(EARL_FAILED, '?')))
            print("  {:<25s} {} — {} pages, {} "
                  "[{} engine(s)]".format(
                      sname,
                      info.get('timestamp', '')[:19],
                      summary.get('pages', '?'),
                      status,
                      len(info.get('engines', []))))
            reports = info.get('reports', {})
            if reports.get('jsonl'):
                print("    {}".format(reports['jsonl']))
    sys.exit(0)


def _resolve_report_jsonl(args_output, config):
    """Find the JSONL path for analysis modes.

    Prefers --name (args.output → output_dir/<name>.jsonl), falls
    back to the most recent registered scan.  Returns the path
    string, or None if no report is available.
    """
    from registry import list_scans
    if args_output:
        candidate = os.path.join(
            config.get('output_dir', '.'),
            args_output + '.jsonl')
        if os.path.exists(candidate):
            return candidate
    scans = list_scans()
    if scans:
        latest = max(
            scans.items(),
            key=lambda x: x[1].get('timestamp', ''))
        reports = latest[1].get('reports', {})
        jsonl = reports.get('jsonl', '')
        if jsonl and os.path.exists(jsonl):
            return jsonl
    return None


def cmd_page_status(args, config):
    """--page-status: check whether a URL is clean in a report."""
    from registry import page_status
    jsonl_path = _resolve_report_jsonl(args.output, config)
    if not jsonl_path:
        print("No scan report found. "
              "Run a scan first or specify --name.")
        sys.exit(1)
    result = page_status(jsonl_path, args.page_status)
    if not result.get('found'):
        print(f"URL not found in report: {args.page_status}")
        sys.exit(1)
    status = 'CLEAN' if result['clean'] else 'FAILING'
    print("{} — {} ({} failed, {} cantTell)".format(
        result['url'], status, result['failed'],
        result['cantTell']))
    if result.get('sc_breakdown'):
        print("\n  WCAG SCs:")
        for sc_id, info in sorted(result['sc_breakdown'].items()):
            parts = []
            if info['failed']:
                parts.append('{} failed'.format(info['failed']))
            if info['cantTell']:
                parts.append('{} cantTell'.format(info['cantTell']))
            print("    {} {} — {}".format(
                sc_id, info['name'], ', '.join(parts)))
    if result.get('findings'):
        print("\n  Findings:")
        for f in result['findings'][:20]:
            engines = f.get('engines', {})
            eng_str = ('+'.join(sorted(engines))
                       if engines else '?')
            print("    [{}] {} — {} — {}".format(
                f['outcome'][:6], f['id'], eng_str,
                f['selector'][:50]))
    sys.exit(0 if result['clean'] else 1)


_SEARCH_PREFIXES = {
    'sc:': 'sc',
    'url:': 'url_pattern',
    'sel:': 'selector_pattern',
    'engine:': 'engine',
    'outcome:': 'outcome',
}


def cmd_search(args, config):
    """--search: query findings in a report.

    Query format:
        sc:1.4.3       — WCAG SC number
        url:/admin/*   — URL substring match (glob)
        sel:*table*    — CSS selector glob
        engine:axe     — engine name
        outcome:failed — EARL outcome
        1.4.3          — bare SC number is treated as `sc:`
        anything-else  — treated as a selector glob
    """
    from registry import search_findings
    query = args.search
    sc = url_pat = sel_pat = outcome_filter = eng_filter = None

    for prefix, kind in _SEARCH_PREFIXES.items():
        if query.startswith(prefix):
            value = query[len(prefix):]
            if kind == 'sc':
                sc = value
            elif kind == 'url_pattern':
                url_pat = value
            elif kind == 'selector_pattern':
                sel_pat = value
            elif kind == 'engine':
                eng_filter = value
            elif kind == 'outcome':
                outcome_filter = value
            break
    else:
        # No prefix: bare SC number → sc:, anything else → selector
        if re.match(r'^\d+\.\d+\.\d+$', query):
            sc = query
        else:
            sel_pat = f'*{query}*'

    jsonl_path = _resolve_report_jsonl(args.output, config)
    if not jsonl_path:
        print("No scan report found.")
        sys.exit(1)

    matches = search_findings(
        jsonl_path, sc=sc, url_pattern=url_pat,
        selector_pattern=sel_pat, outcome=outcome_filter,
        engine=eng_filter)
    print("{} finding(s) matching '{}':\n".format(
        len(matches), args.search))
    for m in matches[:50]:
        engines = m.get('engines', {})
        eng_str = ('+'.join(sorted(engines))
                   if engines else m.get('engine', '?'))
        print("  [{}] {} — {} — {}".format(
            m['outcome'][:6], m['id'], eng_str,
            m['selector'][:50]))
        print("    {}".format(m['url']))
    if len(matches) > 50:
        print(f"\n  ... and {len(matches) - 50} more")
    sys.exit(0)


_HELP_AUDIT_TEXT = """
WCAG Accessibility Audit Guide
===============================

You are a WCAG accessibility auditor. Use a11y-catscan to scan websites for
WCAG 2.1 AA compliance violations and then fix them in the source code.

AUDIT WORKFLOW
--------------
1. SCAN: Run a full crawl to establish a baseline.
     a11y-catscan.py --max-pages 500 --llm https://example.com/
   Read the .md (LLM report) for a concise summary of issues.

2. PRIORITIZE: Fix violations first (WCAG failures), then incompletes.
   Violations are grouped by rule — fix the rule with the most instances
   first for maximum impact.

3. FIX: For each violation, find the template/CSS that generates the
   flagged HTML. Common fixes:
   - color-contrast: darken text or lighten background to reach 4.5:1
   - missing alt text: add descriptive alt attributes to images
   - missing labels: add <label> or aria-label to form controls
   - empty headings/links: add text content or aria-label
   - focus visible: add :focus outline styles

4. VERIFY: After each fix, re-check the specific page:
     a11y-catscan.py --page -q --summary-json https://example.com/fixed-page
   Check exit code: 0 = clean, 1 = still has violations.

5. REGRESSION CHECK: Re-scan previous failures to confirm fixes:
     a11y-catscan.py --rescan baseline.jsonl --diff baseline.jsonl --llm
   The diff shows what was fixed vs what's new vs what remains.

6. SUPPRESS KNOWN ISSUES: For axe-core limitations that aren't real
   accessibility problems (e.g. can't compute contrast on gradients),
   add entries to an allowlist.yaml:
     - rule: color-contrast
       url: /homepage
       reason: axe-core flex layout measurement limitation

UNDERSTANDING RESULTS
---------------------
- VIOLATIONS: Definite WCAG failures. Must be fixed.
- INCOMPLETE: axe-core couldn't auto-verify. May be real issues or
  false positives. Common causes: background gradients, images, pseudo-
  elements blocking contrast computation, elements outside viewport.
- PASSES: Rules that were checked and satisfied.

COMMON AXE-CORE INCOMPLETE TYPES (usually not real issues):
- bgOverlap/elmPartiallyObscured: flex/scroll layout measurement artifacts
- pseudoContent: CSS ::before/::after blocking contrast computation
- bgGradient/bgImage: background-image preventing contrast resolution
  Fix: set explicit background-color on text elements
- shortTextContent: single-character text (e.g. x delete buttons)
  Fix: move character to CSS ::after, leave element empty
- nonBmp: icon font glyphs axe can't evaluate
  Fix: move icon character to CSS ::after on aria-hidden elements

KEY FLAGS FOR LLM WORKFLOWS
----------------------------
--page              Scan one URL, no crawling (fast verify after a fix)
--rule NAME         Check only specific rules (fast, focused)
--summary-json      Machine-parseable one-line JSON output
--llm               Generate compact markdown report (~300 tokens vs 300K)
--diff PREV.jsonl   Show what changed since last scan
--rescan PREV.jsonl Only re-scan pages that previously had issues
--allowlist FILE    Suppress known-acceptable incompletes
-q                  Quiet — no per-page progress, just final summary
-v                  Verbose — add detailed rule/node counts for problem pages

OTHER NOTES
-----------
- robots.txt is respected by default.  Use --ignore-robots to scan
  disallowed paths, or set ignore_robots: true in your config.
- Reports are flushed every 25 pages (configurable with --save-every)
  and on SIGTERM/SIGINT, so partial results survive if the scan is killed.
- The scanner runs at low CPU priority (nice 10) and high OOM score
  (1000) by default so it won't starve production services on shared
  servers.  Both are configurable in a11y-catscan.yaml.
"""


def cmd_help_audit():
    """--help-audit: print the audit workflow guide and exit."""
    print(_HELP_AUDIT_TEXT)
    sys.exit(0)
