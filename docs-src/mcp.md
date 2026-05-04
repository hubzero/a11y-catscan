# MCP tools

a11y-catscan includes an MCP stdio server for Claude Code and other MCP
clients. It is for quick one-page scans and report analysis, not
long-running crawls.

## Start the server

Either command starts the same tool server:

```sh
python3.12 mcp_server.py
./a11y-catscan.py --mcp
```

Example MCP configuration:

```json
{
  "mcpServers": {
    "wcag-audit": {
      "type": "stdio",
      "command": "python3.12",
      "args": ["/path/to/a11y-catscan/mcp_server.py"]
    }
  }
}
```

## Tools

| Tool | Purpose |
|---|---|
| `scan_page` | Scan one HTTP/HTTPS page and return flattened findings |
| `analyze_report` | Group findings from a report |
| `list_engines` | Show engine versions and install status |
| `lookup_wcag` | Look up a Success Criterion |
| `find_issues` | Search a report by SC, URL, selector, outcome, or engine |
| `check_page` | Check one URL inside a report |
| `compare_scans` | Structured diff between two reports |
| `manage_scans` | List, get, or delete registry entries |

## Safety boundaries

The MCP server may be driven by an LLM, so it validates inputs more
strictly than the CLI:

- `scan_page` accepts only `http` and `https`.
- Private, loopback, link-local, reserved, multicast, and unspecified
  scan targets are rejected by default.
- Report tools resolve report-like files (`.json`, `.jsonl`) or
  registered scan names, not arbitrary local files.

For trusted local testing only, you can allow private addresses:

```sh
A11Y_CATSCAN_MCP_ALLOW_PRIVATE=1 python3.12 mcp_server.py
```

Do not use that override for an MCP server exposed to untrusted prompts
or shared clients.

## When to use the CLI instead

Use the CLI for:

- full crawls
- authenticated scans
- resume state
- periodic flushing
- signal handling
- output file generation

MCP tools are best for quick answers while you are working in a codebase:
"does this page still fail?", "which reports mention SC 1.4.3?", or
"what does this criterion mean?"
