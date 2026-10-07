"""HWPX·DOCX·HWPML 의 XML 본문에서 문단 단위로 글자를 뽑는다. 표 칸의 문단도 문서 순서대로.
안전: DOCTYPE 이 있으면 거부(XXE·개체 폭탄), 개체 치환·DTD 읽기·네트워크 접근 모두 끈 lxml 파서."""

from __future__ import annotations

import io
import re

from lxml import etree

from .errors import ExtractError

NS_HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"

PHP_TRIM = " \t\n\r\0\x0b"  # PHP trim() 기본 문자


def load(xml: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False,  # 개체 치환 안 함
        no_network=True,  # 네트워크 접근 금지
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,  # 거대 문서 허용 안 함
        recover=False,
        remove_comments=False,
    )
    try:
        tree = etree.parse(io.BytesIO(xml), parser)
    except (etree.XMLSyntaxError, ValueError, OSError):
        raise ExtractError("XML 을 읽을 수 없음") from None
    di = tree.docinfo
    if di.doctype or di.internalDTD is not None or di.externalDTD is not None:  # DOCTYPE 이 있으면 거부
        raise ExtractError("XML 을 읽을 수 없음")
    root = tree.getroot()
    if root is None:
        raise ExtractError("XML 을 읽을 수 없음")
    return root


def _local(n: etree._Element) -> str:
    return etree.QName(n).localname


def _inline_text(t: etree._Element, special: dict[str, str], use_local: bool) -> str:
    """글자 묶음 안: 글자 + tab/lineBreak/공백 요소, 그 밖의 요소는 버리되 꼬리 글자는 잇는다."""
    s = t.text or ""
    for n in t:
        if isinstance(n.tag, str):
            name = _local(n) if use_local else n.tag
            s += special.get(name, "")
        else:  # 주석·처리 명령: PHP DOM 의 textContent 와 같게 내용을 잇는다
            s += n.text or ""
        s += n.tail or ""
    return s


_HWPX_SPECIAL = {"tab": "\t", "lineBreak": "\n", "fwSpace": " ", "nbSpace": " "}
_HWPML_SPECIAL = {"TAB": "\t", "LINEBREAK": "\n", "NBSPACE": " ", "FWSPACE": " "}


def hwpx(xml: bytes) -> list[str]:
    """HWPX 구역(section*.xml) → 문단 글자 목록."""
    root = load(xml)
    out: list[str] = []
    for p in root.xpath("//hp:p", namespaces={"hp": NS_HP}):
        s = ""
        # 이 문단에 직접 든 글자 묶음만 (안에 든 표의 문단은 따로 돈다)
        for run in p.findall(f"{{{NS_HP}}}run"):
            for t in run.findall(f"{{{NS_HP}}}t"):
                s += _inline_text(t, _HWPX_SPECIAL, True)
        s = s.strip(PHP_TRIM)
        if s != "":
            out.append(s)
    return out


_HWPML_DOCTYPE = re.compile(rb'<!DOCTYPE\s+HWPML\s*\[((?:\s*<!ENTITY\s+\w+\s+"&#x?[0-9a-fA-F]+;"\s*>)*)\s*\]\s*>')
_HWPML_ENTITY = re.compile(rb'<!ENTITY\s+(\w+)\s+"(&#x?[0-9a-fA-F]+;)"')


def hwpml(xml: bytes) -> list[str]:
    """HWPML 2.x(옛 한글 XML, 확장자는 .hwp). `<!DOCTYPE HWPML [<!ENTITY nbsp "&#160;">]>` 처럼 문자 개체 선언을 달고 온다.
    선언이 "이름 → 숫자 문자 참조"뿐일 때만 직접 바꿔 넣고 DOCTYPE 을 떼어 읽는다(그 밖의 DOCTYPE 은 load() 가 거부)."""
    m = _HWPML_DOCTYPE.search(xml)
    if m:
        mapping: dict[bytes, bytes] = {}
        for e in _HWPML_ENTITY.finditer(m.group(1)):
            name = e.group(1)
            if name not in (b"lt", b"gt", b"amp", b"quot", b"apos"):
                mapping[b"&" + name + b";"] = e.group(2)
        xml = xml.replace(m.group(0), b"")
        if mapping:
            # PHP strtr 처럼 긴 키 우선으로 한 번에 바꾼다
            keys = sorted(mapping, key=len, reverse=True)
            rx = re.compile(b"|".join(re.escape(k) for k in keys))
            xml = rx.sub(lambda mo: mapping[mo.group(0)], xml)
    root = load(xml)
    out: list[str] = []
    for p in root.xpath("//BODY//P"):
        s = ""
        for c in p.xpath("./TEXT/CHAR"):
            s += _inline_text(c, _HWPML_SPECIAL, False)
        s = s.strip(PHP_TRIM)
        if s != "":
            out.append(s)
    return out


def docx(xml: bytes) -> list[str]:
    """DOCX document.xml → 문단 글자 목록. 글상자는 대체본(mc:Fallback) 중복을 빼고 한 번만."""
    root = load(xml)
    ns = {"w": NS_W, "mc": NS_MC}
    out: list[str] = []
    for p in root.xpath("//w:p[not(ancestor::mc:Fallback)]", namespaces=ns):
        s = ""
        for n in p.xpath(".//w:t|.//w:tab|.//w:br|.//w:cr", namespaces=ns):
            # 글상자 안 문단은 그 문단 차례에 따로 나오므로 바깥 문단에서는 빼고 센다
            owner = None
            for anc in n.iterancestors():
                if isinstance(anc.tag, str) and _local(anc) == "p":
                    owner = anc
                    break
            if owner is not p:
                continue
            name = _local(n)
            if name == "t":
                s += "".join(n.itertext())
            elif name == "tab":
                s += "\t"
            else:
                s += "\n"
        s = s.strip(PHP_TRIM)
        if s != "":
            out.append(s)
    return out
