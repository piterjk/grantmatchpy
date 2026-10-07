"""서비스 시험: 토큰·경로 검사·내려받기 거부 규칙·리다이렉트 재검사·시간 초과."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.extract import sandbox
from app.fetch import Downloader, FetchError
from app.main import create_app
from app.settings import Settings
from tests.conftest import AUTH, TOKEN
from tests.helpers import fxb

# ── 토큰 ──


def test_health_without_token(client):
    r = client.get("/internal/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/internal/attachments/fetch", "/internal/attachments/extract"])
@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}, {"X-Internal-Token": ""}, {"X-Internal-Token": "한글".encode()}])
def test_token_required(client, path, headers):
    r = client.post(path, json={"url": "https://bizinfo.go.kr/a", "name": "n", "storedPath": "ab/cd"}, headers=headers)
    assert r.status_code == 401


def test_start_requires_token_env():
    with pytest.raises(RuntimeError, match="GRANTMATCHPY_TOKEN"):
        create_app(Settings(token=""))
    env = {k: v for k, v in os.environ.items() if k != "GRANTMATCHPY_TOKEN"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, "-m", "app"], cwd=root, env=env, capture_output=True, timeout=60)
    assert p.returncode != 0 and "GRANTMATCHPY_TOKEN".encode() in p.stderr


# ── 추출 ──


def _store(storage, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    d = storage / sha[:2]
    d.mkdir(exist_ok=True)
    (d / sha).write_bytes(data)
    return f"{sha[:2]}/{sha}"


def test_extract_ok(client, storage):
    sp = _store(storage, fxb("form.hwp"))
    r = client.post("/internal/attachments/extract", json={"storedPath": sp}, headers=AUTH).json()
    assert r["ok"] is True and r["format"] == "hwp" and r["chars"] == 4219 and "parts" not in r
    assert "예비창업패키지 예비창업자 사업계획서" in r["text"]


def test_extract_zip_has_parts(client, storage):
    from tests.helpers import zip_bytes

    sp = _store(storage, zip_bytes({"a.hwpx": fxb("form.hwpx")}))
    r = client.post("/internal/attachments/extract", json={"storedPath": sp}, headers=AUTH).json()
    assert r["ok"] and r["format"] == "zip" and r["parts"][0]["name"] == "a.hwpx"


@pytest.mark.parametrize("bad", ["../x", "ab/../../x", "/etc/passwd", "C:/Windows/win.ini", "ab\\cd", "", "ab//cd", "./ab", "ab/\0x", ".."])
def test_extract_rejects_paths_outside(client, bad):
    r = client.post("/internal/attachments/extract", json={"storedPath": bad}, headers=AUTH)
    assert r.status_code == 400 and r.json()["ok"] is False and r.json()["permanent"] is True


def test_extract_rejects_symlink_escape(client, storage, tmp_path):
    outside = tmp_path / "outside.hwp"
    outside.write_bytes(fxb("form.hwp"))
    try:
        os.symlink(outside, storage / "link")
    except (OSError, NotImplementedError):
        pytest.skip("심볼릭 링크를 만들 수 없는 환경")
    r = client.post("/internal/attachments/extract", json={"storedPath": "link"}, headers=AUTH)
    assert r.status_code == 400


@pytest.mark.parametrize(
    "data,msg,permanent",
    [
        (b"MZ\x90\x00" + b"\0" * 200, "지원하지 않는 형식", True),
        (b"", "빈 파일", True),
    ],
)
def test_extract_errors_permanent(client, storage, data, msg, permanent):
    sp = _store(storage, data) if data else "00/" + "0" * 64
    if not data:
        (storage / "00").mkdir(exist_ok=True)
        (storage / sp).write_bytes(b"")
    r = client.post("/internal/attachments/extract", json={"storedPath": sp}, headers=AUTH).json()
    assert r["ok"] is False and msg in r["error"] and r["permanent"] is permanent


def test_extract_missing_file_not_permanent(client):
    r = client.post("/internal/attachments/extract", json={"storedPath": "ab/" + "a" * 64}, headers=AUTH).json()
    assert r == {"ok": False, "error": "파일 없음", "permanent": False}


def test_extract_timeout(client, storage, monkeypatch):
    sp = _store(storage, fxb("form.hwp"))
    client.app.state.settings.extract_timeout = 1
    monkeypatch.setattr(sandbox, "worker_command", lambda p: [sys.executable, "-c", "import time; time.sleep(60)"])
    t = time.monotonic()
    r = client.post("/internal/attachments/extract", json={"storedPath": sp}, headers=AUTH).json()
    assert time.monotonic() - t < 10
    assert r == {"ok": False, "error": "글자 추출 시간 초과(1초)", "permanent": False}


def test_extract_worker_crash_is_error_not_hang(client, storage, monkeypatch):
    sp = _store(storage, fxb("form.hwp"))
    monkeypatch.setattr(sandbox, "worker_command", lambda p: [sys.executable, "-c", "import os; os._exit(3)"])
    r = client.post("/internal/attachments/extract", json={"storedPath": sp}, headers=AUTH).json()
    assert r["ok"] is False and r["error"].startswith("글자 추출 실패") and r["permanent"] is False


def test_permanent_rules_match_php():
    assert all(main_mod.extract_permanent(m) for m in ["지원하지 않는 형식(x)", "암호가 걸린 HWP 문서", "배포용 HWP", "빈 파일", "파일이 너무 큼(30MB 초과)", "압축 안 파일이 너무 많음"])
    assert not main_mod.extract_permanent("글자 추출 시간 초과(120초)")
    assert not main_mod.extract_permanent("파일 없음")
    assert all(main_mod.fetch_permanent(m) for m in ["응답이 너무 큽니다(30 MB 초과).", "허용 목록에 없는 곳의 첨부입니다: x", "허용하지 않는 첨부 주소입니다."])
    assert not main_mod.fetch_permanent("첨부를 받지 못했습니다(HTTP 500): a")


# ── 내려받기: 거부 규칙 ──


def _fetch(c, url, name="첨부.hwp"):
    return c.post("/internal/attachments/fetch", json={"url": url, "name": name}, headers=AUTH).json()


@pytest.mark.parametrize(
    "url,msg",
    [
        ("http://bizinfo.go.kr/a.hwp", "허용하지 않는"),  # http 기본 금지
        ("ftp://bizinfo.go.kr/a.hwp", "허용하지 않는"),
        ("https://user:pw@bizinfo.go.kr/a.hwp", "허용하지 않는"),
        ("https://user@bizinfo.go.kr/a.hwp", "허용하지 않는"),
        ("https://bizinfo.go.kr:8080/a.hwp", "허용하지 않는"),
        ("https://bizinfo.go.kr:22/a.hwp", "허용하지 않는"),
        ("https:///a.hwp", "허용하지 않는"),
        ("https://evil.com/a.hwp", "허용 목록"),
        ("https://bizinfo.go.kr.evil.com/a.hwp", "허용 목록"),
        ("https://evilbizinfo.go.kr/a.hwp", "허용 목록"),
        ("https://127.0.0.1/a.hwp", "허용 목록"),
        ("https://169.254.169.254/latest/meta-data", "허용 목록"),
        ("not a url", "허용하지 않는"),
        ("", "허용하지 않는"),
        ("https://bizinfo.go.kr/a\r\nHost: evil", "허용하지 않는"),
    ],
)
def test_fetch_rejects(client, url, msg):
    r = _fetch(client, url)
    assert r["ok"] is False and msg in r["error"] and r["permanent"] is True


def _fake_resolver(mapping):
    import socket

    def res(host, port, type=0, **kw):
        ips = mapping[host]
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return res


@pytest.mark.parametrize(
    "ips",
    [["10.0.0.5"], ["192.168.1.1"], ["127.0.0.1"], ["169.254.169.254"], ["172.16.0.9"], ["0.0.0.0"], ["::1"], ["fd00::1"], ["::ffff:10.0.0.1"],
     ["93.184.216.34", "10.0.0.1"], ["100.64.0.1"]],
)
def test_dns_to_private_ip_rejected(settings, ips):
    d = Downloader(settings, resolver=_fake_resolver({"www.bizinfo.go.kr": ips}))
    with pytest.raises(FetchError, match="허용하지 않는"):
        d._assert_allowed("https://www.bizinfo.go.kr/a")


def test_dns_public_ok_and_suffix_hosts(settings):
    d = Downloader(settings, resolver=_fake_resolver({"www.bizinfo.go.kr": ["93.184.216.34"], "bizinfo.go.kr": ["93.184.216.34"]}))
    assert d._assert_allowed("https://www.bizinfo.go.kr/a")[2] == ["93.184.216.34"]
    assert d._assert_allowed("https://BIZINFO.go.kr/a")[0] == "bizinfo.go.kr"
    assert d._assert_allowed("https://bizinfo.go.kr:443/a")[1] == 443


def test_redirect_rechecked_for_allow_list_and_private_ip(settings, monkeypatch):
    """리다이렉트 때마다 허용 목록·사설 IP 를 다시 검사한다 (실제 접속은 가짜로)."""
    d = Downloader(settings, resolver=_fake_resolver({"www.bizinfo.go.kr": ["93.184.216.34"], "inner.bizinfo.go.kr": ["10.1.2.3"]}))
    seen = []

    def fake_get(url, host, port, ips, sink):
        seen.append(url)
        if url.endswith("/start"):
            return 302, "https://inner.bizinfo.go.kr/secret", "", 0  # 허용 목록 안이지만 내부 IP
        raise AssertionError("두 번째 접속까지 가면 안 된다")

    monkeypatch.setattr(d, "_get", fake_get)
    with pytest.raises(FetchError, match="허용하지 않는"):
        d.fetch("https://www.bizinfo.go.kr/start", "n")
    assert len(seen) == 1

    def fake_get2(url, host, port, ips, sink):
        return 302, "https://evil.example.com/x", "", 0

    monkeypatch.setattr(d, "_get", fake_get2)
    with pytest.raises(FetchError, match="허용 목록"):
        d.fetch("https://www.bizinfo.go.kr/start", "n")

    def fake_get3(url, host, port, ips, sink):
        return 302, "http://www.bizinfo.go.kr/x", "", 0  # https → http 로 내려가는 것도 거부

    monkeypatch.setattr(d, "_get", fake_get3)
    with pytest.raises(FetchError, match="허용하지 않는"):
        d.fetch("https://www.bizinfo.go.kr/start", "n")


def test_connects_to_validated_ip_only(settings, monkeypatch):
    """검사한 IP 로 직접 접속한다(검사 뒤 DNS 가 바뀌어도 안전)."""
    d = Downloader(settings, resolver=_fake_resolver({"www.bizinfo.go.kr": ["93.184.216.34"]}))
    got = {}

    def fake_get(url, host, port, ips, sink):
        got.update(host=host, port=port, ips=ips)
        open(sink, "wb").write(b"abc")
        return 200, None, "application/pdf", 3

    monkeypatch.setattr(d, "_get", fake_get)
    f = d.fetch("https://www.bizinfo.go.kr/a.pdf", "n")
    assert got == {"host": "www.bizinfo.go.kr", "port": 443, "ips": ["93.184.216.34"]}
    assert f.stored_path == f"{f.sha256[:2]}/{f.sha256}"


# ── 내려받기: 로컬 임시 서버 ──


def _url(srv, path):
    return f"http://localhost:{srv.server_address[1]}{path}"


def test_fetch_ok_and_dedup(local_client, http_server, storage):
    r = _fetch(local_client, _url(http_server, "/ok.hwp"))
    data = fxb("form.hwp")
    sha = hashlib.sha256(data).hexdigest()
    assert r == {"ok": True, "sha256": sha, "size": len(data), "contentType": "application/octet-stream", "storedPath": f"{sha[:2]}/{sha}"}
    assert (storage / sha[:2] / sha).read_bytes() == data
    r2 = _fetch(local_client, _url(http_server, "/ok2.hwp"))  # 같은 내용 → 한 번만 둔다
    assert r2["sha256"] == sha
    files = [p for p in storage.rglob("*") if p.is_file()]
    assert len(files) == 1  # .part 임시 파일도 남지 않음
    # 이어서 글자 추출
    e = local_client.post("/internal/attachments/extract", json={"storedPath": r["storedPath"]}, headers=AUTH).json()
    assert e["ok"] and e["chars"] == 4219


def test_fetch_private_ip_blocked_by_default(storage, http_server):
    s = Settings(token=TOKEN, storage=str(storage), allow_http=True, allowed_hosts=["localhost"], allowed_ports=[http_server.server_address[1]])
    r = _fetch(TestClient(create_app(s)), _url(http_server, "/ok.hwp"))
    assert r["ok"] is False and "허용하지 않는" in r["error"] and r["permanent"] is True


def test_fetch_empty_html_404(local_client, http_server):
    r = _fetch(local_client, _url(http_server, "/empty"), "빈.hwp")
    assert r == {"ok": False, "error": "첨부가 비어 있습니다: 빈.hwp", "permanent": False}
    r = _fetch(local_client, _url(http_server, "/html"), "로그인")
    assert r["error"] == "첨부 대신 웹 화면이 왔습니다: 로그인" and r["permanent"] is False
    r = _fetch(local_client, _url(http_server, "/404"), "없음")
    assert r["error"] == "첨부를 받지 못했습니다(HTTP 404): 없음" and r["permanent"] is False


def test_fetch_too_big_aborts(local_client, http_server, storage):
    for path in ("/big", "/chunked-big"):
        r = _fetch(local_client, _url(http_server, path))
        assert r["ok"] is False and "너무 큽니다" in r["error"] and r["permanent"] is True
    assert [p for p in storage.rglob("*") if p.is_file()] == []  # 받다 만 파일을 남기지 않는다


def test_fetch_redirects_up_to_three(local_client, http_server):
    assert _fetch(local_client, _url(http_server, "/r/2"))["ok"] is True  # 3번 옮김(/r/2→/r/1→/r/0→/ok)
    r = _fetch(local_client, _url(http_server, "/r/3"))  # 4번 옮김
    assert r == {"ok": False, "error": "첨부 주소가 너무 여러 번 옮겨졌습니다.", "permanent": False}


def test_fetch_redirect_to_other_host_rejected(local_client, http_server):
    r = _fetch(local_client, _url(http_server, "/to-other-host"))  # 127.0.0.1 은 허용 목록(localhost)에 없다
    assert r["ok"] is False and "허용 목록" in r["error"] and r["permanent"] is True
    r = _fetch(local_client, _url(http_server, "/to-evil"))
    assert "허용 목록" in r["error"]


def test_fetch_connection_failure(local_settings):
    local_settings.allowed_ports = [1]  # 아무도 듣지 않는 포트
    c = TestClient(create_app(local_settings))
    r = _fetch(c, "http://localhost:1/x")
    assert r["ok"] is False and r["error"].startswith("연결 실패") and r["permanent"] is False


# ── https: 검사한 IP 로 접속하되 인증서는 원래 호스트 이름으로 검증한다 ──


def test_https_pinned_ip_verifies_hostname(tmp_path, storage, http_server):
    import shutil
    import ssl
    import threading
    from http.server import ThreadingHTTPServer

    from tests.conftest import _Handler

    openssl = shutil.which("openssl") or r"C:\Program Files\Git\usr\bin\openssl.exe"
    if not os.path.isfile(shutil.which("openssl") or openssl):
        pytest.skip("openssl 없음")
    key, crt = str(tmp_path / "k.pem"), str(tmp_path / "c.pem")
    cfg = tmp_path / "o.cnf"
    cfg.write_text("[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=www.bizinfo.go.kr\n[v3]\nsubjectAltName=DNS:www.bizinfo.go.kr\n")
    p = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt, "-days", "2", "-config", str(cfg)],
                       capture_output=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    if p.returncode != 0:
        pytest.skip("시험용 인증서를 만들 수 없음")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        s = Settings(token=TOKEN, storage=str(storage), allow_private_ip=True, allowed_ports=[port], ca_bundle=crt)
        d = Downloader(s, resolver=_fake_resolver({"www.bizinfo.go.kr": ["127.0.0.1"]}))
        f = d.fetch(f"https://www.bizinfo.go.kr:{port}/ok.hwp", "n")
        assert f.size == len(fxb("form.hwp"))
        # 인증서에 없는 호스트 이름이면 연결을 거부한다
        s2 = Settings(token=TOKEN, storage=str(storage), allow_private_ip=True, allowed_ports=[port], ca_bundle=crt, allowed_hosts=["k-startup.go.kr"])
        d2 = Downloader(s2, resolver=_fake_resolver({"www.k-startup.go.kr": ["127.0.0.1"]}))
        with pytest.raises(FetchError, match="연결 실패"):
            d2.fetch(f"https://www.k-startup.go.kr:{port}/ok.hwp", "n")
    finally:
        srv.shutdown()
