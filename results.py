"""Per-page result helpers: counting and cross-engine deduplication.

These functions operate on the page-result dicts produced by
`Scanner.scan_page` (and stored in JSONL).  They do not touch the
browser, so they live in their own module — `report_io`, `registry`,
and the various report generators can use them without pulling in
Playwright.

Result-dict shape (see `engines/base.py` for the per-finding format):

    {
        'url':          str,
        'timestamp':    ISO 8601 str,
        'http_status':  int | None,
        'failed':       [{...finding...}, ...],
        'cantTell':     [...],
        'passed':       [...],
        'inapplicable': [...],
    }
"""

from dataclasses import dataclass, field

from engine_mappings import (
    EARL_FAILED, EARL_CANTTELL, EARL_PASSED, EARL_INAPPLICABLE)


# Impact severity ranking — higher number wins when merging duplicate
# findings across engines.
_IMPACT_RANK = {'critical': 4, 'serious': 3, 'moderate': 2, 'minor': 1}


@dataclass
class RunningTotals:
    """Per-scan finding counters accumulated during the crawl.

    `crawl_and_scan` increments these in `_write_page` after each
    page lands so main() can render the post-scan summary without
    re-iterating the JSONL.  Counts are *node* counts (not
    finding counts) — a single rule violation that hits 12
    elements contributes 12 to the relevant bucket.

    Classification priority (applied in `allowlist.classify_page`):
    a finding with an `aria-*` primary tag is counted under
    `aria` even if it also has an `sc-*` tag, because IBM
    mis-maps several ARIA-naming rules to SC 2.4.1 and we don't
    want those to inflate the WCAG count.
    """
    wcag: int = 0
    aria: int = 0
    bp: int = 0
    incomplete: int = 0
    rules: set = field(default_factory=set)


def count_nodes(result_list):
    """Count total DOM nodes across a list of engine result dicts."""
    total = 0
    for rule_result in result_list:
        total += len(rule_result.get('nodes', []))
    return total


def dedup_page(page_data):
    """Deduplicate findings for one page across engines.

    Merges findings that share the same (selector, primary_tag,
    outcome) into a single entry with multi-engine attribution
    (`engines: {axe: ..., ibm: ...}`).

    Outcome merging: if one engine says 'failed' and another says
    'cantTell' for the same element+tag, they stay separate —
    those are different confidence levels.

    Design note — why we don't dedup at write time:
    The crawl writes raw merged-engine results to JSONL and lets
    each consumer (HTML, LLM, group, search, page-status) call
    iter_deduped() on read.  Moving dedup into the JSONL write
    step would change the on-disk format:
      - raw item:     {'id': 'image-alt', 'engine': 'axe', ...}
      - deduped item: {'id': 'sc-1.1.1', 'engines': {...}, ...}
    `crawl_and_scan` already tracks running summary totals during
    the write so main() doesn't re-iterate the JSONL — that
    eliminated the most expensive re-pass.  The remaining
    dedup-on-read cost (one pass per consumer, typically 2-3 per
    CLI invocation) is small enough that the on-disk format
    change isn't worth the backward-compatibility cost for
    `--diff` consumers and any tools reading the JSONL directly.
    """
    deduped = {}

    for outcome in (EARL_FAILED, EARL_CANTTELL):
        for item in page_data.get(outcome, []):
            tags = item.get('tags', [])
            primary_tags = [t for t in tags
                            if t.startswith(('sc-', 'aria-', 'bp-'))]
            if not primary_tags:
                primary_tags = [item.get('id', 'unknown')]

            engine = item.get('engine', 'unknown')
            rule_id = item.get('id', '')

            for node in item.get('nodes', []):
                selector = (node.get('target', [''])[0]
                            if node.get('target') else '')
                html = node.get('html', '')
                msg = ''
                for ct in ('any', 'all', 'none'):
                    for c in node.get(ct, []):
                        if c.get('message'):
                            msg = c['message']
                            break
                    if msg:
                        break

                for ptag in primary_tags:
                    key = (selector, ptag, outcome)

                    if key not in deduped:
                        deduped[key] = {
                            'selector': selector,
                            'html': html,
                            'tags': list(tags),
                            'outcome': outcome,
                            'primary_tag': ptag,
                            'description': item.get(
                                'description', ''),
                            'help': item.get('help', ''),
                            'helpUrl': item.get('helpUrl', ''),
                            'impact': item.get('impact', ''),
                            'message': msg,
                            'engines': {},
                        }
                    else:
                        existing = deduped[key]
                        for t in tags:
                            if t not in existing['tags']:
                                existing['tags'].append(t)
                        if (_IMPACT_RANK.get(item.get('impact', ''), 0)
                                > _IMPACT_RANK.get(
                                    existing['impact'], 0)):
                            existing['impact'] = item.get(
                                'impact', '')

                    deduped[key]['engines'][engine] = {
                        'rule': rule_id,
                        'impact': item.get('impact', ''),
                    }

    result = {
        'url': page_data.get('url', ''),
        'timestamp': page_data.get('timestamp', ''),
        'http_status': page_data.get('http_status'),
        EARL_FAILED: [],
        EARL_CANTTELL: [],
        EARL_PASSED: page_data.get(EARL_PASSED, []),
        EARL_INAPPLICABLE: page_data.get(EARL_INAPPLICABLE, []),
    }

    for (_, _, outcome), finding in deduped.items():
        item = {
            'id': finding['primary_tag'],
            'engines': finding['engines'],
            'engine_count': len(finding['engines']),
            'outcome': finding['outcome'],
            'description': finding['description'],
            'help': finding['help'],
            'helpUrl': finding['helpUrl'],
            'impact': finding['impact'],
            'tags': finding['tags'],
            'nodes': [{
                'target': [finding['selector']],
                'html': finding['html'],
                'any': ([{'message': finding['message']}]
                        if finding['message'] else []),
            }],
        }
        result[outcome].append(item)

    return result
