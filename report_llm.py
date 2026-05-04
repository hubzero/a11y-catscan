"""LLM-friendly markdown report generator.

Produces a compact (~2-5K token) markdown summary suitable for
feeding to an LLM, instead of the full JSON (which is 100K+ tokens
for large scans).  Groups violations by rule (with deduped HTML
examples) and incompletes by axe `messageKey`.
"""

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from engine_mappings import (
    EARL_FAILED, EARL_CANTTELL, parse_wcag_sc)
from engines.axe import get_axe_version
from allowlist import matches_allowlist
from report_io import iter_deduped


def generate_llm_report(jsonl_path, output_path, start_url,
                        level_label='WCAG 2.1 Level AA',
                        allowlist=None, config=None):
    """Generate a token-efficient markdown summary optimized for LLMs.

    Instead of dumping raw JSON (100K+ tokens for a large scan),
    this produces a compact report (~2-5K tokens) with:
    - Context and instructions for the LLM
    - Violations grouped by rule with deduplicated examples
    - Incompletes grouped by messageKey
    - Affected page lists (URLs only, no repeated node data)
    """
    allowlist = allowlist or []
    axe_ver = get_axe_version()

    # Aggregate: {rule_id -> {info, pages, example_nodes}}
    violations_by_rule = {}
    incompletes_by_key = {}
    total_pages = 0
    pages_with_violations = set()
    pages_with_incompletes = set()
    suppressed_count = 0

    for url, data in iter_deduped(jsonl_path):
        total_pages += 1
        path = urlparse(url).path

        for v in data.get(EARL_FAILED, []):
            rule_id = v.get('id', 'unknown')
            nodes = v.get('nodes', [])
            pages_with_violations.add(path)
            if rule_id not in violations_by_rule:
                violations_by_rule[rule_id] = {
                    'help': v.get('help', ''),
                    'helpUrl': v.get('helpUrl', ''),
                    'impact': v.get('impact', ''),
                    'tags': v.get('tags', []),
                    'count': 0,
                    'pages': [],
                    'examples': [],
                }
            info = violations_by_rule[rule_id]
            info['count'] += len(nodes)
            if path not in info['pages']:
                info['pages'].append(path)
            # Keep up to 3 unique example HTML snippets
            for node in nodes:
                snippet = node.get('html', '')[:200]
                if (snippet
                        and len(info['examples']) < 3
                        and snippet not in info['examples']):
                    info['examples'].append(snippet)

        for v in data.get(EARL_CANTTELL, []):
            nodes = v.get('nodes', [])
            rule_id = v.get('id', 'unknown')
            if matches_allowlist(rule_id, url, nodes, allowlist,
                                 engines_dict=v.get('engines'),
                                 outcome=EARL_CANTTELL):
                suppressed_count += len(nodes)
                continue
            pages_with_incompletes.add(path)
            for node in nodes:
                for check in node.get('any', []):
                    d = check.get('data', {})
                    mk = (d.get('messageKey', '')
                          if isinstance(d, dict) else '')
                    if mk not in incompletes_by_key:
                        incompletes_by_key[mk] = {
                            'count': 0, 'pages': set(),
                            'examples': []}
                    info = incompletes_by_key[mk]
                    info['count'] += 1
                    info['pages'].add(path)
                    snippet = node.get('html', '')[:150]
                    if (snippet
                            and len(info['examples']) < 2
                            and snippet not in info['examples']):
                        info['examples'].append(snippet)

    # Build markdown
    lines = []
    lines.append('# a11y-catscan accessibility scan results\n')
    lines.append(f'Site: {start_url}  ')
    lines.append(f'Level: {level_label}  ')
    lines.append(f'axe-core: {axe_ver}  ')
    lines.append(f'Pages scanned: {total_pages}  ')
    lines.append('Scan date: {}\n'.format(
        datetime.now().strftime('%Y-%m-%d')))
    lines.append(
        '**Scope**: HTML pages only.  This scan does not cover '
        'accessibility of PDFs, videos, audio, PowerPoint, Word '
        'documents, or other media files.')

    # Instructions section — read from a file if configured,
    # otherwise use defaults.  This lets each site customize the
    # LLM prompt for their specific codebase (e.g. "templates are
    # in app/templates/cdm/", "use LESS not CSS", etc.)
    llm_instructions_path = (
        config.get('llm_instructions') if config else None)
    if llm_instructions_path and os.path.exists(llm_instructions_path):
        with open(llm_instructions_path) as f:
            lines.append(f.read().rstrip())
        lines.append('')
    else:
        lines.append('## Instructions\n')
        lines.append(
            'This is a WCAG accessibility scan summary. '
            'When investigating:')
        lines.append(
            '- Each violation needs a code fix — find the source '
            'that generates the flagged HTML')
        lines.append(
            '- Incompletes are items axe-core could not auto-verify '
            '(usually contrast issues)')
        lines.append(
            '- The "examples" show representative HTML — the same '
            'pattern repeats across listed pages')
        lines.append(
            '- Focus on violations first (failures), then '
            'incompletes (may be false positives)\n')

    # Violations
    if violations_by_rule:
        lines.append('## Violations ({} issues on {} pages)\n'.format(
            sum(v['count'] for v in violations_by_rule.values()),
            len(pages_with_violations)))
        for rule_id, info in sorted(
                violations_by_rule.items(),
                key=lambda x: x[1]['count'], reverse=True):
            wcag_scs = ', '.join(sorted(parse_wcag_sc(info['tags'])))
            lines.append('### {} ({}, {} issues)'.format(
                rule_id, info['impact'], info['count']))
            lines.append('{}'.format(info['help']))
            if wcag_scs:
                lines.append(f'WCAG: {wcag_scs}')
            lines.append('Pages: {}'.format(
                ', '.join(info['pages'][:10])))
            if len(info['pages']) > 10:
                lines.append('  ... and {} more'.format(
                    len(info['pages']) - 10))
            lines.append('Examples:')
            for ex in info['examples']:
                lines.append(f'```html\n{ex}\n```')
            lines.append('')
    else:
        lines.append('## Violations: NONE\n')

    # Incompletes
    if incompletes_by_key:
        total_inc = sum(
            v['count'] for v in incompletes_by_key.values())
        lines.append('## Incompletes ({} nodes on {} pages)\n'.format(
            total_inc, len(pages_with_incompletes)))
        for mk, info in sorted(
                incompletes_by_key.items(),
                key=lambda x: x[1]['count'], reverse=True):
            lines.append('### {} — {} nodes, {} pages'.format(
                mk or '(unknown)', info['count'], len(info['pages'])))
            pages_list = sorted(info['pages'])
            lines.append('Pages: {}'.format(
                ', '.join(pages_list[:10])))
            if len(pages_list) > 10:
                lines.append('  ... and {} more'.format(
                    len(pages_list) - 10))
            if info['examples']:
                lines.append('Example:')
                lines.append('```html\n{}\n```'.format(info['examples'][0]))
            lines.append('')
    else:
        lines.append('## Incompletes: NONE\n')

    if suppressed_count:
        lines.append('## Suppressed (allowlist): {} nodes\n'.format(
            suppressed_count))

    # Point to full reports for deeper investigation.  Path.with_suffix
    # only swaps the final extension — `output_path.replace('.md', ...)`
    # would corrupt paths whose directory components contain '.md'.
    out_path = Path(output_path)
    json_sibling = str(out_path.with_suffix('.json'))
    jsonl_sibling = str(out_path.with_suffix('.jsonl'))
    html_sibling = str(out_path.with_suffix('.html'))
    lines.append('## Detailed reports\n')
    lines.append(
        'This is a summary.  For full per-page, per-node details:')
    lines.append('- JSON (full axe-core output): {}'.format(
        json_sibling))
    lines.append('- JSONL (streaming, for --diff/--rescan): {}'.format(
        jsonl_sibling))
    lines.append('- HTML (human-readable report): {}'.format(
        html_sibling))
    lines.append(
        '- Run `a11y-catscan.py --help-audit` for the full audit '
        'workflow guide')
    lines.append('')

    report = '\n'.join(lines)
    with open(output_path, 'w') as f:
        f.write(report)
    return report
