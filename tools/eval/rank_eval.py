"""bench_embed.py 가 저장한 벡터(.npz)로 회사 소개 10개 × 공고 50건 순위를 매겨 표를 찍고 recall@5·MRR 을 계산한다.

사용:  python tools/eval/rank_eval.py <이름> [<이름> ...]      (이름 = tools/eval/_out/<이름>.npz 의 이름, 예: intfloat_multilingual-e5-small__t1)
결과:  표준 출력(마크다운) 과 tools/eval/_out/ranking.md
정답은 tools/eval/gold.json(사람이 표시). 벡터는 L2 정규화돼 있어 내적 = 코사인 유사도."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

EVAL = Path(__file__).parent


def main() -> None:
    names = sys.argv[1:]
    grants = json.loads((EVAL / "grants_50.json").read_text(encoding="utf-8"))
    comps = json.loads((EVAL / "companies.json").read_text(encoding="utf-8"))
    gold = json.loads((EVAL / "gold.json").read_text(encoding="utf-8"))
    gids = [g["id"] for g in grants]
    title = {g["id"]: g["title"] for g in grants}

    lines: list[str] = []
    summary: dict[str, tuple[float, float]] = {}
    for name in names:
        z = np.load(EVAL / "_out" / f"{name}.npz")
        sim = z["queries"] @ z["passages"].T  # (10, 50)
        lines.append(f"\n## {name}\n")
        lines.append("| 회사 소개 | 상위 5개 공고 (정답은 ★, 괄호는 순위 아닌 유사도) |")
        lines.append("|---|---|")
        rec, rr = [], []
        for ci, c in enumerate(comps):
            order = np.argsort(-sim[ci])
            g = set(gold[c["key"]]["gold"])
            top = [gids[j] for j in order[:5]]
            cells = [("★" if gid in g else "") + title[gid][:34] + f" ({sim[ci][gids.index(gid)]:.2f})" for gid in top]
            lines.append(f"| {c['name']} | " + "<br>".join(f"{k + 1}. {t}" for k, t in enumerate(cells)) + " |")
            if g:
                rec.append(len(g & set(top)) / len(g))
                first = next((k + 1 for k, j in enumerate(order) if gids[j] in g), None)
                rr.append(1 / first if first else 0.0)
        summary[name] = (float(np.mean(rec)), float(np.mean(rr)))
    head = ["| 모델 | recall@5 | MRR |", "|---|---|---|"] + [f"| {n} | {r:.3f} | {m:.3f} |" for n, (r, m) in summary.items()]
    out = "\n".join(head + lines)
    (EVAL / "_out" / "ranking.md").write_text(out, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
