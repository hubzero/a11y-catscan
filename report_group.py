"""--group-by summary printer.

Groups deduped scan findings by rule, selector, contrast color,
failure reason, WCAG SC, level, engine, or best-practice category,
and prints a count + one example per group to stdout.
"""

import re
from collections import Counter

from engine_mappings import (
    EARL_FAILED, EARL_CANTTELL, parse_wcag_sc, sc_level)
from allowlist import matches_allowlist  # noqa: F401  (reserved)
from report_io import iter_deduped


def group_results(jsonl_path, group_by, allowlist=None):
    """Group scan results and print a summary table.

    group_by: 'rule', 'selector', 'color', 'reason', 'wcag',
              'level', 'engine', 'bp'
    """
    groups = Counter()
    group_pages = {}
    group_examples = {}

    for url, data in iter_deduped(jsonl_path):
        for category in (EARL_FAILED, EARL_CANTTELL):
            for item in data.get(category, []):
                rule_id = item.get('id', 'unknown')
                tags = item.get('tags', [])
                for node in item.get('nodes', []):
                    target = node.get('target', ['?'])[0]
                    html = node.get('html', '')[:120]

                    # Get the check message
                    msg = ''
                    for ct in ('any', 'all', 'none'):
                        for c in node.get(ct, []):
                            if c.get('message'):
                                msg = c['message']
                                break
                        if msg:
                            break

                    # Determine group key
                    if group_by == 'rule':
                        key = f'{rule_id} ({category[:3]})'
                    elif group_by == 'selector':
                        # Normalize: strip IDs, nth-child numbers
                        key = re.sub(
                            r'#[a-zA-Z_][\w-]*', '#ID', target)
                        key = re.sub(
                            r'nth-child\(\d+\)', 'nth-child(N)', key)
                        key = re.sub(r'\[.*?\]', '', key)
                    elif group_by == 'color':
                        m = re.search(
                            r'foreground color: (#\w+).*'
                            r'background color: (#\w+)', msg)
                        if m:
                            key = '{} on {}'.format(
                                m.group(1), m.group(2))
                        else:
                            key = '(no color info)'
                    elif group_by == 'reason':
                        if 'gradient' in msg:
                            key = 'background gradient'
                        elif 'overlapped' in msg or 'overlap' in msg:
                            key = 'element overlap'
                        elif 'pseudo' in msg:
                            key = 'pseudo-element'
                        elif 'background image' in msg:
                            key = 'background image'
                        elif '1:1' in msg:
                            key = '1:1 (transparent text)'
                        elif 'too short' in msg:
                            key = 'content too short'
                        elif 'could not be determined' in msg:
                            key = 'background undetermined'
                        elif msg:
                            key = msg[:50]
                        else:
                            key = '(no message)'
                    elif group_by == 'wcag':
                        scs = parse_wcag_sc(tags)
                        key = ', '.join(scs) if scs else rule_id
                    elif group_by == 'level':
                        scs = parse_wcag_sc(tags)
                        if scs:
                            lvl, ver = sc_level(next(iter(scs)))
                            key = f'WCAG {ver} {lvl}'
                        elif 'best-practice' in tags:
                            key = 'Best Practice'
                        else:
                            key = 'Unmapped'
                    elif group_by == 'engine':
                        engines = item.get('engines', {})
                        if engines:
                            key = '+'.join(sorted(engines.keys()))
                        else:
                            key = item.get('engine', 'unknown')
                    elif group_by == 'bp':
                        bp_tags = [t[3:] for t in tags
                                   if t.startswith('bp-')]
                        if bp_tags:
                            key = bp_tags[0]
                        elif 'best-practice' in tags:
                            key = '(uncategorized)'
                        else:
                            scs = parse_wcag_sc(tags)
                            key = ('WCAG ' + ', '.join(scs)
                                   if scs else rule_id)
                    else:
                        key = rule_id

                    groups[key] += 1
                    if key not in group_pages:
                        group_pages[key] = set()
                    group_pages[key].add(url)
                    if key not in group_examples:
                        group_examples[key] = {
                            'url': url, 'target': target,
                            'html': html, 'rule': rule_id}

    # Print summary
    total = sum(groups.values())
    print("\n  Grouped by {}: {} nodes in {} groups\n".format(
        group_by, total, len(groups)))
    for key, count in groups.most_common():
        pages = len(group_pages[key])
        ex = group_examples[key]
        print(f"  {count:>5d}  {key} ({pages} pages)")
        if group_by != 'rule':
            print("         rule: {}".format(ex['rule']))
        print("         e.g. {}".format(ex['url']))
        print("              {}".format(ex['target'][:70]))
        print()
