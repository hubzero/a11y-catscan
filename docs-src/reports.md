# Reports

a11y-catscan writes several report formats from the same scan. JSONL is
the source of truth; the other files are derived views.

## Output files

| File | Purpose |
|---|---|
| `.jsonl` | Streaming results, one JSON object per page |
| `.json` | Full combined JSON object for inspection or compatibility |
| `.html` | Human-readable report with summaries and per-page details |
| `.md` | Compact LLM-oriented summary, only with `--llm` |
| `.state.json` | Crawl state for `--resume` |

## EARL outcomes

Findings use W3C EARL outcome names:

| Outcome | Meaning |
|---|---|
| `failed` | Definite automated failure |
| `cantTell` | Needs manual review |
| `passed` | Rule passed |
| `inapplicable` | Rule does not apply |

The summary counts focus on `failed` and `cantTell`.

## Normalized tags

| Tag | Meaning |
|---|---|
| `sc-1.4.3` | WCAG Success Criterion |
| `aria-valid-attrs` | ARIA category |
| `bp-landmarks` | Best-practice category |

The `sc-` prefix means "Success Criterion." It is separate from native
engine tags like `wcag21aa`, which describe WCAG version and level.

## Deduplication

Raw JSONL preserves engine-native results. Report readers apply
cross-engine deduplication by:

```text
(selector, primary_tag, outcome)
```

Merged findings keep an `engines` dictionary. For example, one contrast
failure might show axe and IBM attribution if both tools confirmed it.

## HTML report

Use the HTML report for audit review:

- impact breakdown
- WCAG criteria table
- violation summary by rule
- incomplete summary
- per-page details
- selectors, snippets, and messages
- clean page list

All scanned page snippets are escaped before rendering.

## LLM report

Use `--llm` for a compact Markdown summary:

```sh
./a11y-catscan.py --llm https://example.com/
```

The Markdown groups repeated failures and includes representative HTML.
It is meant to help find the source template or component. It is not a
replacement for the full report.

## Allowlists

Use an allowlist for known-acceptable findings, especially
engine-specific `cantTell` items:

```yaml
- rule: color-contrast
  url: /homepage
  outcome: cantTell
  reason: chart library renders gradient background; manual contrast checked
```

Supported filters:

| Key | Meaning |
|---|---|
| `rule` | Normalized finding id, required |
| `url` | URL substring |
| `target` | selector/target substring |
| `engine` | only suppress this engine when it is the sole engine |
| `outcome` | `failed` or `cantTell` |
| `reason` | human explanation |

Engine-filtered allowlist entries do not suppress multi-engine
confirmed findings.

Run with:

```sh
./a11y-catscan.py --allowlist allowlist.yaml https://example.com/
```

## Registry and analysis

Completed scans are registered by name when a basename is available.
Useful analysis commands:

```sh
./a11y-catscan.py --list-scans
./a11y-catscan.py --name baseline --page-status https://example.com/page
./a11y-catscan.py --name baseline --search sc:1.4.3
./a11y-catscan.py --name baseline --search sel:*main-nav*
```
