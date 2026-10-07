"""공고 첨부 내려받기 (PHP collect/Downloader + CurlClient 이식).
정해진 공공기관 호스트만, https 우선(http 는 설정으로), 리다이렉트는 3번까지 — 매번 허용 목록·사설/내부 IP 를 다시 검사,
30MB 상한(스트리밍으로 받다가 넘으면 중단). 저장소 안에 sha256 이름(`앞2글자/해시`)으로 저장하고 같은 파일은 한 번만 둔다.
형식 판별·글자 추출은 extract 가 파일 머리로 한다(여기서는 확장자를 믿지 않는다)."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from ..settings import Settings


class FetchError(Exception):
    """내려받기 실패. 메시지는 한국어 사유(PHP CollectException 과 같은 문구)."""


@dataclass
class Fetched:
    name: str
    url: str
    sha256: str
    size: int
    stored_path: str  # 저장소 기준 상대 경로 "ab/abcd…"
    content_type: str


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """사설·내부·loopback·link-local·예약·멀티캐스트 등 공인 인터넷이 아닌 주소."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


class Downloader:
    def __init__(self, settings: Settings, resolver=None):
        self.s = settings
        self.dir = os.path.abspath(settings.storage)
        self._resolver = resolver or socket.getaddrinfo  # 시험에서 바꿔 끼운다

    # ── 공개 ──────────────────────────────────────────

    def fetch(self, url: str, name: str) -> Fetched:
        os.makedirs(self.dir, mode=0o770, exist_ok=True)
        tmp = os.path.join(self.dir, ".part-" + secrets.token_hex(6))
        current = url
        try:
            hop = 0
            while True:
                host, port, ips = self._assert_allowed(current)
                status, location, ctype, size = self._get(current, host, port, ips, tmp)
                if 300 <= status < 400 and location:
                    if hop >= 3:
                        raise FetchError("첨부 주소가 너무 여러 번 옮겨졌습니다.")
                    hop += 1
                    current = urljoin(current, location)
                    continue
                if status != 200:
                    raise FetchError(f"첨부를 받지 못했습니다(HTTP {status}): {name}")
                break
            if size == 0:
                raise FetchError(f"첨부가 비어 있습니다: {name}")
            if "text/html" in ctype.lower():
                # 로그인 화면·오류 화면이 파일 대신 온 경우
                raise FetchError(f"첨부 대신 웹 화면이 왔습니다: {name}")
            sha = _sha256_file(tmp)
            rel = f"{sha[:2]}/{sha}"
            final = os.path.join(self.dir, sha[:2], sha)
            if not os.path.isfile(final):
                os.makedirs(os.path.dirname(final), mode=0o770, exist_ok=True)
                os.replace(tmp, final)
            return Fetched(name=name, url=url, sha256=sha, size=size, stored_path=rel, content_type=ctype)
        finally:
            if os.path.isfile(tmp):
                os.unlink(tmp)

    def _verify(self):
        return ssl.create_default_context(cafile=self.s.ca_bundle) if self.s.ca_bundle else True

    # ── 검사 ──────────────────────────────────────────

    def _assert_allowed(self, url: str) -> tuple[str, int, list[str]]:
        """주소 모양·허용 목록·DNS 해석 결과(사설 IP)를 검사하고 (호스트, 포트, 접속할 IP 목록)을 돌려준다."""
        bad = FetchError("허용하지 않는 첨부 주소입니다.")
        try:
            p = urlsplit(url.strip())
            port = p.port
        except ValueError:
            raise bad from None
        host = (p.hostname or "").lower().rstrip(".")
        scheme = p.scheme.lower()
        allowed_schemes = ("https", "http") if self.s.allow_http else ("https",)
        if scheme not in allowed_schemes or host == "" or "@" in p.netloc or p.username is not None or p.password is not None:
            raise bad
        if any(ord(c) < 32 or ord(c) == 127 for c in url.strip()):
            raise bad
        eff_port = port if port is not None else (443 if scheme == "https" else 80)
        if port is not None and port not in self.s.allowed_ports:
            raise bad
        if not any(host == h or host.endswith("." + h) for h in self.s.allowed_hosts):
            raise FetchError(f"허용 목록에 없는 곳의 첨부입니다: {host}")
        return host, eff_port, self._resolve(host, eff_port)

    def _resolve(self, host: str, port: int) -> list[str]:
        try:
            infos = self._resolver(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError):
            raise FetchError(f"연결 실패: 주소를 찾을 수 없습니다({host})") from None
        ips: list[str] = []
        for info in infos:
            addr = info[4][0].split("%")[0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                raise FetchError("허용하지 않는 첨부 주소입니다.") from None
            # 해석된 주소 가운데 하나라도 내부망이면 거부(DNS 로 내부 주소를 가리키는 속임수 방지)
            if not self.s.allow_private_ip and _is_blocked_ip(ip):
                raise FetchError("허용하지 않는 첨부 주소입니다.")
            if addr not in ips:
                ips.append(addr)
        if not ips:
            raise FetchError(f"연결 실패: 주소를 찾을 수 없습니다({host})")
        return ips

    # ── 받기 ──────────────────────────────────────────

    def _get(self, url: str, host: str, port: int, ips: list[str], sink: str) -> tuple[int, str | None, str, int]:
        """(상태, Location, Content-Type, 받은 크기). 검사한 IP 로 직접 접속해 DNS 가 중간에 바뀌어도(rebinding) 안전하다."""
        p = urlsplit(url)
        max_bytes = self.s.fetch_max_bytes
        deadline = time.monotonic() + self.s.fetch_timeout
        last_err: Exception | None = None
        for ip in ips:
            ip_host = f"[{ip}]" if ":" in ip else ip
            target = f"{p.scheme}://{ip_host}:{port}{p.path or '/'}" + (f"?{p.query}" if p.query else "")
            headers = {"Host": host if port in (80, 443) else f"{host}:{port}", "User-Agent": self.s.user_agent,
                       "Accept-Encoding": "gzip, deflate"}
            ext = {"sni_hostname": host} if p.scheme == "https" else {}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("연결 실패: 시간 초과")
            timeout = httpx.Timeout(connect=min(10.0, remaining), read=min(30.0, remaining), write=10.0, pool=5.0)
            size = 0
            try:
                with httpx.Client(follow_redirects=False, timeout=timeout, trust_env=False, verify=self._verify()) as client:
                    with client.stream("GET", target, headers=headers, extensions=ext) as r:
                        status = r.status_code
                        ctype = r.headers.get("content-type", "")
                        location = r.headers.get("location")
                        if status != 200:
                            return status, location, ctype, 0
                        clen = r.headers.get("content-length")
                        if clen and clen.isdigit() and int(clen) > max_bytes and not r.headers.get("content-encoding"):
                            raise FetchError(f"응답이 너무 큽니다({max_bytes // 1_000_000} MB 초과).")
                        with open(sink, "wb") as f:
                            for chunk in r.iter_bytes(65536):  # 압축은 풀린 크기로 센다
                                size += len(chunk)
                                if size > max_bytes:
                                    raise FetchError(f"응답이 너무 큽니다({max_bytes // 1_000_000} MB 초과).")
                                f.write(chunk)
                                if time.monotonic() > deadline:
                                    raise FetchError("연결 실패: 시간 초과(120초)" if self.s.fetch_timeout == 120
                                                     else f"연결 실패: 시간 초과({self.s.fetch_timeout}초)")
                        return status, location, ctype, size
            except FetchError:
                if os.path.isfile(sink):
                    os.unlink(sink)
                raise
            except httpx.TimeoutException:
                last_err = FetchError("연결 실패: 시간 초과")
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_err = FetchError("연결 실패: " + _brief(e))  # 다음 IP 로
            except httpx.HTTPError as e:
                last_err = FetchError("연결 실패: " + _brief(e))
                break
            except OSError as e:
                last_err = FetchError("연결 실패: " + _brief(e))
                break
        if os.path.isfile(sink):
            os.unlink(sink)
        raise last_err or FetchError("연결 실패")


def _brief(e: Exception) -> str:
    s = str(e) or type(e).__name__
    return s[:120]


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
