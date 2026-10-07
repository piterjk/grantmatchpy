"""공고 평가 세트(50건) 뽑기 — 개발 PostgreSQL 읽기 전용. 접속 값은 GM_DST_* (환경 변수 또는 GM_ENV_FILE / grantmatch/.env.local)
에서 읽고 어디에도 찍거나 저장하지 않는다. 결과: tools/eval/grants_50.json

뽑는 법(재현 가능, 무작위 없음 — id 순서와 고정 해시만 쓴다):
  1) 분야 층화 30건: (수집처 × 분야 첫 단계) 묶음을 돌아가며 한 건씩 — 묶음 안에서는 id 의 해시 순서.
  2) 주제 보강 20건: 시험용 회사 소개 10개의 업종 키워드(반려동물·식품·농업·관광·바이오·게임·소재·핀테크·교육·금속)마다 제목에 걸리는 공고 2건.
요약·지원대상이 모두 비어 있는 공고는 뺀다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.embed.passage_text import build_passage_text  # noqa: E402

OUT = Path(__file__).with_name("grants_50.json")
TOPICS = {
    "pet": r"반려|펫",
    "food": r"푸드|식품|외식",
    "agri": r"스마트팜|농업|농식품",
    "tour": r"관광|숙박|여행",
    "bio": r"바이오|의료|헬스",
    "game": r"게임|콘텐츠|웹툰",
    "material": r"소재|친환경|탄소",
    "fintech": r"핀테크|결제",
    "edu": r"교육|인공지능|AI",
    "metal": r"뿌리|금속|스마트공장|가공",
}


def _env() -> dict[str, str]:
    d = dict(os.environ)
    p = Path(os.environ.get("GM_ENV_FILE", r"D:\claude\grantmatch\grantmatch\.env.local"))
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                d.setdefault(k.strip(), v.strip())
    return d


def connect() -> psycopg.Connection:
    e = _env()
    m = re.match(r"jdbc:postgresql://([^:/]+)(?::(\d+))?/([^?]+)", e["GM_DST_URL"])
    if not m:
        raise SystemExit("GM_DST_URL 형식을 알 수 없습니다.")
    return psycopg.connect(
        host=m.group(1), port=int(m.group(2) or 5432), dbname=m.group(3), user=e["GM_DST_USER"],
        password=e["GM_DST_PASSWORD"], options="-c default_transaction_read_only=on",
    )


def _h(i: int) -> str:
    return hashlib.sha256(f"gm-eval-{i}".encode()).hexdigest()


def main() -> None:
    c = connect()
    rows = c.execute(
        "select id, source_code, title, target, category, regions, summary from grants "
        "where coalesce(length(summary),0) + coalesce(length(target),0) > 0 order by id"
    ).fetchall()
    cols = ("id", "source_code", "title", "target", "category", "regions", "summary")
    grants = [dict(zip(cols, r)) for r in rows]

    picked: dict[int, str] = {}
    # 2) 주제 보강을 먼저 정해 두고(중복 방지), 1) 층화는 나머지에서
    for key, pat in TOPICS.items():
        cands = sorted((g for g in grants if re.search(pat, g["title"] or "") and g["id"] not in picked), key=lambda g: _h(g["id"]))
        for g in cands[:2]:
            picked[g["id"]] = f"topic:{key}"
    groups: dict[tuple, list] = {}
    for g in grants:
        if g["id"] in picked:
            continue
        top = (g["category"] or "").split(">")[0].strip()
        groups.setdefault((g["source_code"], top), []).append(g)
    for lst in groups.values():
        lst.sort(key=lambda g: _h(g["id"]))
    keys = sorted(groups, key=lambda k: (-len(groups[k]), k))
    while len(picked) < 50 and any(groups.values()):
        for k in keys:
            if groups[k] and len(picked) < 50:
                g = groups[k].pop(0)
                picked[g["id"]] = f"strata:{k[0]}/{k[1]}"

    byid = {g["id"]: g for g in grants}
    out = []
    for gid in sorted(picked):
        g = byid[gid]
        conds = [(r[0], r[1]) for r in c.execute("select kind, value_json from grant_conditions where grant_id=%s order by id", (gid,))]
        regions = g["regions"] if isinstance(g["regions"], list) else []
        out.append({
            "id": gid, "pick": picked[gid], "source_code": g["source_code"], "category": g["category"], "title": g["title"],
            "target": g["target"], "regions": regions, "summary": g["summary"],
            "conditions": [{"kind": k, "value": v} for k, v in conds],
            "text": build_passage_text(g["title"], g["target"], g["category"], regions, g["summary"], conds),
        })
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(out)}건 저장: {OUT}")
    from collections import Counter
    print(Counter(o["source_code"] for o in out), Counter((o["category"] or "").split(">")[0].strip() for o in out).most_common(8))


if __name__ == "__main__":
    main()
