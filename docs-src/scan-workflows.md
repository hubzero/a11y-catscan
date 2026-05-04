# Scan workflows

Most teams use a11y-catscan in a loop:

1. Establish a baseline.
2. Group recurring failures.
3. Fix a template, component, or CSS rule.
4. Verify one page.
5. Rescan only previously affected pages.
6. Diff against the baseline.

## Full baseline crawl

```sh
./a11y-catscan.py --engine all --max-pages 500 --llm https://example.com/
```

Use all engines for the baseline when time allows. It gives you better
coverage and useful cross-engine confirmation.

## Single-page verification

```sh
./a11y-catscan.py --page -q --summary-json https://example.com/fixed-page
```

This is the fast check after a fix. It does not follow links and it
prints a compact summary when `--summary-json` is set.

To narrow a verification to one axe rule:

```sh
./a11y-catscan.py --page --rule color-contrast https://example.com/fixed-page
```

## URL list scan

For a curated set of templates:

```text
https://example.com/
https://example.com/docs/intro
https://example.com/support/contact
```

Run:

```sh
./a11y-catscan.py --urls pages.txt --engine axe,alfa --llm
```

URL-list mode does not follow discovered links. It scans only the list.

## Rescan previous failures

If a baseline produced `baseline.jsonl`, rescan only pages with
violations or incompletes:

```sh
./a11y-catscan.py --rescan baseline.jsonl --diff baseline.jsonl --llm
```

You can also extract from a JSON or JSONL report:

```sh
./a11y-catscan.py --violations-from baseline.json --diff baseline.jsonl
./a11y-catscan.py --incompletes-from baseline.json --diff baseline.jsonl
```

## Group findings

Grouping helps identify one fix that removes many page-level findings:

```sh
./a11y-catscan.py --group-by wcag https://example.com/
./a11y-catscan.py --group-by selector https://example.com/
./a11y-catscan.py --group-by reason https://example.com/
./a11y-catscan.py --group-by engine https://example.com/
```

Useful groupings:

| Group | Use it for |
|---|---|
| `rule` | Which engine rule is most common |
| `selector` | Which component or template emits repeated failures |
| `reason` | Why incompletes happen, especially contrast uncertainty |
| `wcag` | Audit-level prioritization by Success Criterion |
| `engine` | Tool-specific vs cross-engine findings |
| `bp` | Best-practice / ARIA categories |

## Resume a crawl

Long scans write a `.state.json` file. Resume with:

```sh
./a11y-catscan.py --resume reports/baseline.state.json
```

The state file stores:

- queued URLs
- visited URLs
- start URL
- pages scanned
- URLs banned as logout traps

Resume is for crawls. URL-list and rescan modes intentionally do not
write resume state because their frontier is already explicit.

## Incremental reports

`save_every` controls how often derived JSON/HTML reports are rebuilt
from the JSONL stream:

```sh
./a11y-catscan.py --save-every 10 --max-pages 1000 https://example.com/
```

The JSONL stream is still flushed per page. `save_every` only affects
the convenience reports that are derived from it.
