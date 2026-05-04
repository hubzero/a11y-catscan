"""Test login plugin that simulates session expiry on specific URLs.

Used by the recovery-cycle tests in test_crawl_loop.py.  The
plugin's `is_logged_in` reads `page.url` and returns False
whenever the URL matches an env-configured "trap" substring,
modelling a logout-trap URL: visiting it kills the session, and
revisiting it after re-login kills it again.  A correctly wired
recovery loop should detect the trap, ban it via `_logout_urls`,
and continue scanning the rest of the site.

Env var:
  A11Y_TEST_LOGOUT_TRAPS  comma-separated URL substrings — every
                          page whose URL contains any of these is
                          treated as a logout trap (is_logged_in
                          returns False for it, always).
"""

import os

calls = {
    'login': 0,
    'is_logged_in': 0,
    'init_from_context': 0,
}
exclude_paths = []


def reset():
    calls['login'] = 0
    calls['is_logged_in'] = 0
    calls['init_from_context'] = 0


def _traps():
    raw = os.environ.get('A11Y_TEST_LOGOUT_TRAPS', '')
    return [s for s in (raw.split(',') if raw else []) if s]


async def login(context, config):
    calls['login'] += 1
    return True


async def is_logged_in(page):
    calls['is_logged_in'] += 1
    url = getattr(page, 'url', '') or ''
    for trap in _traps():
        if trap in url:
            return False
    return True


async def init_from_context(context):
    calls['init_from_context'] += 1
