"""Crawl loop: walk a site, scan each page, stream results to disk.

`crawl_and_scan` is the engine behind --crawl, --page, --urls, and
--rescan.  It owns:

  - the Scanner lifecycle (start/restart/stop, browser tracking)
  - the worker sliding window (asyncio task pool, staggered starts)
  - the URL frontier (deque + visited set + logout bans)
  - per-page write to the JSONL stream
  - signal handlers (SIGTERM/SIGINT/SIGUSR1) for graceful exit and
    snapshot-on-demand
  - periodic browser restart (memory-leak mitigation)
  - --resume state save/load with atomic temp+verify+rename

The function returns a 5-tuple:
    (page_count, jsonl_path, wall_time, total_page_time, totals)

where `totals` is a dict accumulated during the crawl so main()
doesn't need a second pass over the JSONL for its summary.
"""

import asyncio
import json
import os
import signal
import sys
import time
from collections import deque
from pathlib import Path

from engine_mappings import (
    EARL_FAILED, EARL_CANTTELL, EARL_PASSED, EARL_INAPPLICABLE)
from scanner import Scanner, WCAG_LEVELS, DEFAULT_LEVEL
from results import RunningTotals, count_nodes, dedup_page
from engines.axe import get_axe_version
from allowlist import classify_page
from report_html import generate_html_report
from report_io import iter_jsonl
from crawl_utils import (
    RateLimiter, safe_int, load_cookies,
    normalize_url, is_same_origin, should_scan,
    set_http_cookie_header,
    register_browser_pid, cleanup_browsers,
)


def crawl_and_scan(
    start_url: str,
    max_pages: int = 50,
    tags: list[str] | None = None,
    rules: list[str] | None = None,
    level: str | None = None,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    exclude_regex: list | None = None,
    verbose: bool = False,
    quiet: bool = False,
    config: dict | None = None,
    json_path: str | None = None,
    html_path: str | None = None,
    save_every: int = 25,
    level_label: str | None = None,
    allowlist=None,
    seed_urls: list[str] | None = None,
    robots_parser=None,
    resume_state: dict | None = None,
) -> tuple:
    """Crawl the site starting from start_url and scan each page.

    If json_path is provided, results are flushed to disk every
    `save_every` pages and on SIGTERM/SIGINT so partial runs preserve
    progress.

    Returns:
        (page_count, jsonl_path, wall_time, total_page_time, totals)
        where `totals` is a dict with wcag/aria/bp/incomplete/rules
        accumulated during the crawl.
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
    # Chromium is CPU- and memory-hungry; on a shared web server we'd
    # rather the scan be slow than cause Apache/MySQL to be
    # unresponsive.
    niceness = safe_int(config.get('niceness', 10), 10)
    oom_score = safe_int(config.get('oom_score_adj', 1000), 1000)
    if niceness:
        try:
            os.nice(niceness)  # higher = lower CPU priority (0-19)
        except (OSError, PermissionError):
            pass  # not fatal — just means we run at normal priority
    if oom_score:
        try:
            # Tell the Linux OOM killer to sacrifice this process
            # first.  1000 = highest possible score = killed before
            # anything else.
            with open('/proc/self/oom_score_adj', 'w') as f:
                f.write(str(oom_score))
        except (OSError, PermissionError):
            pass  # not on Linux or no permission — harmless

    page_wait = safe_int(config.get('page_wait', 1), 1)
    wait_strategy = config.get('wait_until', 'networkidle')
    if wait_strategy not in (
            'networkidle', 'load', 'domcontentloaded', 'commit'):
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
            engine_names = ([e] if e in ('axe', 'alfa', 'ibm', 'htmlcs')
                            else ['axe'])

    # Scanner handles browser + engine lifecycle.
    # Constructed here, started inside _pw_sliding_window().
    scanner = Scanner(
        engines=engine_names,
        level=scan_level,
        tags=tags, rules=rules,
        chromium_path=config.get('chromium_path'),
        ignore_certificate_errors=config.get(
            'ignore_certificate_errors')
        in (True, 'true', 'yes', '1'),
        wait_until=wait_strategy,
        page_wait=page_wait,
        auth=config.get('auth', {}),
        config=config,
        verbose=verbose, quiet=quiet,
    )
    num_workers = safe_int(config.get('workers', 1), 1)
    # Set up auth cookie header for any http_status() utility calls.
    _auth_cookies = load_cookies(config)
    if _auth_cookies:
        set_http_cookie_header('; '.join(
            '{}={}'.format(c['name'], c['value'])
            for c in _auth_cookies))

    base_url = start_url

    # Initialize crawl state — either from a saved state file
    # (--resume) or from scratch.  URLs that cause session logout
    # are discovered during recovery mode and persisted in state
    # files.
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
                print("  {} banned logout URLs".format(
                    len(_logout_urls)))
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

    # Running counters populated by _write_page so main() doesn't
    # have to re-iterate the JSONL just to compute the summary.
    running_totals = RunningTotals()

    # MEMORY STRATEGY: Stream results to a JSONL file (one JSON
    # object per line) instead of accumulating everything in a
    # Python dict.  Without this, a 5000-page scan would hold
    # ~500MB+ of results in memory.  By writing each page's results
    # to disk immediately, memory usage stays constant regardless
    # of scan size.  The JSONL is later converted to the final
    # JSON/HTML reports by streaming through the file line-by-line.
    jsonl_path = (json_path + 'l') if json_path else None
    # Open the JSONL once and hold the handle for the lifetime of
    # the scan.  _write_page appends + flushes per page; the finally
    # block at the end of crawl_and_scan closes it.  This avoids
    # ~3 syscalls per page (open/fstat/close) on what could be a
    # 5000-page crawl.
    jsonl_file = open(jsonl_path, 'w') if jsonl_path else None

    if not quiet:
        print("Starting axe-core {} accessibility scan...".format(
            get_axe_version()))
        print(f"  Start URL: {start_url}")
        print("  Level: {} ({})".format(level_label, ', '.join(tags)))
        print(f"  Max pages: {max_pages}")
        if page_wait > 1:
            print(f"  Page wait: {page_wait}s")
        if json_path and save_every:
            print("  Incremental save every {} pages".format(
                save_every))
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
        if not jsonl_file:
            return
        try:
            jsonl_file.write(
                json.dumps({url: page_data}, default=str) + '\n')
            # flush per page so SIGUSR1 snapshots and concurrent
            # readers see fully-written lines.
            jsonl_file.flush()
        except OSError as e:
            print("  WARNING: failed to write results for {}: {}".format(
                url, e), file=sys.stderr)

        # Update running summary counters from the deduped form.
        # main() reads these instead of re-iterating the JSONL.
        try:
            classify_page(
                dedup_page(page_data), url, allowlist, running_totals)
        except Exception as e:
            if verbose:
                print("  WARNING: classify failed for {}: {}".format(
                    url, e), file=sys.stderr)

    def _flush(reason=''):
        """Build final JSON + HTML from the JSONL stream on disk."""
        if not json_path or not jsonl_path:
            return
        try:
            # Convert JSONL → final JSON by reading each line and
            # writing it into a single JSON object.  We stream
            # line-by-line so memory stays constant regardless of
            # scan size.
            tmp = json_path + '.tmp'
            with open(tmp, 'w') as out:
                out.write('{\n')
                first_entry = True
                for page_url, page_data in iter_jsonl(jsonl_path):
                    if not first_entry:
                        out.write(',\n')
                    json_key = json.dumps(page_url)
                    json_val = json.dumps(page_data, default=str)
                    out.write(f'  {json_key}: {json_val}')
                    first_entry = False
                out.write('\n}\n')
            os.replace(tmp, json_path)
            if html_path:
                try:
                    generate_html_report(
                        jsonl_path, html_path, start_url,
                        level_label or 'WCAG', allowlist=allowlist)
                except Exception as e:
                    print('  (html flush failed: {})'.format(
                        str(e)[:80]))
            if reason:
                print('  [flushed {} pages ({})]'.format(
                    page_count, reason))
        except Exception as e:
            print(f'  (flush failed: {e})')

    # Rate limiter shared across all workers to enforce robots.txt
    # crawl delay.  This is separate from page_wait (which is
    # per-worker JS settle time).  Only the robots.txt crawl_delay
    # is a cross-worker rate limit — page_wait is applied per-worker
    # after each page load to let JavaScript settle.
    crawl_delay = 0
    if robots_parser is not None:
        delay = robots_parser.crawl_delay('a11y-catscan')
        if delay is not None:
            crawl_delay = int(delay)
    rate_limiter = RateLimiter(crawl_delay)

    def _vskip(url, reason):
        """Print a skip notice in verbose mode."""
        if verbose and not quiet:
            print(f"  skip: {url} — {reason}")

    # SIGTERM/SIGINT handler: flush partial results and save state.
    interrupted = False

    def _save_state(reason=''):
        """Save crawl state (queue + visited) for --resume.

        Uses write-to-temp + verify + atomic rename to avoid
        corrupting the state file on crash or disk-full.
        """
        if not json_path or no_crawl or not queue:
            return
        # Path.with_suffix is correct here because it only swaps the
        # final extension — `json_path.replace('.json', ...)` would
        # corrupt paths whose directory components contain '.json'.
        state_path = str(Path(json_path).with_suffix('.state.json'))
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
                    or len(check.get('visited', []))
                    != len(state['visited'])):
                raise ValueError(
                    'verification failed: queue {}/{}, '
                    'visited {}/{}'.format(
                        len(check.get('queue', [])),
                        len(state['queue']),
                        len(check.get('visited', [])),
                        len(state['visited'])))

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
                print(
                    "  Crawl state saved: {} ({} queued, "
                    "{} visited)".format(
                        state_path, len(queue), len(visited)))
        except Exception as e:
            print(f"  (state save failed: {e})")
            # Clean up temp on failure, leave current intact
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Signal handler sets the interrupted flag.  Each scan mode
    # checks this flag and breaks out of its loop.  We don't call
    # sys.exit() here because asyncio.run() swallows SystemExit and
    # leaves browsers orphaned.
    def _on_signal(signum, frame):
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        print('\n!! Signal {} — flushing {} pages...'.format(
            signum, page_count))
        _flush(reason=f'signal {signum}')
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
    # Chromium accumulates garbage (DOM nodes, JS heaps, image
    # caches) over hundreds of pages, causing slowdowns and
    # occasional 60s+ hangs.
    restart_every = safe_int(config.get('restart_every', 500), 500)

    if not quiet and num_workers > 1:
        print(f"  Workers: {num_workers} (parallel)")

    try:
        # --- Playwright async sliding window ---
        # We maintain a sliding window of N concurrent async tasks.
        # As each task finishes, we print its result immediately and
        # start the next.

        async def _pw_sliding_window():
            nonlocal page_count, total_page_time

            # Suppress Playwright's 'TargetClosedError' Future
            # warnings at shutdown.  These are harmless — they fire
            # when the browser connection drops during cleanup.
            loop = asyncio.get_event_loop()
            _orig_handler = loop.get_exception_handler()

            def _suppress_target_closed(loop, context):
                msg = str(context.get('exception', ''))
                if ('TargetClosedError' in msg
                        or 'Browser closed' in msg):
                    return  # suppress
                if _orig_handler:
                    _orig_handler(loop, context)
                else:
                    loop.default_exception_handler(context)
            loop.set_exception_handler(_suppress_target_closed)

            # Scanner manages browser + engines + auth.
            await scanner.start()
            try:
                register_browser_pid(scanner.browser.process.pid)
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

                # Scanner handles: navigate, validate, run
                # engines, resolve elements, close page.
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
                        _vskip(
                            url,
                            "redirect → {} (already visited)"
                            .format(actual))
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
                # Build page_data in the format _write_page
                # expects
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

            # Merge login plugin exclude_paths into the scan
            # filter
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

            async def _drain_pending():
                """Wait for all in-flight tasks to finish.

                Used by the recovery and browser-restart paths
                to bring the worker pool to a quiescent state.
                For each completed task: write its page,
                accumulate timing, enqueue any newly discovered
                links.

                Worker-task exceptions are swallowed here
                because each `_scan` already has its own
                per-page error handling (skip_result on
                navigation failure, etc.) and a hard exception
                from a worker task means the page is
                unrecoverable for this scan.  Surface the
                cause in verbose mode so it's visible during
                debugging.
                """
                nonlocal page_count, total_page_time
                while pending:
                    d, _ = await asyncio.wait(
                        pending.keys(),
                        return_when=asyncio.FIRST_COMPLETED)
                    for t in d:
                        url_for_task = pending.pop(t, None)
                        task_workers.pop(t, None)
                        try:
                            r = t.result()
                        except Exception as e:
                            if verbose:
                                print(
                                    "  WARNING: drain task for "
                                    "{} raised: {}".format(
                                        url_for_task, e),
                                    file=sys.stderr)
                            r = None
                        if r is not None:
                            page_count += 1
                            u, pd, nl, _w, el = r
                            total_page_time += el
                            # Single-writer: still on main loop;
                            # see _write_page docstring.
                            _write_page(u, pd)
                            for lnk in nl:
                                if (lnk not in visited
                                        and lnk not in queue):
                                    queue.append(lnk)

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
                    except Exception as e:
                        # Worker-task exception → page
                        # unrecoverable for this scan.  Already
                        # handled per-page via skip_result;
                        # surface here for verbose debugging
                        # only.
                        if verbose:
                            print(
                                "  WARNING: scan task raised: "
                                "{}".format(e), file=sys.stderr)

                    if result is not None:
                        url, page_data, new_links, _, elapsed = (
                            result)
                        total_page_time += elapsed
                        v_count = count_nodes(
                            page_data.get(EARL_FAILED, []))
                        i_count = count_nodes(
                            page_data.get(EARL_CANTTELL, []))
                        if not quiet:
                            pw_w = len(str(max_pages))
                            parts = []
                            if v_count:
                                parts.append(
                                    f'{v_count} failed')
                            if i_count:
                                parts.append(
                                    '{} cantTell'.format(
                                        i_count))
                            ss = (', '.join(parts)
                                  if parts else 'clean')
                            print(
                                "[{}/{}] W{} {} — {} "
                                "({:.1f}s)".format(
                                    str(page_count).rjust(pw_w),
                                    max_pages, wid, url, ss,
                                    elapsed))
                            if verbose:
                                print(
                                    "  V: {} ({} nodes), I: "
                                    "{} ({} nodes), Queue: {}"
                                    .format(
                                        len(page_data.get(
                                            EARL_FAILED, [])),
                                        v_count,
                                        len(page_data.get(
                                            EARL_CANTTELL, [])),
                                        i_count, len(queue)))
                        # Single-writer: called from main loop
                        # only, never from worker tasks.
                        # See _write_page.
                        _write_page(url, page_data)

                        for link in new_links:
                            if (link not in visited
                                    and link not in queue):
                                queue.append(link)
                    else:
                        page_count -= 1

                # Recovery mode: drain all workers, re-login,
                # test suspect URLs serially, then resume.
                if (_recovery_mode.is_set()
                        and scanner.context):
                    await _drain_pending()
                    active_wids.clear()

                    if not quiet:
                        print(
                            "  [recovery: {} suspect URLs, "
                            "re-logging in]".format(
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
                        print(
                            "  [recovery done: {} banned, "
                            "{} requeued]".format(
                                len(_logout_urls),
                                len(safe_urls)))

                # Fill empty slots with freed worker IDs first,
                # then allocate new ones if needed.
                while (len(pending) < num_workers
                       and (page_count + len(pending)
                            < max_pages)):
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

                # Restart browser periodically to prevent
                # memory leaks.  Wait for all in-flight pages
                # to finish first.
                if (restart_every and page_count > 0
                        and page_count % restart_every == 0
                        and page_count < max_pages):
                    await _drain_pending()
                    # Restart browser + engines to prevent
                    # Chromium memory leaks.
                    if not quiet:
                        print(
                            "  [restarting browser after "
                            "{} pages]".format(page_count))
                    await scanner.restart_browser()
                    try:
                        register_browser_pid(
                            scanner.browser.process.pid)
                    except Exception:
                        pass
                    active_wids.clear()

                    # Refill the sliding window after restart
                    for i in range(num_workers):
                        if (page_count + len(pending)
                                >= max_pages):
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
            cleanup_browsers()
        except Exception as e:
            print(f"  Playwright error: {e}",
                  file=sys.stderr)
            cleanup_browsers()

    finally:
        # Close the JSONL writer before _flush reads the file.
        if jsonl_file is not None:
            try:
                jsonl_file.close()
            except OSError:
                pass
        _flush(reason='final')
        _save_state()

    wall_time = time.time() - scan_start_time
    return (page_count, jsonl_path, wall_time, total_page_time,
            running_totals)
