# Troubleshooting

This page lists the failure modes people usually hit first.

## axe-core not found

Symptom:

```text
ERROR: axe-core not found
Run: npm install
```

Fix:

```sh
npm install
```

The engine JavaScript files live under `node_modules/`. Python
dependencies alone are not enough.

## Browser launch fails

Install the Playwright browser:

```sh
python3.12 -m playwright install chromium
```

In containers or restricted sandboxes, Chromium may need additional OS
packages or permission to create namespaces, sockets, and temporary
profiles. Run the browser tests outside the sandbox when validating the
project:

```sh
.venv/bin/pytest -q
```

## A scan is clean but should not be

Check whether the page was skipped:

- HTTP status 4xx/5xx
- non-HTML content type
- tiny / empty response
- redirect off origin
- no engine results

Recent versions fail closed when every selected engine fails to start.
If only one engine is noisy, try a second engine to compare:

```sh
./a11y-catscan.py --page --engine axe,htmlcs https://example.com/page
```

## Crawling stops early

Common reasons:

- `max_pages` reached.
- robots.txt disallowed the path.
- include/exclude filters removed discovered links.
- all discovered links were off-origin.
- skipped extensions removed non-HTML resources.

Use verbose output:

```sh
./a11y-catscan.py -v --max-pages 50 https://example.com/
```

## Authenticated scan keeps logging out

Add obvious logout paths to the plugin:

```python
exclude_paths = ["/logout", "/users/logout"]
```

If recovery still triggers, inspect the saved `.state.json` file for
`logout_urls`. These are URLs the recovery cycle identified as session
breakers.

## Reports are too large

Use the streaming JSONL and LLM summary:

```sh
./a11y-catscan.py --llm --max-pages 500 https://example.com/
```

For analysis, prefer:

```sh
./a11y-catscan.py --group-by selector --name baseline
./a11y-catscan.py --search sc:1.4.3 --name baseline
```

## Too many color-contrast incompletes

Automated contrast engines struggle with gradients, images,
pseudo-elements, overlays, and clipped text. Options:

- Add explicit fallback `background-color` in CSS.
- Verify manually and allowlist known false positives.
- Group by reason to find repeated patterns:

```sh
./a11y-catscan.py --group-by reason --name baseline
```

## MCP rejects local URLs

By default, MCP `scan_page` rejects private and loopback targets to
avoid SSRF. For trusted local testing:

```sh
A11Y_CATSCAN_MCP_ALLOW_PRIVATE=1 python3.12 mcp_server.py
```

Keep that override out of shared MCP configurations.
