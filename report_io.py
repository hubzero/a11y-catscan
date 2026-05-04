"""Streaming readers for JSON / JSONL scan reports.

Each scan writes one JSON object per line to a `.jsonl` file (one
page per line) and a final aggregated `.json` file.  Consumers
(diff, search, page-status, HTML/LLM report generation) iterate
these files line-by-line so memory stays constant for arbitrarily
large scans.

This module also exposes the deduped variant: `iter_deduped` runs
each page through `scanner.dedup_page` so consumers see one finding
per (selector, primary_tag, outcome) with multi-engine attribution.

Used by:
    a11y-catscan.py — HTML report, LLM report, group-by, diff
    registry.py     — search_findings, page_status

Imports `dedup_page` from `results.py`, which is browser-agnostic.
Importing report_io does not pull in Playwright.
"""

import json
import sys

from engine_mappings import EARL_FAILED
from results import dedup_page


def iter_jsonl(jsonl_path):
    """Iterate (url, data) pairs from a JSONL results file.

    Skips blank or corrupt lines (e.g. from a partial write after a
    crash).  Corrupt lines are reported on stderr but do not raise.
    """
    with open(jsonl_path, 'r') as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                print("  WARNING: corrupt JSONL line {} in {}, "
                      "skipping".format(lineno, jsonl_path),
                      file=sys.stderr)
                continue
            for url, data in obj.items():
                yield url, data


def iter_report(path):
    """Iterate (url, data) pairs from a JSON or JSONL report file.

    Auto-detects format: if the file parses as a JSON object, iterates
    its key-value pairs.  Otherwise falls back to JSONL line-by-line.
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
    yield from iter_jsonl(path)


def iter_deduped(jsonl_path):
    """Iterate (url, deduped_data) from a JSONL results file.

    Same interface as `iter_jsonl` but with cross-engine deduplication
    applied to each page.  Findings that share the same
    (selector, primary_tag, outcome) merge into one entry with
    multi-engine attribution (`engines: {axe: ..., ibm: ...}`).
    """
    for url, page_data in iter_jsonl(jsonl_path):
        yield url, dedup_page(page_data)


def extract_urls_from_report(path, which=EARL_FAILED):
    """Return URLs whose page has at least one finding in `which`.

    `which` is an EARL outcome key — typically EARL_FAILED for
    --rescan or EARL_CANTTELL for --incompletes-from.
    """
    urls = []
    for url, data in iter_report(path):
        if data.get(which):
            urls.append(url)
    return urls
