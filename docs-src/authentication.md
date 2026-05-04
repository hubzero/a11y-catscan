# Authentication

Authenticated scans use a site-specific login plugin. The plugin gets a
Playwright browser context and drives the login flow just as a user
would.

## Plugin contract

Required:

```python
async def login(context, config) -> bool:
    ...
```

Optional:

```python
async def is_logged_in(page) -> bool:
    ...

async def init_from_context(context) -> None:
    ...

exclude_paths = ["/logout"]
```

`login()` returns `True` only when the browser context is authenticated.
`is_logged_in()` runs after page loads and lets the crawl detect a
mid-scan logout.

## YAML configuration

```yaml
auth:
  login_script: login-hubzero.py
  credentials_file: ~/.a11y-catscan-creds
  login_url: /login
```

The sample `login-hubzero.py` reads credentials from a file and logs in
through the rendered form. Treat it as an example, not a universal
plugin.

## Saved session state

After a successful login, Playwright storage state is saved to:

```text
.auth-state.json
```

This file is gitignored. Subsequent scans try saved state before running
the login script. If the saved state no longer looks authenticated, the
scanner falls back to `login()`.

## Avoiding logout traps

A plugin can export:

```python
exclude_paths = ["/logout"]
```

These paths are merged into the crawl exclusion list after the scanner
starts. This prevents obvious logout links from entering the frontier.

The recovery cycle handles less obvious cases, such as links that look
safe but invalidate the session when visited.

## Session recovery

When `is_logged_in(page)` returns `False`:

1. The current URL is marked suspect.
2. In-flight workers drain.
3. The scanner re-runs login.
4. Suspect URLs are tested serially.
5. URLs that immediately break the session are banned.
6. Safe suspects are requeued for a normal scan.

If re-login fails, recovery is disabled for the rest of the run. That
keeps the crawl bounded when the auth plugin or credentials are broken.

## Cookie-based HTTP probes

The cheap `http_status()` probe can send cookies loaded from
`auth.cookies_file` when configured. This helps the pre-browser HEAD/GET
check see authenticated pages as HTML instead of redirects to login.

```yaml
auth:
  cookies_file: ~/.a11y-catscan-cookies.json
```

Cookie files and storage state should never be committed. Rotate any
sessions that were accidentally tracked or shared.
