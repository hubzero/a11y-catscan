"""
Siteimprove Alfa engine for a11y-catscan.

Runs as a Node.js subprocess connected to the shared Chromium browser
via Chrome DevTools Protocol (CDP).  Unlike the injection-based engines,
Alfa uses Playwright's CDP connection to access already-loaded pages
without a second page load.

Engine:     Siteimprove Alfa
Type:       Node.js subprocess via CDP (TypeScript rule engine)
License:    MIT
Upstream:   https://github.com/Siteimprove/alfa
npm:        @siteimprove/alfa-rules, @siteimprove/alfa-playwright
Synced to:  0.114.3 (2026-04-20)

Architecture:

    Python (a11y-catscan)           Node.js (alfa-engine.mjs)
    ─────────────────────           ─────────────────────────
    Launch subprocess ────────────→ Start, connect to Chromium via CDP
    Send {cdp, level}  ───stdin──→ Load rules, filter by level
    Receive {rules, level} ←stdout─ Report ready
    Send {pageId: url}  ──stdin──→ Find page by URL, run rules
    Receive {ok, violations, ...} ←stdout─ Return results
    Send {quit: true}   ──stdin──→ Exit

    All communication is newline-delimited JSON over stdin/stdout.
    The subprocess connects to Chromium via connectOverCDP() using
    the WebSocket URL from http://127.0.0.1:9222/json/version.

    Alfa scans are serialized via asyncio.Lock because the subprocess
    is single-threaded and processes one page at a time.

Native result types and EARL mapping:

    Alfa uses W3C ACT Rules, which define EARL outcomes directly:

        Alfa Outcome.Value  → EARL outcome
        ─────────────────────────────────────
        'failed'            → failed        ACT rule failed
        'cantTell'          → cantTell      Rule inconclusive
        'passed'            → passed        Rule passed
        'inapplicable'      → inapplicable  Rule does not apply

    Alfa is the most EARL-native engine — its TypeScript source
    explicitly references https://www.w3.org/TR/EARL10-Schema/#outcome
    and its Outcome.Value enum is a 1:1 EARL mapping.

    alfa-engine.mjs translates 'failed' → violations and
    'cantTell' → incomplete before sending to Python.

WCAG mapping:

    Every Alfa rule declares its WCAG requirements at runtime via
    the ACT rule metadata.  The mapping is extracted live when rules
    are loaded in alfa-engine.mjs, not hardcoded.

    All Alfa rules are WCAG-mapped.  There are no best-practice rules.

Level handling:

    --level maps to Alfa conformance filtering:
        a   → Only rules for SC at level A
        aa  → Rules for SC at level A + AA (default)
        aaa → All rules (A + AA + AAA)

    Filtering is done in alfa-engine.mjs using a WCAG_LEVELS lookup
    table that maps each SC to its conformance level.

    Rule counts by level (Alfa 0.114.3):
        AAA: 91 rules
        AA:  82 rules
        A:   ~60 rules

Browser requirements:

    Alfa requires --remote-debugging-port=9222 on Chromium launch
    to enable CDP access.  This is returned by browser_launch_args().

Rules:

    91 rules in Alfa 0.114.3 (at AAA level; 82 at AA, ~60 at A).
    All rules are WCAG-mapped via ACT.  No best-practice rules.
    See ALFA_RULES dict below for the complete list.
"""

import asyncio
import json
import os
import sys

from .base import Engine, SCRIPT_DIR
from engine_mappings import (
    EARL_FAILED, EARL_CANTTELL, bp_category, aria_category)

# Alfa native outcomes → EARL (1:1 mapping, Alfa is EARL-native).
ALFA_OUTCOME_MAP = {
    'failed': EARL_FAILED,
    'cantTell': EARL_CANTTELL,
    # 'passed' and 'inapplicable' are not sent to Python by
    # alfa-engine.mjs (only violations and incomplete are forwarded).
}

# Complete rule inventory for Siteimprove Alfa 0.114.3.
# Each entry: 'sia-rN': (description, [WCAG SCs])
# Rules with empty SCs implement ACT rules without direct SC mapping.
# Synced to @siteimprove/alfa-rules 0.114.3 — extracted via
# package introspection of _requirements[].chapter.
ALFA_RULES = {
    'sia-r1':   ('Page has title', ['2.4.2']),
    'sia-r2':   ('Images have accessible name', ['1.1.1']),
    'sia-r3':   ('Deprecated id attr usage', ['4.1.1']),
    'sia-r4':   ('HTML has lang', ['3.1.1']),
    'sia-r5':   ('lang is valid', ['3.1.1']),
    'sia-r7':   ('Element lang is valid', ['3.1.2']),
    'sia-r8':   ('Form fields have accessible name', ['4.1.2']),
    'sia-r9':   ('No meta refresh/redirect', ['2.2.1', '2.2.4', '3.2.5']),
    'sia-r10':  ('Autocomplete is valid', ['1.3.5']),
    'sia-r11':  ('Link has accessible name', ['2.4.4', '2.4.9', '4.1.2']),
    'sia-r12':  ('Button has accessible name', ['4.1.2']),
    'sia-r13':  ('iframe has accessible name', ['4.1.2']),
    'sia-r14':  ('Label in name matches visible text', ['2.5.3']),
    'sia-r15':  ('img has accessible name', ['4.1.2']),
    'sia-r16':  ('Form element has accessible name', ['1.3.1', '4.1.2']),
    'sia-r17':  ('img with empty alt is decorative', ['4.1.2']),
    'sia-r18':  ('aria-* attributes defined', []),
    'sia-r19':  ('aria-* values valid', []),
    'sia-r20':  ('Required aria-* present', []),
    'sia-r21':  ('ARIA role valid', []),
    'sia-r22':  ('Required owned elements present', []),
    'sia-r23':  ('Required context role present', []),
    'sia-r24':  ('Media has text alternative', ['1.2.8']),
    'sia-r25':  ('Audio has text alternative', ['1.2.1']),
    'sia-r26':  ('Video has text alternative', ['1.2.1']),
    'sia-r27':  ('Video has captions', ['1.2.2']),
    'sia-r28':  ('Object has accessible name', ['1.1.1', '4.1.2']),
    'sia-r29':  ('Audio has text alternative', ['1.2.1']),
    'sia-r30':  ('Audio-only has text alternative', ['1.2.1']),
    'sia-r31':  ('Video has text alternative', ['1.2.1']),
    'sia-r32':  ('Video has audio description', ['1.2.5']),
    'sia-r33':  ('Video has audio description', ['1.2.5']),
    'sia-r35':  ('Audio-only has visual alternative', ['1.2.1']),
    'sia-r37':  ('Video has audio description', ['1.2.5']),
    'sia-r38':  ('Video has audio/text alternative', ['1.2.3', '1.2.5', '1.2.8']),
    'sia-r39':  ('Image not clipped to single char', ['1.1.1']),
    'sia-r40':  ('meta viewport allows zoom', ['1.4.4']),
    'sia-r41':  ('Link purpose context-independent', ['2.4.9']),
    'sia-r42':  ('Heading has accessible name', ['1.3.1']),
    'sia-r43':  ('SVG has accessible name', ['1.1.1']),
    'sia-r44':  ('Not orientation-locked', ['1.3.4']),
    'sia-r45':  ('Heading hierarchy', ['1.3.1']),
    'sia-r46':  ('Header cells in table', ['1.3.1']),
    'sia-r47':  ('Content resizable to 200%', ['1.4.4']),
    'sia-r48':  ('Deprecated element not used', []),
    'sia-r49':  ('Deprecated attribute not used', []),
    'sia-r50':  ('Audio has control mechanism', ['1.4.2']),
    'sia-r53':  ('Heading is descriptive', []),
    'sia-r54':  ('Landmark has unique role', []),
    'sia-r55':  ('Landmark visible role', []),
    'sia-r56':  ('Landmark is top-level', []),
    'sia-r57':  ('Text has minimum contrast candidate', []),
    'sia-r59':  ('Body has main landmark', []),
    'sia-r60':  ('aria-hidden not on focusable', []),
    'sia-r61':  ('Document has one main landmark', []),
    'sia-r62':  ('Link distinguishable by more than color', ['1.4.1']),
    'sia-r63':  ('Object has text alternative', ['1.1.1']),
    'sia-r64':  ('Required context role present', ['1.3.1']),
    'sia-r65':  ('Focus visible', ['2.4.7']),
    'sia-r66':  ('Text has enhanced contrast', ['1.4.6']),
    'sia-r67':  ('Image has text alternative', ['1.1.1']),
    'sia-r68':  ('Table cell related to header', ['1.3.1']),
    'sia-r69':  ('Text has minimum contrast', ['1.4.3', '1.4.6']),
    'sia-r70':  ('Deprecated role not used', []),
    'sia-r71':  ('Paragraph not justified', ['1.4.8']),
    'sia-r72':  ('Paragraph max width', []),
    'sia-r73':  ('Line height >= 1.5', ['1.4.8']),
    'sia-r74':  ('Font size in relative units', ['1.4.8']),
    'sia-r75':  ('Font size >= 9px', []),
    'sia-r76':  ('Table not used for layout', ['1.3.1']),
    'sia-r77':  ('Summary element for table', ['1.3.1']),
    'sia-r78':  ('No positive tabindex', []),
    'sia-r79':  ('Element not clipped', []),
    'sia-r80':  ('Line height not clipped', ['1.4.8']),
    'sia-r81':  ('Link identifiable', ['2.4.4', '2.4.9']),
    'sia-r83':  ('Font resizable to 200%', ['1.4.4']),
    'sia-r84':  ('Keyboard operable', ['2.1.1', '2.1.3']),
    'sia-r85':  ('Scrollable region focusable', []),
    'sia-r86':  ('aria-hidden no focusable children', []),
    'sia-r87':  ('No first-child letter exception', []),
    'sia-r90':  ('Role has required states', ['4.1.2']),
    'sia-r91':  ('Text spacing not clipped', ['1.4.12']),
    'sia-r92':  ('Text spacing not hidden', ['1.4.12']),
    'sia-r93':  ('Text spacing not lost', ['1.4.12']),
    'sia-r94':  ('Input has accessible name', ['4.1.2']),
    'sia-r95':  ('Element keyboard focusable', ['2.1.1']),
    'sia-r96':  ('No meta redirect', ['2.2.4', '3.2.5']),
    'sia-r110': ('Required owned elements match', ['1.3.1']),
    'sia-r111': ('Target size >= 44px', ['2.5.5']),
    'sia-r113': ('Target size >= 24px', ['2.5.8']),
    'sia-r116': ('Form has accessible name', ['4.1.2']),
}


def _find_node():
    """Return path to node binary, preferring ~/local/bin/node."""
    local = os.path.join(os.path.expanduser('~/local/bin'), 'node')
    if os.path.isfile(local):
        return local
    return 'node'


class AlfaEngine(Engine):
    """Siteimprove Alfa engine — Node.js subprocess communicating via CDP."""

    name = 'alfa'

    def __init__(self, scan_level, verbose=False, quiet=False):
        super().__init__(scan_level, verbose, quiet)
        self._proc = None
        # Alfa's Node.js subprocess is single-threaded and processes
        # one page at a time via stdin/stdout.  The lock serializes
        # scan requests when multiple async workers try to use Alfa
        # concurrently.
        self._lock = asyncio.Lock()
        self._ready_info = None
        self.ws_endpoint = None  # Set after start()

    def _alfa_level(self):
        """Derive Alfa conformance level from scan_level."""
        if self.scan_level:
            if self.scan_level.endswith('aaa'):
                return 'aaa'
            if (self.scan_level.endswith('a')
                    and not self.scan_level.endswith('aa')):
                return 'a'
        return 'aa'

    async def start(self, browser=None):
        """Start the Alfa Node.js subprocess.

        Alfa launches Chromium via Playwright's launchServer() and
        returns a GUID-protected WebSocket endpoint.  The Scanner
        connects Python Playwright to this endpoint.  No open debug
        ports — the GUID acts as a bearer token.
        """
        node_path = _find_node()
        alfa_script = os.path.join(SCRIPT_DIR, 'alfa-engine.mjs')
        env = os.environ.copy()
        env['PATH'] = (
            os.path.expanduser('~/local/bin')
            + ':' + env.get('PATH', ''))

        self._proc = await asyncio.create_subprocess_exec(
            node_path, alfa_script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            cwd=SCRIPT_DIR, env=env)

        level = self._alfa_level()
        init_msg = json.dumps({
            'level': level,
            'args': ['--disable-dev-shm-usage', '--disable-gpu'],
        }) + '\n'
        self._proc.stdin.write(init_msg.encode())
        await self._proc.stdin.drain()

        ready_line = await asyncio.wait_for(
            self._proc.stdout.readline(), timeout=30)
        self._ready_info = json.loads(ready_line)

        if not self._ready_info.get('ok'):
            raise RuntimeError('Alfa init failed: {}'.format(
                self._ready_info.get('error', '?')))

        self.ws_endpoint = self._ready_info.get('wsEndpoint')
        if not self.quiet:
            print("  Alfa: {} rules at level {} (server ready)".format(
                self._ready_info.get('rules', '?'),
                self._ready_info.get('level', '?')))

    async def _run_alfa(self, page_url, cookies=None):
        """Send a page URL to Alfa for scanning, return raw results.

        Alfa navigates to the URL in its own context within the shared
        browser.  Cookies are forwarded from the Python auth session
        so Alfa sees the same content as the other engines.
        """
        if not self._proc or self._proc.returncode is not None:
            return None
        async with self._lock:
            msg = json.dumps({
                'pageId': page_url,
                'cookies': cookies or [],
            }) + '\n'
            self._proc.stdin.write(msg.encode())
            await self._proc.stdin.drain()
            line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=120)
            return json.loads(line)

    async def scan(self, page):
        if not self._proc or self._proc.returncode is not None:
            return []

        out = []
        try:
            # Forward auth cookies so Alfa sees the same page
            cookies = []
            try:
                raw_cookies = await page.context.cookies()
                # Playwright cookie format works directly with addCookies
                cookies = [{
                    'name': c['name'],
                    'value': c['value'],
                    'domain': c.get('domain', ''),
                    'path': c.get('path', '/'),
                } for c in raw_cookies]
            except Exception:
                pass

            alfa_result = await self._run_alfa(page.url, cookies)
            if not alfa_result or not alfa_result.get('ok'):
                return []

            for earl, items in ((EARL_FAILED, alfa_result.get('violations', [])),
                                 (EARL_CANTTELL, alfa_result.get('incomplete', []))):
                for v in items:
                    rule_id = v['rule']
                    tags = [
                        'sc-' + sc
                        for sc in v.get('wcag', [])]
                    # Add ARIA and best-practice tags
                    aria_cat = aria_category('alfa', rule_id)
                    if aria_cat:
                        tags.append('aria-' + aria_cat)
                    bp_cat = bp_category('alfa', rule_id)
                    if bp_cat:
                        tags.append('best-practice')
                        tags.append('bp-' + bp_cat)
                    out.append({
                        'id': rule_id,
                        'engine': 'alfa',
                        'outcome': earl,
                        'description': v.get('message', ''),
                        'help': v.get('message', ''),
                        'helpUrl': v.get('uri', ''),
                        'impact': 'serious' if earl == EARL_FAILED else 'moderate',
                        'tags': tags,
                        'nodes': [{
                            'target': [v.get('target', '')],
                            'html': v.get('html', ''),
                            'any': [{'message': v.get('message', '')}],
                        }],
                    })
        except Exception as e:
            if self.verbose and not self.quiet:
                print("  alfa error: {}".format(e))

        return out

    async def stop(self):
        """Shut down the Alfa Node.js subprocess."""
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.stdin.write(
                    json.dumps({'quit': True}).encode() + b'\n')
                await self._proc.stdin.drain()
                await asyncio.wait_for(
                    self._proc.wait(), timeout=10)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
