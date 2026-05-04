# Configuration

Configuration lives in `a11y-catscan.yaml`. Copy the example and edit it
for the site being audited:

```sh
cp a11y-catscan.yaml.example a11y-catscan.yaml
```

The local config is gitignored. Command-line flags override YAML values
for the current run.

## Minimal config

```yaml
url: https://example.com/
level: wcag21aa
max_pages: 50
output_dir: ./reports

exclude_paths:
  - /login
  - /api
  - /administrator
```

Then run:

```sh
./a11y-catscan.py
```

The positional URL wins over `url` in the file:

```sh
./a11y-catscan.py https://staging.example.com/
```

## Engines

| Engine | CLI value | Notes |
|---|---|---|
| axe-core | `axe` | Default engine. Strong broad coverage, especially contrast, names, labels, ARIA, structure |
| Siteimprove Alfa | `alfa` | ACT-rule engine, run through a Node.js subprocess |
| IBM Equal Access | `ibm` | IBM Accessibility Checker rule set |
| HTML_CodeSniffer | `htmlcs` | Older but useful WCAG-oriented sniff rules |
| All engines | `all` | Expensive, but best for baseline audits |

Set one engine in YAML:

```yaml
engine: axe
```

Or select several on the command line:

```sh
./a11y-catscan.py --engine axe,alfa,ibm https://example.com/
```

## WCAG levels

Common values:

| Level | Meaning |
|---|---|
| `wcag2a` | WCAG 2.0 Level A |
| `wcag2aa` | WCAG 2.0 Level AA |
| `wcag21aa` | WCAG 2.1 Level AA, the default |
| `wcag22aa` | WCAG 2.2 Level AA |
| `best` | WCAG 2.1 AA plus best-practice rules |

Example:

```sh
./a11y-catscan.py --level wcag22aa https://example.com/
```

Best-practice rules are not counted as WCAG compliance failures unless
you explicitly use `--level best`.

## URL scope

Use include paths to restrict a crawl:

```yaml
include_paths:
  - /docs
  - /support
```

Use exclude paths for known non-content or destructive routes:

```yaml
exclude_paths:
  - /login
  - /logout
  - /api
  - /administrator
```

Use regex sparingly for families of internal pages:

```yaml
exclude_regex:
  - ^/internal/[^/]+/(admin|settings|logs)(/|$)
```

## Query normalization

`strip_query_params` reduces duplicate URLs that render the same page
template:

```yaml
strip_query_params:
  - sort
  - sortdir
  - limit
  - start
```

Path-conditional stripping is useful when a parameter is meaningful on
one route but just a filter on another:

```yaml
strip_query_params:
  - path: ^/(tags|resources)
    querystring: [parent, area]
```

## Output names and directories

```sh
./a11y-catscan.py --name baseline --output-dir reports https://example.com/
```

This writes:

```text
reports/baseline.json
reports/baseline.jsonl
reports/baseline.html
```

`--name` is a filename, not a path. Use `--output-dir` for directories.

## Performance settings

| Setting | Default | Purpose |
|---|---:|---|
| `workers` | `1` | Parallel page scans |
| `wait_until` | `networkidle` | Playwright navigation strategy |
| `page_wait` | `1` | Extra wait after load when not using `networkidle` |
| `save_every` | `25` | Flush derived reports every N pages |
| `restart_every` | `500` | Restart Chromium to control memory growth |
| `niceness` | `10` | Lower process CPU priority |
| `oom_score_adj` | `1000` | Prefer scanner for Linux OOM killing |

For large sites, raise workers slowly and watch memory:

```sh
./a11y-catscan.py --workers 4 --max-pages 1000 https://example.com/
```

Alfa scans serialize internally, so `--engine all` will not scale
linearly with worker count.
