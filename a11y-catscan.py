#!/usr/bin/env python3
"""
a11y-catscan - WCAG accessibility scanner using axe-core, Playwright, and Chromium.

Crawls a website and runs axe-core accessibility checks on each page,
producing HTML and JSON reports.

Usage:
    a11y-catscan.py [OPTIONS] START_URL

Examples:
    # Full crawl scan
    a11y-catscan.py https://example.com/
    a11y-catscan.py --max-pages 500 --llm https://example.com/

    # Quick single-page check after a fix
    a11y-catscan.py --page -q --summary-json https://example.com/fixed-page

    # Re-scan only pages that failed previously
    a11y-catscan.py --rescan previous.jsonl --diff previous.jsonl --llm

    # Check just contrast issues
    a11y-catscan.py --page --rule color-contrast https://example.com/page

    # Scan a specific list of URLs
    a11y-catscan.py --urls pages.txt --llm

Exit codes: 0 = no failures, 1 = failures found.
"""

import argparse
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import time

# Engine rule → WCAG SC mappings (IBM, HTMLCS, SC metadata)
try:
    from engine_mappings import (
        SC_META, sc_level, sc_name,
        EARL_FAILED, EARL_CANTTELL, EARL_PASSED, EARL_INAPPLICABLE)
except ImportError:
    SC_META = {}
    def sc_level(sc): return ('?', '?')
    def sc_name(sc): return ''
    EARL_FAILED = 'failed'
    EARL_CANTTELL = 'cantTell'
    EARL_PASSED = 'passed'
    EARL_INAPPLICABLE = 'inapplicable'

# Engine classes and Scanner
from engines import AxeEngine, IbmEngine, HtmlcsEngine, AlfaEngine
from scanner import (
    Scanner, count_nodes, dedup_page,
    WCAG_LEVELS, DEFAULT_LEVEL, HTML_TYPES)
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

# Required dependencies.  Catch ImportError here rather than letting
# Python's traceback confuse users who haven't installed them.
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed.", file=sys.stderr)
    print("  Install it with:  pip install pyyaml", file=sys.stderr)
    sys.exit(2)

# All supporting files (config, node_modules) live alongside this script.
# This lets the tool work as a self-contained directory you can clone
# and run from anywhere without installation.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AXE_JS_PATH = os.path.join(SCRIPT_DIR, 'node_modules', 'axe-core', 'axe.min.js')
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, 'a11y-catscan.yaml')

# File extensions that are never HTML pages.  Using a frozenset gives O(1)
# lookup instead of scanning a list on every URL the crawler discovers.
SKIP_EXTENSIONS = frozenset((
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico',
    '.css', '.js', '.zip', '.tar', '.gz', '.mp4', '.mp3',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.xml', '.json', '.rss', '.atom', '.woff', '.woff2',
    '.ttf', '.eot', '.bmp', '.webp', '.csv',
))

# axe-core version (read from the bundled file header on first use)
AXE_VERSION = None

# WCAG_LEVELS and DEFAULT_LEVEL imported from scanner.py


def _safe_int(val, default=0):
    """Convert to int, returning default on failure."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# count_nodes imported from scanner.py
_count_nodes = count_nodes


# _ELEMENT_RESOLVER_JS moved to scanner.py


def _parse_wcag_sc(tags):
    """Extract WCAG success criteria numbers from tags.

    Handles normalized format 'sc-1.4.3' (preferred) and legacy
    axe-core format 'wcag143'.  Level tags like 'wcag2a' are ignored.
    """
    criteria = set()
    for tag in tags:
        # Normalized format: sc-X.Y.Z
        if tag.startswith('sc-'):
            sc = tag[3:]  # strip 'sc-'
            if re.match(r'^\d+\.\d+\.\d+$', sc):
                criteria.add(sc)
            continue
        # Legacy axe format: wcagXYZ (digits only, no dots)
        m = re.match(r'^wcag(\d)(\d)(\d+)$', tag)
        if m:
            sc = '{}.{}.{}'.format(m.group(1), m.group(2), m.group(3))
            criteria.add(sc)
    return criteria


def _sc_level(sc):
    """Return (level, version) for a WCAG SC."""
    return sc_level(sc)


def load_allowlist(path):
    """Load an allowlist file that suppresses known-acceptable cantTell findings.

    Format (YAML):
        - rule: color-contrast
          reason: axe-core limitation on scroll-snap flex layouts
        - rule: color-contrast
          url: /groups/mmsc/usage
          reason: Google Charts SVG
        - rule: aria-allowed-attr
          target: "#main-nav"

    Returns a list of dicts with keys: rule, url (optional), target (optional).
    """
    if not path or not os.path.exists(path):
        return []
    entries = []
    with open(path) as f:
        data = yaml.safe_load(f) or []
    if isinstance(data, list):
        entries = data
    return entries


def _matches_allowlist(rule_id, url, nodes, allowlist,
                       engines_dict=None, outcome=None):
    """Check if a result matches any allowlist entry.

    Allowlist entries use normalized tags (sc-*, aria-*, bp-*) in the
    rule field.  All specified filters are AND — a finding must match
    all of them to be suppressed.

    Args:
        rule_id: Normalized finding ID (e.g. 'sc-2.4.7', 'bp-landmarks')
        url: Page URL
        nodes: List of node dicts
        allowlist: List of allowlist entry dicts
        engines_dict: Engines attribution dict from deduped findings
                      e.g. {'ibm': {'rule': '...'}, 'axe': {...}}
        outcome: EARL outcome ('failed', 'cantTell')

    Returns True if the result should be suppressed.
    """
    # Engine names that contributed to this finding
    finding_engines = set(engines_dict.keys()) if engines_dict else set()

    for entry in allowlist:
        # Rule must match the normalized ID
        if entry.get('rule') != rule_id:
            continue

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
            # it's a real issue regardless of IBM noise
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

        # All filters passed — this result is allowlisted
        return True

    return False


def load_config(config_path=None):
    """Load site configuration from YAML file.

    Returns a dict with config values.  Missing keys get sensible defaults.
    """
    config = {}
    path = config_path or DEFAULT_CONFIG_PATH
    if os.path.exists(path):
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    return config


def get_axe_version():
    """Read axe-core version from the bundled JS file header."""
    global AXE_VERSION
    if AXE_VERSION is None:
        try:
            with open(AXE_JS_PATH, 'r') as f:
                header = f.read(200)
            m = re.search(r'axe v([\d.]+)', header)
            AXE_VERSION = m.group(1) if m else 'unknown'
        except Exception:
            AXE_VERSION = 'unknown'
    return AXE_VERSION


# Track all browser processes we launch so we can kill them on exit.
# This prevents orphaned chromium processes when the script
# crashes, is killed, or exits abnormally.
_browser_pids = set()


def _register_browser_pid(pid):
    """Register a browser process PID for cleanup on exit."""
    _browser_pids.add(pid)


def _cleanup_browsers():
    """Kill any browser processes we launched.  Called via atexit."""
    for pid in list(_browser_pids):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    _browser_pids.clear()

    # Also kill any chromium processes that are children of
    # this process (catches anything missed by PID tracking).
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ['ps', '-eo', 'pid,ppid,comm'],
            capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                try:
                    child_pid = int(parts[0])
                    parent_pid = int(parts[1])
                    comm = parts[2]
                    if parent_pid == my_pid and (
                            'chrome' in comm):
                        os.kill(child_pid, signal.SIGKILL)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass


atexit.register(_cleanup_browsers)


class RateLimiter:
    """Rate limiter that enforces a minimum delay between calls.

    Used to ensure that all async workers together don't exceed the
    robots.txt Crawl-delay.  Each worker calls wait_time() before making a
    request and sleeps (via asyncio.sleep) if needed to maintain the
    minimum interval.
    """

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._last_time = 0

    def wait_time(self):
        """Return seconds to sleep (for async callers that need asyncio.sleep)."""
        if self.min_interval <= 0:
            return 0
        now = time.time()
        elapsed = now - self._last_time
        delay = max(0, self.min_interval - elapsed)
        self._last_time = now + delay
        return delay





def load_cookies(config):
    """Load cookies from the cookies file.

    Returns a list of cookie dicts, or an empty list if not configured
    or file doesn't exist.
    """
    auth = config.get('auth', {})
    if not auth:
        return []
    cookies_file = auth.get('cookies_file', '')
    if not cookies_file:
        return []
    cookies_file = os.path.expanduser(cookies_file)
    if not os.path.isfile(cookies_file):
        return []
    try:
        with open(cookies_file) as f:
            cookies = json.load(f)
        return cookies if isinstance(cookies, list) else []
    except Exception:
        return []



# Parameters to strip from URLs during normalization.  These are common
# pagination, sorting, and redirect params that produce the same page
# template with different data.  Stripping them deduplicates the crawl
# frontier so we don't scan /resources?sort=date AND /resources?sort=title.
_strip_params = set()        # global params to strip from all URLs
_strip_params_by_path = []   # list of (pattern, param_set) for path-conditional stripping


_strip_path_rules_compiled = []  # compiled (regex, param_set) pairs


def normalize_url(url):
    """Normalize URL for deduplication.

    Strips fragment, trailing slash, and any query parameters listed in
    _strip_params (global) or matching a path-conditional rule from
    _strip_params_by_path.  Configured via strip_query_params in YAML.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip('/') or '/'

    # Build the set of params to strip for this URL
    query = parsed.query
    if query and (_strip_params or _strip_path_rules_compiled):
        from urllib.parse import parse_qs, urlencode
        strip = set(_strip_params)
        for regex, param_set in _strip_path_rules_compiled:
            if regex.search(path):
                strip.update(param_set)
        if strip:
            params = parse_qs(query, keep_blank_values=True)
            filtered = {k: v for k, v in params.items() if k not in strip}
            query = urlencode(filtered, doseq=True) if filtered else ''

    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, ''))


def is_same_origin(url, base_url):
    """Check whether two URLs share the same scheme+host+port."""
    return urlparse(url).netloc == urlparse(base_url).netloc


def load_robots_txt(base_url):
    """Fetch and parse the site's robots.txt.

    Returns a RobotFileParser that can check whether a URL is allowed.
    Returns None if robots.txt can't be fetched (we'll allow everything).
    """
    parsed = urlparse(base_url)
    robots_url = '{}://{}/robots.txt'.format(parsed.scheme, parsed.netloc)
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
        return parser
    except Exception:
        return None


def should_scan(url, base_url, include_paths, exclude_paths, exclude_regex=None,
                robots_parser=None):
    """Decide whether a URL should be scanned based on all filter rules.

    Checks (in order): same-origin, file extension, include/exclude paths,
    exclude regex, query string filters, and robots.txt.
    """
    if not is_same_origin(url, base_url):
        return False
    parsed = urlparse(url)
    path = parsed.path

    # Skip non-HTML resources (O(1) lookup via frozenset)
    ext = os.path.splitext(path.lower())[1]
    if ext in SKIP_EXTENSIONS:
        return False

    if include_paths:
        if not any(path.startswith(p) for p in include_paths):
            return False

    if exclude_paths:
        if any(path.startswith(p) for p in exclude_paths):
            return False

    if exclude_regex:
        for pat in exclude_regex:
            if pat.search(path):
                return False

    # Skip query strings that produce non-HTML output
    query = parsed.query
    if 'action=pdf' in query:
        return False

    # Respect robots.txt if a parser was provided (--ignore-robots disables this).
    # We check both the exact URL and with a trailing slash, because our URL
    # normalizer strips trailing slashes but robots.txt Disallow patterns
    # often include them (e.g. "Disallow: /tools/" blocks /tools/ but
    # technically not /tools without the slash).
    if robots_parser is not None:
        if not robots_parser.can_fetch('a11y-catscan', url):
            return False
        url_with_slash = url.rstrip('/') + '/'
        if not robots_parser.can_fetch('a11y-catscan', url_with_slash):
            return False

    return True


_http_cookie_header = ''  # set by crawl_and_scan when auth cookies loaded


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Handler that stops urllib from following redirects."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def http_status(url, timeout=10):
    """Return (status_code, content_type) via a lightweight HEAD request.

    Falls back to GET if the server rejects HEAD (some do).
    Returns (0, '') on network error.  Does NOT follow redirects —
    redirects return the 3xx status so the browser can handle them
    with its own session cookies.

    This is used as a pre-check before loading pages in Chromium.
    It's much cheaper than a full browser load and lets us identify
    error pages (4xx/5xx) and non-HTML responses (application/json)
    without wasting Chromium resources.

    If auth cookies are loaded, they are sent with the request so
    authenticated pages return 200 instead of 302→login.
    """
    def _ct(r):
        """Extract base content-type (without charset)."""
        ct = r.headers.get('Content-Type', '')
        return ct.split(';')[0].strip().lower()

    headers = {'User-Agent': 'a11y-catscan/1.0'}
    if _http_cookie_header:
        headers['Cookie'] = _http_cookie_header

    try:
        req = urllib.request.Request(url, method='HEAD', headers=headers)
        with _no_redirect_opener.open(req, timeout=timeout) as r:
            return (r.status, _ct(r))
    except urllib.error.HTTPError as e:
        return (e.code, _ct(e))
    except Exception:
        # HEAD failed (connection error, or server rejects HEAD) — try GET
        try:
            req = urllib.request.Request(url, method='GET', headers=headers)
            with _no_redirect_opener.open(req, timeout=timeout) as r:
                return (r.status, _ct(r))
        except urllib.error.HTTPError as e:
            return (e.code, _ct(e))
        except Exception:
            return (0, '')



def crawl_and_scan(start_url, max_pages=50, tags=None, rules=None, level=None,
                   include_paths=None, exclude_paths=None, exclude_regex=None,
                   verbose=False, quiet=False, config=None,
                   json_path=None, html_path=None, save_every=25,
                   level_label=None, allowlist=None, seed_urls=None,
                   robots_parser=None, resume_state=None):
    """Crawl the site starting from start_url and scan each page with axe-core.

    If json_path is provided, results are flushed to disk every `save_every`
    pages and on SIGTERM/SIGINT so partial runs preserve progress.
    """
    config = config or {}

    # Line-buffered stdout so progress prints live
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    if tags is None:
        level = level or DEFAULT_LEVEL
        level_info = WCAG_LEVELS.get(level)
        if level_info is None:
            print("ERROR: Unknown level '{}'. Valid levels: {}".format(
                level, ', '.join(sorted(WCAG_LEVELS.keys()))))
            sys.exit(1)
        tags = level_info['tags']
        level_label = level_label or level_info['label']
    else:
        level_label = level_label or 'custom'

    # Lower priority so the scan doesn't starve production services.
    # Chromium is CPU- and memory-hungry; on a shared web server we'd rather
    # the scan be slow than cause Apache/MySQL to be unresponsive.
    niceness = _safe_int(config.get('niceness', 10), 10)
    oom_score = _safe_int(config.get('oom_score_adj', 1000), 1000)
    if niceness:
        try:
            os.nice(niceness)  # higher = lower CPU priority (0-19)
        except (OSError, PermissionError):
            pass  # not fatal — just means we run at normal priority
    if oom_score:
        try:
            # Tell the Linux OOM killer to sacrifice this process first.
            # 1000 = highest possible score = killed before anything else.
            with open('/proc/self/oom_score_adj', 'w') as f:
                f.write(str(oom_score))
        except (IOError, PermissionError):
            pass  # not on Linux or no permission — harmless

    page_wait = _safe_int(config.get('page_wait', 1), 1)
    wait_strategy = config.get('wait_until', 'networkidle')
    if wait_strategy not in ('networkidle', 'load', 'domcontentloaded', 'commit'):
        wait_strategy = 'networkidle'
    # Scan level — determines WCAG version, conformance level,
    # and whether best practices are included.
    # Format: wcagXXy where XX=version (20,21,22), y=level (a,aa,aaa)
    # Special: 'best' = WCAG 2.1 AA + all best practices
    scan_level = level or config.get('level', 'wcag21aa')
    include_best = scan_level == 'best'
    if include_best:
        scan_level = 'wcag21aa'  # base level for best

    # Engine selection — list of engines to run
    engine_names = config.get('engines', None)
    if not engine_names:
        # Legacy single-engine config
        e = config.get('engine', 'axe')
        if e == 'all':
            engine_names = ['axe', 'alfa', 'ibm', 'htmlcs']
        elif e == 'both':
            engine_names = ['axe', 'alfa']
        else:
            engine_names = [e] if e in ('axe', 'alfa', 'ibm', 'htmlcs') else ['axe']

    # Scanner handles browser + engine lifecycle.
    # Constructed here, started inside _pw_sliding_window().
    scanner = Scanner(
        engines=engine_names,
        level=scan_level,
        tags=tags, rules=rules,
        chromium_path=config.get('chromium_path'),
        ignore_certificate_errors=config.get(
            'ignore_certificate_errors') in (True, 'true', 'yes', '1'),
        wait_until=wait_strategy,
        page_wait=page_wait,
        auth=config.get('auth', {}),
        config=config,
        verbose=verbose, quiet=quiet,
    )
    num_workers = _safe_int(config.get('workers', 1), 1)
    # Set up auth cookie header for any http_status() utility calls.
    global _http_cookie_header
    _auth_cookies = load_cookies(config)
    if _auth_cookies:
        _http_cookie_header = '; '.join(
            '{}={}'.format(c['name'], c['value']) for c in _auth_cookies)

    base_url = start_url

    # Initialize crawl state — either from a saved state file (--resume)
    # or from scratch.
    # URLs that cause session logout — discovered during recovery mode,
    # persisted in state files.
    _logout_urls = set()

    if resume_state:
        queue = deque(resume_state['queue'])
        visited = set(resume_state['visited'])
        _logout_urls = set(resume_state.get('logout_urls', []))
        no_crawl = False
        if not quiet:
            print("  Resuming: {} queued, {} already visited".format(
                len(queue), len(visited)))
            if _logout_urls:
                print("  {} banned logout URLs".format(len(_logout_urls)))
    elif seed_urls:
        visited = set()
        queue = deque(normalize_url(u) for u in seed_urls)
        no_crawl = True  # Don't follow links when using a URL list
    else:
        visited = set()
        queue = deque([normalize_url(start_url)])
        no_crawl = False
    page_count = 0
    scan_start_time = time.time()
    total_page_time = 0  # accumulated per-page scan times

    # MEMORY STRATEGY: Stream results to a JSONL file (one JSON object per line)
    # instead of accumulating everything in a Python dict.  Without this, a
    # 5000-page scan would hold ~500MB+ of results in memory.  By writing each
    # page's results to disk immediately, memory usage stays constant regardless
    # of scan size.  The JSONL is later converted to the final JSON/HTML reports
    # by streaming through the file line-by-line.
    jsonl_path = (json_path + 'l') if json_path else None
    if jsonl_path:
        with open(jsonl_path, 'w'):
            pass  # truncate for fresh scan

    if not quiet:
        print("Starting axe-core {} accessibility scan...".format(get_axe_version()))
        print("  Start URL: {}".format(start_url))
        print("  Level: {} ({})".format(level_label, ', '.join(tags)))
        print("  Max pages: {}".format(max_pages))
        if page_wait > 1:
            print("  Page wait: {}s".format(page_wait))
        if json_path and save_every:
            print("  Incremental save every {} pages".format(save_every))
        print()

    def _write_page(url, page_data):
        """Append one page's results to the JSONL file.

        IMPORTANT: This must only be called from the main event loop
        (the `for task in done:` block), never from worker tasks.
        All engines' findings for a page are combined into page_data
        before this call, so each JSONL line contains one page with
        all engines' results together.  This single-writer design is
        relied on for correctness — the JSONL is consumed by dedup,
        --diff, --group-by, and report generation, all of which
        assume one complete line per page with no interleaving.
        """
        if not jsonl_path:
            return
        try:
            with open(jsonl_path, 'a') as f:
                f.write(json.dumps({url: page_data}, default=str) + '\n')
        except (IOError, OSError) as e:
            print("  WARNING: failed to write results for {}: {}".format(
                url, e), file=sys.stderr)

    def _flush(reason=''):
        """Build final JSON + HTML from the JSONL stream on disk."""
        if not json_path or not jsonl_path:
            return
        try:
            # Convert JSONL → final JSON by reading each line and
            # writing it into a single JSON object.  We stream line-by-line
            # so memory stays constant regardless of scan size.
            tmp = json_path + '.tmp'
            with open(tmp, 'w') as out:
                out.write('{\n')
                first_entry = True
                for page_url, page_data in _iter_jsonl(jsonl_path):
                    if not first_entry:
                        out.write(',\n')
                    json_key = json.dumps(page_url)
                    json_val = json.dumps(page_data, default=str)
                    out.write('  {}: {}'.format(json_key, json_val))
                    first_entry = False
                out.write('\n}\n')
            os.replace(tmp, json_path)
            if html_path:
                try:
                    generate_html_report(jsonl_path, html_path, start_url,
                                         level_label or 'WCAG', allowlist=allowlist)
                except Exception as e:
                    print('  (html flush failed: {})'.format(str(e)[:80]))
            if reason:
                print('  [flushed {} pages ({})]'.format(page_count, reason))
        except Exception as e:
            print('  (flush failed: {})'.format(e))

    # Rate limiter shared across all workers to enforce robots.txt crawl delay.
    # This is separate from page_wait (which is per-worker JS settle time).
    # Only the robots.txt crawl_delay is a cross-worker rate limit — page_wait
    # is applied per-worker after each page load to let JavaScript settle.
    crawl_delay = 0
    if robots_parser is not None:
        delay = robots_parser.crawl_delay('a11y-catscan')
        if delay is not None:
            crawl_delay = int(delay)
    rate_limiter = RateLimiter(crawl_delay)

    def _vskip(url, reason):
        """Print a skip notice in verbose mode."""
        if verbose and not quiet:
            print("  skip: {} — {}".format(url, reason))

    # Content types that indicate an HTML page worth scanning.
    _HTML_TYPES = {'text/html', 'application/xhtml+xml', ''}

    # SIGTERM/SIGINT handler: flush partial results and save state.
    interrupted = False

    def _save_state(reason=''):
        """Save crawl state (queue + visited) for --resume.

        Uses write-to-temp + verify + atomic rename to avoid
        corrupting the state file on crash or disk-full.
        """
        if not json_path or no_crawl or not queue:
            return
        state_path = json_path.replace('.json', '.state.json')
        tmp_path = state_path + '.tmp'
        old_path = state_path + '.old'
        try:
            state = {
                'queue': list(queue),
                'visited': sorted(visited),
                'start_url': start_url,
                'pages_scanned': page_count,
                'logout_urls': sorted(_logout_urls),
            }

            # Write to temp file
            with open(tmp_path, 'w') as f:
                json.dump(state, f)

            # Verify: re-read and check key counts match
            with open(tmp_path) as f:
                check = json.load(f)
            if (len(check.get('queue', [])) != len(state['queue'])
                    or len(check.get('visited', [])) != len(state['visited'])):
                raise ValueError(
                    'verification failed: queue {}/{}, visited {}/{}'.format(
                        len(check.get('queue', [])), len(state['queue']),
                        len(check.get('visited', [])), len(state['visited'])))

            # Rotate: current → .old, temp → current
            if os.path.exists(state_path):
                os.replace(state_path, old_path)
            os.replace(tmp_path, state_path)

            # Remove old only after new is safely in place
            try:
                os.unlink(old_path)
            except OSError:
                pass

            if not quiet:
                print("  Crawl state saved: {} ({} queued, {} visited)".format(
                    state_path, len(queue), len(visited)))
        except Exception as e:
            print("  (state save failed: {})".format(e))
            # Clean up temp on failure, leave current intact
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Signal handler sets the interrupted flag.  Each scan mode checks this
    # flag and breaks out of its loop.  We don't call sys.exit() here because
    # asyncio.run() swallows SystemExit and leaves browsers orphaned.
    def _on_signal(signum, frame):
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        print('\n!! Signal {} — flushing {} pages...'.format(signum, page_count))
        _flush(reason='signal {}'.format(signum))
        _save_state()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # SIGUSR1: save state on demand without stopping the scan.
    def _on_usr1(signum, frame):
        print('\n  [SIGUSR1 — saving state snapshot]')
        _flush(reason='snapshot')
        _save_state()
    try:
        signal.signal(signal.SIGUSR1, _on_usr1)
    except (AttributeError, OSError):
        pass  # SIGUSR1 not available on Windows

    # Periodically restart the browser to prevent memory leaks.
    # Chromium accumulates garbage (DOM nodes, JS heaps, image caches) over
    # hundreds of pages, causing slowdowns and occasional 60s+ hangs.
    restart_every = _safe_int(config.get('restart_every', 500), 500)

    if not quiet and num_workers > 1:
        print("  Workers: {} (parallel)".format(num_workers))

    try:
        # --- Playwright async sliding window ---
        # We maintain a sliding window of N concurrent async tasks.
        # As each task finishes, we print its result immediately and
        # start the next.
        import asyncio

        async def _pw_sliding_window():
            nonlocal page_count, total_page_time

            # Suppress Playwright's 'TargetClosedError' Future warnings
            # at shutdown.  These are harmless — they fire when the
            # browser connection drops during cleanup.
            loop = asyncio.get_event_loop()
            _orig_handler = loop.get_exception_handler()
            def _suppress_target_closed(loop, context):
                msg = str(context.get('exception', ''))
                if 'TargetClosedError' in msg or 'Browser closed' in msg:
                    return  # suppress
                if _orig_handler:
                    _orig_handler(loop, context)
                else:
                    loop.default_exception_handler(context)
            loop.set_exception_handler(_suppress_target_closed)

            # Scanner manages browser + engines + auth.
            await scanner.start()
            try:
                _register_browser_pid(
                    scanner.browser.process.pid)
            except Exception:
                pass

                # Recovery mode events (crawl-level, not in Scanner)
                _recovery_mode = asyncio.Event()
                _recovery_done = asyncio.Event()
                _suspect_urls = []

                async def _scan(url, worker_id=0,
                                skip_rate_limit=False):
                    """Scan one URL using Scanner + crawl-level checks."""
                    if _recovery_mode.is_set():
                        await _recovery_done.wait()

                    if not skip_rate_limit:
                        delay = rate_limiter.wait_time()
                        if delay > 0:
                            await asyncio.sleep(delay)

                    # Scanner handles: navigate, validate, run engines,
                    # resolve elements, close page.
                    result = await scanner.scan_page(
                        url, extract_links=(not no_crawl),
                        dedup=False)  # dedup at report time

                    if result.get('skipped'):
                        _vskip(url, result['skipped'])
                        return None

                    # Crawl-level checks that Scanner doesn't handle:
                    actual = normalize_url(result.get('url', url))

                    # Origin check
                    if not is_same_origin(result['url'], base_url):
                        _vskip(url, "redirect off-origin → {}".format(
                            result['url']))
                        return None

                    # Redirect dedup
                    if actual != url:
                        if actual in visited:
                            _vskip(url, "redirect → {} (already visited)".format(
                                actual))
                            return None
                        visited.add(actual)
                        if verbose and not quiet:
                            print("  redirect: {} → {}".format(
                                url, actual))

                    # Session check (triggers recovery if lost)
                    # Scanner exposes check_session but we need
                    # a page to check — do it via a quick test
                    # only if we have auth configured.
                    # Note: Scanner already checked during scan.
                    # For mid-scan expiry, we rely on the login
                    # plugin's is_logged_in check which Scanner
                    # doesn't call (crawl-level concern).
                    # TODO: expose session check in Scanner results

                    new_links = [
                        normalize_url(lnk)
                        for lnk in result.get('links', [])
                        if lnk]

                    elapsed = result.get('elapsed', 0)
                    # Build page_data in the format _write_page expects
                    page_data = {
                        'url': actual,
                        'timestamp': result.get('timestamp', ''),
                        'http_status': result.get('http_status'),
                        EARL_FAILED: result.get(EARL_FAILED, []),
                        EARL_CANTTELL: result.get(EARL_CANTTELL, []),
                        EARL_PASSED: result.get(EARL_PASSED, []),
                        EARL_INAPPLICABLE: result.get(
                            EARL_INAPPLICABLE, []),
                    }
                    return (actual, page_data,
                            new_links, worker_id, elapsed)

                # Merge login plugin exclude_paths into the scan filter
                _all_exclude = list(exclude_paths or [])
                _all_exclude.extend(scanner.login_exclude_paths)

                def _next_url():
                    """Pull the next scannable URL from the queue."""
                    while queue:
                        url = queue.popleft()
                        if url in visited or url in _logout_urls:
                            continue
                        visited.add(url)
                        if should_scan(
                                url, base_url, include_paths,
                                _all_exclude, exclude_regex,
                                robots_parser):
                            return url
                    return None

                # Fill initial window with staggered starts.
                # Worker IDs start at 1 for display.
                pending = {}
                next_worker_id = 1
                # Stagger initial starts by the crawl delay (not
                # page_wait) so each worker's first request is
                # spaced at the rate limit interval.
                stagger = max(crawl_delay, 1) if crawl_delay else (
                    page_wait / max(num_workers, 1))

                def _make_staggered(u, delay, w):
                    """Factory to avoid closure capture bug."""

                    async def _task():
                        if delay > 0:
                            await asyncio.sleep(delay)
                        return await _scan(
                            u, worker_id=w, skip_rate_limit=True)
                    return _task

                for i in range(num_workers):
                    url = _next_url()
                    if url is None or page_count >= max_pages:
                        break
                    wid = next_worker_id
                    next_worker_id += 1
                    task = asyncio.create_task(
                        _make_staggered(url, i * stagger, wid)())
                    pending[task] = url

                # Track which worker IDs are in use.
                # task_workers maps task -> worker_id
                task_workers = {}
                active_wids = set()
                for i, task in enumerate(pending.keys()):
                    wid = i + 1
                    task_workers[task] = wid
                    active_wids.add(wid)

                def _free_wid():
                    """Return the lowest available worker ID."""
                    for w in range(1, num_workers + 1):
                        if w not in active_wids:
                            return w
                    return num_workers  # shouldn't happen

                # Sliding window: as each finishes, print result,
                # feed discovered links, fill empty worker slots.
                while pending and not interrupted:
                    done, _ = await asyncio.wait(
                        pending.keys(),
                        return_when=asyncio.FIRST_COMPLETED)

                    # Collect freed worker IDs from completed tasks
                    freed_wids = []
                    for task in done:
                        del pending[task]
                        wid = task_workers.pop(task, 0)
                        active_wids.discard(wid)
                        freed_wids.append(wid)

                        page_count += 1
                        result = None
                        try:
                            result = task.result()
                        except Exception:
                            pass

                        if result is not None:
                            url, page_data, new_links, _, elapsed = result
                            total_page_time += elapsed
                            v_count = _count_nodes(
                                page_data.get(EARL_FAILED, []))
                            i_count = _count_nodes(
                                page_data.get(EARL_CANTTELL, []))
                            if not quiet:
                                pw_w = len(str(max_pages))
                                parts = []
                                if v_count:
                                    parts.append(
                                        '{} failed'.format(
                                            v_count))
                                if i_count:
                                    parts.append(
                                        '{} cantTell'.format(
                                            i_count))
                                ss = (', '.join(parts)
                                      if parts else 'clean')
                                print("[{}/{}] W{} {} — {} ({:.1f}s)".format(
                                    str(page_count).rjust(pw_w),
                                    max_pages, wid, url, ss,
                                    elapsed))
                                if verbose:
                                    print(
                                        "  V: {} ({} nodes), I: {} ({} nodes),"
                                        " Queue: {}".format(
                                            len(page_data.get(EARL_FAILED, [])),
                                            v_count,
                                            len(page_data.get(EARL_CANTTELL, [])),
                                            i_count, len(queue)))
                            # Single-writer: called from main loop only,
                            # never from worker tasks.  See _write_page.
                            _write_page(url, page_data)

                            for link in new_links:
                                if (link not in visited
                                        and link not in queue):
                                    queue.append(link)
                        else:
                            page_count -= 1

                    # Recovery mode: drain all workers, re-login,
                    # test suspect URLs serially, then resume.
                    if _recovery_mode.is_set() and scanner.context:
                        # Drain remaining in-flight tasks
                        while pending:
                            d2, _ = await asyncio.wait(
                                pending.keys(),
                                return_when=asyncio.FIRST_COMPLETED)
                            for t2 in d2:
                                del pending[t2]
                                task_workers.pop(t2, None)
                                try:
                                    r2 = t2.result()
                                except Exception:
                                    r2 = None
                                if r2 is not None:
                                    page_count += 1
                                    u2, pd2, nl2, _, el2 = r2
                                    total_page_time += el2
                                    # Single-writer: recovery drain,
                                    # still main loop. See _write_page.
                                    _write_page(u2, pd2)
                                    for lnk in nl2:
                                        if (lnk not in visited
                                                and lnk not in queue):
                                            queue.append(lnk)
                        active_wids.clear()

                        if not quiet:
                            print("  [recovery: {} suspect URLs, re-logging in]".format(
                                len(_suspect_urls)))

                        # Re-login via Scanner
                        ctx, _ = await scanner.relogin('recovery')

                        # Test each suspect URL serially
                        safe_urls = []
                        for surl in list(_suspect_urls):
                            result = await scanner.scan_page(
                                surl, dedup=False)
                            if result.get('skipped'):
                                safe_urls.append(surl)
                            else:
                                # Check session after scanning
                                # If page loaded without skipping,
                                # assume session is OK. If the page
                                # triggered a logout, the next scan
                                # will detect it.
                                safe_urls.append(surl)

                        # Requeue safe URLs
                        for surl in safe_urls:
                            if surl not in visited:
                                queue.appendleft(surl)
                        _suspect_urls.clear()

                        _recovery_mode.clear()
                        _recovery_done.set()

                        if not quiet:
                            print("  [recovery done: {} banned, {} requeued]".format(
                                len(_logout_urls), len(safe_urls)))

                    # Fill empty slots with freed worker IDs first,
                    # then allocate new ones if needed.
                    while (len(pending) < num_workers
                           and page_count + len(pending) < max_pages):
                        next_url = _next_url()
                        if next_url is None:
                            break
                        if freed_wids:
                            wid = freed_wids.pop(0)
                        else:
                            wid = _free_wid()
                        active_wids.add(wid)
                        t = asyncio.create_task(
                            _scan(next_url, worker_id=wid))
                        pending[t] = next_url
                        task_workers[t] = wid

                    if (json_path and save_every
                            and page_count % save_every == 0):
                        _flush()

                    # Restart browser periodically to prevent memory leaks.
                    # Wait for all in-flight pages to finish first.
                    if (restart_every and page_count > 0
                            and page_count % restart_every == 0
                            and page_count < max_pages):
                        # Drain remaining in-flight tasks
                        while pending:
                            done2, _ = await asyncio.wait(
                                pending.keys(),
                                return_when=asyncio.FIRST_COMPLETED)
                            for t2 in done2:
                                del pending[t2]
                                task_workers.pop(t2, None)
                                page_count += 1
                                try:
                                    r2 = t2.result()
                                except Exception:
                                    r2 = None
                                if r2 is not None:
                                    u2, pd2, nl2, _, el2 = r2
                                    total_page_time += el2
                                    # Single-writer: restart drain,
                                    # still main loop. See _write_page.
                                    _write_page(u2, pd2)
                                    for lnk in nl2:
                                        if (lnk not in visited
                                                and lnk not in queue):
                                            queue.append(lnk)
                                else:
                                    page_count -= 1
                        # Restart browser + engines to prevent
                        # Chromium memory leaks.
                        if not quiet:
                            print("  [restarting browser after"
                                  " {} pages]".format(page_count))
                        await scanner.restart_browser()
                        try:
                            _register_browser_pid(
                                scanner.browser.process.pid)
                        except Exception:
                            pass
                        active_wids.clear()

                        # Refill the sliding window after restart
                        for i in range(num_workers):
                            if page_count + len(pending) >= max_pages:
                                break
                            next_url = _next_url()
                            if next_url is None:
                                break
                            wid = i + 1
                            active_wids.add(wid)
                            t = asyncio.create_task(
                                _scan(next_url, worker_id=wid))
                            pending[t] = next_url
                            task_workers[t] = wid

                # Cancel any in-flight tasks (e.g. after ^C) so
                # Python doesn't dump "Task exception was never
                # retrieved" tracebacks at shutdown.
                for task in list(pending.keys()):
                    task.cancel()
                for task in list(pending.keys()):
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

                # Shut down scanner (engines + browser)
                await scanner.stop()

        try:
            asyncio.run(_pw_sliding_window())
        except (KeyboardInterrupt, SystemExit):
            _cleanup_browsers()
        except Exception as e:
            print("  Playwright error: {}".format(e), file=sys.stderr)
            _cleanup_browsers()

    finally:
        _flush(reason='final')
        _save_state()

    wall_time = time.time() - scan_start_time
    return page_count, jsonl_path, wall_time, total_page_time


def _iter_jsonl(jsonl_path):
    """Iterate (url, data) pairs from a JSONL results file.

    Skips blank or corrupt lines (e.g. from a partial write after a crash).
    """
    with open(jsonl_path, 'r') as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                print("  WARNING: corrupt JSONL line {} in {}, skipping".format(
                    lineno, jsonl_path), file=sys.stderr)
                continue
            for url, data in obj.items():
                yield url, data


def _iter_report(path):
    """Iterate (url, data) pairs from a JSON or JSONL report file.

    Auto-detects format: if the file parses as a JSON object, iterates
    its key-value pairs. Otherwise falls back to JSONL line-by-line.
    """
    with open(path, 'r') as f:
        first = f.read(1)
        f.seek(0)
        if first == '{':
            try:
                data = json.load(f)
                if isinstance(data, dict):
                    for url, page_data in data.items():
                        if isinstance(page_data, dict):
                            yield url, page_data
                    return
            except (json.JSONDecodeError, ValueError):
                f.seek(0)
    # Fall back to JSONL
    yield from _iter_jsonl(path)


def _iter_deduped(jsonl_path):
    """Iterate (url, deduped_data) from a JSONL results file.

    Same interface as _iter_jsonl but with cross-engine deduplication
    applied to each page.  Findings that share the same
    (selector, primary_tag, outcome) are merged into one finding
    with multi-engine attribution.
    """
    for url, page_data in _iter_jsonl(jsonl_path):
        yield url, dedup_page(page_data)


def _extract_urls_from_report(path, which=EARL_FAILED):
    """Extract URLs with failures or cantTell results from a report file."""
    urls = []
    for url, data in _iter_report(path):
        if data.get(which):
            urls.append(url)
    return urls


def _group_results(jsonl_path, group_by, allowlist=None):
    """Group scan results and print a summary table.

    group_by: 'rule', 'selector', 'color', 'reason', 'wcag'
    """
    import re
    from collections import Counter

    groups = Counter()
    group_pages = {}
    group_examples = {}

    for url, data in _iter_deduped(jsonl_path):
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
                        key = '{} ({})'.format(
                            rule_id, category[:3])
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
                        scs = _parse_wcag_sc(tags)
                        key = ', '.join(scs) if scs else rule_id
                    elif group_by == 'level':
                        scs = _parse_wcag_sc(tags)
                        if scs:
                            lvl, ver = _sc_level(next(iter(scs)))
                            key = 'WCAG {} {}'.format(ver, lvl)
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
                            scs = _parse_wcag_sc(tags)
                            key = 'WCAG ' + ', '.join(scs) if scs else rule_id
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
        print("  {:>5d}  {} ({} pages)".format(count, key, pages))
        if group_by != 'rule':
            print("         rule: {}".format(ex['rule']))
        print("         e.g. {}".format(ex['url']))
        print("              {}".format(ex['target'][:70]))
        print()


def generate_html_report(jsonl_path, output_path, start_url,
                         level_label='WCAG 2.1 Level AA', allowlist=None):
    """Generate an HTML report by streaming through JSONL results on disk.

    Memory usage is O(unique_rules) regardless of page count.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    axe_ver = get_axe_version()
    allowlist = allowlist or []

    # --- Pass 1: aggregate stats (constant memory) ---
    total_pages = 0
    total_violations = 0
    total_violation_nodes = 0
    total_incomplete_nodes = 0
    total_suppressed = 0
    impact_counts = {'critical': 0, 'serious': 0, 'moderate': 0, 'minor': 0}
    rule_summary = {}
    incomplete_summary = {}
    wcag_criteria = {}

    def _track_wcag(tags, category, count=1):
        for sc in _parse_wcag_sc(tags):
            if sc not in wcag_criteria:
                wcag_criteria[sc] = {EARL_FAILED: 0, EARL_CANTTELL: 0, EARL_PASSED: 0}
            wcag_criteria[sc][category] += count

    for url, data in _iter_deduped(jsonl_path):
        total_pages += 1
        for v in data.get(EARL_FAILED, []):
            nodes = v.get('nodes', [])
            total_violations += 1
            total_violation_nodes += len(nodes)
            impact = v.get('impact', 'unknown')
            if impact in impact_counts:
                impact_counts[impact] += len(nodes)
            rule_id = v.get('id', 'unknown')
            if rule_id not in rule_summary:
                rule_summary[rule_id] = {
                    'description': v.get('description', ''),
                    'help': v.get('help', ''),
                    'helpUrl': v.get('helpUrl', ''),
                    'impact': impact,
                    'tags': v.get('tags', []),
                    'count': 0,
                    'pages': [],
                }
            rule_summary[rule_id]['count'] += len(nodes)
            rule_summary[rule_id]['pages'].append(url)
            _track_wcag(v.get('tags', []), EARL_FAILED, len(nodes))

        for v in data.get(EARL_CANTTELL, []):
            nodes = v.get('nodes', [])
            rule_id = v.get('id', 'unknown')
            if _matches_allowlist(rule_id, url, nodes, allowlist,
                                  engines_dict=v.get('engines'),
                                  outcome=EARL_CANTTELL):
                total_suppressed += len(nodes)
                continue
            total_incomplete_nodes += len(nodes)
            if rule_id not in incomplete_summary:
                incomplete_summary[rule_id] = {
                    'help': v.get('help', ''),
                    'helpUrl': v.get('helpUrl', ''),
                    'impact': v.get('impact', 'unknown'),
                    'count': 0,
                    'pages': [],
                }
            incomplete_summary[rule_id]['count'] += len(nodes)
            incomplete_summary[rule_id]['pages'].append(url)
            _track_wcag(v.get('tags', []), EARL_CANTTELL, len(nodes))

        for v in data.get(EARL_PASSED, []):
            _track_wcag(v.get('tags', []), EARL_PASSED)

    sorted_rules = sorted(rule_summary.items(), key=lambda x: x[1]['count'], reverse=True)
    sorted_incomplete = sorted(incomplete_summary.items(), key=lambda x: x[1]['count'], reverse=True)

    impact_colors = {
        'critical': '#d32f2f',
        'serious': '#e65100',
        'moderate': '#f9a825',
        'minor': '#1565c0',
    }

    html_parts = []
    html_parts.append("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A11y CatScan Report</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto; padding: 20px; }
  h1 { color: #1a237e; margin-bottom: 5px; }
  h2 { color: #283593; margin: 30px 0 15px; border-bottom: 2px solid #e8eaf6; padding-bottom: 5px; }
  h3 { color: #3949ab; margin: 20px 0 10px; }
  .meta { color: #666; margin-bottom: 20px; }
  .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                   gap: 15px; margin: 20px 0; }
  .summary-card { background: #f5f5f5; border-radius: 8px; padding: 20px; text-align: center; }
  .summary-card .number { font-size: 2em; font-weight: bold; }
  .summary-card .label { color: #666; font-size: 0.9em; }
  .impact-critical { border-left: 4px solid #d32f2f; }
  .impact-serious { border-left: 4px solid #e65100; }
  .impact-moderate { border-left: 4px solid #f9a825; }
  .impact-minor { border-left: 4px solid #1565c0; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; color: white;
           font-size: 0.8em; font-weight: bold; margin-right: 5px; }
  .badge-critical { background: #d32f2f; }
  .badge-serious { background: #e65100; }
  .badge-moderate { background: #f9a825; color: #333; }
  .badge-minor { background: #1565c0; }
  table { width: 100%; border-collapse: collapse; margin: 10px 0; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #e0e0e0; }
  th { background: #e8eaf6; font-weight: 600; }
  tr:hover { background: #f5f5f5; }
  .rule-card { background: #fafafa; border: 1px solid #e0e0e0; border-radius: 8px;
               padding: 15px; margin: 10px 0; }
  .tag { display: inline-block; background: #e8eaf6; color: #3949ab; padding: 1px 6px;
         border-radius: 3px; font-size: 0.75em; margin: 2px; }
  details { margin: 5px 0; }
  summary { cursor: pointer; font-weight: 500; padding: 5px 0; }
  .node-detail { background: #fff; border: 1px solid #e0e0e0; border-radius: 4px;
                 padding: 10px; margin: 5px 0; }
  .html-snippet { background: #263238; color: #aed581; padding: 8px 12px; border-radius: 4px;
                  font-family: 'Fira Code', monospace; font-size: 0.85em; overflow-x: auto;
                  white-space: pre-wrap; word-break: break-all; }
  .target { color: #666; font-family: monospace; font-size: 0.85em; }
  a { color: #1565c0; }
  .page-section { margin: 25px 0; padding: 15px; border: 1px solid #e0e0e0; border-radius: 8px; }
  .page-url { font-size: 0.9em; color: #1565c0; word-break: break-all; }
  .wcag-ref { font-size: 0.8em; color: #666; }
</style>
</head>
<body>
""")

    html_parts.append('<h1>A11y CatScan Report</h1>')
    html_parts.append('<p class="meta">Scanned: {} | {} | Generated: {} | axe-core {}</p>'.format(
        _esc(start_url), _esc(level_label), now, axe_ver))
    html_parts.append('<p class="meta">Scope: HTML pages only. '
                      'Does not cover PDFs, videos, audio, PowerPoint, Word documents, '
                      'or other media files.</p>')

    html_parts.append('<div class="summary-grid">')
    html_parts.append(
        '<div class="summary-card"><div class="number">{}</div>'
        '<div class="label">Pages Scanned</div></div>'.format(total_pages))
    html_parts.append(
        '<div class="summary-card"><div class="number" style="color:#d32f2f">{}</div>'
        '<div class="label">Total Issues</div></div>'.format(total_violation_nodes))
    html_parts.append(
        '<div class="summary-card"><div class="number">{}</div>'
        '<div class="label">Unique Rules</div></div>'.format(len(rule_summary)))
    html_parts.append(
        '<div class="summary-card"><div class="number">{}</div>'
        '<div class="label">Needs Review</div></div>'.format(total_incomplete_nodes))
    if total_suppressed:
        html_parts.append(
            '<div class="summary-card"><div class="number" style="color:#888">{}</div>'
            '<div class="label">Suppressed (allowlist)</div></div>'.format(total_suppressed))
    html_parts.append('</div>')

    html_parts.append('<h2>Impact Breakdown</h2>')
    html_parts.append('<div class="summary-grid">')
    for impact in ('critical', 'serious', 'moderate', 'minor'):
        cnt = impact_counts[impact]
        html_parts.append(
            '<div class="summary-card impact-{imp}">'
            '<div class="number" style="color:{color}">{cnt}</div>'
            '<div class="label">{imp_cap}</div></div>'.format(
                imp=impact, color=impact_colors[impact],
                cnt=cnt, imp_cap=impact.capitalize()))
    html_parts.append('</div>')

    # WCAG criteria summary
    if wcag_criteria:
        sorted_sc = sorted(wcag_criteria.items(), key=lambda x: x[0])
        html_parts.append('<h2>WCAG Success Criteria</h2>')
        html_parts.append('<table><tr><th>Criterion</th><th>Name</th>'
                          '<th style="color:#d32f2f">Violations</th>'
                          '<th style="color:#e65100">Incomplete</th>'
                          '<th style="color:#2e7d32">Passes</th>'
                          '<th>Status</th></tr>')
        for sc, counts in sorted_sc:
            name = sc_name(sc)
            v = counts[EARL_FAILED]
            i = counts[EARL_CANTTELL]
            p = counts[EARL_PASSED]
            if v > 0:
                status = '<span class="badge badge-critical">FAIL</span>'
            elif i > 0:
                status = '<span class="badge badge-serious">REVIEW</span>'
            else:
                status = '<span style="color:#2e7d32;font-weight:bold">PASS</span>'
            html_parts.append(
                '<tr><td>{sc}</td><td>{name}</td>'
                '<td>{v}</td><td>{i}</td><td>{p}</td>'
                '<td>{status}</td></tr>'.format(
                    sc=_esc(sc), name=_esc(name),
                    v=v or '', i=i or '', p=p or '',
                    status=status))
        html_parts.append('</table>')

    if sorted_rules:
        html_parts.append('<h2>Violation Summary by Rule</h2>')
        html_parts.append('<table><tr><th>Rule</th><th>Impact</th><th>Issues</th>'
                          '<th>Pages</th><th>Description</th></tr>')
        for rule_id, info in sorted_rules:
            impact = info['impact']
            html_parts.append(
                '<tr><td><a href="{url}">{id}</a></td>'
                '<td><span class="badge badge-{imp}">{imp_cap}</span></td>'
                '<td>{count}</td><td>{pages}</td><td>{desc}</td></tr>'.format(
                    url=_esc(info['helpUrl']), id=_esc(rule_id),
                    imp=impact, imp_cap=impact.capitalize(),
                    count=info['count'], pages=len(set(info['pages'])),
                    desc=_esc(info['help'])))
        html_parts.append('</table>')

    # Incomplete summary table
    if sorted_incomplete:
        html_parts.append('<h2>Incomplete Summary (Needs Manual Review)</h2>')
        html_parts.append('<p class="meta">axe-core could not automatically determine '
                          'pass/fail for these items — typically color-contrast on elements '
                          'with background images, gradients, or pseudo-elements.</p>')
        html_parts.append('<table><tr><th>Rule</th><th>Nodes</th>'
                          '<th>Pages</th><th>Description</th></tr>')
        for rule_id, info in sorted_incomplete:
            html_parts.append(
                '<tr><td><a href="{url}">{id}</a></td>'
                '<td>{count}</td><td>{pages}</td><td>{desc}</td></tr>'.format(
                    url=_esc(info['helpUrl']), id=_esc(rule_id),
                    count=info['count'], pages=len(set(info['pages'])),
                    desc=_esc(info['help'])))
        html_parts.append('</table>')

    # --- Pass 2: per-page details (deduped, stream again) ---
    html_parts.append('<h2>Per-Page Details</h2>')
    clean_pages = []
    for url, data in _iter_deduped(jsonl_path):
        violations = data.get(EARL_FAILED, [])
        incomplete = data.get(EARL_CANTTELL, [])
        # Filter out allowlisted incompletes
        shown_incomplete = []
        for v in incomplete:
            rule_id = v.get('id', '')
            nodes = v.get('nodes', [])
            if not _matches_allowlist(rule_id, url, nodes, allowlist,
                                        engines_dict=v.get('engines'),
                                        outcome=EARL_CANTTELL):
                shown_incomplete.append(v)
        if not violations and not shown_incomplete:
            clean_pages.append(url)
            continue

        v_count = _count_nodes(violations)
        i_count = _count_nodes(shown_incomplete)
        html_parts.append('<div class="page-section">')
        html_parts.append('<h3><a href="{}" class="page-url">{}</a></h3>'.format(
            _esc(url), _esc(url)))
        html_parts.append('<p>{} violation(s), {} issue(s) &mdash; {} incomplete, {} node(s)</p>'.format(
            len(violations), v_count, len(shown_incomplete), i_count))

        for v in violations:
            impact = v.get('impact', 'unknown')
            html_parts.append('<div class="rule-card impact-{}">'.format(impact))
            html_parts.append(
                '<strong><span class="badge badge-{}">{}</span> '
                '<a href="{}">{}</a></strong>'.format(
                    impact, impact.capitalize(),
                    _esc(v.get('helpUrl', '')), _esc(v.get('id', ''))))
            html_parts.append('<p>{}</p>'.format(_esc(v.get('help', ''))))
            tags = v.get('tags', [])
            wcag_tags = [t for t in tags if t.startswith('wcag')]
            if wcag_tags:
                html_parts.append('<p class="wcag-ref">WCAG: {}</p>'.format(
                    ' '.join('<span class="tag">{}</span>'.format(_esc(t)) for t in wcag_tags)))
            html_parts.append(_render_nodes_html(v.get('nodes', []), limit=20))
            html_parts.append('</div>')

        if shown_incomplete:
            html_parts.append('<h4 style="margin-top:1em;color:#e65100;">Incomplete (needs manual review)</h4>')
            for v in shown_incomplete:
                html_parts.append('<div class="rule-card">')
                html_parts.append(
                    '<strong><a href="{}">{}</a></strong>'.format(
                        _esc(v.get('helpUrl', '')), _esc(v.get('id', ''))))
                html_parts.append('<p>{}</p>'.format(_esc(v.get('help', ''))))
                html_parts.append(_render_nodes_html(v.get('nodes', []), limit=10, snippet_max=300))
                html_parts.append('</div>')

        html_parts.append('</div>')

    if clean_pages:
        html_parts.append('<h2>Fully Clean Pages ({})'.format(len(clean_pages)))
        html_parts.append('</h2><ul>')
        for url in clean_pages:
            html_parts.append('<li><a href="{}">{}</a></li>'.format(_esc(url), _esc(url)))
        html_parts.append('</ul>')

    html_parts.append('</body></html>')

    with open(output_path, 'w') as f:
        f.write('\n'.join(html_parts))


def _esc(text):
    """Escape HTML special characters."""
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;'))


def _render_nodes_html(nodes, limit=20, snippet_max=500):
    """Render axe-core node details as HTML fragments."""
    parts = []
    parts.append('<details><summary>{} element(s)</summary>'.format(len(nodes)))
    for node in nodes[:limit]:
        parts.append('<div class="node-detail">')
        target = node.get('target', [])
        if target:
            parts.append('<p class="target">Selector: {}</p>'.format(
                _esc(', '.join(str(t) for t in target))))
        html_snippet = node.get('html', '')
        if html_snippet:
            if len(html_snippet) > snippet_max:
                html_snippet = html_snippet[:snippet_max] + '...'
            parts.append('<div class="html-snippet">{}</div>'.format(_esc(html_snippet)))
        messages = []
        for check in node.get('any', []) + node.get('all', []) + node.get('none', []):
            msg = check.get('message', '')
            if msg:
                messages.append(msg)
        if messages:
            parts.append('<ul>')
            for msg in messages[:5]:
                parts.append('<li>{}</li>'.format(_esc(msg)))
            parts.append('</ul>')
        parts.append('</div>')
    if len(nodes) > limit:
        parts.append('<p><em>... and {} more</em></p>'.format(len(nodes) - limit))
    parts.append('</details>')
    return '\n'.join(parts)


def generate_llm_report(jsonl_path, output_path, start_url,
                        level_label='WCAG 2.1 Level AA', allowlist=None,
                        config=None):
    """Generate a token-efficient markdown summary optimized for LLMs.

    Instead of dumping raw JSON (100K+ tokens for a large scan), this
    produces a compact report (~2-5K tokens) with:
    - Context and instructions for the LLM
    - Violations grouped by rule with deduplicated examples
    - Incompletes grouped by messageKey
    - Affected page lists (URLs only, no repeated node data)
    """
    allowlist = allowlist or []
    axe_ver = get_axe_version()

    # Aggregate: {rule_id -> {info, pages, example_nodes}}
    violations_by_rule = {}
    incompletes_by_key = {}
    total_pages = 0
    pages_with_violations = set()
    pages_with_incompletes = set()
    suppressed_count = 0

    for url, data in _iter_deduped(jsonl_path):
        total_pages += 1
        path = urlparse(url).path

        for v in data.get(EARL_FAILED, []):
            rule_id = v.get('id', 'unknown')
            nodes = v.get('nodes', [])
            pages_with_violations.add(path)
            if rule_id not in violations_by_rule:
                violations_by_rule[rule_id] = {
                    'help': v.get('help', ''),
                    'helpUrl': v.get('helpUrl', ''),
                    'impact': v.get('impact', ''),
                    'tags': v.get('tags', []),
                    'count': 0,
                    'pages': [],
                    'examples': [],
                }
            info = violations_by_rule[rule_id]
            info['count'] += len(nodes)
            if path not in info['pages']:
                info['pages'].append(path)
            # Keep up to 3 unique example HTML snippets
            for node in nodes:
                snippet = node.get('html', '')[:200]
                if snippet and len(info['examples']) < 3 and snippet not in info['examples']:
                    info['examples'].append(snippet)

        for v in data.get(EARL_CANTTELL, []):
            nodes = v.get('nodes', [])
            rule_id = v.get('id', 'unknown')
            if _matches_allowlist(rule_id, url, nodes, allowlist,
                                  engines_dict=v.get('engines'),
                                  outcome=EARL_CANTTELL):
                suppressed_count += len(nodes)
                continue
            pages_with_incompletes.add(path)
            for node in nodes:
                for check in node.get('any', []):
                    d = check.get('data', {})
                    mk = d.get('messageKey', '') if isinstance(d, dict) else ''
                    if mk not in incompletes_by_key:
                        incompletes_by_key[mk] = {'count': 0, 'pages': set(), 'examples': []}
                    info = incompletes_by_key[mk]
                    info['count'] += 1
                    info['pages'].add(path)
                    snippet = node.get('html', '')[:150]
                    if snippet and len(info['examples']) < 2 and snippet not in info['examples']:
                        info['examples'].append(snippet)

    # Build markdown
    lines = []
    lines.append('# a11y-catscan accessibility scan results\n')
    lines.append('Site: {}  '.format(start_url))
    lines.append('Level: {}  '.format(level_label))
    lines.append('axe-core: {}  '.format(axe_ver))
    lines.append('Pages scanned: {}  '.format(total_pages))
    lines.append('Scan date: {}\n'.format(datetime.now().strftime('%Y-%m-%d')))
    lines.append('**Scope**: HTML pages only.  This scan does not cover accessibility of '
                 'PDFs, videos, audio, PowerPoint, Word documents, or other media files.')

    # Instructions section — read from a file if configured, otherwise use defaults.
    # This lets each site customize the LLM prompt for their specific codebase
    # (e.g. "templates are in app/templates/cdm/", "use LESS not CSS", etc.)
    llm_instructions_path = config.get('llm_instructions') if config else None
    if llm_instructions_path and os.path.exists(llm_instructions_path):
        with open(llm_instructions_path) as f:
            lines.append(f.read().rstrip())
        lines.append('')
    else:
        lines.append('## Instructions\n')
        lines.append('This is a WCAG accessibility scan summary. When investigating:')
        lines.append('- Each violation needs a code fix — find the source that generates the flagged HTML')
        lines.append('- Incompletes are items axe-core could not auto-verify (usually contrast issues)')
        lines.append('- The "examples" show representative HTML — the same pattern repeats across listed pages')
        lines.append('- Focus on violations first (failures), then incompletes (may be false positives)\n')

    # Violations
    if violations_by_rule:
        lines.append('## Violations ({} issues on {} pages)\n'.format(
            sum(v['count'] for v in violations_by_rule.values()),
            len(pages_with_violations)))
        for rule_id, info in sorted(
                violations_by_rule.items(),
                key=lambda x: x[1]['count'], reverse=True):
            wcag_scs = ', '.join(sorted(_parse_wcag_sc(info['tags'])))
            lines.append('### {} ({}, {} issues)'.format(rule_id, info['impact'], info['count']))
            lines.append('{}'.format(info['help']))
            if wcag_scs:
                lines.append('WCAG: {}'.format(wcag_scs))
            lines.append('Pages: {}'.format(', '.join(info['pages'][:10])))
            if len(info['pages']) > 10:
                lines.append('  ... and {} more'.format(len(info['pages']) - 10))
            lines.append('Examples:')
            for ex in info['examples']:
                lines.append('```html\n{}\n```'.format(ex))
            lines.append('')
    else:
        lines.append('## Violations: NONE\n')

    # Incompletes
    if incompletes_by_key:
        total_inc = sum(v['count'] for v in incompletes_by_key.values())
        lines.append('## Incompletes ({} nodes on {} pages)\n'.format(
            total_inc, len(pages_with_incompletes)))
        for mk, info in sorted(
                incompletes_by_key.items(),
                key=lambda x: x[1]['count'], reverse=True):
            lines.append('### {} — {} nodes, {} pages'.format(
                mk or '(unknown)', info['count'], len(info['pages'])))
            pages_list = sorted(info['pages'])
            lines.append('Pages: {}'.format(', '.join(pages_list[:10])))
            if len(pages_list) > 10:
                lines.append('  ... and {} more'.format(len(pages_list) - 10))
            if info['examples']:
                lines.append('Example:')
                lines.append('```html\n{}\n```'.format(info['examples'][0]))
            lines.append('')
    else:
        lines.append('## Incompletes: NONE\n')

    if suppressed_count:
        lines.append('## Suppressed (allowlist): {} nodes\n'.format(suppressed_count))

    # Point to full reports for deeper investigation
    json_sibling = output_path.replace('.md', '.json')
    jsonl_sibling = output_path.replace('.md', '.jsonl')
    html_sibling = output_path.replace('.md', '.html')
    lines.append('## Detailed reports\n')
    lines.append('This is a summary.  For full per-page, per-node details:')
    lines.append('- JSON (full axe-core output): {}'.format(json_sibling))
    lines.append('- JSONL (streaming, for --diff/--rescan): {}'.format(jsonl_sibling))
    lines.append('- HTML (human-readable report): {}'.format(html_sibling))
    lines.append('- Run `a11y-catscan.py --help-audit` for the full audit workflow guide')
    lines.append('')

    report = '\n'.join(lines)
    with open(output_path, 'w') as f:
        f.write(report)
    return report


def diff_scans(old_jsonl, new_jsonl, allowlist=None):
    """Compare two scans and print what changed.

    Returns (fixed_count, new_count) violation nodes.
    """
    allowlist = allowlist or []

    def _violation_keys(jsonl_path):
        """Return {(url_path, rule_id): node_count} for violations."""
        keys = {}
        for url, data in _iter_jsonl(jsonl_path):
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
            print("    - {} on {} ({} nodes)".format(rule, path, count))

    if added:
        print("\n  NEW ({} rule/page combos, {} nodes):".format(
            len(added), sum(added.values())))
        for (path, rule), count in sorted(added.items()):
            print("    + {} on {} ({} nodes)".format(rule, path, count))

    if remaining:
        print("\n  REMAINING ({} rule/page combos, {} nodes)".format(
            len(remaining), sum(remaining.values())))

    if not fixed and not added:
        print("\n  No changes in violations.")

    return sum(fixed.values()), sum(added.values())


def main():
    parser = argparse.ArgumentParser(
        description='Scan a website for WCAG accessibility violations using axe-core.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('url', nargs='?', default=None,
                        help='Starting URL to scan')
    parser.add_argument('--config', default=None,
                        help='Path to YAML config file (default: a11y-catscan.yaml alongside script)')
    parser.add_argument('--level', default=None,
                        choices=sorted(WCAG_LEVELS.keys()),
                        help='WCAG conformance level (default: wcag21aa)')
    parser.add_argument('--max-pages', type=int, default=None,
                        help='Maximum pages to scan (default: 50)')
    parser.add_argument('--tags', default=None,
                        help='Comma-separated axe-core tags (overrides --level)')
    parser.add_argument('--include-path', action='append', default=None,
                        help='Only scan URLs starting with this prefix (repeatable)')
    parser.add_argument('--exclude-path', action='append', default=None,
                        help='Skip URLs starting with this prefix (repeatable, adds to config)')
    parser.add_argument('--no-default-excludes', action='store_true',
                        help='Ignore exclude_paths from config file')
    parser.add_argument('--ignore-robots', action='store_true',
                        help='Ignore robots.txt (by default, disallowed paths are skipped)')
    parser.add_argument('--name', '--output', default=None, dest='output',
                        help='Job name used as the basename for all output files '
                             '(default: a11y-catscan-YYYY-MM-DD-HHMMSS)')
    parser.add_argument('--output-dir', default=None,
                        help='Output directory (default: from config or current directory)')
    parser.add_argument('--allowlist', default=None,
                        help='YAML file of known-acceptable incompletes to suppress')
    parser.add_argument('--workers', type=int, default=None,
                        help='Number of parallel browser instances (default: 1). '
                             'Each uses ~200-500MB RAM. Rate limits are shared.')
    parser.add_argument('--wait-until', default=None,
                        choices=['networkidle', 'load', 'domcontentloaded', 'commit'],
                        help='Page load strategy (default: networkidle). '
                             'networkidle waits for no network activity for 500ms. '
                             'load uses the traditional load event + page_wait delay.')
    parser.add_argument('--engine', default=None,
                        metavar='ENGINE[,ENGINE,...]',
                        help='Accessibility engines, comma-separated (default: axe). '
                             'Engines: axe, alfa, ibm, htmlcs, all. '
                             'Example: --engine axe,alfa')
    parser.add_argument('--save-every', type=int, default=None,
                        help='Flush reports every N pages (default: 25). '
                             'Partial results survive if the scan is killed.')
    parser.add_argument('--diff', default=None, metavar='PREV.jsonl',
                        help='Compare against a previous scan JSONL and show what changed')
    parser.add_argument('--urls', default=None, metavar='FILE',
                        help='Scan URLs from a file (one per line) instead of crawling')
    parser.add_argument('--rescan', default=None, metavar='PREV.jsonl',
                        help='Re-scan only pages that had violations or incompletes in a previous scan')
    parser.add_argument('--violations-from', default=None, metavar='REPORT',
                        help='Extract and re-scan only pages with violations from a previous JSON or JSONL report')
    parser.add_argument('--incompletes-from', default=None, metavar='REPORT',
                        help='Extract and re-scan only pages with incompletes from a previous JSON or JSONL report')
    parser.add_argument('--group-by', default=None,
                        choices=['rule', 'selector', 'color', 'reason',
                                 'wcag', 'level', 'engine', 'bp'],
                        help='After scanning, print a grouped summary. '
                             'rule: by axe rule ID. selector: by CSS selector pattern. '
                             'color: by foreground/background color pair. '
                             'reason: by incomplete reason category. '
                             'wcag: by WCAG success criterion.')
    parser.add_argument('--resume', default=None, metavar='STATE.json',
                        help='Resume a previous crawl from its saved state file')
    parser.add_argument('--rule', action='append', default=None,
                        help='Only run specific axe rules (repeatable, e.g. --rule color-contrast)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--page', action='store_true',
                       help='Scan only the given URL (no crawling). Fast single-page verify.')
    group.add_argument('--crawl', action='store_true', default=True,
                       help='Crawl and discover pages from the starting URL (default).')
    parser.add_argument('--llm', action='store_true',
                        help='Generate a compact markdown summary optimized for LLM context')
    parser.add_argument('--summary-json', action='store_true',
                        help='Print a one-line JSON summary to stdout (machine-parseable)')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Suppress per-page progress, only show final summary')
    parser.add_argument('--help-audit', action='store_true',
                        help='Print a guide for using this tool to perform a WCAG audit')
    parser.add_argument('--cleanup', action='store_true',
                        help='Kill orphaned chromium processes from previous runs and exit')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show detailed rule/node counts for pages with issues')

    # Report analysis flags (no scan needed — operate on previous reports)
    report_group = parser.add_argument_group('report analysis')
    report_group.add_argument('--list-scans', action='store_true',
                              help='List all registered scan names and exit')
    report_group.add_argument('--page-status', default=None, metavar='URL',
                              help='Check a specific URL in the latest scan '
                                   '(or use --name to specify which scan)')
    report_group.add_argument('--search', default=None, metavar='SC_OR_PATTERN',
                              help='Search findings in a report. Prefix with '
                                   'sc: for WCAG SC (sc:1.4.3), url: for URL '
                                   'pattern (url:/groups/*), sel: for selector '
                                   '(sel:*table*), or engine: (engine:axe)')

    args = parser.parse_args()

    config = load_config(args.config)

    if args.cleanup:
        # Kill any orphaned chromium processes owned by this user
        killed = 0
        try:
            result = subprocess.run(
                ['ps', '-u', str(os.getuid()), '-o', 'pid,comm'],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        pid = int(parts[0])
                        comm = parts[1]
                        if pid != os.getpid() and (
                                'chrome' in comm
                                or 'chrome' in comm):
                            os.kill(pid, signal.SIGKILL)
                            killed += 1
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
        except Exception:
            pass
        print("Killed {} orphaned browser process(es).".format(killed))
        sys.exit(0)

    # Report analysis flags — operate on previous reports, no scan needed.
    if args.list_scans:
        from registry import list_scans
        scans = list_scans()
        if not scans:
            print("No registered scans.")
        else:
            print("{} registered scan(s):\n".format(len(scans)))
            for sname, info in sorted(scans.items()):
                summary = info.get('summary', {})
                status = ('clean' if summary.get('clean')
                          else '{} failed'.format(
                              summary.get(EARL_FAILED, '?')))
                print("  {:<25s} {} — {} pages, {} [{} engine(s)]".format(
                    sname,
                    info.get('timestamp', '')[:19],
                    summary.get('pages', '?'),
                    status,
                    len(info.get('engines', []))))
                reports = info.get('reports', {})
                if reports.get('jsonl'):
                    print("    {}".format(reports['jsonl']))
        sys.exit(0)

    if args.page_status:
        from registry import page_status, list_scans
        # Find the report to check — use --name if given, else latest
        jsonl_path = None
        if args.output:
            jsonl_path = os.path.join(
                config.get('output_dir', '.'),
                args.output + '.jsonl')
        if not jsonl_path or not os.path.exists(jsonl_path):
            # Use most recent registered scan
            scans = list_scans()
            if scans:
                latest = max(scans.items(),
                             key=lambda x: x[1].get('timestamp', ''))
                reports = latest[1].get('reports', {})
                jsonl_path = reports.get('jsonl', '')
        if not jsonl_path or not os.path.exists(jsonl_path):
            print("No scan report found. Run a scan first or specify --name.")
            sys.exit(1)
        result = page_status(jsonl_path, args.page_status)
        if not result.get('found'):
            print("URL not found in report: {}".format(args.page_status))
            sys.exit(1)
        status = 'CLEAN' if result['clean'] else 'FAILING'
        print("{} — {} ({} failed, {} cantTell)".format(
            result['url'], status, result['failed'], result['cantTell']))
        if result.get('sc_breakdown'):
            print("\n  WCAG SCs:")
            for sc_id, info in sorted(result['sc_breakdown'].items()):
                parts = []
                if info['failed']:
                    parts.append('{} failed'.format(info['failed']))
                if info['cantTell']:
                    parts.append('{} cantTell'.format(info['cantTell']))
                print("    {} {} — {}".format(
                    sc_id, info['name'], ', '.join(parts)))
        if result.get('findings'):
            print("\n  Findings:")
            for f in result['findings'][:20]:
                engines = f.get('engines', {})
                eng_str = ('+'.join(sorted(engines))
                           if engines else '?')
                print("    [{}] {} — {} — {}".format(
                    f['outcome'][:6], f['id'], eng_str,
                    f['selector'][:50]))
        sys.exit(0 if result['clean'] else 1)

    if args.search:
        from registry import search_findings, list_scans
        # Parse search query
        query = args.search
        sc = url_pat = sel_pat = outcome_filter = eng_filter = None
        if query.startswith('sc:'):
            sc = query[3:]
        elif query.startswith('url:'):
            url_pat = query[4:]
        elif query.startswith('sel:'):
            sel_pat = query[4:]
        elif query.startswith('engine:'):
            eng_filter = query[7:]
        elif query.startswith('outcome:'):
            outcome_filter = query[8:]
        else:
            # Default: treat as SC if it looks like one, else selector
            if re.match(r'^\d+\.\d+\.\d+$', query):
                sc = query
            else:
                sel_pat = '*' + query + '*'
        # Find report
        jsonl_path = None
        if args.output:
            jsonl_path = os.path.join(
                config.get('output_dir', '.'),
                args.output + '.jsonl')
        if not jsonl_path or not os.path.exists(jsonl_path):
            scans = list_scans()
            if scans:
                latest = max(scans.items(),
                             key=lambda x: x[1].get('timestamp', ''))
                reports = latest[1].get('reports', {})
                jsonl_path = reports.get('jsonl', '')
        if not jsonl_path or not os.path.exists(jsonl_path):
            print("No scan report found.")
            sys.exit(1)
        matches = search_findings(
            jsonl_path, sc=sc, url_pattern=url_pat,
            selector_pattern=sel_pat, outcome=outcome_filter,
            engine=eng_filter)
        print("{} finding(s) matching '{}':\n".format(
            len(matches), args.search))
        for m in matches[:50]:
            engines = m.get('engines', {})
            eng_str = ('+'.join(sorted(engines))
                       if engines else m.get('engine', '?'))
            print("  [{}] {} — {} — {}".format(
                m['outcome'][:6], m['id'], eng_str,
                m['selector'][:50]))
            print("    {}".format(m['url']))
        if len(matches) > 50:
            print("\n  ... and {} more".format(len(matches) - 50))
        sys.exit(0)

    if args.help_audit:
        print("""
WCAG Accessibility Audit Guide
===============================

You are a WCAG accessibility auditor. Use a11y-catscan to scan websites for
WCAG 2.1 AA compliance violations and then fix them in the source code.

AUDIT WORKFLOW
--------------
1. SCAN: Run a full crawl to establish a baseline.
     a11y-catscan.py --max-pages 500 --llm https://example.com/
   Read the .md (LLM report) for a concise summary of issues.

2. PRIORITIZE: Fix violations first (WCAG failures), then incompletes.
   Violations are grouped by rule — fix the rule with the most instances
   first for maximum impact.

3. FIX: For each violation, find the template/CSS that generates the
   flagged HTML. Common fixes:
   - color-contrast: darken text or lighten background to reach 4.5:1
   - missing alt text: add descriptive alt attributes to images
   - missing labels: add <label> or aria-label to form controls
   - empty headings/links: add text content or aria-label
   - focus visible: add :focus outline styles

4. VERIFY: After each fix, re-check the specific page:
     a11y-catscan.py --page -q --summary-json https://example.com/fixed-page
   Check exit code: 0 = clean, 1 = still has violations.

5. REGRESSION CHECK: Re-scan previous failures to confirm fixes:
     a11y-catscan.py --rescan baseline.jsonl --diff baseline.jsonl --llm
   The diff shows what was fixed vs what's new vs what remains.

6. SUPPRESS KNOWN ISSUES: For axe-core limitations that aren't real
   accessibility problems (e.g. can't compute contrast on gradients),
   add entries to an allowlist.yaml:
     - rule: color-contrast
       url: /homepage
       reason: axe-core flex layout measurement limitation

UNDERSTANDING RESULTS
---------------------
- VIOLATIONS: Definite WCAG failures. Must be fixed.
- INCOMPLETE: axe-core couldn't auto-verify. May be real issues or
  false positives. Common causes: background gradients, images, pseudo-
  elements blocking contrast computation, elements outside viewport.
- PASSES: Rules that were checked and satisfied.

COMMON AXE-CORE INCOMPLETE TYPES (usually not real issues):
- bgOverlap/elmPartiallyObscured: flex/scroll layout measurement artifacts
- pseudoContent: CSS ::before/::after blocking contrast computation
- bgGradient/bgImage: background-image preventing contrast resolution
  Fix: set explicit background-color on text elements
- shortTextContent: single-character text (e.g. x delete buttons)
  Fix: move character to CSS ::after, leave element empty
- nonBmp: icon font glyphs axe can't evaluate
  Fix: move icon character to CSS ::after on aria-hidden elements

KEY FLAGS FOR LLM WORKFLOWS
----------------------------
--page              Scan one URL, no crawling (fast verify after a fix)
--rule NAME         Check only specific rules (fast, focused)
--summary-json      Machine-parseable one-line JSON output
--llm               Generate compact markdown report (~300 tokens vs 300K)
--diff PREV.jsonl   Show what changed since last scan
--rescan PREV.jsonl Only re-scan pages that previously had issues
--allowlist FILE    Suppress known-acceptable incompletes
-q                  Quiet — no per-page progress, just final summary
-v                  Verbose — add detailed rule/node counts for problem pages

OTHER NOTES
-----------
- robots.txt is respected by default.  Use --ignore-robots to scan
  disallowed paths, or set ignore_robots: true in your config.
- Reports are flushed every 25 pages (configurable with --save-every)
  and on SIGTERM/SIGINT, so partial results survive if the scan is killed.
- The scanner runs at low CPU priority (nice 10) and high OOM score
  (1000) by default so it won't starve production services on shared
  servers.  Both are configurable in a11y-catscan.yaml.
""")
        sys.exit(0)

    # Load config
    config = load_config(args.config)

    # Resolve URL: command line > config > error
    url = args.url or config.get('url')
    if not url:
        parser.error('No URL specified. Provide a URL argument or set "url" in config.')
    if not url.startswith('http'):
        url = 'https://' + url

    # Resolve tags/level
    tags = None
    if args.tags:
        tags = [t.strip() for t in args.tags.split(',')]
    level = args.level or config.get('level', DEFAULT_LEVEL)
    level_info = WCAG_LEVELS.get(level, {})
    level_label = level_info.get('label', 'Custom') if not args.tags else 'Custom tags'

    # Load URL list from file or previous scan
    seed_urls = None
    if args.rescan:
        if not os.path.exists(args.rescan):
            parser.error('Rescan file not found: {}'.format(args.rescan))
        seed_urls = []
        for prev_url, prev_data in _iter_jsonl(args.rescan):
            if prev_data.get(EARL_FAILED) or prev_data.get(EARL_CANTTELL):
                seed_urls.append(prev_url)
        if not seed_urls:
            print("No failures in previous scan — nothing to rescan.")
            sys.exit(0)
        print("Rescanning {} pages with previous failures".format(len(seed_urls)))
    if args.violations_from:
        if not os.path.exists(args.violations_from):
            parser.error('Report not found: {}'.format(args.violations_from))
        seed_urls = _extract_urls_from_report(args.violations_from, EARL_FAILED)
        if not seed_urls:
            print("No failures in previous report — nothing to rescan.")
            sys.exit(0)
        print("Rescanning {} pages with previous failures".format(len(seed_urls)))
        if not url:
            url = seed_urls[0]
    if args.incompletes_from:
        if not os.path.exists(args.incompletes_from):
            parser.error('Report not found: {}'.format(args.incompletes_from))
        seed_urls = _extract_urls_from_report(args.incompletes_from, EARL_CANTTELL)
        if not seed_urls:
            print("No incompletes in previous report — nothing to rescan.")
            sys.exit(0)
        print("Rescanning {} pages with previous incompletes".format(len(seed_urls)))
        if not url:
            url = seed_urls[0]
    if args.urls:
        if not os.path.exists(args.urls):
            parser.error('URL file not found: {}'.format(args.urls))
        with open(args.urls) as f:
            seed_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        if not seed_urls:
            parser.error('No URLs found in {}'.format(args.urls))
        if not url:
            url = seed_urls[0]

    # Resolve max pages
    if args.page:
        max_pages = 1
    elif seed_urls:
        max_pages = len(seed_urls)
    else:
        max_pages = args.max_pages or _safe_int(config.get('max_pages', 50), 50)

    # Resolve exclude paths: config defaults + CLI additions
    exclude_paths = []
    if not args.no_default_excludes:
        exclude_paths.extend(config.get('exclude_paths', []))
    if args.exclude_path:
        for p in args.exclude_path:
            if p not in exclude_paths:
                exclude_paths.append(p)

    # Resolve include paths: CLI only (config can set defaults)
    include_paths = args.include_path or config.get('include_paths')

    # Resolve exclude regex from config
    exclude_regex = None
    regex_list = config.get('exclude_regex', [])
    if regex_list and not args.no_default_excludes:
        exclude_regex = []
        for pattern in regex_list:
            try:
                exclude_regex.append(re.compile(pattern))
            except re.error as e:
                print("WARNING: invalid exclude_regex '{}': {}".format(pattern, e),
                      file=sys.stderr)

    # Query parameters to strip from URLs during normalization.
    # This deduplicates sort/filter/pagination variants of the same page.
    # Entries can be plain strings (global) or dicts with path + params
    # for path-conditional stripping.
    global _strip_params, _strip_path_rules_compiled
    strip_list = config.get('strip_query_params', [])
    if isinstance(strip_list, str):
        strip_list = [s.strip() for s in strip_list.split(',')]
    _strip_params = set()
    _strip_path_rules_compiled = []
    for entry in strip_list:
        if isinstance(entry, str):
            _strip_params.add(entry)
        elif isinstance(entry, dict) and 'path' in entry:
            params = entry.get('querystring', entry.get('params', []))
            if isinstance(params, str):
                params = [p.strip() for p in params.split(',')]
            try:
                _strip_path_rules_compiled.append(
                    (re.compile(entry['path']), set(params)))
            except re.error as e:
                print("WARNING: invalid strip_query_params path "
                      "regex '{}': {}".format(entry['path'], e),
                      file=sys.stderr)

    # Resolve output
    save_every = args.save_every or _safe_int(config.get('save_every', 25), 25)

    # Workers: number of parallel browser instances
    if args.workers:
        config['workers'] = args.workers
    if args.wait_until:
        config['wait_until'] = args.wait_until
    if args.engine:
        engines = []
        for e in args.engine.split(','):
            e = e.strip()
            if e == 'all':
                engines = ['axe', 'alfa', 'ibm', 'htmlcs']
                break
            elif e in ('axe', 'alfa', 'ibm', 'htmlcs'):
                engines.append(e)
            else:
                parser.error("Unknown engine: {}. "
                    "Choose from: axe, alfa, ibm, htmlcs, all"
                    .format(e))
        config['engines'] = engines
    basename = args.output or 'a11y-catscan-{}'.format(datetime.now().strftime('%Y-%m-%d-%H%M%S'))
    output_dir = args.output_dir or config.get('output_dir', os.getcwd())
    os.makedirs(output_dir, exist_ok=True)

    # Load allowlist
    allowlist_path = args.allowlist or config.get('allowlist')
    allowlist = load_allowlist(allowlist_path) if allowlist_path else []
    if allowlist:
        print("Allowlist: {} entries from {}".format(len(allowlist), allowlist_path))

    # Load robots.txt unless told to ignore it.
    # By default we respect robots.txt — it's polite and often excludes
    # the same paths we'd want to skip anyway (admin, API, login, etc.).
    ignore_robots = args.ignore_robots or config.get('ignore_robots') in (
        True, 'true', 'yes', '1')
    robots_parser = None
    if not ignore_robots:
        robots_parser = load_robots_txt(url)
        if robots_parser and not args.quiet:
            print("Respecting robots.txt (use --ignore-robots to override)")

    html_path = os.path.join(output_dir, basename + '.html')
    json_path = os.path.join(output_dir, basename + '.json')

    # Load saved crawl state for --resume
    resume_state = None
    if args.resume:
        try:
            with open(args.resume) as f:
                resume_state = json.load(f)
            if not args.quiet:
                print("Resuming from: {}".format(args.resume))
        except Exception as e:
            print("ERROR: cannot load state file: {}".format(e),
                  file=sys.stderr)
            sys.exit(2)

    scanned, jsonl_path, wall_time, total_page_time = crawl_and_scan(
        url,
        max_pages=max_pages,
        tags=tags,
        rules=args.rule,
        level=args.level,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        exclude_regex=exclude_regex,
        verbose=args.verbose,
        quiet=args.quiet,
        config=config,
        json_path=json_path,
        html_path=html_path,
        save_every=save_every,
        level_label=level_label,
        allowlist=allowlist,
        seed_urls=seed_urls,
        robots_parser=robots_parser,
        resume_state=resume_state,
    )

    # Final reports already flushed by crawl_and_scan
    print("\nJSON report: {}".format(json_path))
    print("HTML report: {}".format(html_path))

    if args.llm and jsonl_path and os.path.exists(jsonl_path):
        llm_path = os.path.join(output_dir, basename + '.md')
        generate_llm_report(jsonl_path, llm_path, url,
                            level_label=level_label, allowlist=allowlist,
                            config=config)
        print("LLM report: {}".format(llm_path))

    # Summary (single pass through JSONL).
    # Separate WCAG failures from best-practice/ARIA-only failures.
    # The --level setting determines what counts as a compliance failure:
    #   wcag*  → only sc-* tagged failures count
    #   best   → sc-* AND bp-*/aria-* failures count
    scan_level_used = args.level or config.get('level', DEFAULT_LEVEL)
    include_bp_in_count = (scan_level_used == 'best')
    total_wcag_failed = 0
    total_aria_failed = 0
    total_bp_failed = 0
    total_incomplete = 0
    violation_rules = set()
    if jsonl_path and os.path.exists(jsonl_path):
        for page_url, data in _iter_deduped(jsonl_path):
            for v in data.get(EARL_FAILED, []):
                if allowlist and _matches_allowlist(
                        v.get('id', ''), page_url,
                        v.get('nodes', []), allowlist,
                        engines_dict=v.get('engines'),
                        outcome=EARL_FAILED):
                    continue
                tags = v.get('tags', [])
                has_wcag = any(t.startswith('sc-') for t in tags)
                has_aria = any(t.startswith('aria-') for t in tags)
                has_bp = any(t.startswith('bp-') for t in tags)
                nodes = _count_nodes([v])
                # Classification priority: if a finding has an
                # aria-* tag, it's an ARIA conformance issue even
                # if IBM also mapped it to a WCAG SC.  This prevents
                # ARIA landmark naming rules (incorrectly mapped to
                # SC 2.4.1 by IBM) from inflating the WCAG count.
                if has_aria:
                    total_aria_failed += nodes
                elif has_wcag:
                    total_wcag_failed += nodes
                    violation_rules.add(v.get('id', ''))
                elif has_bp:
                    total_bp_failed += nodes
                else:
                    total_bp_failed += nodes
            for v in data.get(EARL_CANTTELL, []):
                if allowlist and _matches_allowlist(
                        v.get('id', ''), page_url,
                        v.get('nodes', []), allowlist,
                        engines_dict=v.get('engines'),
                        outcome=EARL_CANTTELL):
                    continue
                total_incomplete += _count_nodes([v])

    # Compliance count: WCAG only, unless --level best
    compliance_failed = total_wcag_failed
    if include_bp_in_count:
        compliance_failed += total_bp_failed

    throughput = (wall_time / scanned) if scanned else 0
    print("\nScan complete: {} pages in {:.1f}s ({:.1f}s/page)".format(
        scanned, wall_time, throughput))
    print("  WCAG failed: {} node(s)".format(total_wcag_failed))
    if total_aria_failed:
        print("  ARIA: {} node(s)".format(total_aria_failed))
    if total_bp_failed:
        print("  Best practice: {} node(s)".format(total_bp_failed))
    print("  Can't tell: {} node(s) needing manual review".format(
        total_incomplete))

    if args.summary_json:
        summary = {
            'pages': scanned,
            EARL_FAILED: compliance_failed,
            'wcag_failed': total_wcag_failed,
            'aria_failed': total_aria_failed,
            'bp_failed': total_bp_failed,
            EARL_CANTTELL: total_incomplete,
            'rules': sorted(violation_rules),
            'clean': compliance_failed == 0,
        }
        print(json.dumps(summary))

    # Register scan in the named scan registry
    if basename and jsonl_path:
        from registry import register_scan
        report_paths = {
            'json': json_path,
            'jsonl': jsonl_path,
            'html': html_path,
        }
        register_scan(
            name=basename,
            report_paths=report_paths,
            url=url,
            engines=config.get('engines', ['axe']),
            summary={
                'pages': scanned,
                EARL_FAILED: compliance_failed,
                EARL_CANTTELL: total_incomplete,
                'clean': compliance_failed == 0,
            })

    # Diff against previous scan
    if args.diff and jsonl_path and os.path.exists(jsonl_path):
        if os.path.exists(args.diff):
            print("\nDiff vs {}:".format(args.diff))
            diff_scans(args.diff, jsonl_path, allowlist=allowlist)
        else:
            print("\nWARNING: diff file not found: {}".format(args.diff))

    # Group-by summary
    if args.group_by and jsonl_path and os.path.exists(jsonl_path):
        _group_results(jsonl_path, args.group_by, allowlist=allowlist)

    # Exit code: 0 = clean, 1 = violations found
    if compliance_failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    if '--mcp' in sys.argv:
        # Start as MCP server (Model Context Protocol) for Claude Code.
        # Exposes scan_page, scan_site, analyze_report, list_engines,
        # lookup_wcag as structured tools over stdio transport.
        from mcp_server import mcp as _mcp_server
        _mcp_server.run(transport='stdio')
    else:
        main()
