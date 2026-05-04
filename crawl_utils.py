"""URL/HTTP utilities used by the crawl loop.

This module is browser-free.  It owns:

  - SKIP_EXTENSIONS — file extensions that are never HTML pages.
  - normalize_url   — canonicalise URLs for the crawl frontier
                      (drop fragment, trailing slash, configured
                      query params).
  - is_same_origin / load_robots_txt / should_scan — filter helpers.
  - http_status     — cheap HEAD/GET probe before launching the
                      browser on a URL (4xx/5xx, non-HTML, …).
  - RateLimiter     — async-friendly rate limit between requests.
  - load_cookies    — read auth cookies from the configured file.

`normalize_url` reads two module-level configurations
(`_strip_params`, `_strip_path_rules_compiled`).  These are
populated by `configure_strip_rules()` from the CLI's main() so the
crawl loop and the URL filter share one canonicalisation policy.
"""

import atexit
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser


# ── Small numeric helper ─────────────────────────────────────

def safe_int(val, default=0):
    """Convert to int, returning default on failure."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# ── Browser process cleanup ──────────────────────────────────

# Track all browser processes we launch so we can kill them on
# exit.  This prevents orphaned chromium processes when the script
# crashes, is killed, or exits abnormally.
_browser_pids = set()


def register_browser_pid(pid):
    """Register a browser process PID for cleanup on exit."""
    _browser_pids.add(pid)


def cleanup_browsers():
    """Kill any browser processes we launched.  Runs via atexit."""
    for pid in list(_browser_pids):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    _browser_pids.clear()

    # Also kill any chromium processes that are children of this
    # process (catches anything missed by PID tracking).
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
                    if parent_pid == my_pid and 'chrome' in comm:
                        os.kill(child_pid, signal.SIGKILL)
                except (ValueError, ProcessLookupError,
                        PermissionError):
                    pass
    except Exception:
        pass


atexit.register(cleanup_browsers)


# File extensions that are never HTML pages.  Using a frozenset
# gives O(1) lookup instead of scanning a list on every URL.
SKIP_EXTENSIONS = frozenset((
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico',
    '.css', '.js', '.zip', '.tar', '.gz', '.mp4', '.mp3',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.xml', '.json', '.rss', '.atom', '.woff', '.woff2',
    '.ttf', '.eot', '.bmp', '.webp', '.csv',
))


# ── Rate limiting ─────────────────────────────────────────────

class RateLimiter:
    """Rate limiter that enforces a minimum delay between calls.

    Used to ensure that all async workers together don't exceed the
    robots.txt Crawl-delay.  Each worker calls wait_time() before
    making a request and sleeps (via asyncio.sleep) if needed to
    maintain the minimum interval.
    """

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._last_time = 0

    def wait_time(self):
        """Return seconds to sleep before issuing the next request."""
        if self.min_interval <= 0:
            return 0
        now = time.time()
        elapsed = now - self._last_time
        delay = max(0, self.min_interval - elapsed)
        self._last_time = now + delay
        return delay


# ── Cookies ──────────────────────────────────────────────────

def load_cookies(config):
    """Load cookies from the configured cookies file.

    Returns a list of cookie dicts, or an empty list if not
    configured or the file doesn't exist.
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


# ── URL normalization ────────────────────────────────────────

# Parameters to strip from URLs during normalization.  These are
# common pagination, sorting, and redirect params that produce
# the same page template with different data.  Stripping them
# deduplicates the crawl frontier so we don't scan
# /resources?sort=date AND /resources?sort=title.
_strip_params = set()         # global params to strip from all URLs
_strip_path_rules_compiled = []  # (regex, param_set) pairs


def configure_strip_rules(global_params, path_rules_compiled):
    """Set the URL-normalization strip rules.

    Called once by `main()` after parsing config / CLI flags.

    Args:
        global_params:        iterable of param names stripped from
                              every URL.
        path_rules_compiled:  iterable of (compiled-regex, param-set)
                              tuples for path-conditional stripping.
    """
    global _strip_params, _strip_path_rules_compiled
    _strip_params = set(global_params)
    _strip_path_rules_compiled = list(path_rules_compiled)


def normalize_url(url):
    """Normalize URL for deduplication.

    Strips fragment, trailing slash, and any query parameters
    listed in `_strip_params` (global) or matching a path-conditional
    rule from `_strip_path_rules_compiled`.  Configured via
    `configure_strip_rules` at startup.
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
            filtered = {k: v for k, v in params.items()
                        if k not in strip}
            query = urlencode(filtered, doseq=True) if filtered else ''

    return urlunparse((
        parsed.scheme, parsed.netloc, path, parsed.params, query, ''))


def is_same_origin(url, base_url):
    """Check whether two URLs share the same scheme+host+port."""
    return urlparse(url).netloc == urlparse(base_url).netloc


def load_robots_txt(base_url):
    """Fetch and parse the site's robots.txt.

    Returns a RobotFileParser that can check whether a URL is
    allowed.  Returns None if robots.txt can't be fetched (we'll
    allow everything).
    """
    parsed = urlparse(base_url)
    robots_url = '{}://{}/robots.txt'.format(
        parsed.scheme, parsed.netloc)
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
        return parser
    except Exception:
        return None


def should_scan(url, base_url, include_paths, exclude_paths,
                exclude_regex=None, robots_parser=None):
    """Decide whether a URL should be scanned based on filter rules.

    Checks (in order): same-origin, file extension, include/exclude
    paths, exclude regex, query string filters, and robots.txt.
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

    # Respect robots.txt if a parser was provided
    # (--ignore-robots disables this).  We check both the exact URL
    # and with a trailing slash, because our URL normalizer strips
    # trailing slashes but robots.txt Disallow patterns often
    # include them (e.g. "Disallow: /tools/" blocks /tools/ but
    # technically not /tools without the slash).
    if robots_parser is not None:
        if not robots_parser.can_fetch('a11y-catscan', url):
            return False
        url_with_slash = url.rstrip('/') + '/'
        if not robots_parser.can_fetch(
                'a11y-catscan', url_with_slash):
            return False

    return True


# ── HTTP probe ───────────────────────────────────────────────

# Cookie header sent with http_status() probes when auth is
# configured.  Set via `set_http_cookie_header()` from main().
_http_cookie_header = ''


def set_http_cookie_header(header):
    """Set the Cookie header used by `http_status` probes."""
    global _http_cookie_header
    _http_cookie_header = header or ''


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Handler that stops urllib from following redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def http_status(url, timeout=10):
    """Return (status_code, content_type) via a HEAD request.

    Falls back to GET if the server rejects HEAD (some do).
    Returns (0, '') on network error.  Does NOT follow redirects —
    redirects return the 3xx status so the browser can handle them
    with its own session cookies.

    Used as a pre-check before loading pages in Chromium: it's
    much cheaper than a full browser load and lets us identify
    error pages (4xx/5xx) and non-HTML responses (application/json)
    without wasting Chromium resources.

    If auth cookies are loaded (via `set_http_cookie_header`),
    they are sent with the request so authenticated pages return
    200 instead of 302→login.
    """

    def _ct(r):
        ct = r.headers.get('Content-Type', '')
        return ct.split(';')[0].strip().lower()

    headers = {'User-Agent': 'a11y-catscan/1.0'}
    if _http_cookie_header:
        headers['Cookie'] = _http_cookie_header

    try:
        req = urllib.request.Request(
            url, method='HEAD', headers=headers)
        with _no_redirect_opener.open(req, timeout=timeout) as r:
            return (r.status, _ct(r))
    except urllib.error.HTTPError as e:
        return (e.code, _ct(e))
    except Exception:
        # HEAD failed (connection error, or server rejects HEAD)
        # — try GET
        try:
            req = urllib.request.Request(
                url, method='GET', headers=headers)
            with _no_redirect_opener.open(req, timeout=timeout) as r:
                return (r.status, _ct(r))
        except urllib.error.HTTPError as e:
            return (e.code, _ct(e))
        except Exception:
            return (0, '')
