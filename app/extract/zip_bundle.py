"""공고 첨부 묶음(일반 ZIP) 안의 HWP·HWPX·HWPML·DOCX·PDF·안내 메모(.txt) 글자를 파일마다 뽑아 이어 붙인다.
그림·디자인 파일(png·jpg·ai 등)은 열지 않고 건너뛴다.
- zip_guard 상한(항목 수·풀린 크기·압축률)을 그대로 쓰고, 읽는 파일은 MAX_PARTS 개까지
- 안의 형식도 파일 머리로 판별. ZIP 안의 ZIP 은 한 단계만 더 연다(그 아래는 건너뜀)
- 같은 이름의 HWP(또는 HWPX·DOCX)가 있으면 PDF 는 사본으로 보고 건너뜀
- 파일 이름: UTF-8 이 아니면 CP949(옛 압축 프로그램)로 읽는다
한 파일이 깨져도 묶음 전체를 실패시키지 않고 그 파일만 parts 에 이유를 남긴다."""

from __future__ import annotations

import os
import posixpath
import re
import tempfile
from typing import Any

from . import text_extractor, zip_guard
from .errors import ExtractError
from .natsort import natkey

MAX_PARTS = 40
MAX_DEPTH = 1
MAX_NOTE = 1024 * 1024  # 안내 메모(.txt) 상한
PICTURES = frozenset(["png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "svg", "ai", "psd", "eps", "heic"])


def extract(path: str, depth: int = 0) -> dict[str, Any]:
    z = zip_guard.open_zip(path)
    try:
        entries: list[dict[str, Any]] = []
        for i, info in enumerate(z.infolist()):
            raw = _raw_name(info)
            name = _decode_name(raw)
            base = posixpath.basename(name.replace("\\", "/"))
            if (
                raw.endswith(b"/")
                or base == ""
                or name.startswith("__MACOSX/")
                or base.startswith("._")
                or base.lower() in ("thumbs.db", ".ds_store", "desktop.ini")
            ):
                continue
            filename, ext = _split_ext(base)
            entries.append({"index": i, "name": name, "stem": _stem(filename), "ext": ext.lower()})
        entries.sort(key=lambda e: natkey(e["name"]))

        editable = {e["stem"] for e in entries if e["ext"] in ("hwp", "hwpx", "docx")}

        texts: list[str] = []
        parts: list[dict[str, Any]] = []
        read = 0
        for e in entries:
            if e["ext"] == "pdf" and e["stem"] in editable:
                parts.append({"name": e["name"], "skipped": "같은 이름의 한글·워드 파일이 있어 PDF 사본은 건너뜀"})
                continue
            if e["ext"] in PICTURES:
                # .ai 는 PDF 호환이라 PDF 로 읽히지만 로고·시안이라 글자가 없다 — 열지 않는다
                parts.append({"name": e["name"], "skipped": "그림·디자인 파일(글자 없음)"})
                continue
            if read >= MAX_PARTS:
                parts.append({"name": e["name"], "skipped": "묶음 안 파일이 너무 많아 건너뜀"})
                continue
            read += 1
            tmp = None
            try:
                data = zip_guard.read_index(z, e["index"])
                if e["ext"] == "txt":
                    r = _note(data)
                else:
                    fd, tmp = tempfile.mkstemp(prefix="jzb")
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    del data
                    r = _one(tmp, depth)
                parts.append({"name": e["name"], "format": r["format"], "chars": r["chars"]})
                if r["text"] != "":
                    texts.append(f"[파일: {e['name']}]\n{r['text']}")
            except ExtractError as ex:
                parts.append({"name": e["name"], "skipped": str(ex)})
            except Exception:  # noqa: BLE001 — 예상 못 한 오류도 묶음 전체를 멈추지 않는다
                parts.append({"name": e["name"], "skipped": "파일을 읽는 중 오류(손상)"})
            finally:
                if tmp and os.path.isfile(tmp):
                    os.unlink(tmp)
        return {"text": "\n".join(texts), "parts": parts}
    finally:
        z.close()


def _one(tmp: str, depth: int) -> dict[str, Any]:
    if os.path.getsize(tmp) == 0:
        raise ExtractError("빈 파일")
    fmt = text_extractor.detect(tmp)
    if fmt == "zip":
        if depth >= MAX_DEPTH:
            raise ExtractError("압축 안의 압축은 한 단계까지만 엶")
        inner = extract(tmp, depth + 1)
        text = text_extractor.normalize(inner["text"])
        return {"format": "zip", "text": text, "chars": len(text)}
    return text_extractor.extract(tmp)


def _note(data: bytes) -> dict[str, Any]:
    """묶음 안 .txt — 대개 "사업자등록증 사본(스캔하여 pdf로 제출 요망)" 같은 제출 서류 안내 메모."""
    if data == b"":
        raise ExtractError("빈 파일")
    if len(data) > MAX_NOTE or b"\0" in data:
        raise ExtractError("글 파일이 아님(또는 너무 큼)")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        s = data.decode("utf-8")
    except UnicodeDecodeError:
        s = data.decode("cp949", errors="replace").replace("�", "?")
    text = text_extractor.normalize(s)
    return {"format": "txt", "text": text, "chars": len(text)}


def _raw_name(info) -> bytes:
    """ZIP 에 적힌 원래 이름 바이트 (zipfile 은 UTF-8 표시가 없으면 cp437 로 풀어 두므로 되돌린다)."""
    name = info.orig_filename  # filename 은 zipfile 이 경로 구분자 등을 손본 것이라 원본을 쓴다
    if info.flag_bits & 0x800:
        return name.encode("utf-8", errors="surrogatepass")
    return name.encode("cp437", errors="replace")


def _decode_name(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp949")
    except UnicodeDecodeError:
        return raw.decode("cp949", errors="replace").replace("�", "?")


def _split_ext(base: str) -> tuple[str, str]:
    """PHP pathinfo 의 filename·extension (점이 없으면 확장자 없음)."""
    i = base.rfind(".")
    if i < 0:
        return base, ""
    return base[:i], base[i + 1 :]


def _stem(filename: str) -> str:
    """비교용 이름: 확장자·공백 차이 무시."""
    return re.sub(r"\s+", "", filename.lower())
