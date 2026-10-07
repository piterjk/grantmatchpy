"""시험 도우미: 자료 경로, ZIP 만들기."""

from __future__ import annotations

import io
import os
import re
import zipfile

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fx(name: str) -> str:
    return os.path.join(FIX, name)


def fxb(name: str) -> bytes:
    with open(fx(name), "rb") as f:
        return f.read()


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def tmp_with(tmp_path, ext: str, data: bytes, name: str | None = None) -> str:
    p = tmp_path / (name or f"extract-{os.urandom(5).hex()}.{ext}")
    p.write_bytes(data)
    return str(p)


def zip_bytes(files: dict, raw_names: dict | None = None) -> bytes:
    """이름 → 내용 으로 ZIP 바이트를 만든다. 이름이 '/' 로 끝나면 폴더 항목.
    raw_names: {이름: 원래 바이트} — 이름을 UTF-8 표시 없이 그 바이트(예: CP949) 그대로 적는다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        holders = {}
        for name, data in files.items():
            if raw_names and name in raw_names:
                raw = raw_names[name]
                holder = ("Q" * len(raw)).encode()  # 같은 길이의 임시 이름
                holders[holder] = raw
                z.writestr(holder.decode(), data)
            else:
                z.writestr(name, data)
    out = buf.getvalue()
    for holder, raw in holders.items():
        out = out.replace(holder, raw)
    return out


def zip_of(tmp_path, ext: str, files: dict, raw_names: dict | None = None) -> str:
    return tmp_with(tmp_path, ext, zip_bytes(files, raw_names))
