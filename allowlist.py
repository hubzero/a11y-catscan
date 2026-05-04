"""Allowlist parsing and matching.

The allowlist file (YAML) lists known-acceptable findings that
should be suppressed in reports.  Each entry can filter on rule,
url, target, engine, and outcome — all AND'd together.  See
`load_allowlist` for the file format.

`Allowlist` is the indexed (rule_id → entries) container; its
`matches()` method is the single suppression check called per
finding by every report consumer.  The legacy
`matches_allowlist(rule_id, ..., allowlist)` function still works
for callers that pass either an `Allowlist` instance or a plain
list.

`classify_page` is a small helper that the crawl loop uses to
keep running totals (so main() doesn't need to re-iterate the
JSONL after the scan).
"""

import os

import yaml

from engine_mappings import EARL_FAILED, EARL_CANTTELL
from results import count_nodes


class Allowlist:
    """Indexed allowlist — O(1) average lookup by rule_id.

    Acts like a sequence of entry dicts (`bool(allowlist)`,
    `len(allowlist)`, iteration) so existing callers that just
    test truthiness or iterate keep working unchanged.
    """

    __slots__ = ('_entries', '_by_rule')

    def __init__(self, entries):
        # Filter out non-dict items defensively (YAML can produce
        # surprises) so the index stays clean.
        self._entries = [e for e in (entries or [])
                         if isinstance(e, dict)]
        self._by_rule = {}
        for entry in self._entries:
            rule = entry.get('rule', '')
            self._by_rule.setdefault(rule, []).append(entry)

    def __bool__(self):
        return bool(self._entries)

    def __len__(self):
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)

    def matches(self, rule_id, url, nodes,
                engines_dict=None, outcome=None):
        """True iff this finding matches any allowlist entry.

        See `matches_allowlist` for the filter semantics.
        """
        candidates = self._by_rule.get(rule_id)
        if not candidates:
            return False
        return _match_any(
            candidates, rule_id, url, nodes,
            engines_dict, outcome)


def load_allowlist(path):
    """Load an allowlist file that suppresses known-acceptable findings.

    Format (YAML):
        - rule: color-contrast
          reason: axe-core limitation on scroll-snap flex layouts
        - rule: color-contrast
          url: /groups/mmsc/usage
          reason: Google Charts SVG
        - rule: aria-allowed-attr
          target: "#main-nav"

    Returns an `Allowlist` (indexed by rule_id).  Empty allowlist
    if the path is missing or the file isn't a YAML list.
    """
    if not path or not os.path.exists(path):
        return Allowlist([])
    with open(path) as f:
        data = yaml.safe_load(f) or []
    if isinstance(data, list):
        return Allowlist(data)
    return Allowlist([])


def _match_any(entries, rule_id, url, nodes,
               engines_dict, outcome):
    """Return True if any of `entries` matches.

    Each entry has already been pre-filtered to share the same
    `rule` value as `rule_id`, so this only checks the secondary
    filters (engine/outcome/url/target).
    """
    finding_engines = (set(engines_dict.keys())
                       if engines_dict else set())

    for entry in entries:
        # Engine filter: only suppress when the finding came from
        # this specific engine.  A finding confirmed by both axe
        # and IBM with engine:'ibm' only suppresses if IBM is the
        # SOLE engine.  Multi-engine findings aren't suppressed
        # by single-engine allowlist entries.
        entry_engine = entry.get('engine', '')
        if entry_engine:
            if entry_engine not in finding_engines:
                continue
            # Don't suppress multi-engine findings with a
            # single-engine filter — if axe also found it,
            # it's a real issue regardless of IBM noise.
            if len(finding_engines) > 1:
                continue

        # Outcome filter
        entry_outcome = entry.get('outcome', '')
        if entry_outcome and entry_outcome != outcome:
            continue

        # URL filter
        entry_url = entry.get('url', '')
        if entry_url and entry_url not in url:
            continue

        # Target filter
        entry_target = entry.get('target', '')
        if entry_target:
            target_found = False
            for node in nodes:
                if entry_target in str(node.get('target', '')):
                    target_found = True
                    break
            if not target_found:
                continue

        # All filters passed — this result is allowlisted.
        return True
    return False


def matches_allowlist(rule_id, url, nodes, allowlist,
                      engines_dict=None, outcome=None):
    """Check if a result matches any allowlist entry.

    Allowlist entries use normalized tags (sc-*, aria-*, bp-*) in
    the `rule` field.  All specified filters are AND — a finding
    must match all of them to be suppressed.

    Args:
        rule_id:      Normalized finding ID
                      (e.g. 'sc-2.4.7', 'bp-landmarks')
        url:          Page URL
        nodes:        List of node dicts
        allowlist:    Allowlist instance OR a plain list of entry
                      dicts (legacy).
        engines_dict: Engines attribution dict from deduped findings
                      (e.g. {'ibm': {...}, 'axe': {...}})
        outcome:      EARL outcome ('failed', 'cantTell')

    Returns True if the result should be suppressed.
    """
    if isinstance(allowlist, Allowlist):
        return allowlist.matches(
            rule_id, url, nodes,
            engines_dict=engines_dict, outcome=outcome)
    # Legacy: caller passed a plain list.  Filter to entries with
    # the matching rule first so the secondary checks only see
    # candidates.
    if not allowlist:
        return False
    candidates = [e for e in allowlist
                  if isinstance(e, dict) and e.get('rule') == rule_id]
    if not candidates:
        return False
    return _match_any(
        candidates, rule_id, url, nodes,
        engines_dict, outcome)


def classify_page(deduped_data, page_url, allowlist, totals):
    """Update running totals from one already-deduped page result.

    Reads the failed/cantTell items, applies the allowlist, and
    increments the per-tag-class node counters in `totals`.  Used
    by `crawl_and_scan` to keep a running tally so main() doesn't
    need to re-iterate the JSONL after the scan.

    `totals` is a `results.RunningTotals` instance (mutated
    in place).  For backward compat with old callers that pass a
    plain dict, dict-style access is also accepted.
    """
    is_dict = isinstance(totals, dict)

    def _bump(field, n):
        if is_dict:
            totals[field] += n
        else:
            setattr(totals, field, getattr(totals, field) + n)

    def _add_rule(rule_id):
        if is_dict:
            totals['rules'].add(rule_id)
        else:
            totals.rules.add(rule_id)

    for v in deduped_data.get(EARL_FAILED, []):
        if allowlist and matches_allowlist(
                v.get('id', ''), page_url,
                v.get('nodes', []), allowlist,
                engines_dict=v.get('engines'),
                outcome=EARL_FAILED):
            continue
        tags = v.get('tags', [])
        nodes = count_nodes([v])
        # Classification priority: aria-* wins over sc-* — prevents
        # ARIA landmark naming rules (which IBM mis-maps to
        # SC 2.4.1) from inflating the WCAG count.
        if any(t.startswith('aria-') for t in tags):
            _bump('aria', nodes)
        elif any(t.startswith('sc-') for t in tags):
            _bump('wcag', nodes)
            _add_rule(v.get('id', ''))
        else:
            # bp-* tagged or untagged — both go into the
            # best-practice bucket.
            _bump('bp', nodes)

    for v in deduped_data.get(EARL_CANTTELL, []):
        if allowlist and matches_allowlist(
                v.get('id', ''), page_url,
                v.get('nodes', []), allowlist,
                engines_dict=v.get('engines'),
                outcome=EARL_CANTTELL):
            continue
        _bump('incomplete', count_nodes([v]))
