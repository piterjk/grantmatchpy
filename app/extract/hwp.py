"""HWP 5.0 (구형 한글 바이너리) 본문 글자 추출. 근거: 한글과컴퓨터 공개 "한글 문서 파일 형식 5.0".

OLE 복합 문서(olefile) → FileHeader 로 압축·암호·배포용 확인 → BodyText/SectionN (압축이면 raw deflate) 레코드 나열.
레코드 머리 32비트: 태그(10) · 수준(10) · 크기(12, 4095 면 다음 4바이트가 크기).
HWPTAG_PARA_TEXT(67) 의 UTF-16LE 글자를 문단으로 모은다. 표 칸 글자도 같은 흐름에 문단 레코드로 들어 있다."""

from __future__ import annotations

import io
import re
import struct
import zlib

import olefile

from .errors import ExtractError

TAG_PARA_TEXT = 67
MAX_SECTION_BYTES = 64 * 1024 * 1024  # 압축 폭탄 방지: 구역 하나를 풀었을 때 상한

# 글자 1칸짜리 제어 문자 (나머지 0~31 은 8칸 = 제어 문자 + 정보 12바이트 + 같은 제어 문자)
_CHAR_CONTROLS = frozenset([0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31])
_SECTION_RX = re.compile(r"^BodyText/Section(\d+)$")


def _open(data: bytes) -> olefile.OleFileIO:
    try:
        return olefile.OleFileIO(io.BytesIO(data))
    except Exception:  # noqa: BLE001 — olefile 은 손상 파일에서 여러 예외를 낸다
        raise ExtractError("OLE 문서가 아님(또는 손상)") from None


def _stream(ole: olefile.OleFileIO, path: str) -> bytes:
    try:
        return ole.openstream(path).read()
    except Exception:  # noqa: BLE001
        raise ExtractError(f"스트림을 읽을 수 없음(손상): {path}") from None


def text(data: bytes) -> str:
    ole = _open(data)
    try:
        try:
            paths = ["/".join(p) for p in ole.listdir(streams=True, storages=False)]
        except Exception:  # noqa: BLE001
            raise ExtractError("OLE 디렉터리를 읽을 수 없음(손상)") from None
        if "FileHeader" not in paths:
            raise ExtractError("HWP 머리(FileHeader) 없음")
        head = _stream(ole, "FileHeader")
        if not head.startswith(b"HWP Document File"):
            raise ExtractError("HWP 문서가 아님")
        if len(head) < 40:
            raise ExtractError("HWP 머리(FileHeader)가 너무 짧음")
        props = struct.unpack_from("<I", head, 36)[0]
        if props & 0b10:
            raise ExtractError("암호가 걸린 HWP 문서")
        if props & 0b100:
            raise ExtractError("배포용 HWP 문서(본문 암호화) — 원문 HWPX·PDF 를 받아 주세요")
        compressed = bool(props & 0b1)

        sections: dict[int, str] = {}
        for p in paths:
            m = _SECTION_RX.match(p)
            if m:
                sections[int(m.group(1))] = p
        if not sections:
            raise ExtractError("HWP 본문(BodyText) 없음")

        paras: list[str] = []
        for _, path in sorted(sections.items()):
            raw = _stream(ole, path)
            if compressed:
                raw = _inflate(raw, path)
            paras.extend(_paragraphs(raw))
        return "\n".join(paras)
    finally:
        ole.close()


def _inflate(raw: bytes, path: str) -> bytes:
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(raw, MAX_SECTION_BYTES + 1)
    except zlib.error:
        raise ExtractError(f"HWP 구역 압축을 풀 수 없음(손상 또는 너무 큼): {path}") from None
    if len(out) > MAX_SECTION_BYTES or not d.eof:
        raise ExtractError(f"HWP 구역 압축을 풀 수 없음(손상 또는 너무 큼): {path}")
    return out


def _paragraphs(s: bytes) -> list[str]:
    out: list[str] = []
    n = len(s)
    o = 0
    while o + 4 <= n:
        h = struct.unpack_from("<I", s, o)[0]
        tag = h & 0x3FF
        size = (h >> 20) & 0xFFF
        o += 4
        if size == 0xFFF:
            if o + 4 > n:
                break
            size = struct.unpack_from("<I", s, o)[0]
            o += 4
        if size > n - o:
            raise ExtractError("HWP 레코드 크기가 구역 밖")
        if tag == TAG_PARA_TEXT:
            t = _para_text(s[o : o + size])
            if t != "":
                out.append(t)
        o += size
    return out


def _para_text(b: bytes) -> str:
    cnt = len(b) // 2
    units = struct.unpack_from(f"<{cnt}H", b, 0) if cnt else ()
    buf: list[int] = []
    i = 0
    while i < cnt:
        c = units[i]
        if c >= 32:
            buf.append(c)
            i += 1
            continue
        if c in _CHAR_CONTROLS:
            if c == 10:
                buf.append(10)  # 줄 바꿈
            elif c == 30 or c == 31:
                buf.append(32)  # 묶음 빈칸·고정폭 빈칸
            elif c == 24:
                buf.append(0x2D)  # 하이픈
            i += 1
            continue
        if c == 9:
            buf.append(9)  # 탭 (8칸 제어)
        i += 8  # 표·그림·필드 등 확장/인라인 제어 문자는 건너뛴다
    # UTF-16 대리쌍까지 맞게 바꾼다 (짝 없는 대리 글자는 PHP mbstring 처럼 '?')
    raw = struct.pack(f"<{len(buf)}H", *buf)
    s = raw.decode("utf-16-le", errors="replace").replace("\ufffd", "?")
    return s.strip(" \t\r\n")
