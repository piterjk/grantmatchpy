"""공고 첨부에서 글자 추출. 형식은 확장자가 아니라 파일 머리(서명)로 판별한다 — 확장자를 속인 파일을 걸러낸다.
  OLE(D0 CF 11 E0) → HWP · ZIP + mimetype application/hwp+zip → HWPX · ZIP + word/document.xml → DOCX · %PDF → PDF
  <?xml … <HWPML → HWPML 2.x(옛 한글 XML, 확장자 .hwp) · 그 밖의 ZIP(오피스 문서 제외) → 첨부 묶음, 안의 파일마다 추출(zip_bundle)

외부에서 받은 파일이므로 운영에서는 이 모듈을 직접 부르지 말고 sandbox.extract() 로 별도 프로세스에서 돌린다."""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any

from . import hwp, pdf_text, xml_text, zip_guard
from .errors import ExtractError

MAX_FILE = 30 * 1024 * 1024
_PHP_TRIM = " \t\n\r\0\x0b"
_PHP_TRIM_B = _PHP_TRIM.encode()
_HWPML_START = re.compile(rb"<HWPML[\s>]")
_HWPX_SECTION = re.compile(r"^Contents/section(\d+)\.xml$")
_MANIFEST_ENC = re.compile(rb'full-path="Contents/section\d+\.xml"[^>]*>\s*<[^>]*encryption-data')


def extract(path: str) -> dict[str, Any]:
    """{format, text, chars[, parts]}. parts: 형식이 zip(첨부 묶음)일 때만 — 안의 파일마다 형식·글자 수 또는 건너뛴 이유."""
    if not os.path.isfile(path):
        raise ExtractError("파일 없음")
    size = os.path.getsize(path)
    if size == 0:
        raise ExtractError("빈 파일")
    if size > MAX_FILE:
        raise ExtractError("파일이 너무 큼(30MB 초과)")

    fmt = detect(path)
    parts = None
    if fmt == "hwp":
        text = hwp.text(_read(path))
    elif fmt == "hwpx":
        text = _hwpx(path)
    elif fmt == "docx":
        text = _docx(path)
    elif fmt == "pdf":
        text = pdf_text.extract(path)
    elif fmt == "hwpml":
        text = "\n".join(xml_text.hwpml(_read(path)))
    else:  # zip
        from . import zip_bundle

        b = zip_bundle.extract(path)
        parts = b["parts"]
        text = b["text"]
    text = normalize(text)
    r: dict[str, Any] = {"format": fmt, "text": text, "chars": len(text)}
    if parts is not None:
        r["parts"] = parts
    return r


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def detect(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(8)
        if head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            return "hwp"
        if head.startswith(b"%PDF-"):
            return "pdf"
        h = head[3:] if head.startswith(b"\xef\xbb\xbf") else head
        if h.lstrip(_PHP_TRIM_B).startswith(b"<?xml"):
            f.seek(0)
            if _HWPML_START.search(f.read(4096)):
                return "hwpml"
    if head.startswith(b"PK\x03\x04"):
        z = zip_guard.open_zip(path, r"^mimetype$")  # 판별은 이름만 본다(묶음 ZIP 은 zip_bundle 이 전체를 검사)
        try:
            mt = zip_guard.read_name(z, "mimetype")
            if (mt or b"").strip(_PHP_TRIM_B) == b"application/hwp+zip":
                return "hwpx"
            if "word/document.xml" in z.NameToInfo:
                return "docx"
            # 엑셀·파워포인트 등 다른 오피스 문서는 읽지 않음, 그 밖의 ZIP 은 첨부 묶음
            if "[Content_Types].xml" in z.NameToInfo:
                raise ExtractError("지원하지 않는 형식(엑셀·파워포인트 등)")
            return "zip"
        finally:
            z.close()
    raise ExtractError("지원하지 않는 형식(HWP·HWPX·HWPML·DOCX·PDF·ZIP 묶음만 읽음)")


def _hwpx(path: str) -> str:
    z = zip_guard.open_zip(path, r"^(Contents/section\d+\.xml|META-INF/manifest\.xml)$")
    try:
        # 배포용 문서: 본문이 암호화돼 있다(manifest 의 encryption-data). 글자를 읽을 수 없으니 이유를 알려 거부
        manifest = zip_guard.read_name(z, "META-INF/manifest.xml") or b""
        if _MANIFEST_ENC.search(manifest):
            raise ExtractError("배포용(암호화) 한글 문서라 글자를 읽을 수 없음")
        sections: dict[int, str] = {}
        for info in z.infolist():
            m = _HWPX_SECTION.match(info.filename)
            if m:
                sections[int(m.group(1))] = info.filename
        if not sections:
            raise ExtractError("HWPX 본문 없음")
        paras: list[str] = []
        for _, name in sorted(sections.items()):
            paras.extend(xml_text.hwpx(zip_guard.read_name(z, name) or b""))
        return "\n".join(paras)
    finally:
        z.close()


def _docx(path: str) -> str:
    z = zip_guard.open_zip(path, r"^word/document\.xml$")
    try:
        return "\n".join(xml_text.docx(zip_guard.read_name(z, "word/document.xml") or b""))
    finally:
        z.close()


_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿­"))
_BLANKS = re.compile(r"[ \t 　]+")


def normalize(t: str) -> str:
    """문단 사이 한 줄, 문단 안 공백 정리, 보이지 않는 글자 제거 (PHP TextExtractor::normalize 와 같은 결과)."""
    t = t.translate(_ZERO_WIDTH)
    # 줄바꿈·탭 말고 제어 문자(유니코드 범주 C*: 제어·서식·대리·사용자 정의·미배정) 제거
    drop = {ord(ch): None for ch in set(t) if ch not in "\n\t" and unicodedata.category(ch)[0] == "C"}
    if drop:
        t = t.translate(drop)
    lines = (_BLANKS.sub(" ", ln).strip(_PHP_TRIM) for ln in t.split("\n"))
    return "\n".join(ln for ln in lines if ln != "")
