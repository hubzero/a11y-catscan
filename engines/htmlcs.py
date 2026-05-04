"""
HTML_CodeSniffer engine for a11y-catscan.

Injects HTMLCS.js into the page and calls HTMLCS.process().  Same
mechanism as the HTML_CodeSniffer bookmarklet.

Engine:     HTML_CodeSniffer (originally by Squiz, maintained by community)
Type:       Browser injection (JavaScript)
License:    BSD-3-Clause
Upstream:   https://github.com/nickersk/HTML_CodeSniffer
npm:        html_codesniffer
Synced to:  2.5.1 (2026-04-20)

Native result types and EARL mapping:

    HTMLCS returns messages with a numeric type field:

        type    Name      → EARL outcome
        ────────────────────────────────────
        1       ERROR     → failed      Definite WCAG failure
        2       WARNING   → cantTell    Needs manual review
        3       NOTICE    → (skipped)   Informational — not actionable

    HTMLCS does not have a concept of "passed" or "inapplicable" —
    it only reports problems and warnings.

WCAG mapping:

    Every HTMLCS rule code embeds its WCAG SC directly:

        WCAG2AA.Principle1.Guideline1_4.1_4_3.G18
                                        ^^^^^
                                        SC 1.4.3

    We extract the SC with htmlcs_code_to_sc() from engine_mappings.py
    and convert it to normalized tags ('sc-1.4.3').

    There are no HTMLCS best-practice rules — every rule is
    WCAG-derived by definition (the code format requires it).

Level handling:

    --level maps to HTMLCS standard names:
        wcag21a   → 'WCAG2A'    (Level A rules only)
        wcag21aa  → 'WCAG2AA'   (Level A + AA rules, default)
        wcag21aaa → 'WCAG2AAA'  (All levels)

    The standard name determines which WCAG principles/guidelines
    are active.  HTMLCS does not distinguish WCAG versions (2.0/2.1/2.2)
    — it uses a single ruleset per conformance level.

Rule code format:

    WCAG2AA.Principle{N}.Guideline{X_Y}.{X_Y_Z}.TechniqueCode

    Example: WCAG2AA.Principle1.Guideline1_3.1_3_1.H39.3.NoCaption
    - Standard: WCAG2AA
    - Principle 1 (Perceivable)
    - Guideline 1.3 (Adaptable)
    - SC 1.3.1 (Info and Relationships)
    - Technique H39.3
    - Check: NoCaption

    The last segment (e.g. 'NoCaption') is used as the rule ID in
    normalized results.

Rules:

    80 sniff files in HTML_CodeSniffer 2.5.1 covering 76 WCAG SCs.
    No best-practice rules — every check maps to a WCAG SC.
    See HTMLCS_SNIFFS dict below for the complete SC inventory.
"""

import os
import sys

from .base import Engine, SCRIPT_DIR, NODE_MODULES
from engine_mappings import (
    htmlcs_code_to_sc, EARL_FAILED, EARL_CANTTELL)

# HTMLCS message type → EARL outcome.
HTMLCS_OUTCOME_MAP = {
    1: EARL_FAILED,     # ERROR — definite failure
    2: EARL_CANTTELL,   # WARNING — needs manual review
    3: None,            # NOTICE — informational, not emitted
}

# WCAG SCs covered by HTMLCS sniff files (80 sniffs, 76 unique SCs).
# Each entry: 'X.Y.Z': (SC name, conformance level)
# Some SCs have multiple sniff files (e.g. 1.4.3 has 1_4_3.js,
# 1_4_3_Contrast.js, 1_4_3_F24.js).
# Synced to html_codesniffer 2.5.1 — check Standards/WCAG2AAA/Sniffs/.
HTMLCS_SNIFFS = {
    # Level A
    '1.1.1': ('Non-text Content', 'A'),
    '1.2.1': ('Audio-only and Video-only', 'A'),
    '1.2.2': ('Captions (Prerecorded)', 'A'),
    '1.2.3': ('Audio Description or Media Alternative', 'A'),
    '1.3.1': ('Info and Relationships', 'A'),  # also has _A.js, _AAA.js variants
    '1.3.2': ('Meaningful Sequence', 'A'),
    '1.3.3': ('Sensory Characteristics', 'A'),
    '1.3.4': ('Orientation', 'AA'),  # WCAG 2.1
    '1.3.5': ('Identify Input Purpose', 'AA'),  # WCAG 2.1
    '1.3.6': ('Identify Purpose', 'AAA'),  # WCAG 2.1
    '1.4.1': ('Use of Color', 'A'),
    '1.4.2': ('Audio Control', 'A'),
    '1.4.3': ('Contrast (Minimum)', 'AA'),  # 3 sniff files
    '1.4.4': ('Resize Text', 'AA'),
    '1.4.5': ('Images of Text', 'AA'),
    '1.4.6': ('Contrast (Enhanced)', 'AAA'),
    '1.4.7': ('Low or No Background Audio', 'AAA'),
    '1.4.8': ('Visual Presentation', 'AAA'),
    '1.4.9': ('Images of Text (No Exception)', 'AAA'),
    '1.4.10': ('Reflow', 'AA'),  # WCAG 2.1
    '1.4.11': ('Non-text Contrast', 'AA'),  # WCAG 2.1
    '1.4.12': ('Text Spacing', 'AA'),  # WCAG 2.1
    '1.4.13': ('Content on Hover or Focus', 'AA'),  # WCAG 2.1
    '2.1.1': ('Keyboard', 'A'),
    '2.1.2': ('No Keyboard Trap', 'A'),
    '2.1.4': ('Character Key Shortcuts', 'A'),  # WCAG 2.1
    '2.2.1': ('Timing Adjustable', 'A'),
    '2.2.2': ('Pause, Stop, Hide', 'A'),
    '2.2.3': ('No Timing', 'AAA'),
    '2.2.4': ('Interruptions', 'AAA'),
    '2.2.5': ('Re-authenticating', 'AAA'),
    '2.2.6': ('Timeouts', 'AAA'),  # WCAG 2.1
    '2.3.1': ('Three Flashes or Below Threshold', 'A'),
    '2.3.2': ('Three Flashes', 'AAA'),
    '2.3.3': ('Animation from Interactions', 'AAA'),
    '2.4.1': ('Bypass Blocks', 'A'),
    '2.4.2': ('Page Titled', 'A'),
    '2.4.3': ('Focus Order', 'A'),
    '2.4.4': ('Link Purpose (In Context)', 'A'),
    '2.4.5': ('Multiple Ways', 'AA'),
    '2.4.6': ('Headings and Labels', 'AA'),
    '2.4.7': ('Focus Visible', 'AA'),
    '2.4.8': ('Location', 'AAA'),
    '2.4.9': ('Link Purpose (Link Only)', 'AAA'),
    '2.5.1': ('Pointer Gestures', 'A'),  # WCAG 2.1
    '2.5.2': ('Pointer Cancellation', 'A'),  # WCAG 2.1
    '2.5.3': ('Label in Name', 'A'),  # WCAG 2.1
    '2.5.4': ('Motion Actuation', 'A'),  # WCAG 2.1
    '2.5.5': ('Target Size', 'AAA'),  # WCAG 2.1
    '2.5.6': ('Concurrent Input Mechanisms', 'AAA'),  # WCAG 2.1
    '3.1.1': ('Language of Page', 'A'),
    '3.1.2': ('Language of Parts', 'AA'),
    '3.1.3': ('Unusual Words', 'AAA'),
    '3.1.4': ('Abbreviations', 'AAA'),
    '3.1.5': ('Reading Level', 'AAA'),
    '3.1.6': ('Pronunciation', 'AAA'),
    '3.2.1': ('On Focus', 'A'),
    '3.2.2': ('On Input', 'A'),
    '3.2.3': ('Consistent Navigation', 'AA'),
    '3.2.4': ('Consistent Identification', 'AA'),
    '3.2.5': ('Change on Request', 'AAA'),
    '3.3.1': ('Error Identification', 'A'),
    '3.3.2': ('Labels or Instructions', 'A'),
    '3.3.3': ('Error Suggestion', 'AA'),
    '3.3.4': ('Error Prevention (Legal, Financial, Data)', 'AA'),
    '3.3.5': ('Help', 'AAA'),
    '3.3.6': ('Error Prevention (All)', 'AAA'),
    '4.1.1': ('Parsing', 'A'),
    '4.1.2': ('Name, Role, Value', 'A'),
    '4.1.3': ('Status Messages', 'AA'),  # WCAG 2.1
}


def _sc_to_wcag_tags(sc):
    """Convert a WCAG SC number like '1.4.3' to normalized tags."""
    if not sc:
        return []
    return ['sc-{}'.format(sc)]


class HtmlcsEngine(Engine):
    """HTML_CodeSniffer engine — injects HTMLCS.js and calls process()."""

    name = 'htmlcs'

    def __init__(self, scan_level, verbose=False, quiet=False):
        super().__init__(scan_level, verbose, quiet)
        self._source = None
        self._standard = self._map_standard(scan_level)

    @staticmethod
    def _map_standard(scan_level):
        """Map scan_level string to HTMLCS standard name.

        scan_level is like 'wcag21aa', 'wcag22aaa', 'wcag21a'.
        The conformance level is the trailing a/aa/aaa after the
        version digits.
        """
        if scan_level.endswith('aaa'):
            return 'WCAG2AAA'
        if scan_level.endswith('aa'):
            return 'WCAG2AA'
        if scan_level.endswith('a'):
            return 'WCAG2A'
        return 'WCAG2AA'

    async def start(self, browser=None):
        htmlcs_path = os.path.join(
            NODE_MODULES, 'html_codesniffer', 'build', 'HTMLCS.js')
        if not os.path.exists(htmlcs_path):
            print("ERROR: HTML_CodeSniffer not found. "
                  "Run: npm install", file=sys.stderr)
            raise FileNotFoundError(htmlcs_path)
        with open(htmlcs_path, 'r') as f:
            self._source = f.read()

    async def scan(self, page):
        if not self._source:
            return []

        out = []
        try:
            await page.add_script_tag(content=self._source)
            htmlcs_results = await page.evaluate(
                """(std) => {
                    function _sel(el) {
                        if (!el || el === document) return '';
                        if (el.id) return '#' + CSS.escape(el.id);
                        var path = [];
                        var node = el;
                        while (node && node.parentElement) {
                            var seg = node.tagName.toLowerCase();
                            var parent = node.parentElement;
                            var sibs = Array.from(parent.children)
                                .filter(function(c) {
                                    return c.tagName === node.tagName;
                                });
                            if (sibs.length > 1) {
                                seg += ':nth-of-type('
                                    + (sibs.indexOf(node) + 1) + ')';
                            }
                            if (node.id) {
                                path.unshift('#'
                                    + CSS.escape(node.id));
                                break;
                            }
                            path.unshift(seg);
                            node = parent;
                        }
                        return path.join(' > ');
                    }
                    return new Promise(r => {
                        HTMLCS.process(
                            std, document, () => {
                            r(HTMLCS.getMessages()
                                .map(m => ({
                                type: m.type,
                                code: m.code || '',
                                msg: m.msg || '',
                                selector: m.element
                                    ? _sel(m.element) : '',
                                html: m.element
                                    && m.element.outerHTML
                                    ? m.element.outerHTML
                                        .substring(0, 200)
                                    : ''
                            })));
                        });
                    });
                }""", self._standard)

            for r in htmlcs_results:
                t = r.get('type', 0)
                code = r.get('code', '')
                sc = htmlcs_code_to_sc(code)
                tags = _sc_to_wcag_tags(sc) if sc else []

                selector = r.get('selector', '')
                node = {
                    'target': [selector] if selector else [r.get('code', '')],
                    'html': r.get('html', ''),
                    'any': [{'message': r.get('msg', '')}],
                }

                # type 1 = ERROR -> failed, type 2 = WARNING -> cantTell
                if t == 1:
                    out.append({
                        'id': code.split('.')[-1],
                        'engine': 'htmlcs',
                        'outcome': EARL_FAILED,
                        'description': r.get('msg', ''),
                        'help': r.get('msg', ''),
                        'helpUrl': '',
                        'impact': 'serious',
                        'tags': tags,
                        'nodes': [node],
                    })
                elif t == 2:
                    out.append({
                        'id': code.split('.')[-1],
                        'engine': 'htmlcs',
                        'outcome': EARL_CANTTELL,
                        'description': r.get('msg', ''),
                        'help': r.get('msg', ''),
                        'helpUrl': '',
                        'impact': 'moderate',
                        'tags': tags,
                        'nodes': [node],
                    })
        except Exception as e:
            if self.verbose and not self.quiet:
                print("  htmlcs error: {}".format(e))

        return out
