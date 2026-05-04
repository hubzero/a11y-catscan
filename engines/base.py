"""
Base class for a11y-catscan accessibility scan engines.

All engines inherit from Engine and implement the same interface:

    start(browser)   — one-time setup (load JS, start subprocesses)
    scan(page)       — scan a Playwright page, return normalized results
    stop()           — tear down resources

Normalized result format (one dict per finding):

    {
        'id':          str,   # Engine-specific rule ID
        'engine':      str,   # 'axe', 'ibm', 'htmlcs', 'alfa'
        'outcome':     str,   # EARL outcome (see below)
        'description': str,   # Human-readable description
        'help':        str,   # Short help text
        'helpUrl':     str,   # URL to detailed documentation
        'impact':      str,   # 'critical', 'serious', 'moderate', 'minor'
        'tags':        list,  # 'sc-1.4.3', 'aria-valid-roles', 'bp-landmarks'
        'nodes':       list,  # Affected DOM nodes (see below)
    }

EARL outcomes (W3C Evaluation and Report Language 1.0):

    'failed'        — Definite accessibility failure.
    'cantTell'      — Automated check inconclusive; needs manual review.
    'passed'        — Test passed for this element.
    'inapplicable'  — Test does not apply to this page/element.

    Reference: https://www.w3.org/TR/EARL10-Schema/#outcome

    Every major engine uses EARL internally:
    - axe-core:  RawNodeResult<'passed' | 'failed' | 'cantTell'>
    - Alfa:      Outcome.Value enum (passed/failed/cantTell/inapplicable)
    - IBM ace:   ACT mapping field uses passed/fail/cantTell/inapplicable
    - HTMLCS:    ERROR=failed, WARNING=cantTell (implicit mapping)

Node format:

    {
        'target': [str],           # CSS selector or DOM path
        'html':   str,             # Snippet of the element's outer HTML
        'any':    [{'message': str}]  # Check messages
    }
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE_MODULES = os.path.join(SCRIPT_DIR, 'node_modules')


class Engine:
    """Base class for accessibility scan engines.

    Subclasses must implement scan().  start() and stop() are optional.
    """

    #: Short engine name used in result dicts ('axe', 'ibm', etc.)
    name = None

    def __init__(self, scan_level, verbose=False, quiet=False):
        self.scan_level = scan_level
        self.verbose = verbose
        self.quiet = quiet

    @classmethod
    def browser_launch_args(cls):
        """Return extra Chromium launch args needed by this engine.

        Most engines need none.  Override in subclasses that require
        special browser flags (e.g. Alfa needs --remote-debugging-port).
        """
        return []

    async def start(self, browser=None):
        """One-time setup: load JS sources, start subprocesses.

        Called once before the first scan().  The browser argument is
        the Playwright Browser instance (needed by Alfa for CDP).
        """
        pass

    async def scan(self, page):
        """Scan a Playwright page and return normalized results.

        Args:
            page: Playwright Page object (already navigated to the URL)

        Returns:
            List of normalized result dicts (see module docstring).
        """
        raise NotImplementedError

    async def stop(self):
        """Tear down resources (subprocesses, temp files).

        Called once after all scanning is complete, and again before
        browser restarts.
        """
        pass
