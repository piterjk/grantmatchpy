"""압축 파일(HWPX·DOCX·첨부 묶음 ZIP)을 열기 전 검사: 항목 수·풀었을 때 크기·압축률 상한 (압축 폭탄 방지).
PHP ZipGuard 와 같은 상한·같은 메시지."""

from __future__ import annotations

import re
import zipfile
import zlib

from .errors import ExtractError

MAX_ENTRIES = 2000
MAX_TOTAL = 200 * 1024 * 1024  # 풀었을 때 합계
MAX_ENTRY = 64 * 1024 * 1024  # 항목 하나
MAX_RATIO = 200  # 풀린 크기 / 압축 크기


def open_zip(path: str, only: str | None = None) -> zipfile.ZipFile:
    """only: 크기·압축률을 검사할 항목 이름 정규식(HWPX·DOCX 는 읽는 본문 XML 만 — 본문 그림이 커도 거부하지 않게).
    None 이면 모든 항목(첨부 묶음 ZIP)."""
    try:
        z = zipfile.ZipFile(path, "r")
    except (zipfile.BadZipFile, OSError, EOFError, NotImplementedError, ValueError, zlib.error):
        raise ExtractError("압축 파일을 열 수 없음") from None
    try:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ExtractError("압축 안 파일이 너무 많음")
        rx = re.compile(only) if only is not None else None
        total = 0
        for st in infos:
            if rx is not None and not rx.search(st.filename):
                continue
            if st.file_size > MAX_ENTRY:
                raise ExtractError(f"압축 안 파일이 너무 큼: {st.filename}")
            if st.compress_size > 0 and st.file_size / st.compress_size > MAX_RATIO and st.file_size > 1024 * 1024:
                raise ExtractError(f"압축률이 비정상: {st.filename}")
            total += st.file_size
            if total > MAX_TOTAL:
                raise ExtractError("풀었을 때 크기가 너무 큼")
    except BaseException:
        z.close()
        raise
    return z


def _read_info(z: zipfile.ZipFile, info: zipfile.ZipInfo, label: str) -> bytes:
    try:
        with z.open(info) as f:
            data = f.read(info.file_size + 1)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, zlib.error, ValueError):
        raise ExtractError(label) from None
    if len(data) > info.file_size:
        raise ExtractError(label)
    return data


def read_name(z: zipfile.ZipFile, name: str) -> bytes | None:
    """이름으로 꺼내되, 머리에 적힌 크기보다 많이 나오면 거부. 없으면 None."""
    info = z.NameToInfo.get(name)
    if info is None:
        return None
    return _read_info(z, info, f"압축 안 파일 크기가 맞지 않음: {name}")


def read_index(z: zipfile.ZipFile, index: int) -> bytes:
    """순번으로 꺼내기(이름 인코딩이 제각각인 일반 ZIP 묶음용)."""
    infos = z.infolist()
    if not 0 <= index < len(infos):
        raise ExtractError("압축 안 파일을 찾을 수 없음")
    return _read_info(z, infos[index], "압축 안 파일 크기가 맞지 않음")
