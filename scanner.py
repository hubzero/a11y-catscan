"""
Scanner class for a11y-catscan.

Manages browser + engine lifecycle and provides a reusable scan_page()
method.  Used by the MCP server (direct import) and the CLI crawl
loop (replaces inline scanning code).

Usage:
    async with Scanner(engines=['axe', 'ibm'], level='wcag21aa') as scanner:
        result = await scanner.scan_page('https://example.com')
        # result is a dict with failed/cantTell/passed/inapplicable lists

Or without context manager:
    scanner = Scanner(engines=['axe'])
    await scanner.start()
    result = await scanner.scan_page(url)
    await scanner.stop()
"""

import asyncio
import importlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

from engine_mappings import (
    EARL_FAILED, EARL_CANTTELL, EARL_PASSED, EARL_INAPPLICABLE)
from engines import (
    AxeEngine, IbmEngine, HtmlcsEngine, AlfaEngine, make_engine)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# HTML content types we'll scan (anything else is skipped).
HTML_TYPES = {'text/html', 'application/xhtml+xml', ''}

# WCAG level presets — maps level name to axe-core runOnly tags
# and a human-readable label.
WCAG_LEVELS = {
    'wcag2a': {
        'tags': ['wcag2a'],
        'label': 'WCAG 2.0 Level A',
    },
    'wcag2aa': {
        'tags': ['wcag2a', 'wcag2aa'],
        'label': 'WCAG 2.0 Level AA',
    },
    'wcag2aaa': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag2aaa'],
        'label': 'WCAG 2.0 Level AAA',
    },
    'wcag21a': {
        'tags': ['wcag2a', 'wcag21a'],
        'label': 'WCAG 2.1 Level A',
    },
    'wcag21aa': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'],
        'label': 'WCAG 2.1 Level AA',
    },
    'wcag21aaa': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag2aaa',
                 'wcag21a', 'wcag21aa', 'wcag21aaa'],
        'label': 'WCAG 2.1 Level AAA',
    },
    'wcag22a': {
        'tags': ['wcag2a', 'wcag21a', 'wcag22a'],
        'label': 'WCAG 2.2 Level A',
    },
    'wcag22aa': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'],
        'label': 'WCAG 2.2 Level AA',
    },
    'wcag22aaa': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag2aaa',
                 'wcag21a', 'wcag21aa', 'wcag21aaa',
                 'wcag22aa', 'wcag22aaa'],
        'label': 'WCAG 2.2 Level AAA',
    },
    'best': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa',
                 'best-practice'],
        'label': 'WCAG 2.1 Level AA + Best Practices',
    },
}
DEFAULT_LEVEL = 'wcag21aa'


# count_nodes / dedup_page moved to results.py — re-exported here so
# existing callers (`from scanner import dedup_page, count_nodes`)
# continue to work.  New code should import from results.py directly.
from results import count_nodes, dedup_page  # noqa: F401


# JavaScript element resolver — runs in the live DOM after engines
# scan, normalizing element references to uniform CSS selectors.
# See engines/base.py docstring for the full result format spec.
#
# Handles: CSS selectors (axe, htmlcs), XPath (IBM), tag+attrs (Alfa).
# Generates deterministic nth-of-type selectors for cross-engine dedup.
_ELEMENT_RESOLVER_JS = """(refs) => {
    function findEl(ref) {
        if (ref.css) {
            try { return document.querySelector(ref.css); } catch(e) {}
        }
        if (ref.xpath) {
            try {
                return document.evaluate(ref.xpath, document, null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            } catch(e) {}
        }
        if (ref.id) {
            let el = document.getElementById(ref.id);
            if (el) return el;
        }
        if (ref.tag) {
            let sel = ref.tag;
            if (ref.attrs) {
                for (const [k, v] of Object.entries(ref.attrs)) {
                    sel += '[' + k + '=' + JSON.stringify(v) + ']';
                }
            }
            try { return document.querySelector(sel); } catch(e) {}
        }
        return null;
    }

    function uniqueSelector(el) {
        if (!el || el === document) return 'html';
        if (el.id) return '#' + CSS.escape(el.id);
        let path = [];
        let node = el;
        while (node && node.parentElement) {
            let seg = node.tagName.toLowerCase();
            let parent = node.parentElement;
            let siblings = Array.from(parent.children)
                .filter(c => c.tagName === node.tagName);
            if (siblings.length > 1) {
                seg += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
            }
            if (node.id) {
                path.unshift('#' + CSS.escape(node.id));
                break;
            }
            path.unshift(seg);
            node = parent;
        }
        return path.join(' > ');
    }

    return refs.map(ref => {
        let el = findEl(ref);
        if (!el) return null;
        return {
            selector: uniqueSelector(el),
            html: el.outerHTML.substring(0, 200),
        };
    });
}"""


class Scanner:
    """Reusable accessibility scanner.

    Manages Playwright browser, engine lifecycle, and per-page scanning.
    Each scan_page() call opens a new tab, runs all engines, resolves
    element selectors, and returns structured results.

    Thread safety: multiple concurrent scan_page() calls are safe —
    each opens its own page from the shared browser.  Alfa serializes
    internally via asyncio.Lock.
    """

    def __init__(
        self,
        engines: list[str] | None = None,
        *,
        level: str = DEFAULT_LEVEL,
        tags: list[str] | None = None,
        rules: list[str] | None = None,
        chromium_path: str | None = None,
        ignore_certificate_errors: bool = False,
        wait_until: str = 'networkidle',
        page_wait: int = 1,
        auth: dict | None = None,
        config: dict | None = None,
        verbose: bool = False,
        quiet: bool = False,
    ) -> None:
        # Scan configuration
        self._engine_names = engines or ['axe']
        self._scan_level = level
        self._include_best = level == 'best'
        if self._include_best:
            self._scan_level = 'wcag21aa'

        # axe tag/rule filtering
        level_info = WCAG_LEVELS.get(level, WCAG_LEVELS[DEFAULT_LEVEL])
        self._tags = tags or level_info.get('tags')
        self._rules = rules

        # Browser options
        self._chromium_path = chromium_path
        self._ignore_certs = ignore_certificate_errors
        self._wait_until = wait_until
        self._page_wait = page_wait

        # Auth
        self._auth_config = auth or {}
        self._config = config or {}

        # Display
        self.verbose = verbose
        self.quiet = quiet

        # State (set by start())
        self._pw = None
        self._browser = None
        self._context = None  # authenticated BrowserContext or None
        self._engines = []
        self._login_plugin = None
        self._storage_state_path = os.path.join(
            SCRIPT_DIR, '.auth-state.json')
        self._started = False

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def engine_names(self) -> list[str]:
        return [type(e).__name__ for e in self._engines]

    @property
    def login_exclude_paths(self) -> list[str]:
        """Paths the login plugin wants excluded (e.g. /logout)."""
        if (self._login_plugin
                and hasattr(self._login_plugin, 'exclude_paths')):
            return list(self._login_plugin.exclude_paths)
        return []

    @property
    def browser(self):
        """The Playwright Browser instance (for advanced crawl-loop use)."""
        return self._browser

    @property
    def context(self):
        """The authenticated BrowserContext, or None."""
        return self._context

    async def start(self) -> None:
        """Launch browser and start all engines."""
        if self._started:
            return

        from playwright.async_api import async_playwright
        self._pw = await async_playwright().__aenter__()

        # Instantiate engine objects via the central factory.  Extras
        # (tags/rules/include_best) get forwarded only to engines that
        # accept them.
        self._engines = [
            make_engine(
                name, self._scan_level,
                verbose=self.verbose, quiet=self.quiet,
                tags=self._tags, rules=self._rules,
                include_best=self._include_best)
            for name in self._engine_names
        ]

        # Collect browser launch args from all engines
        launch_args = ['--disable-dev-shm-usage', '--disable-gpu']
        for eng in self._engines:
            for arg in eng.browser_launch_args():
                if arg not in launch_args:
                    launch_args.append(arg)

        # Launch Chromium.
        # If Alfa is enabled, Alfa's Node.js subprocess launches
        # Chromium via Playwright's launchServer() and returns a
        # GUID-protected WebSocket endpoint.  Python connects to
        # that endpoint — no open debug ports, no CDP.
        # If Alfa is not enabled, use Playwright's native launch().
        alfa_eng = None
        for eng in self._engines:
            if isinstance(eng, AlfaEngine):
                alfa_eng = eng
                break

        if alfa_eng:
            # Start Alfa first — it launches the browser server
            await alfa_eng.start(None)
            ws_url = alfa_eng.ws_endpoint
            if not ws_url:
                raise RuntimeError(
                    'Alfa did not return a WebSocket endpoint')
            # Connect Python to Alfa's browser server
            self._browser = await self._pw.chromium.connect(ws_url)
            if not self.quiet:
                print("  Connected to Alfa browser server")
        else:
            launch_kw = {'headless': True, 'args': launch_args}
            if (self._chromium_path
                    and os.path.isfile(self._chromium_path)):
                launch_kw['executable_path'] = self._chromium_path
            self._browser = await self._pw.chromium.launch(
                **launch_kw)

        # Authenticate if configured
        login_script = self._auth_config.get('login_script', '')
        if login_script:
            await self._setup_auth(login_script)

        # Start non-Alfa engines (Alfa already started if present —
        # it launched the browser server above)
        for eng in list(self._engines):
            if isinstance(eng, AlfaEngine):
                continue  # already started
            try:
                await eng.start(self._browser)
            except Exception as e:
                if not self.quiet:
                    print("  Engine start failed ({}): {}".format(
                        type(eng).__name__, e))
                self._engines.remove(eng)

        self._started = True

    async def stop(self) -> None:
        """Stop engines and close browser."""
        if not self._started:
            return

        for eng in self._engines:
            try:
                await eng.stop()
            except Exception:
                pass

        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._pw:
            try:
                await self._pw.__aexit__(None, None, None)
            except Exception:
                pass
            self._pw = None

        # Let the event loop process any pending cleanup callbacks
        # from Playwright's browser connection.  Without this,
        # asyncio.run() closes the loop with pending Futures that
        # produce 'TargetClosedError' and 'Event loop is closed'
        # tracebacks during garbage collection.
        await asyncio.sleep(0)

        self._started = False

    async def scan_page(
        self,
        url: str,
        *,
        extract_links: bool = False,
        dedup: bool = True,
    ) -> dict:
        """Scan a single page with all engines.

        Args:
            url: URL to scan
            extract_links: If True, extract <a href> links (for crawling)
            dedup: If True, deduplicate findings across engines

        Returns:
            Dict with url, timestamp, http_status, EARL outcome lists,
            optional links list, and elapsed time.  If the page is
            skipped (not HTML, error, etc.), the outcome lists are empty
            and 'skipped' is set to the reason string.
        """
        if not self._started:
            raise RuntimeError('Scanner not started — call start() first')

        t0 = time.time()

        # Open a new page (tab) for this scan
        if self._context:
            page = await self._context.new_page()
        else:
            page = await self._browser.new_page(
                viewport={'width': 1280, 'height': 1024},
                ignore_https_errors=self._ignore_certs)

        try:
            return await self._scan_page_impl(
                page, url, extract_links, dedup, t0)
        except Exception as e:
            return self._skip_result(
                url, f'error: {e}', t0)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def restart_browser(self) -> None:
        """Restart browser and engines (for memory leak prevention).

        Preserves auth state.  Called by the crawl loop every N pages.
        """
        # Stop all engines.  Defensive swallow — we're tearing
        # down before relaunch, so a stop failure shouldn't block
        # restart.  Surface for debugging.
        for eng in self._engines:
            try:
                await eng.stop()
            except Exception as e:
                if self.verbose:
                    print(
                        "  WARNING: engine stop failed during "
                        "restart ({}): {}".format(
                            type(eng).__name__, e),
                        file=sys.stderr)

        # Close browser (Alfa's server dies with its subprocess)
        try:
            await self._browser.close()
        except Exception as e:
            if self.verbose:
                print("  WARNING: browser close failed during "
                      "restart: {}".format(e), file=sys.stderr)

        # Relaunch — same logic as start()
        alfa_eng = None
        for eng in self._engines:
            if isinstance(eng, AlfaEngine):
                alfa_eng = eng
                break

        if alfa_eng:
            await alfa_eng.start(None)
            self._browser = await self._pw.chromium.connect(
                alfa_eng.ws_endpoint)
        else:
            launch_args = ['--disable-dev-shm-usage', '--disable-gpu']
            launch_kw = {'headless': True, 'args': launch_args}
            if (self._chromium_path
                    and os.path.isfile(self._chromium_path)):
                launch_kw['executable_path'] = self._chromium_path
            self._browser = await self._pw.chromium.launch(
                **launch_kw)

        # Re-authenticate
        if self._login_plugin:
            self._context = None
            ctx, success = await self._try_saved_state()
            if not success:
                ctx, success = await self._do_login('restart')
            self._context = ctx if success else None
            if not success and not self.quiet:
                print("  [re-login failed after restart]")

        # Restart engines
        for eng in self._engines:
            try:
                await eng.start(self._browser)
            except Exception as e:
                if not self.quiet:
                    print("  Engine restart failed ({}): {}".format(
                        type(eng).__name__, e))

    async def check_session(self, page) -> bool:
        """Check if the authenticated session is still active.

        Returns True if OK (or no auth configured), False if expired.
        """
        if (self._login_plugin
                and hasattr(self._login_plugin, 'is_logged_in')):
            return await self._login_plugin.is_logged_in(page)
        return True

    async def relogin(self, reason: str = '') -> tuple:
        """Re-run the login flow.  Returns (context, success)."""
        if self._context:
            try:
                await self._context.close()
            except Exception as e:
                if self.verbose:
                    print(
                        "  WARNING: context close failed before "
                        "relogin: {}".format(e),
                        file=sys.stderr)
        return await self._do_login(reason)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.stop()

    # ── Private methods ──────────────────────────────────────────

    async def _scan_page_impl(self, page, url, extract_links, dedup, t0):
        """Core scanning logic — navigate, validate, run engines, resolve."""
        # Navigate
        response = await page.goto(
            url, wait_until=self._wait_until, timeout=60000)
        if self._page_wait and self._wait_until != 'networkidle':
            await page.wait_for_timeout(self._page_wait * 1000)

        if response is None:
            return self._skip_result(url, 'no response', t0)

        status = response.status
        content_type = (response.headers.get('content-type', '')
                        .split(';')[0].strip().lower())

        if status >= 400:
            return self._skip_result(
                url, f'HTTP {status}', t0)
        if content_type and content_type not in HTML_TYPES:
            return self._skip_result(
                url, f'not HTML ({content_type})', t0)

        # Content validation — combined into one CDP round-trip
        # instead of three.  Returns the doc's contentType, the rendered
        # HTML length, and a small prefix used to confirm <html>.
        probe = await page.evaluate(
            "({contentType: document.contentType,"
            " htmlLength: document.documentElement.outerHTML.length,"
            " htmlStart: document.documentElement"
            ".outerHTML.substring(0, 80)})") or {}
        html_length = probe.get('htmlLength') or 0
        if html_length < 100:
            return self._skip_result(
                url, f'empty response ({html_length} bytes)', t0)
        doc_ct = (probe.get('contentType') or '').lower()
        if doc_ct and doc_ct not in HTML_TYPES:
            return self._skip_result(
                url, f'not HTML ({doc_ct})', t0)
        page_start = probe.get('htmlStart') or ''
        if page_start and '<html' not in page_start.lower():
            return self._skip_result(url, 'not HTML', t0)

        # Run all engines
        results = {
            EARL_FAILED: [],
            EARL_CANTTELL: [],
            EARL_PASSED: [],
            EARL_INAPPLICABLE: [],
        }
        for eng in self._engines:
            items = await eng.scan(page)
            for item in items:
                outcome = item.get('outcome', '')
                if outcome in results:
                    results[outcome].append(item)

        if (not results[EARL_FAILED]
                and not results[EARL_CANTTELL]
                and not results[EARL_PASSED]):
            return self._skip_result(url, 'no engine results', t0)

        # Resolve element references to uniform CSS selectors
        await self._resolve_elements(page, results)

        # Extract links if requested (for crawling)
        links = []
        if extract_links:
            try:
                raw_links = await page.evaluate(
                    "Array.from(document.querySelectorAll('a[href]'))"
                    ".map(a=>a.href).filter(h=>h.startsWith('http'))")
                links = raw_links or []
            except Exception as e:
                # Link-extraction failure means the crawler won't
                # follow this page's links.  Surface in verbose
                # mode but don't abort — the scan results for this
                # page are still valid.
                if self.verbose:
                    print(
                        "  WARNING: link extraction failed for "
                        "{}: {}".format(url, e), file=sys.stderr)

        actual_url = page.url
        elapsed = time.time() - t0

        page_data = {
            'url': actual_url,
            'requested_url': url,
            'timestamp': datetime.now().isoformat(),
            'http_status': status if status != 0 else None,
            EARL_FAILED: results[EARL_FAILED],
            EARL_CANTTELL: results[EARL_CANTTELL],
            EARL_PASSED: results[EARL_PASSED],
            EARL_INAPPLICABLE: results[EARL_INAPPLICABLE],
            'elapsed': elapsed,
        }
        if extract_links:
            page_data['links'] = links

        if dedup:
            page_data = dedup_page(page_data)
            # Preserve fields dedup doesn't know about
            page_data['requested_url'] = url
            page_data['elapsed'] = elapsed
            if extract_links:
                page_data['links'] = links

        return page_data

    async def _resolve_elements(self, page, results):
        """Normalize element selectors across all engines."""
        node_refs = []
        node_map = []  # (result_item, node_index)

        for outcome_list in results.values():
            for item in outcome_list:
                eng = item.get('engine', '')
                for ni, node in enumerate(item.get('nodes', [])):
                    target = (node.get('target', [''])[0]
                              if node.get('target') else '')
                    ref = {}
                    if eng == 'ibm' and target.startswith('/'):
                        ref['xpath'] = target
                    elif target:
                        ref['css'] = target
                    if ref:
                        node_map.append((item, ni))
                        node_refs.append(ref)

        if not node_refs:
            return

        try:
            resolved = await page.evaluate(
                _ELEMENT_RESOLVER_JS, node_refs)
            for idx, res in enumerate(resolved):
                if res:
                    item, ni = node_map[idx]
                    nodes = item.get('nodes', [])
                    if ni < len(nodes):
                        nodes[ni]['target'] = [res['selector']]
                        nodes[ni]['html'] = res['html']
        except Exception as e:
            # Resolver failure means selectors stay in their native
            # engine form (axe ElementHandle, IBM xpath, etc.).
            # Reports remain readable but selector quality drops, so
            # surface the cause when verbose; never abort the scan.
            if self.verbose:
                print(f"  WARNING: element resolver failed: {e}",
                      file=sys.stderr)

    def _skip_result(self, url, reason, t0):
        """Build a result dict for a skipped page."""
        return {
            'url': url,
            'requested_url': url,
            'timestamp': datetime.now().isoformat(),
            'http_status': None,
            EARL_FAILED: [],
            EARL_CANTTELL: [],
            EARL_PASSED: [],
            EARL_INAPPLICABLE: [],
            'skipped': reason,
            'elapsed': time.time() - t0,
        }

    # ── Auth helpers ─────────────────────────────────────────────

    async def _setup_auth(self, login_script):
        """Load login plugin and authenticate."""
        script_path = os.path.expanduser(login_script)
        if not os.path.isabs(script_path):
            script_path = os.path.join(SCRIPT_DIR, script_path)
        if not os.path.isfile(script_path):
            if not self.quiet:
                print("  Login script not found: {}".format(
                    script_path))
            return

        spec = importlib.util.spec_from_file_location(
            'login_plugin', script_path)
        self._login_plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self._login_plugin)

        # Try saved auth state first (fast)
        ctx, success = await self._try_saved_state()
        if success:
            self._context = ctx
            if not self.quiet:
                print("  Authenticated from saved state")
            return

        # Fall back to login script
        ctx, success = await self._do_login()
        if success:
            self._context = ctx
        else:
            if not self.quiet:
                print("  Login failed — scanning as anonymous")
            if ctx:
                await ctx.close()

    async def _try_saved_state(self):
        """Try loading saved auth state.  Returns (context, success)."""
        if not os.path.isfile(self._storage_state_path):
            return None, False
        try:
            ctx = await self._browser.new_context(
                viewport={'width': 1280, 'height': 1024},
                ignore_https_errors=self._ignore_certs,
                storage_state=self._storage_state_path)
            # Verify by loading the start URL
            start_url = self._config.get('url', '')
            if start_url:
                test_page = await ctx.new_page()
                await test_page.goto(
                    start_url, wait_until='networkidle',
                    timeout=30000)
                if '/login' in test_page.url:
                    await test_page.close()
                    await ctx.close()
                    return None, False
                logged_in = await test_page.evaluate(
                    "!!document.querySelector("
                    "'.loggedin, #account, [data-loggedin]')")
                await test_page.close()
                if not logged_in:
                    await ctx.close()
                    return None, False
                if hasattr(self._login_plugin, 'init_from_context'):
                    await self._login_plugin.init_from_context(ctx)
            return ctx, True
        except Exception:
            return None, False

    async def _do_login(self, reason=''):
        """Run the login script.  Returns (context, success)."""
        ctx = await self._browser.new_context(
            viewport={'width': 1280, 'height': 1024},
            ignore_https_errors=self._ignore_certs)
        try:
            ok = await self._login_plugin.login(ctx, self._config)
        except Exception as e:
            if not self.quiet:
                print("  Login error{}: {}".format(
                    ' (' + reason + ')' if reason else '', e))
            return ctx, False
        if ok:
            try:
                await ctx.storage_state(
                    path=self._storage_state_path)
            except Exception:
                pass
        return ctx, ok
