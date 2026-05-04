"""--diff CLI: print what changed between two scans.

This is the *printing* variant of diff used by `a11y-catscan --diff`.
For the structured (dict-returning) variant used by the MCP server,
see `registry.diff_scans`.
"""

from urllib.parse import urlparse

from engine_mappings import EARL_FAILED
from report_io import iter_jsonl


def print_diff(old_jsonl, new_jsonl, allowlist=None):
    """Compare two scans and print what changed.

    Returns (fixed_count, new_count) violation nodes.

    Note: distinct from `registry.diff_scans` — that one returns a
    structured dict for programmatic consumers (the MCP server).
    This is the printing variant used by the --diff CLI flag.
    """
    allowlist = allowlist or []

    def _violation_keys(jsonl_path):
        """Return {(url_path, rule_id): node_count} for violations."""
        keys = {}
        for url, data in iter_jsonl(jsonl_path):
            path = urlparse(url).path
            for v in data.get(EARL_FAILED, []):
                key = (path, v.get('id', ''))
                keys[key] = keys.get(key, 0) + len(v.get('nodes', []))
        return keys

    old = _violation_keys(old_jsonl)
    new = _violation_keys(new_jsonl)

    fixed = {k: v for k, v in old.items() if k not in new}
    added = {k: v for k, v in new.items() if k not in old}
    remaining = {k: v for k, v in new.items() if k in old}

    if fixed:
        print("\n  FIXED ({} rule/page combos, {} nodes):".format(
            len(fixed), sum(fixed.values())))
        for (path, rule), count in sorted(fixed.items()):
            print("    - {} on {} ({} nodes)".format(
                rule, path, count))

    if added:
        print("\n  NEW ({} rule/page combos, {} nodes):".format(
            len(added), sum(added.values())))
        for (path, rule), count in sorted(added.items()):
            print("    + {} on {} ({} nodes)".format(
                rule, path, count))

    if remaining:
        print("\n  REMAINING ({} rule/page combos, {} nodes)".format(
            len(remaining), sum(remaining.values())))

    if not fixed and not added:
        print("\n  No changes in violations.")

    return sum(fixed.values()), sum(added.values())
