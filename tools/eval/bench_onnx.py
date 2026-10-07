"""bench_embed.py 의 ONNX Runtime 판 — torch 없이 onnxruntime + tokenizers 만으로 e5 를 돌릴 때의 시간·메모리·품질을 잰다.

사용:  python tools/eval/bench_onnx.py <onnx 파일 이름> <스레드 수> [배치] [저장소]
       예) python tools/eval/bench_onnx.py model_qint8_avx512_vnni.onnx 1 1   (저장소 기본 intfloat/multilingual-e5-small, 접두어는 e5 일 때만)
모델은 .models 캐시(오프라인). 결과는 _out/<저장소 짧은 이름>-onnx-<파일>__t<스레드>[__b<배치>].json/.npz."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("HF_HOME", str(ROOT / ".models"))

import numpy as np  # noqa: E402
import psutil  # noqa: E402

EVAL = Path(__file__).parent


def main() -> None:
    fname, threads = sys.argv[1], int(sys.argv[2])
    batch = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    proc = psutil.Process()
    base = proc.memory_info().rss

    from huggingface_hub import hf_hub_download
    import onnxruntime as ort
    from tokenizers import Tokenizer

    repo = sys.argv[4] if len(sys.argv) > 4 else "intfloat/multilingual-e5-small"
    e5 = "e5" in repo
    pre_q, pre_p = ("query: ", "passage: ") if e5 else ("", "")
    t0 = time.perf_counter()
    tok = Tokenizer.from_file(hf_hub_download(repo, "tokenizer.json"))
    tok.enable_truncation(max_length=512)  # ko-sroberta 도 512(torch 쪽과 동일)
    tok.enable_padding(pad_id=tok.token_to_id("<pad>") if e5 else tok.token_to_id("[PAD]"), pad_token="<pad>" if e5 else "[PAD]")
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    if os.environ.get("ORT_NOARENA") == "1":
        so.enable_cpu_mem_arena = False
        so.enable_mem_pattern = False
    sess = ort.InferenceSession(hf_hub_download(repo, "onnx/" + fname), so, providers=["CPUExecutionProvider"])
    load_s = time.perf_counter() - t0
    after_load = proc.memory_info().rss
    names = {i.name for i in sess.get_inputs()}

    def enc(texts):
        out = []
        for i in range(0, len(texts), batch):
            e = tok.encode_batch(texts[i : i + batch])
            ids = np.array([x.ids for x in e], dtype=np.int64)
            am = np.array([x.attention_mask for x in e], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": am}
            if "token_type_ids" in names:
                feed["token_type_ids"] = np.zeros_like(ids)
            h = sess.run(None, feed)[0]
            m = am[..., None].astype(np.float32)
            v = (h * m).sum(1) / m.sum(1)
            out.append(v / np.linalg.norm(v, axis=1, keepdims=True))
        return np.concatenate(out)

    grants = json.loads((EVAL / "grants_50.json").read_text(encoding="utf-8"))
    comps = json.loads((EVAL / "companies.json").read_text(encoding="utf-8"))
    pt = [pre_p + g["text"] for g in grants]
    qt = [pre_q + c["text"] for c in comps]
    t = time.perf_counter(); pv = enc(pt); cold = time.perf_counter() - t
    t = time.perf_counter(); enc(pt); warm = time.perf_counter() - t
    qv = enc(qt)
    peak = proc.memory_info().peak_wset
    res = {
        "model": f"{repo} onnx {fname}", "threads": threads, "batch": batch, "dim": int(pv.shape[1]), "load_s": round(load_s, 2),
        "passage50_cold_s": round(cold, 2), "passage50_warm_s": round(warm, 2), "per_item_warm_s": round(warm / 50, 4),
        "est_3000_min": round(warm / 50 * 3000 / 60, 1), "base_rss_mb": round(base / 2**20),
        "after_load_rss_mb": round(after_load / 2**20), "peak_rss_mb": round(peak / 2**20),
    }
    name = f"{repo.split('/')[1]}-onnx-{fname.replace('.onnx', '')}__t{threads}" + (f"__b{batch}" if batch != 8 else "")
    (EVAL / "_out").mkdir(exist_ok=True)
    (EVAL / "_out" / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    np.savez(EVAL / "_out" / f"{name}.npz", passages=pv, queries=qv)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
