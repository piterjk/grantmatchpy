"""시험 공통: 임시 저장소, 로컬 임시 HTTP 서버(외부 사이트에는 요청하지 않는다), 서비스 클라이언트."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.helpers import fxb

TOKEN = "test-token-not-secret"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 조용히
        pass

    def _send(self, status=200, body=b"", ctype="application/octet-stream", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        p = self.path
        if p == "/ok.hwp":
            self._send(body=fxb("form.hwp"))
        elif p == "/ok2.hwp":
            self._send(body=fxb("form.hwp"))  # 같은 내용, 다른 주소
        elif p == "/empty":
            self._send(body=b"")
        elif p == "/html":
            self._send(body=b"<html>login</html>", ctype="text/html; charset=utf-8")
        elif p == "/404":
            self._send(404, b"nope")
        elif p == "/big":
            self._send(body=b"x" * 2_000_000)
        elif p == "/chunked-big":  # Content-Length 없이 계속 보내기(스트리밍 중단 확인)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"y" * 100_000)
            except OSError:
                pass
        elif p.startswith("/r/"):  # /r/N → N 번 옮긴 뒤 /ok.hwp
            n = int(p.split("/")[2])
            self._send(302, b"", headers={"Location": "/ok.hwp" if n <= 0 else f"/r/{n - 1}"})
        elif p == "/to-other-host":
            port = self.server.server_address[1]
            self._send(302, b"", headers={"Location": f"http://127.0.0.1:{port}/ok.hwp"})
        elif p == "/to-evil":
            self._send(302, b"", headers={"Location": "http://evil.example.com/a.hwp"})
        else:
            self._send(404, b"?")


@pytest.fixture(scope="session")
def http_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.fixture()
def storage(tmp_path):
    d = tmp_path / "attachments"
    d.mkdir()
    return d


@pytest.fixture()
def settings(storage):
    return Settings(token=TOKEN, storage=str(storage))


@pytest.fixture()
def local_settings(storage, http_server):
    """로컬 시험 서버(127.0.0.1:포트)로 내려받기를 시험하는 설정 — 운영에서는 켜지 않을 완화 설정."""
    port = http_server.server_address[1]
    return Settings(
        token=TOKEN, storage=str(storage), allow_http=True, allow_private_ip=True, allowed_hosts=["localhost"],
        allowed_ports=[port], fetch_max_bytes=1_000_000,
    )


@pytest.fixture()
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture()
def local_client(local_settings):
    return TestClient(create_app(local_settings))


AUTH = {"X-Internal-Token": TOKEN}
