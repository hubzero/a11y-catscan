"""
IBM Equal Access engine for a11y-catscan.

Injects ace.js into the page and calls ace.Checker().check().  Same
mechanism as the IBM Accessibility Checker browser extension.

Engine:     IBM Equal Access / accessibility-checker-engine
Type:       Browser injection (JavaScript)
License:    Apache-2.0
Upstream:   https://github.com/IBMa/equal-access
npm:        accessibility-checker-engine
Rule docs:  https://www.ibm.com/able/requirements/checker-rule-sets
Synced to:  4.0.16 (2026-04-20)

Native result types and EARL mapping:

    IBM uses a two-dimensional classification: policy × confidence.

    Policy (value[0]):
        VIOLATION       Rule is a WCAG requirement
        RECOMMENDATION  Rule is a best practice (not strictly WCAG)
        INFORMATION     Informational notice

    Confidence (value[1]):
        PASS            Element passed
        FAIL            Definite failure
        POTENTIAL       Automated check inconclusive
        MANUAL          Requires manual verification

    Mapping to EARL:

        VIOLATION + FAIL       → failed      WCAG failure
        RECOMMENDATION + FAIL  → failed      Best-practice failure (only with --level best)
        * + POTENTIAL           → cantTell    Needs manual review
        * + PASS                → passed      (not currently emitted — too noisy)
        INFORMATION + *         → (skipped)   Informational only

    158 rules are mapped to WCAG SCs via IBM_SC_MAP in engine_mappings.py.
    15 rules can produce RECOMMENDATION results; these are mapped to
    best-practice categories via IBM_BP_MAP.

    IBM's ACT rule mapping field explicitly uses EARL outcome names
    (pass/fail/cantTell/inapplicable), confirming alignment.

WCAG level handling:

    --level maps to IBM rulesets:
        wcag20*  → 'WCAG_2_0'
        wcag21*  → 'WCAG_2_1'  (default)
        wcag22*  → 'WCAG_2_2'

    The ruleset controls which rules are active.  IBM does not filter
    by A/AA/AAA — the VIOLATION vs RECOMMENDATION category handles
    severity.  Our engine_mappings.py provides the SC-level detail.

Rules:

    158 rules in ace.js 4.0.16.  All map to WCAG SCs via IBM_SC_MAP
    in engine_mappings.py.  15 rules can also produce RECOMMENDATION
    (best-practice) results.  See IBM_RECOMMENDATION_RULES below.
"""

import os
import sys

from .base import Engine, SCRIPT_DIR, NODE_MODULES
from engine_mappings import (
    ibm_rule_to_tags, bp_category, aria_category,
    EARL_FAILED, EARL_CANTTELL)

# IBM result value[1] (confidence) → EARL outcome.
# value[0] (policy: VIOLATION/RECOMMENDATION/INFORMATION) determines
# whether a FAIL is a WCAG failure or a best-practice failure.
IBM_OUTCOME_MAP = {
    'FAIL': EARL_FAILED,       # Definite failure (WCAG or best-practice)
    'POTENTIAL': EARL_CANTTELL,  # Needs manual review
    'PASS': 'passed',           # Element passed (not currently emitted)
    'MANUAL': EARL_CANTTELL,    # Requires manual check
}

# Rules that can produce RECOMMENDATION (best-practice) results.
# Most IBM rules only produce VIOLATION results.  These 15 can
# produce RECOMMENDATION, meaning the finding is a best practice
# rather than a strict WCAG requirement.
#
# Each entry: 'rule_id': (description, [WCAG SCs])
# Synced to ace.js 4.0.16 — run a scan and check value[0] to verify.
IBM_RECOMMENDATION_RULES = {
    'a_target_warning':            ('Links opening new windows warn user', ['3.2.2']),
    'aria_child_valid':            ('Valid ARIA child roles', ['1.3.1']),
    'aria_content_in_landmark':    ('Content in landmarks', ['2.4.1']),
    'aria_contentinfo_misuse':     ('Contentinfo used correctly', ['2.4.1']),
    'element_mouseevent_keyboard': ('Mouse events have keyboard equivalent', ['2.1.1']),
    'element_tabbable_role_valid': ('Tabbable elements have appropriate roles', []),
    'fieldset_legend_valid':       ('Fieldset has valid legend', ['1.3.1']),
    'heading_content_exists':      ('Headings have content', ['2.4.6']),
    'img_alt_background':          ('Background image alt text reviewed', ['1.1.1']),
    'input_fields_grouped':        ('Related form fields grouped', ['1.3.1']),
    'media_alt_brief':             ('Media alt text is brief', ['1.1.1']),
    'script_onclick_avoid':        ('Avoid onclick on non-interactive elements', ['2.1.1']),
    'select_options_grouped':      ('Select options grouped', ['1.3.1']),
    'style_highcontrast_visible':  ('High contrast mode visibility', ['1.1.1', '1.3.2', '1.4.11']),
    'table_layout_linearized':     ('Layout tables linearize correctly', ['1.3.1']),
}


class IbmEngine(Engine):
    """IBM Equal Access engine — injects ace.js and calls Checker.check()."""

    name = 'ibm'

    def __init__(self, scan_level, verbose=False, quiet=False,
                 include_best=False):
        super().__init__(scan_level, verbose, quiet)
        self.include_best = include_best
        self._source = None
        self._ruleset = self._map_ruleset(scan_level)

    @staticmethod
    def _map_ruleset(scan_level):
        """Map scan_level string to IBM ruleset name."""
        if '22' in scan_level:
            return 'WCAG_2_2'
        elif '20' in scan_level:
            return 'WCAG_2_0'
        return 'WCAG_2_1'

    async def start(self, browser=None):
        ace_path = os.path.join(
            NODE_MODULES, 'accessibility-checker-engine', 'ace.js')
        if not os.path.exists(ace_path):
            print("ERROR: IBM Equal Access not found. "
                  "Run: npm install", file=sys.stderr)
            raise FileNotFoundError(ace_path)
        with open(ace_path, 'r') as f:
            self._source = f.read()

    async def scan(self, page):
        if not self._source:
            return []

        out = []
        try:
            await page.add_script_tag(content=self._source)
            ibm_results = await page.evaluate(
                """(rs) => {
                    return new ace.Checker()
                        .check(document, [rs]);
                }""", self._ruleset)

            for r in ibm_results.get('results', []):
                cat = r.get('value', ['', ''])[0]
                outcome = r.get('value', ['', ''])[1]
                is_wcag = (cat == 'VIOLATION')
                tags = ibm_rule_to_tags(r['ruleId'])
                if not is_wcag:
                    tags.append('best-practice')
                    bp_cat = bp_category('ibm', r['ruleId'])
                    if bp_cat:
                        tags.append('bp-' + bp_cat)
                aria_cat = aria_category('ibm', r['ruleId'])
                if aria_cat:
                    tags.append('aria-' + aria_cat)

                node = {
                    'target': [r.get('path', {}).get('dom', '')],
                    'html': r.get('path', {}).get('dom', ''),
                    'any': [{'message': r.get('message', '')}],
                }

                if outcome == 'FAIL' and (is_wcag or self.include_best):
                    out.append({
                        'id': r['ruleId'],
                        'engine': 'ibm',
                        'outcome': EARL_FAILED,
                        'description': r.get('message', ''),
                        'help': r.get('message', ''),
                        'helpUrl': '',
                        'impact': 'serious' if is_wcag else 'minor',
                        'tags': tags,
                        'nodes': [node],
                    })
                elif outcome == 'POTENTIAL':
                    out.append({
                        'id': r['ruleId'],
                        'engine': 'ibm',
                        'outcome': EARL_CANTTELL,
                        'description': r.get('message', ''),
                        'help': r.get('message', ''),
                        'helpUrl': '',
                        'impact': 'moderate',
                        'tags': tags,
                        'nodes': [node],
                    })
        except Exception as e:
            if self.verbose and not self.quiet:
                print("  ibm error: {}".format(e))

        return out
