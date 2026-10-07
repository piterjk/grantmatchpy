"""공고 한 건을 임베딩 입력 글로 만든다(평가 도구와 이후 embedding_jobs 작업자가 같이 쓴다).

형식:
  [사업명: ..] [지원대상: ..] [분야: ..] [지역: .., ..]
  {title}
  {target}
  {summary}
  {검수한 조건 문장들, 한 줄씩}
비어 있는 필드는 줄(또는 머리말 항목)을 뺀다. 길이는 max_chars 로 자른다(모델 최대 입력 512 토큰 ≒ 한국어 약 700자 이내)."""

from __future__ import annotations

from typing import Any, Iterable

DEFAULT_MAX_CHARS = 900  # 한국어 e5/bge 계열 토크나이저로 대략 450~550 토큰. 모델 쪽에서도 max_seq_length 로 한 번 더 자른다.

_COMMON = {"TAX": "국세·지방세 체납 중인 자는 제외된다.", "DUPLICATE": "중복 지원은 제한된다.", "SANCTION": "제재 중인 자는 제외된다."}
_SIZE = {"MICRO": "소공인", "SMALL": "소기업", "MEDIUM": "중기업"}
_STATUS = {"EARLY": "예비·초기 창업", "SMALL": "소상공인", "ETC": "기타"}


def _join(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if str(x).strip())
    return str(v)


def condition_sentence(kind: str, value: Any) -> str:
    """grant_conditions 한 행(kind·value_json)을 사람이 읽는 한 문장으로. 알 수 없으면 빈 문자열."""
    if value is None or value == "" or value == []:
        return ""
    if kind == "OTHER":
        return str(value).strip()
    if kind == "AGE_MAX":
        return f"만 {value}세 이하"
    if kind == "AGE_MIN":
        return f"만 {value}세 이상"
    if kind == "FOUNDED_WITHIN_YEARS":
        return f"창업 {value}년 이내"
    if kind == "EMPLOYEES_MAX":
        return f"상시 근로자 {value}명 이하"
    if kind == "REGION":
        return f"지역: {_join(value)}"
    if kind == "INDUSTRY":
        return f"업종: {_join(value)}"
    if kind == "BUSINESS_STATUS":
        vs = value if isinstance(value, list) else [value]
        return "사업 단계: " + ", ".join(_STATUS.get(str(x), str(x)) for x in vs)
    if kind == "COMPANY_SIZE":
        vs = value if isinstance(value, list) else [value]
        return "기업 규모: " + ", ".join(_SIZE.get(str(x), str(x)) for x in vs)
    if kind == "CERTIFICATION":
        return "인증: " + _join(value)
    if kind == "COMMON":
        return _COMMON.get(str(value), "")
    return ""


def build_passage_text(
    title: str | None,
    target: str | None,
    category: str | None,
    regions: Iterable[str] | None,
    summary: str | None,
    conditions: Iterable[tuple[str, Any]] = (),
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    title = (title or "").strip()
    target = (target or "").strip()
    category = (category or "").strip()
    summary = (summary or "").strip()
    reg = ", ".join(str(r).strip() for r in (regions or []) if str(r).strip())

    head = []
    if title:
        head.append(f"[사업명: {title}]")
    if target:
        head.append(f"[지원대상: {target}]")
    if category:
        head.append(f"[분야: {category}]")
    if reg:
        head.append(f"[지역: {reg}]")

    lines = [" ".join(head)] if head else []
    lines += [x for x in (title, target, summary) if x]
    for kind, value in conditions:
        s = condition_sentence(kind, value)
        if s:
            lines.append(s)
    text = "\n".join(lines)
    return text[:max_chars]
