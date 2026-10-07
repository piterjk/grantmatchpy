"""임베딩 모델 한 개·스레드 수 한 설정을 재는 도구(프로세스 하나에 설정 하나 — 최대 메모리를 깨끗하게 재려고).

사용:  python tools/eval/bench_embed.py <모델> <스레드 수|default> [출력 폴더]
결과:  tools/eval/_out/<모델 이름>__t<스레드>.json (시간·메모리·차원) 과 .npz (공고·회사 소개 벡터)
HF_HOME 을 grantmatchpy/.models 로 잡아 모델 캐시를 그 안에만 둔다. 외부 호출 없음(캐시에 있으면 오프라인)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("HF_HOME", str(ROOT / ".models"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np  # noqa: E402
import psutil  # noqa: E402
import torch  # noqa: E402

EVAL = Path(__file__).parent
# 접두어 규칙: e5 계열은 질의 "query: " / 문서 "passage: ", 한국어 sroberta 는 접두어 없음
PREFIX = {"e5": ("query: ", "passage: ")}


def prefixes(model: str) -> tuple[str, str]:
    return PREFIX["e5"] if "e5" in model.lower() else ("", "")


def main() -> None:
    model, threads = sys.argv[1], sys.argv[2]
    batch = int(sys.argv[4]) if len(sys.argv) > 4 else 8
    out = Path(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != "-" else EVAL / "_out"
    out.mkdir(parents=True, exist_ok=True)
    default_threads = torch.get_num_threads()
    if threads != "default":
        torch.set_num_threads(int(threads))
    proc = psutil.Process()
    base_rss = proc.memory_info().rss

    from sentence_transformers import SentenceTransformer

    t0 = time.perf_counter()
    m = SentenceTransformer(model, device="cpu")
    m.max_seq_length = 512  # ko-sroberta 기본 128 은 공고 글(수백 토큰)에 짧아 512 로 맞춘다
    load_s = time.perf_counter() - t0
    after_load = proc.memory_info().rss

    grants = json.loads((EVAL / "grants_50.json").read_text(encoding="utf-8"))
    comps = json.loads((EVAL / "companies.json").read_text(encoding="utf-8"))
    qp, pp = prefixes(model)
    ptexts = [pp + g["text"] for g in grants]
    qtexts = [qp + c["text"] for c in comps]
    toks = [len(m.tokenizer(g["text"])["input_ids"]) for g in grants]

    def run(texts):
        t = time.perf_counter()
        v = m.encode(texts, batch_size=batch, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        return v, time.perf_counter() - t

    pv, cold = run(ptexts)  # 첫 실행(워밍업 포함)
    pv2, warm = run(ptexts)  # 두 번째 실행
    qv, q_s = run(qtexts)
    peak = getattr(proc.memory_info(), "peak_wset", proc.memory_info().rss)

    wdir = Path(m._modules["0"].auto_model.config._name_or_path) if False else None
    res = {
        "model": model, "threads": torch.get_num_threads(), "threads_default": default_threads,
        "dim": int(pv.shape[1]), "load_s": round(load_s, 2),
        "passage50_cold_s": round(cold, 2), "passage50_warm_s": round(warm, 2),
        "per_item_warm_s": round(warm / len(ptexts), 4), "per_item_cold_s": round(cold / len(ptexts), 4),
        "query10_s": round(q_s, 2), "est_3000_min": round(warm / len(ptexts) * 3000 / 60, 1),
        "batch": batch, "base_rss_mb": round(base_rss / 2**20), "after_load_rss_mb": round(after_load / 2**20), "peak_rss_mb": round(peak / 2**20),
        "tokens_mean": round(float(np.mean(toks))), "tokens_max": int(max(toks)),
        "params_m": round(sum(p.numel() for p in m.parameters()) / 1e6),
    }
    name = model.replace("/", "_") + (f"__b{batch}" if batch != 8 else "")
    tag = threads if threads != "default" else f"default{default_threads}"
    (out / f"{name}__t{tag}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    np.savez(out / f"{name}__t{tag}.npz", passages=pv, queries=qv)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
