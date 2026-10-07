"""PDF 글자 추출. poppler 의 pdftotext 가 있으면 그것(한글 CID 글꼴에 강하고 빠름), 없거나 실패하면 pypdf(순수 파이썬).
글자 없는 스캔 PDF 는 빈 결과 → 호출 쪽이 "글자 없음(스캔본)"으로 관리자에게 알린다(OCR 은 하지 않음)."""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys

from .errors import ExtractError

MAX_PAGES = 300
MAX_OUTPUT = 32 * 1024 * 1024


def find_pdftotext() -> str | None:
    """환경 변수 PDFTOTEXT_BIN → Git for Windows 의 poppler → /usr/bin·/usr/local/bin 순."""
    env = os.environ.get("PDFTOTEXT_BIN", "")
    candidates = [env] if env else []
    candidates += ["C:/Program Files/Git/mingw64/bin/pdftotext.exe", "/usr/bin/pdftotext", "/usr/local/bin/pdftotext"]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _clean(t: str) -> str:
    """줄마다 앞뒤 공백 정리, 빈 줄 하나로."""
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip(" \t\n\r\0\x0b") for ln in t.split("\n")]
    return re.sub(r"\n{2,}", "\n", "\n".join(lines)).strip(" \t\n\r\0\x0b")


def _kill_tree(p: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=10)
        else:
            p.kill()
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass


def _run_pdftotext(bin_path: str, path: str, timeout: int) -> str | None:
    """성공하면 UTF-8 글자, 실패·시간 초과면 None (그러면 pypdf 로 한 번 더)."""
    cmd = [bin_path, "-enc", "UTF-8", "-l", str(MAX_PAGES), "-q", path, "-"]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        p.communicate()
        return None
    if p.returncode != 0:
        return None
    if len(out) > MAX_OUTPUT:
        raise ExtractError("추출한 글자가 너무 많음")
    return out.decode("utf-8", errors="replace")


def extract(path: str, timeout_sec: int = 30) -> str:
    bin_path = find_pdftotext()
    if bin_path is not None:
        out = _run_pdftotext(bin_path, path, timeout_sec)
        if out is not None:
            return _clean(out)
        # 실패하면(손상·암호) 아래 순수 파이썬으로 한 번 더
    try:
        import pypdf

        with open(path, "rb") as f:
            reader = pypdf.PdfReader(io.BytesIO(f.read()))
            if reader.is_encrypted and int(reader.decrypt("")) == 0:
                raise ExtractError("PDF 를 읽을 수 없음(손상·암호): 암호가 걸린 PDF")
            n = len(reader.pages)
            if n > MAX_PAGES:
                raise ExtractError("PDF 쪽이 너무 많음")
            texts = [(reader.pages[i].extract_text() or "") for i in range(n)]
        return _clean("\n".join(texts))
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001 — 손상 PDF 는 어떤 예외든 날 수 있다
        raise ExtractError("PDF 를 읽을 수 없음(손상·암호): " + (str(e) or type(e).__name__)[:80]) from None
