"""End-to-end test for crawl_and_scan's signal-handler flush paths.

crawl_and_scan installs three signal handlers when the scan starts:
  - SIGTERM/SIGINT → flush in-progress JSONL + save .state.json,
                     break out of the worker loop, exit gracefully
  - SIGUSR1       → snapshot state without stopping

This test runs the crawler in a subprocess, sends SIGUSR1 to trigger
a state snapshot, then SIGTERM to shut it down, and verifies the
state file was created. Subprocess isolation is necessary because the
handlers are installed via signal.signal(), which mutates main-thread
state pytest itself uses.

Marked browser because it spins up a real Chromium via crawl_and_scan.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITE = Path(__file__).resolve().parent / 'fixtures' / 'site'

_AXE_JS = PROJECT_ROOT / 'node_modules' / 'axe-core' / 'axe.min.js'

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        not _AXE_JS.is_file(),
        reason='node_modules not installed (run: npm install)'),
]


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass


@pytest.fixture
def fixture_site():
    handler = partial(_QuietHandler, directory=str(SITE))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        for _ in range(50):
            try:
                with socket.create_connection(
                        ('127.0.0.1', port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        yield 'http://127.0.0.1:{}'.format(port)
    finally:
        server.shutdown()
        server.server_close()


# Driver script the subprocess executes. It runs crawl_and_scan with
# a single worker against the URL passed on argv, writes results to
# the given json_path, and lets the scanner block long enough (via
# page_wait) for us to send signals from the parent.
_DRIVER = r"""
import os, sys
sys.path.insert(0, {project_root!r})
import importlib.util
spec = importlib.util.spec_from_file_location(
    'cli', os.path.join({project_root!r}, 'a11y-catscan.py'))
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

start_url = sys.argv[1]
json_path = sys.argv[2]
print('READY', flush=True)
cli.crawl_and_scan(
    start_url=start_url,
    max_pages=10,
    level='wcag21aa',
    quiet=False,
    config={{
        'engine': 'axe',
        'niceness': 0,
        'oom_score_adj': 0,
        'workers': 1,
        # Long page_wait keeps each page in flight for ~3 s, giving
        # the parent test a window to send signals.
        'wait_until': 'load',
        'page_wait': 3,
    }},
    json_path=json_path,
    save_every=0,
)
"""


class TestSignalHandlers:
    def _spawn(self, tmp_path, fixture_site):
        json_path = str(tmp_path / 'scan.json')
        driver = _DRIVER.format(project_root=str(PROJECT_ROOT))
        proc = subprocess.Popen(
            [sys.executable, '-c', driver,
             fixture_site + '/index.html', json_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        # Wait for the driver to print READY (means it's about to
        # call crawl_and_scan and has installed signal handlers).
        deadline = time.time() + 15
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    raise RuntimeError(
                        'driver exited before READY: {}'
                        .format(proc.returncode))
                continue
            if line.strip() == 'READY':
                break
        else:
            proc.kill()
            raise RuntimeError(
                'driver did not print READY within 15s')
        # Give crawl_and_scan a moment to register signal handlers
        # and start the first page load.
        time.sleep(2)
        return proc, json_path

    def test_sigusr1_writes_state_snapshot(
            self, tmp_path, fixture_site):
        # SIGUSR1 should write a .state.json snapshot without
        # stopping the scan.  _save_state always writes when
        # invoked (it doesn't bail on an empty queue), so a
        # single signal is enough — no retry loop needed.
        proc, json_path = self._spawn(tmp_path, fixture_site)
        state_path = json_path.replace('.json', '.state.json')
        try:
            os.kill(proc.pid, signal.SIGUSR1)
            deadline = time.time() + 10
            while time.time() < deadline:
                if os.path.isfile(state_path):
                    break
                time.sleep(0.1)
            assert os.path.isfile(state_path), (
                'SIGUSR1 should produce a state snapshot')
            # State file is valid JSON with the expected keys
            with open(state_path) as f:
                state = json.load(f)
            assert 'queue' in state
            assert 'visited' in state
            assert 'start_url' in state
        finally:
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def test_sigterm_flushes_and_exits_cleanly(
            self, tmp_path, fixture_site):
        # SIGTERM should flush any completed page results to JSONL
        # and let the process exit (the handler sets `interrupted`,
        # workers drain, the loop exits, _flush is called).
        proc, json_path = self._spawn(tmp_path, fixture_site)
        try:
            # Give it enough time to scan at least one page so the
            # JSONL has content to flush.
            time.sleep(7)
            os.kill(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail(
                    'SIGTERM did not stop the scanner within 30s')

            # JSONL should exist and contain at least one page
            jsonl = json_path + 'l'
            assert os.path.isfile(jsonl)
            with open(jsonl) as f:
                lines = [l for l in f if l.strip()]
            assert lines, (
                'Expected at least one flushed page in JSONL '
                'after SIGTERM')
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
