"""HTML report generator.

Streams a JSONL scan result to a single HTML file with summary tables
(impact breakdown, WCAG criteria, rule summary, incomplete summary)
and per-page detail sections.

Memory: O(unique_rules) regardless of page count.  Output is written
fragment-by-fragment via `out.write()` so a 5000-page scan never
holds the whole document in RAM.
"""

from datetime import datetime

from engine_mappings import (
    EARL_FAILED, EARL_CANTTELL, EARL_PASSED,
    sc_name, parse_wcag_sc)
from engines.axe import get_axe_version
from allowlist import matches_allowlist
from report_io import iter_deduped
from results import count_nodes


_IMPACT_COLORS = {
    'critical': '#d32f2f',
    'serious': '#e65100',
    'moderate': '#f9a825',
    'minor': '#1565c0',
}


def _esc(text):
    """Escape HTML special characters."""
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;'))


def _render_nodes_html(nodes, limit=20, snippet_max=500):
    """Render the per-node detail block (selectors, HTML, messages)."""
    parts = []
    parts.append('<details><summary>{} element(s)</summary>'.format(
        len(nodes)))
    for node in nodes[:limit]:
        parts.append('<div class="node-detail">')
        target = node.get('target', [])
        if target:
            parts.append('<p class="target">Selector: {}</p>'.format(
                _esc(', '.join(str(t) for t in target))))
        html_snippet = node.get('html', '')
        if html_snippet:
            if len(html_snippet) > snippet_max:
                html_snippet = html_snippet[:snippet_max] + '...'
            parts.append('<div class="html-snippet">{}</div>'.format(
                _esc(html_snippet)))
        messages = []
        for check in (node.get('any', []) + node.get('all', [])
                      + node.get('none', [])):
            msg = check.get('message', '')
            if msg:
                messages.append(msg)
        if messages:
            parts.append('<ul>')
            for msg in messages[:5]:
                parts.append('<li>{}</li>'.format(_esc(msg)))
            parts.append('</ul>')
        parts.append('</div>')
    if len(nodes) > limit:
        parts.append('<p><em>... and {} more</em></p>'.format(
            len(nodes) - limit))
    parts.append('</details>')
    return '\n'.join(parts)


def generate_html_report(jsonl_path, output_path, start_url,
                         level_label='WCAG 2.1 Level AA',
                         allowlist=None):
    """Generate an HTML report by streaming through JSONL on disk.

    Memory usage is O(unique_rules) regardless of page count.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    axe_ver = get_axe_version()
    allowlist = allowlist or []

    # --- Pass 1: aggregate stats (constant memory) ---
    total_pages = 0
    total_violations = 0
    total_violation_nodes = 0
    total_incomplete_nodes = 0
    total_suppressed = 0
    impact_counts = {
        'critical': 0, 'serious': 0, 'moderate': 0, 'minor': 0}
    rule_summary = {}
    incomplete_summary = {}
    wcag_criteria = {}

    def _track_wcag(tags, category, count=1):
        for sc in parse_wcag_sc(tags):
            if sc not in wcag_criteria:
                wcag_criteria[sc] = {
                    EARL_FAILED: 0,
                    EARL_CANTTELL: 0,
                    EARL_PASSED: 0,
                }
            wcag_criteria[sc][category] += count

    for url, data in iter_deduped(jsonl_path):
        total_pages += 1
        for v in data.get(EARL_FAILED, []):
            nodes = v.get('nodes', [])
            total_violations += 1
            total_violation_nodes += len(nodes)
            impact = v.get('impact', 'unknown')
            if impact in impact_counts:
                impact_counts[impact] += len(nodes)
            rule_id = v.get('id', 'unknown')
            if rule_id not in rule_summary:
                rule_summary[rule_id] = {
                    'description': v.get('description', ''),
                    'help': v.get('help', ''),
                    'helpUrl': v.get('helpUrl', ''),
                    'impact': impact,
                    'tags': v.get('tags', []),
                    'count': 0,
                    'pages': set(),
                }
            rule_summary[rule_id]['count'] += len(nodes)
            rule_summary[rule_id]['pages'].add(url)
            _track_wcag(v.get('tags', []), EARL_FAILED, len(nodes))

        for v in data.get(EARL_CANTTELL, []):
            nodes = v.get('nodes', [])
            rule_id = v.get('id', 'unknown')
            if matches_allowlist(rule_id, url, nodes, allowlist,
                                 engines_dict=v.get('engines'),
                                 outcome=EARL_CANTTELL):
                total_suppressed += len(nodes)
                continue
            total_incomplete_nodes += len(nodes)
            if rule_id not in incomplete_summary:
                incomplete_summary[rule_id] = {
                    'help': v.get('help', ''),
                    'helpUrl': v.get('helpUrl', ''),
                    'impact': v.get('impact', 'unknown'),
                    'count': 0,
                    'pages': set(),
                }
            incomplete_summary[rule_id]['count'] += len(nodes)
            incomplete_summary[rule_id]['pages'].add(url)
            _track_wcag(v.get('tags', []), EARL_CANTTELL, len(nodes))

        for v in data.get(EARL_PASSED, []):
            _track_wcag(v.get('tags', []), EARL_PASSED)

    sorted_rules = sorted(
        rule_summary.items(), key=lambda x: x[1]['count'], reverse=True)
    sorted_incomplete = sorted(
        incomplete_summary.items(),
        key=lambda x: x[1]['count'], reverse=True)

    # Stream-write directly to disk instead of accumulating fragments
    # in a list (a 5000-page scan would otherwise hold hundreds of MB
    # of HTML in RAM before flushing).  `w(s)` writes one fragment.
    out = open(output_path, 'w')

    def w(s):
        out.write(s)
        out.write('\n')

    w("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A11y CatScan Report</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto; padding: 20px; }
  h1 { color: #1a237e; margin-bottom: 5px; }
  h2 { color: #283593; margin: 30px 0 15px; border-bottom: 2px solid #e8eaf6; padding-bottom: 5px; }
  h3 { color: #3949ab; margin: 20px 0 10px; }
  .meta { color: #666; margin-bottom: 20px; }
  .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                   gap: 15px; margin: 20px 0; }
  .summary-card { background: #f5f5f5; border-radius: 8px; padding: 20px; text-align: center; }
  .summary-card .number { font-size: 2em; font-weight: bold; }
  .summary-card .label { color: #666; font-size: 0.9em; }
  .impact-critical { border-left: 4px solid #d32f2f; }
  .impact-serious { border-left: 4px solid #e65100; }
  .impact-moderate { border-left: 4px solid #f9a825; }
  .impact-minor { border-left: 4px solid #1565c0; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; color: white;
           font-size: 0.8em; font-weight: bold; margin-right: 5px; }
  .badge-critical { background: #d32f2f; }
  .badge-serious { background: #e65100; }
  .badge-moderate { background: #f9a825; color: #333; }
  .badge-minor { background: #1565c0; }
  table { width: 100%; border-collapse: collapse; margin: 10px 0; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #e0e0e0; }
  th { background: #e8eaf6; font-weight: 600; }
  tr:hover { background: #f5f5f5; }
  .rule-card { background: #fafafa; border: 1px solid #e0e0e0; border-radius: 8px;
               padding: 15px; margin: 10px 0; }
  .tag { display: inline-block; background: #e8eaf6; color: #3949ab; padding: 1px 6px;
         border-radius: 3px; font-size: 0.75em; margin: 2px; }
  details { margin: 5px 0; }
  summary { cursor: pointer; font-weight: 500; padding: 5px 0; }
  .node-detail { background: #fff; border: 1px solid #e0e0e0; border-radius: 4px;
                 padding: 10px; margin: 5px 0; }
  .html-snippet { background: #263238; color: #aed581; padding: 8px 12px; border-radius: 4px;
                  font-family: 'Fira Code', monospace; font-size: 0.85em; overflow-x: auto;
                  white-space: pre-wrap; word-break: break-all; }
  .target { color: #666; font-family: monospace; font-size: 0.85em; }
  a { color: #1565c0; }
  .page-section { margin: 25px 0; padding: 15px; border: 1px solid #e0e0e0; border-radius: 8px; }
  .page-url { font-size: 0.9em; color: #1565c0; word-break: break-all; }
  .wcag-ref { font-size: 0.8em; color: #666; }
</style>
</head>
<body>
""")

    w('<h1>A11y CatScan Report</h1>')
    w('<p class="meta">Scanned: {} | {} | Generated: {} | '
      'axe-core {}</p>'.format(
          _esc(start_url), _esc(level_label), now, axe_ver))
    w('<p class="meta">Scope: HTML pages only. '
      'Does not cover PDFs, videos, audio, PowerPoint, Word '
      'documents, or other media files.</p>')

    w('<div class="summary-grid">')
    w('<div class="summary-card"><div class="number">{}</div>'
      '<div class="label">Pages Scanned</div></div>'.format(total_pages))
    w('<div class="summary-card">'
      '<div class="number" style="color:#d32f2f">{}</div>'
      '<div class="label">Total Issues</div></div>'.format(
          total_violation_nodes))
    w('<div class="summary-card"><div class="number">{}</div>'
      '<div class="label">Unique Rules</div></div>'.format(
          len(rule_summary)))
    w('<div class="summary-card"><div class="number">{}</div>'
      '<div class="label">Needs Review</div></div>'.format(
          total_incomplete_nodes))
    if total_suppressed:
        w('<div class="summary-card">'
          '<div class="number" style="color:#888">{}</div>'
          '<div class="label">Suppressed (allowlist)</div></div>'
          .format(total_suppressed))
    w('</div>')

    w('<h2>Impact Breakdown</h2>')
    w('<div class="summary-grid">')
    for impact in ('critical', 'serious', 'moderate', 'minor'):
        cnt = impact_counts[impact]
        w('<div class="summary-card impact-{imp}">'
          '<div class="number" style="color:{color}">{cnt}</div>'
          '<div class="label">{imp_cap}</div></div>'.format(
              imp=impact, color=_IMPACT_COLORS[impact],
              cnt=cnt, imp_cap=impact.capitalize()))
    w('</div>')

    # WCAG criteria summary
    if wcag_criteria:
        sorted_sc = sorted(wcag_criteria.items(), key=lambda x: x[0])
        w('<h2>WCAG Success Criteria</h2>')
        w('<table><tr><th>Criterion</th><th>Name</th>'
          '<th style="color:#d32f2f">Violations</th>'
          '<th style="color:#e65100">Incomplete</th>'
          '<th style="color:#2e7d32">Passes</th>'
          '<th>Status</th></tr>')
        for sc, counts in sorted_sc:
            name = sc_name(sc)
            v = counts[EARL_FAILED]
            i = counts[EARL_CANTTELL]
            p = counts[EARL_PASSED]
            if v > 0:
                status = '<span class="badge badge-critical">FAIL</span>'
            elif i > 0:
                status = ('<span class="badge badge-serious">'
                          'REVIEW</span>')
            else:
                status = ('<span style="color:#2e7d32;'
                          'font-weight:bold">PASS</span>')
            w('<tr><td>{sc}</td><td>{name}</td>'
              '<td>{v}</td><td>{i}</td><td>{p}</td>'
              '<td>{status}</td></tr>'.format(
                  sc=_esc(sc), name=_esc(name),
                  v=v or '', i=i or '', p=p or '',
                  status=status))
        w('</table>')

    if sorted_rules:
        w('<h2>Violation Summary by Rule</h2>')
        w('<table><tr><th>Rule</th><th>Impact</th><th>Issues</th>'
          '<th>Pages</th><th>Description</th></tr>')
        for rule_id, info in sorted_rules:
            impact = info['impact']
            w('<tr><td><a href="{url}">{id}</a></td>'
              '<td><span class="badge badge-{imp}">'
              '{imp_cap}</span></td>'
              '<td>{count}</td><td>{pages}</td><td>{desc}</td></tr>'
              .format(
                  url=_esc(info['helpUrl']), id=_esc(rule_id),
                  imp=impact, imp_cap=impact.capitalize(),
                  count=info['count'], pages=len(info['pages']),
                  desc=_esc(info['help'])))
        w('</table>')

    # Incomplete summary table
    if sorted_incomplete:
        w('<h2>Incomplete Summary (Needs Manual Review)</h2>')
        w('<p class="meta">axe-core could not automatically determine '
          'pass/fail for these items — typically color-contrast on '
          'elements with background images, gradients, or '
          'pseudo-elements.</p>')
        w('<table><tr><th>Rule</th><th>Nodes</th>'
          '<th>Pages</th><th>Description</th></tr>')
        for rule_id, info in sorted_incomplete:
            w('<tr><td><a href="{url}">{id}</a></td>'
              '<td>{count}</td><td>{pages}</td><td>{desc}</td></tr>'
              .format(
                  url=_esc(info['helpUrl']), id=_esc(rule_id),
                  count=info['count'], pages=len(info['pages']),
                  desc=_esc(info['help'])))
        w('</table>')

    # --- Pass 2: per-page details (deduped, stream again) ---
    w('<h2>Per-Page Details</h2>')
    clean_pages = []
    for url, data in iter_deduped(jsonl_path):
        violations = data.get(EARL_FAILED, [])
        incomplete = data.get(EARL_CANTTELL, [])
        # Filter out allowlisted incompletes
        shown_incomplete = []
        for v in incomplete:
            rule_id = v.get('id', '')
            nodes = v.get('nodes', [])
            if not matches_allowlist(rule_id, url, nodes, allowlist,
                                     engines_dict=v.get('engines'),
                                     outcome=EARL_CANTTELL):
                shown_incomplete.append(v)
        if not violations and not shown_incomplete:
            clean_pages.append(url)
            continue

        v_count = count_nodes(violations)
        i_count = count_nodes(shown_incomplete)
        w('<div class="page-section">')
        w('<h3><a href="{}" class="page-url">{}</a></h3>'.format(
            _esc(url), _esc(url)))
        w('<p>{} violation(s), {} issue(s) &mdash; {} incomplete, '
          '{} node(s)</p>'.format(
              len(violations), v_count,
              len(shown_incomplete), i_count))

        for v in violations:
            impact = v.get('impact', 'unknown')
            w('<div class="rule-card impact-{}">'.format(impact))
            w('<strong><span class="badge badge-{}">{}</span> '
              '<a href="{}">{}</a></strong>'.format(
                  impact, impact.capitalize(),
                  _esc(v.get('helpUrl', '')),
                  _esc(v.get('id', ''))))
            w('<p>{}</p>'.format(_esc(v.get('help', ''))))
            tags = v.get('tags', [])
            wcag_tags = [t for t in tags if t.startswith('wcag')]
            if wcag_tags:
                w('<p class="wcag-ref">WCAG: {}</p>'.format(
                    ' '.join(
                        '<span class="tag">{}</span>'.format(_esc(t))
                        for t in wcag_tags)))
            w(_render_nodes_html(v.get('nodes', []), limit=20))
            w('</div>')

        if shown_incomplete:
            w('<h4 style="margin-top:1em;color:#e65100;">'
              'Incomplete (needs manual review)</h4>')
            for v in shown_incomplete:
                w('<div class="rule-card">')
                w('<strong><a href="{}">{}</a></strong>'.format(
                    _esc(v.get('helpUrl', '')),
                    _esc(v.get('id', ''))))
                w('<p>{}</p>'.format(_esc(v.get('help', ''))))
                w(_render_nodes_html(
                    v.get('nodes', []), limit=10, snippet_max=300))
                w('</div>')

        w('</div>')

    if clean_pages:
        w('<h2>Fully Clean Pages ({})'.format(len(clean_pages)))
        w('</h2><ul>')
        for url in clean_pages:
            w('<li><a href="{}">{}</a></li>'.format(
                _esc(url), _esc(url)))
        w('</ul>')

    w('</body></html>')
    out.close()
