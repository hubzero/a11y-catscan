"""Tier 2: CLI's http_status() pre-flight HEAD/GET probe.

Drives a small ThreadingHTTPServer that returns canned status codes
and content types so we can exercise the HEAD path, the GET fallback,
and the network-error path without leaving the test process.
"""

import socket
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import pytest


# ── Fixture HTTP server ─────────────────────────────────────────

class _CannedHandler(BaseHTTPRequestHandler):
    """Routes by URL path:

      /ok            HEAD/GET → 200 text/html
      /json          HEAD/GET → 200 application/json
      /redirect      HEAD/GET → 302 with Location header (no body)
      /not-found     HEAD/GET → 404 text/html
      /no-head       HEAD → 405 method not allowed; GET → 200 text/html
      /500           HEAD/GET → 500 text/html
    """

    def log_message(self, *args, **kwargs):
        pass

    def _route(self, method):
        path = self.path
        if path == '/ok':
            self.send_response(200)
            self.send_header('Content-Type',
                              'text/html; charset=utf-8')
            self.end_headers()
            if method == 'GET':
                self.wfile.write(b'<html><body>OK</body></html>')
        elif path == '/json':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            if method == 'GET':
                self.wfile.write(b'{"ok": true}')
        elif path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/ok')
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
        elif path == '/not-found':
            self.send_response(404)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            if method == 'GET':
                self.wfile.write(b'<html><body>404</body></html>')
        elif path == '/no-head':
            if method == 'HEAD':
                self.send_response(405)
                self.send_header('Allow', 'GET')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html><body>via GET</body></html>')
        elif path == '/500':
            self.send_response(500)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        self._route('HEAD')

    def do_GET(self):
        self._route('GET')


@pytest.fixture
def canned_server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _CannedHandler)
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


# ── http_status tests ──────────────────────────────────────────

class TestHttpStatus:
    def test_200_html(self, cli, canned_server):
        status, ct = cli.http_status(canned_server + '/ok')
        assert status == 200
        assert ct == 'text/html'

    def test_200_json(self, cli, canned_server):
        status, ct = cli.http_status(canned_server + '/json')
        assert status == 200
        assert ct == 'application/json'

    def test_redirect_returns_3xx_without_following(
            self, cli, canned_server):
        # The probe must NOT follow redirects — the browser will,
        # carrying its session cookies. Returning 302 here is correct.
        status, ct = cli.http_status(canned_server + '/redirect')
        assert status == 302

    def test_not_found_returns_404(self, cli, canned_server):
        status, _ = cli.http_status(canned_server + '/not-found')
        assert status == 404

    def test_server_error_returns_500(self, cli, canned_server):
        status, _ = cli.http_status(canned_server + '/500')
        assert status == 500

    def test_get_fallback_when_head_405(self, cli, canned_server):
        # Server rejects HEAD with 405 — but http_status uses
        # urllib.error.HTTPError to surface the status, so 405 is
        # what the caller sees. The GET fallback only fires on
        # connection-level errors (where HEAD raises a non-HTTPError).
        status, _ = cli.http_status(canned_server + '/no-head')
        assert status == 405

    def test_unreachable_server_returns_zero(self, cli):
        # No listener on this port → connection refused → (0, '')
        status, ct = cli.http_status(
            'http://127.0.0.1:1/never', timeout=1)
        assert status == 0
        assert ct == ''

    def test_get_fallback_when_head_raises_non_http_error(
            self, cli, monkeypatch):
        # The GET fallback only fires when HEAD raises a non-
        # HTTPError exception (e.g. a connection reset or socket
        # error mid-request).  Mock the opener so HEAD raises and
        # GET returns a fake response — the contract is that we
        # see GET's status, not (0, '').
        import urllib.request
        import crawl_utils

        class _FakeResp:
            status = 200
            headers = {'Content-Type': 'text/html; charset=utf-8'}

            def __enter__(self): return self
            def __exit__(self, *a): pass

        calls = {'count': 0}

        def _open(req, timeout=10):
            calls['count'] += 1
            if req.get_method() == 'HEAD':
                raise OSError('simulated connection reset')
            return _FakeResp()

        monkeypatch.setattr(
            crawl_utils._no_redirect_opener, 'open', _open)
        status, ct = cli.http_status('http://example.test/x')
        assert status == 200
        assert ct == 'text/html'
        # HEAD attempt + GET fallback = 2 calls
        assert calls['count'] == 2

    def test_get_fallback_returns_zero_when_get_also_fails(
            self, cli, monkeypatch):
        # Both HEAD and GET raise non-HTTPError → (0, '').
        import crawl_utils

        def _open(req, timeout=10):
            raise OSError('network unreachable')

        monkeypatch.setattr(
            crawl_utils._no_redirect_opener, 'open', _open)
        status, ct = cli.http_status('http://example.test/x')
        assert status == 0
        assert ct == ''

    def test_get_fallback_surfaces_http_error_status(
            self, cli, monkeypatch):
        # GET fallback path returning an HTTPError should surface
        # the error code rather than (0, '').
        import urllib.error
        import crawl_utils

        class _FakeHeaders:
            def get(self, key, default=''):
                return ('text/html'
                        if key.lower() == 'content-type'
                        else default)

        def _open(req, timeout=10):
            if req.get_method() == 'HEAD':
                raise OSError('simulated connection reset')
            err = urllib.error.HTTPError(
                req.full_url, 500, 'fail', _FakeHeaders(), None)
            raise err

        monkeypatch.setattr(
            crawl_utils._no_redirect_opener, 'open', _open)
        status, ct = cli.http_status('http://example.test/x')
        assert status == 500
        assert ct == 'text/html'
