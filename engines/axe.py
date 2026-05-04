"""
axe-core engine for a11y-catscan.

Injects axe.min.js into the page and calls axe.run().  Same mechanism
as the axe browser extensions and axe-core/playwright integration.

Engine:     axe-core (Deque Systems)
Type:       Browser injection (JavaScript)
License:    MPL-2.0
Upstream:   https://github.com/dequelabs/axe-core
npm:        axe-core
Synced to:  4.11.3 (2026-04-20)

Native result types and EARL mapping:

    axe category    → EARL outcome
    ─────────────────────────────────
    violations      → failed        Definite WCAG failure
    incomplete      → cantTell      Needs manual review (e.g. color on gradient)
    passes          → passed        Element passed the check
    inapplicable    → inapplicable  Rule does not apply to this page

    axe-core uses EARL internally: its RawNodeResult type is
    'passed' | 'failed' | 'cantTell'.  The user-facing category
    names (violations, incomplete, passes) are presentation aliases.

WCAG mapping:

    axe-core tags each rule with WCAG SC identifiers:
        'wcag143'  → SC 1.4.3 (Contrast Minimum)
        'wcag2a'   → Level A
        'wcag21aa' → WCAG 2.1 Level AA

    Rules without wcag* tags are best practices.  We tag these with
    'best-practice' and a 'bp-<category>' tag from AXE_BP_MAP.

    30 rules are best practices (not WCAG-mapped).

Level handling:

    --level maps to axe runOnly tag filters:
        wcag21a   → ['wcag2a', 'wcag21a']
        wcag21aa  → ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']
        wcag21aaa → all tags
        best      → includes 'best-practice' tag

    When --rule or --tags are specified, they override level-based
    tag filtering.

Rules:

    104 rules in axe-core 4.11.3 (74 WCAG + 30 best-practice).
    See AXE_RULES dict below for the complete list with EARL outcome
    mapping and WCAG SC references.
"""

import os
import re
import sys

from .base import Engine, SCRIPT_DIR, NODE_MODULES

# Bundled axe-core JS path (constant relative to node_modules).
AXE_JS_PATH = os.path.join(NODE_MODULES, 'axe-core', 'axe.min.js')

_AXE_VERSION = None


def get_axe_version():
    """Read the bundled axe-core version from its JS header.

    Cached after first call.  Returns 'unknown' if the file is
    present but unparseable; 'not installed' if missing.
    """
    global _AXE_VERSION
    if _AXE_VERSION is not None:
        return _AXE_VERSION
    try:
        with open(AXE_JS_PATH, 'r') as f:
            header = f.read(200)
        m = re.search(r'axe v([\d.]+)', header)
        _AXE_VERSION = m.group(1) if m else 'unknown'
    except (IOError, OSError):
        _AXE_VERSION = 'not installed'
    return _AXE_VERSION
from engine_mappings import (
    bp_category, aria_category,
    EARL_FAILED, EARL_CANTTELL, EARL_PASSED, EARL_INAPPLICABLE)


_AXE_SC_TAG_RE = re.compile(r'^wcag(\d)(\d)(\d+)$')
_AXE_LEVEL_TAG_RE = re.compile(r'^wcag\d+a')


def _normalize_axe_tags(tags):
    """Convert axe-core native tags to normalized format.

    axe-core tags like 'wcag143' (SC 1.4.3) become 'sc-1.4.3'.
    Level tags like 'wcag2a', 'wcag21aa' are dropped (internal to axe).
    Other tags (cat.*, best-practice, ACT) are kept as-is.
    """
    out = []
    for tag in tags:
        # SC tags: wcag + single digit + single digit + one-or-more digits
        m = _AXE_SC_TAG_RE.match(tag)
        if m:
            out.append('sc-{}.{}.{}'.format(
                m.group(1), m.group(2), m.group(3)))
        elif _AXE_LEVEL_TAG_RE.match(tag):
            # Level/version tags like wcag2a, wcag21aa — drop
            continue
        else:
            out.append(tag)
    return out

# axe-core result categories → EARL outcomes.
# Every rule can produce any of these four categories.
AXE_OUTCOME_MAP = {
    'violations': EARL_FAILED,       # Definite failure
    'incomplete': EARL_CANTTELL,      # Needs manual review
    'passes': EARL_PASSED,            # Element passed
    'inapplicable': EARL_INAPPLICABLE,  # Rule does not apply
}

# Complete rule inventory for axe-core 4.11.3.
# Each entry: 'rule-id': (description, [WCAG SCs], is_best_practice)
# WCAG SCs are empty for best-practice-only rules.
# Synced to axe-core 4.11.3 — run `axe.getRules()` to verify.
AXE_RULES = {
    # WCAG rules (74)
    'area-alt':                    ('Image map areas have alt text', ['1.1.1'], False),
    'aria-allowed-attr':           ('ARIA attributes valid for role', ['4.1.2'], False),
    'aria-braille-equivalent':     ('Braille ARIA has non-braille equivalent', ['4.1.2'], False),
    'aria-command-name':           ('ARIA buttons/links have accessible name', ['4.1.2'], False),
    'aria-conditional-attr':       ('ARIA conditional attrs used correctly', ['4.1.2'], False),
    'aria-deprecated-role':        ('No deprecated ARIA roles', ['4.1.2'], False),
    'aria-hidden-body':            ('aria-hidden not on body', ['4.1.2'], False),
    'aria-hidden-focus':           ('aria-hidden elements not focusable', ['4.1.2'], False),
    'aria-input-field-name':       ('ARIA input fields have names', ['4.1.2'], False),
    'aria-meter-name':             ('ARIA meters have names', ['4.1.2'], False),
    'aria-progressbar-name':       ('ARIA progressbars have names', ['4.1.2'], False),
    'aria-prohibited-attr':        ('No prohibited ARIA attrs', ['4.1.2'], False),
    'aria-required-attr':          ('Required ARIA attrs present', ['4.1.2'], False),
    'aria-required-children':      ('Required ARIA children present', ['1.3.1'], False),
    'aria-required-parent':        ('Required ARIA parents present', ['1.3.1'], False),
    'aria-roledescription':        ('roledescription on valid roles', ['4.1.2'], False),
    'aria-roles':                  ('Valid ARIA role values', ['4.1.2'], False),
    'aria-toggle-field-name':      ('ARIA toggle fields have names', ['4.1.2'], False),
    'aria-tooltip-name':           ('ARIA tooltips have names', ['4.1.2'], False),
    'aria-valid-attr':             ('Valid aria-* attribute names', ['4.1.2'], False),
    'aria-valid-attr-value':       ('Valid aria-* attribute values', ['4.1.2'], False),
    'audio-caption':               ('Audio has captions', ['1.2.1'], False),
    'autocomplete-valid':          ('Autocomplete attrs valid', ['1.3.5'], False),
    'avoid-inline-spacing':        ('Text spacing adjustable', ['1.4.12'], False),
    'blink':                       ('No blink elements', ['2.2.2'], False),
    'button-name':                 ('Buttons have discernible text', ['4.1.2'], False),
    'bypass':                      ('Skip navigation mechanism', ['2.4.1'], False),
    'color-contrast':              ('Sufficient color contrast', ['1.4.3'], False),
    'color-contrast-enhanced':     ('Enhanced color contrast', ['1.4.6'], False),
    'css-orientation-lock':        ('Not locked to orientation', ['1.3.4'], False),
    'definition-list':             ('dl structured correctly', ['1.3.1'], False),
    'dlitem':                      ('dt/dd in dl', ['1.3.1'], False),
    'document-title':              ('Page has title', ['2.4.2'], False),
    'duplicate-id':                ('Unique id values', ['4.1.1'], False),
    'duplicate-id-active':         ('Unique active element ids', ['4.1.1'], False),
    'duplicate-id-aria':           ('Unique ARIA/label ids', ['4.1.1'], False),
    'form-field-multiple-labels':  ('No multiple labels', ['1.3.1'], False),
    'frame-focusable-content':     ('Focusable frame content accessible', ['4.1.2'], False),
    'frame-title':                 ('Frames have titles', ['4.1.2'], False),
    'frame-title-unique':          ('Frame titles unique', ['4.1.2'], False),
    'html-has-lang':               ('HTML has lang', ['3.1.1'], False),
    'html-lang-valid':             ('Valid lang value', ['3.1.1'], False),
    'html-xml-lang-mismatch':      ('lang/xml:lang match', ['3.1.1'], False),
    'identical-links-same-purpose': ('Same-name links same purpose', ['2.4.9'], False),
    'image-alt':                   ('Images have alt text', ['1.1.1'], False),
    'input-button-name':           ('Input buttons have text', ['4.1.2'], False),
    'input-image-alt':             ('Input images have alt', ['1.1.1'], False),
    'label':                       ('Form elements have labels', ['4.1.2'], False),
    'label-content-name-mismatch': ('Label matches visible text', ['2.5.3'], False),
    'link-in-text-block':          ('Links distinguished from text', ['1.4.1'], False),
    'link-name':                   ('Links have discernible text', ['4.1.2'], False),
    'list':                        ('Lists structured correctly', ['1.3.1'], False),
    'listitem':                    ('li in ul/ol', ['1.3.1'], False),
    'marquee':                     ('No marquee elements', ['2.2.2'], False),
    'meta-refresh':                ('No meta refresh', ['2.2.1'], False),
    'meta-refresh-no-exceptions':  ('No meta refresh', ['2.2.4'], False),
    'meta-viewport':               ('Viewport allows zoom', ['1.4.4'], False),
    'nested-interactive':          ('No nested interactive elements', ['4.1.2'], False),
    'no-autoplay-audio':           ('No autoplay audio', ['1.4.2'], False),
    'object-alt':                  ('Objects have alt text', ['1.1.1'], False),
    'p-as-heading':                ('p not styled as heading', ['1.3.1'], False),
    'role-img-alt':                ('role=img has alt', ['1.1.1'], False),
    'scrollable-region-focusable': ('Scrollable regions focusable', ['2.1.1'], False),
    'select-name':                 ('Selects have accessible name', ['4.1.2'], False),
    'server-side-image-map':       ('No server-side image maps', ['2.1.1'], False),
    'summary-name':                ('Summary has name', ['4.1.2'], False),
    'svg-img-alt':                 ('SVG images have alt', ['1.1.1'], False),
    'table-fake-caption':          ('Tables use caption element', ['1.3.1'], False),
    'target-size':                 ('Touch targets large enough', ['2.5.8'], False),
    'td-has-header':               ('Data cells have headers', ['1.3.1'], False),
    'td-headers-attr':             ('headers attr refs valid', ['1.3.1'], False),
    'th-has-data-cells':           ('th has data cells', ['1.3.1'], False),
    'valid-lang':                  ('Valid lang values', ['3.1.2'], False),
    'video-caption':               ('Video has captions', ['1.2.2'], False),
    # Best-practice rules (30) — no WCAG SC, all map to bp-* categories
    'accesskeys':                  ('Unique accesskey values', [], True),
    'aria-allowed-role':           ('Appropriate roles for elements', [], True),
    'aria-dialog-name':            ('Dialogs have accessible names', [], True),
    'aria-text':                   ('role=text used correctly', [], True),
    'aria-treeitem-name':          ('Treeitems have accessible names', [], True),
    'empty-heading':               ('Headings have discernible text', [], True),
    'empty-table-header':          ('Table headers have text', [], True),
    'focus-order-semantics':       ('Focus order roles appropriate', [], True),
    'frame-tested':                ('Frames contain axe script', [], True),
    'heading-order':               ('Heading order semantically correct', [], True),
    'hidden-content':              ('Hidden content flagged for review', [], True),
    'image-redundant-alt':         ('Alt not repeated as adjacent text', [], True),
    'label-title-only':            ('Visible labels, not title-only', [], True),
    'landmark-banner-is-top-level':       ('Banner at top level', [], True),
    'landmark-complementary-is-top-level': ('Complementary at top level', [], True),
    'landmark-contentinfo-is-top-level':   ('Contentinfo at top level', [], True),
    'landmark-main-is-top-level':          ('Main at top level', [], True),
    'landmark-no-duplicate-banner':        ('One banner landmark', [], True),
    'landmark-no-duplicate-contentinfo':   ('One contentinfo landmark', [], True),
    'landmark-no-duplicate-main':          ('One main landmark', [], True),
    'landmark-one-main':           ('Has main landmark', [], True),
    'landmark-unique':             ('Unique landmark labels', [], True),
    'meta-viewport-large':         ('Viewport allows significant zoom', [], True),
    'page-has-heading-one':        ('Page has h1', [], True),
    'presentation-role-conflict':  ('No presentation role conflicts', [], True),
    'region':                      ('Content in landmarks', [], True),
    'scope-attr-valid':            ('Correct scope attributes', [], True),
    'skip-link':                   ('Skip links have focusable targets', [], True),
    'tabindex':                    ('No tabindex > 0', [], True),
    'table-duplicate-name':        ('Caption/summary not duplicated', [], True),
}


class AxeEngine(Engine):
    """axe-core engine — injects axe.min.js and calls axe.run()."""

    name = 'axe'

    def __init__(self, scan_level, verbose=False, quiet=False,
                 tags=None, rules=None):
        super().__init__(scan_level, verbose, quiet)
        self.tags = tags
        self.rules = rules
        self._source = None
        self._run_opts = {}

    async def start(self, browser=None):
        if not os.path.exists(AXE_JS_PATH):
            print("ERROR: axe-core not found at {}".format(AXE_JS_PATH),
                  file=sys.stderr)
            print("Run: npm install", file=sys.stderr)
            raise FileNotFoundError(AXE_JS_PATH)
        with open(AXE_JS_PATH, 'r') as f:
            self._source = f.read()

        # Build run options — rules override tags
        if self.rules:
            self._run_opts = {
                'runOnly': {'type': 'rule', 'values': self.rules}}
        elif self.tags:
            self._run_opts = {
                'runOnly': {'type': 'tag', 'values': self.tags}}

    async def scan(self, page):
        if not self._source:
            return []

        await page.add_script_tag(content=self._source)
        axe_results = await page.evaluate(
            """(opts) => {
                return axe.run(document, opts)
                    .catch(e => ({error: e.toString()}));
            }""", self._run_opts)

        if not axe_results or 'error' in axe_results:
            err = (axe_results or {}).get('error', 'unknown error')
            if self.verbose and not self.quiet:
                print("  axe error: {}".format(err))
            return []

        # Map axe result categories to EARL outcomes
        axe_to_earl = {
            'violations': EARL_FAILED,
            'incomplete': EARL_CANTTELL,
            'passes': EARL_PASSED,
            'inapplicable': EARL_INAPPLICABLE,
        }
        out = []
        for axe_key, earl in axe_to_earl.items():
            for item in axe_results.get(axe_key, []):
                item['engine'] = 'axe'
                item['outcome'] = earl
                # Normalize axe tags: wcag143 → sc-1.4.3
                item['tags'] = _normalize_axe_tags(
                    item.get('tags', []))
                # Tag best-practice and ARIA items
                rule_id = item.get('id', '')
                tags = item['tags']
                bp_cat = bp_category('axe', rule_id)
                if bp_cat:
                    if 'best-practice' not in tags:
                        tags.append('best-practice')
                    tags.append('bp-' + bp_cat)
                aria_cat = aria_category('axe', rule_id)
                if aria_cat:
                    aria_tag = 'aria-' + aria_cat
                    if aria_tag not in tags:
                        tags.append(aria_tag)
                out.append(item)
        return out
