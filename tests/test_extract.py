"""PHP packages/extract/tests/ExtractTest.php 의 31개 시험에 대응하는 시험.
자료: K-Startup 2026 예비창업패키지 공고 첨부 원본(양식 hwp·docx, 공고문 hwpx), 양식 hwp 를 바꾼 hwpx, Word 로 만든 계획서 PDF."""

from __future__ import annotations

import io
import json
import struct
import sys
import time
import zipfile

import pytest

from app.extract import ExtractError, detect, extract, sandbox
from tests.helpers import FIX, fx, fxb, squash, tmp_with, zip_bytes, zip_of


# 1~5 형식을 파일 머리로 판별한다
@pytest.mark.parametrize(
    "file,fmt",
    [("form.hwp", "hwp"), ("form.hwpx", "hwpx"), ("notice.hwpx", "hwpx"), ("form.docx", "docx"), ("plan_word.pdf", "pdf")],
)
def test_01_detect(file, fmt):
    assert detect(fx(file)) == fmt


# 6
def test_06_hwp_matches_hwpx():
    """HWP 해석기: 같은 양식의 HWPX 와 글자가 똑같다 (줄 263개, 4,219자)"""
    hwp = extract(fx("form.hwp"))
    hwpx = extract(fx("form.hwpx"))
    assert hwp["chars"] == hwpx["chars"] == 4219
    assert squash(hwp["text"]) == squash(hwpx["text"])
    assert len(hwp["text"].split("\n")) == 263
    assert "예비창업패키지 예비창업자 사업계획서" in hwp["text"]
    assert "대학교(원)명 및 소재지" in hwp["text"]  # 표 밖 안내문
    assert "순번\n직위\n담당 업무" in hwp["text"]  # 표 칸도 순서대로


# 7
def test_07_hwpx_notice():
    t = extract(fx("notice.hwpx"))["text"]
    assert "신청자 명의의 사업자등록증을 보유하고 있지 않으며, 법인의 법률상 대표권을 가지고 있지 않은 자" in t


# 8
def test_08_docx_textbox_once():
    t = extract(fx("form.docx"))["text"]
    assert t.count("1. 문제 인식 (Problem)_창업 아이템의 필요성") == 1
    assert "정부지원사업비(원)" in t


# 9
def test_09_pdf():
    r = extract(fx("plan_word.pdf"))
    assert r["format"] == "pdf"
    assert squash("건강 기록 분석 기술이 적용된 반려견 맞춤 사료 추천 앱") in squash(r["text"])
    assert squash("20,000,000") in squash(r["text"])


def test_09b_pdf_pypdf_fallback(monkeypatch):
    """pdftotext 가 없으면 pypdf 로 읽는다."""
    from app.extract import pdf_text

    monkeypatch.setattr(pdf_text, "find_pdftotext", lambda: None)
    r = extract(fx("plan_word.pdf"))
    assert squash("건강 기록 분석 기술이 적용된 반려견 맞춤 사료 추천 앱") in squash(r["text"])


# 10
def test_10_fake_extension_and_unknown(tmp_path):
    exe = tmp_with(tmp_path, "hwp", b"MZ\x90\x00" + b"\0" * 200)
    with pytest.raises(ExtractError, match="지원하지 않는 형식"):
        extract(exe)
    xlsx = zip_of(tmp_path, "xlsx", {"[Content_Types].xml": "<Types/>", "xl/workbook.xml": "<workbook/>"})
    with pytest.raises(ExtractError, match="엑셀"):
        extract(xlsx)


# 11
def test_11_zip_bundle(tmp_path):
    xlsx = zip_bytes({"[Content_Types].xml": "<Types/>"})
    zp = zip_of(tmp_path, "zip", {
        "[붙임1] 공고문.hwpx": fxb("notice.hwpx"),
        "[붙임2] 신청서.hwp": fxb("form.hwp"),
        "양식/[붙임3] 계획서.docx": fxb("form.docx"),
        "[붙임4] 예시 계획서.pdf": fxb("plan_word.pdf"),
        "[붙임5] 산출내역.xlsx": xlsx,
        "양식/": b"",
        "__MACOSX/._공고문.hwpx": b"junk",
    })
    r = extract(zp)
    assert r["format"] == "zip"
    assert [p["name"] for p in r["parts"]] == ["[붙임1] 공고문.hwpx", "[붙임2] 신청서.hwp", "[붙임4] 예시 계획서.pdf", "[붙임5] 산출내역.xlsx", "양식/[붙임3] 계획서.docx"]
    assert [p.get("format") for p in r["parts"] if "format" in p] == ["hwpx", "hwp", "pdf", "docx"]
    assert "엑셀" in r["parts"][3]["skipped"]
    assert "[파일: [붙임2] 신청서.hwp]" in r["text"]
    assert squash("건강 기록 분석 기술이 적용된 반려견 맞춤 사료 추천 앱") in squash(r["text"])
    assert r["chars"] == len(r["text"])
    hwp = extract(fx("form.hwp"))  # 묶음 안 각 파일의 글자는 따로 읽은 것과 같다
    assert r["parts"][1]["chars"] == hwp["chars"] and hwp["text"] in r["text"]


# 12
def test_12_pdf_copy_skipped(tmp_path):
    r = extract(zip_of(tmp_path, "zip", {
        "공고문.hwp": fxb("form.hwp"),
        "공고문 .pdf": fxb("plan_word.pdf"),
        "다른 자료.pdf": fxb("plan_word.pdf"),
    }))
    by = {p["name"]: p for p in r["parts"]}
    assert "사본" in by["공고문 .pdf"]["skipped"]
    assert by["다른 자료.pdf"]["format"] == "pdf"
    assert by["공고문.hwp"]["format"] == "hwp"


# 13
def test_13_zip_in_zip_one_level(tmp_path):
    deep = zip_bytes({"c.hwp": fxb("form.hwp")})
    inner = zip_bytes({"b.hwpx": fxb("form.hwpx"), "deep.zip": deep})
    r = extract(zip_of(tmp_path, "zip", {
        "inner.zip": inner,
        "broken.hwp": fxb("form.hwp")[:3000],
        "fake.pdf": b"MZ\x90\x00" + b"\0" * 100,
        "empty.hwp": b"",
    }))
    by = {p["name"]: p for p in r["parts"]}
    assert by["inner.zip"]["format"] == "zip"
    assert by["inner.zip"]["chars"] > 100
    assert "[파일: b.hwpx]" in r["text"]
    assert "[파일: c.hwp]" not in r["text"]
    assert "skipped" in by["broken.hwp"]
    assert "지원하지 않는 형식" in by["fake.pdf"]["skipped"]
    assert by["empty.hwp"]["skipped"] == "빈 파일"


# 14
def test_14_cp949_names(tmp_path):
    name = "붙임1 신청서.hwp"
    p = zip_of(tmp_path, "zip", {name: fxb("form.hwp")}, raw_names={name: name.encode("cp949")})
    r = extract(p)
    assert r["parts"][0]["name"] == name and r["parts"][0]["format"] == "hwp"


# 15
def test_15_zip_via_sandbox(tmp_path):
    p = zip_of(tmp_path, "zip", {"a.hwpx": fxb("form.hwpx"), "b.bin": b"\x00\x01junk"})
    r = sandbox.extract(p, timeout_sec=60)
    assert r["format"] == "zip"
    assert [x["name"] for x in r["parts"]] == ["a.hwpx", "b.bin"]
    assert "지원하지 않는 형식" in r["parts"][1]["skipped"]


# 16
def test_16_notes_and_pictures(tmp_path):
    r = extract(zip_of(tmp_path, "zip", {
        "3. 사업자등록증 사본.txt": "사업자등록증(스캔하여 pdf로 제출 요망)".encode(),
        "4. 통장사본.txt": "통장사본(pdf로 제출 요망)".encode("cp949"),
        "로고/로고.ai": fxb("plan_word.pdf"),  # .ai 는 PDF 호환
        "홍보지.png": b"\x89PNG\r\n\x1a\n" + b"\0" * 50,
    }))
    by = {p["name"]: p for p in r["parts"]}
    assert by["3. 사업자등록증 사본.txt"]["format"] == "txt"
    assert "통장사본(pdf로 제출 요망)" in r["text"]
    assert "그림" in by["로고/로고.ai"]["skipped"]
    assert "그림" in by["홍보지.png"]["skipped"]
    assert "반려견" not in r["text"]


def hwpml(doctype: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n' + doctype + "\n"
        '<HWPML Version="2.1"><HEAD SecCnt="1"><DOCSUMMARY><TITLE>머리 제목은 본문 아님</TITLE></DOCSUMMARY></HEAD>'
        '<BODY><SECTION Id="0">'
        '<P ParaShape="1"><TEXT CharShape="1"><CHAR>제1조(목적)&nbsp;이 법은</CHAR></TEXT><TEXT CharShape="2"><CHAR> 연구개발을<TAB/>지원한다.</CHAR></TEXT></P>'
        "<P><TEXT><TABLE><ROW><CELL><PARALIST><P><TEXT><CHAR>표 칸 글자</CHAR></TEXT></P></PARALIST></CELL></ROW></TABLE></TEXT></P>"
        "<P><TEXT><CHAR>제2조(정의)</CHAR></TEXT></P>"
        "</SECTION></BODY><TAIL/></HWPML>"
    ).encode("utf-8")


# 17
def test_17_hwpml(tmp_path):
    p = tmp_with(tmp_path, "hwp", hwpml('<!DOCTYPE HWPML [\n\t<!ENTITY nbsp\t"&#160;">\n]>'))
    r = extract(p)
    assert r["format"] == "hwpml"
    assert r["text"] == "제1조(목적) 이 법은 연구개발을 지원한다.\n표 칸 글자\n제2조(정의)"
    with open(p, "rb") as f:  # 묶음 안에 있어도
        b = extract(zip_of(tmp_path, "zip", {"1. 국가연구개발혁신법(법률).hwp": f.read()}))
    assert b["parts"][0]["format"] == "hwpml"


# 18
def test_18_hwpml_doctype_rejected(tmp_path):
    evil = tmp_with(tmp_path, "hwp", hwpml('<!DOCTYPE HWPML [<!ENTITY nbsp SYSTEM "file:///etc/passwd">]>'))
    with pytest.raises(ExtractError, match="XML"):
        extract(evil)
    bomb = tmp_with(tmp_path, "hwp", hwpml('<!DOCTYPE HWPML [<!ENTITY a "aaaa"><!ENTITY nbsp "&a;&a;">]>'))
    with pytest.raises(ExtractError, match="XML"):
        extract(bomb)


# 19
def test_19_zip_bomb(tmp_path):
    p = zip_of(tmp_path, "hwpx", {"mimetype": "application/hwp+zip", "Contents/section0.xml": b"A" * (70 * 1024 * 1024)})
    import os

    assert os.path.getsize(p) < 1024 * 1024
    with pytest.raises(ExtractError):
        extract(p)


# 20
def test_20_xxe_hwpx(tmp_path):
    xml = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
        "<hp:p><hp:run><hp:t>&e;</hp:t></hp:run></hp:p></hs:sec>"
    )
    p = zip_of(tmp_path, "hwpx", {"mimetype": "application/hwp+zip", "Contents/section0.xml": xml})
    with pytest.raises(ExtractError, match="XML 을 읽을 수 없음"):
        extract(p)


def _patch_hwp_props(bit: int) -> bytes:
    data = fxb("form.hwp")
    at = data.index(b"HWP Document File")  # FileHeader 는 작아서 미니 스트림에 그대로 들어 있다
    props = struct.unpack_from("<I", data, at + 36)[0] | bit
    return data[: at + 36] + struct.pack("<I", props) + data[at + 40 :]


# 21~22
@pytest.mark.parametrize("bit,msg", [(0b10, "암호가 걸린"), (0b100, "배포용")])
def test_21_encrypted_hwp(tmp_path, bit, msg):
    with pytest.raises(ExtractError, match=msg):
        extract(tmp_with(tmp_path, "hwp", _patch_hwp_props(bit)))


# 23~26
@pytest.mark.parametrize("file,keep", [("form.hwp", 1024), ("form.hwp", 33536), ("form.hwpx", 12000), ("plan_word.pdf", 2048)])
def test_23_truncated_fail_only_with_extract_error(tmp_path, file, keep):
    data = fxb(file)[:keep]
    p = tmp_with(tmp_path, file.rsplit(".", 1)[1], data)
    with pytest.raises(ExtractError):
        extract(p)


# 27
def test_27_sandbox_same_result():
    direct = extract(fx("form.hwp"))
    sand = sandbox.extract(fx("form.hwp"), timeout_sec=60)
    assert sand == direct
    with pytest.raises(ExtractError, match="파일 없음"):
        sandbox.extract(fx("없는파일.hwp"))


# 28
def test_28_timeout_kills_process():
    start = time.monotonic()
    with pytest.raises(ExtractError, match=r"글자 추출 시간 초과\(1초\)"):
        sandbox.extract("x", timeout_sec=1, cmd=[sys.executable, "-c", "import time; time.sleep(30)"])
    assert time.monotonic() - start < 8


# 29
def test_29_warnings_do_not_break_json():
    """라이브러리 경고가 나도 결과 JSON 은 깨지지 않는다(경고는 표준 오류로)."""
    code = (
        "import sys, warnings; warnings.warn('Undefined array key 5'); sys.stderr.write('noise\\n');"
        "from app.extract.worker import main; main(['w', sys.argv[1]])"
    )
    r = sandbox.extract(fx("form.hwpx"), timeout_sec=60, cmd=[sys.executable, "-W", "always", "-c", code, fx("form.hwpx")])
    assert r["format"] == "hwpx"


# 30
def test_30_big_picture_in_hwpx_ok(tmp_path):
    """HWPX 본문 그림이 커도(64MB 넘는 BMP) 본문 글자는 읽는다 — 크기 검사는 읽는 본문만."""
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(fxb("form.hwpx"))) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            z.writestr(info.filename, src.read(info.filename))
        z.writestr("BinData/image9.bmp", b"\0" * (70 * 1024 * 1024))
    p = tmp_with(tmp_path, "hwpx", buf.getvalue())
    r = extract(p)
    assert r["format"] == "hwpx" and r["chars"] == extract(fx("form.hwpx"))["chars"]


# 31
def test_31_encrypted_hwpx(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(fxb("form.hwpx"))) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            if info.filename != "META-INF/manifest.xml":
                z.writestr(info.filename, src.read(info.filename))
        z.writestr(
            "META-INF/manifest.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?><odf:manifest xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            '<odf:file-entry full-path="Contents/section0.xml" media-type="application/xml" size="90768"><odf:encryption-data checksum-type="sha256-1k" checksum="x">'
            '<odf:algorithm algorithm-name="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/></odf:encryption-data></odf:file-entry></odf:manifest>',
        )
    with pytest.raises(ExtractError, match="배포용"):
        extract(tmp_with(tmp_path, "hwpx", buf.getvalue()))


# ── 추가 시험(Python 이식에서 더한 것) ──

def test_x1_empty_and_missing(tmp_path):
    with pytest.raises(ExtractError, match="빈 파일"):
        extract(tmp_with(tmp_path, "hwp", b""))
    with pytest.raises(ExtractError, match="파일 없음"):
        extract(str(tmp_path / "nope"))


def test_x2_too_large_file(tmp_path, monkeypatch):
    from app.extract import text_extractor

    monkeypatch.setattr(text_extractor, "MAX_FILE", 100)
    with pytest.raises(ExtractError, match="너무 큼"):
        extract(fx("form.hwp"))


def test_x3_zip_too_many_entries(tmp_path):
    p = zip_of(tmp_path, "zip", {f"f{i}.txt": b"x" for i in range(2001)})
    with pytest.raises(ExtractError, match="너무 많음"):
        extract(p)


def test_x4_ratio_guard(tmp_path):
    p = zip_of(tmp_path, "zip", {"a.txt": b"\0" * (5 * 1024 * 1024)})  # 압축률 200배 넘고 1MB 넘음
    with pytest.raises(ExtractError, match="압축률"):
        extract(p)


def test_x5_bundle_part_limit(tmp_path):
    files = {f"{i:02d}.txt": f"메모 {i}".encode() for i in range(45)}
    r = extract(zip_of(tmp_path, "zip", files))
    read = [p for p in r["parts"] if "format" in p]
    skipped = [p for p in r["parts"] if "너무 많아" in p.get("skipped", "")]
    assert len(read) == 40 and len(skipped) == 5


def test_x6_normalize_like_php():
    from app.extract import normalize

    t = "가​나­  다\t\t라　 마\r\n\n\x07바\n  \n사\n"
    assert normalize(t) == "가나 다 라 마\n바\n사"


def test_x7_natural_order():
    from app.extract.natsort import natkey

    names = ["a10.hwp", "a2.hwp", "a1.hwp", "[붙임10]", "[붙임2]"]
    assert sorted(names, key=natkey) == ["[붙임2]", "[붙임10]", "a1.hwp", "a2.hwp", "a10.hwp"]


def test_x8_memory_limit_applies():
    code = (
        "from app.extract.limits import apply_memory_limit\n"
        "assert apply_memory_limit(300)\n"
        "try:\n    x = bytearray(900*1024*1024)\n    print('NOLIMIT')\n"
        "except MemoryError:\n    print('LIMITED')\n"
    )
    code_, out, err, to = sandbox.run_command([sys.executable, "-c", code], 60, None)
    assert b"LIMITED" in out, (out, err)
