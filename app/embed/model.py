"""문장 임베딩 모델 — 지연 로딩 싱글톤. 외부 API 없이 이 프로세스 안에서 CPU 로만 돌린다.

기본(EMBED_BACKEND=onnx): onnxruntime + tokenizers 로 intfloat/multilingual-e5-small 의 int8 ONNX 를 돌린다(torch 불필요, 최대 메모리 약 550MB).
선택(EMBED_BACKEND=torch): sentence-transformers(torch fp32). 메모리가 약 1.3GB 라 MemoryMax 1GB 에는 안 맞는다 — requirements-torch.txt 를 따로 설치해야 한다.

접두어: e5 계열은 질의 "query: ", 문서 "passage: " 를 붙인다(모델 이름에 "e5" 가 있으면). 벡터는 L2 정규화된 float 리스트.
모델 파일은 Hugging Face 캐시(HF_HOME, 이 저장소 개발은 ./.models)에서 찾는다. 서버에서는 처음 한 번 내려받은 뒤 HF_HUB_OFFLINE=1 로 쓰면 외부 접속이 없다.
EMBED_MODEL 에 폴더 경로를 주면(tokenizer.json 과 <EMBED_ONNX_FILE> 이 든 폴더) 그 폴더를 그대로 쓴다."""

from __future__ import annotations

import math
import os
import threading
from typing import Protocol, Sequence

from ..settings import Settings

KINDS = ("query", "passage")
E5_PREFIX = {"query": "query: ", "passage": "passage: "}


class Embedder(Protocol):
    """서비스가 쓰는 임베딩 모델 계약 — 진짜 모델과 시험용 가짜가 같은 모양."""

    name: str

    @property
    def dim(self) -> int: ...

    def embed(self, texts: Sequence[str], kind: str) -> list[list[float]]: ...


class ModelLoadError(RuntimeError):
    """모델 파일을 못 찾거나 불러오지 못함(서비스는 503 으로 알린다)."""


def l2_normalize(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [float(x) / n for x in v] if n > 0 else [float(x) for x in v]


class EmbedModel:
    """지연 로딩: 처음 embed()/dim 을 부를 때 모델을 올린다. 동시에 한 요청만 계산한다(CPU 1개·메모리 상한 때문)."""

    def __init__(self, settings: Settings):
        self.name = settings.embed_model
        self._backend = settings.embed_backend
        self._onnx_file = settings.embed_onnx_file
        self._threads = max(1, settings.embed_threads)
        self._batch = max(1, settings.embed_batch)
        self._max_tokens = settings.embed_max_tokens
        self._prefixed = "e5" in self.name.lower()
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._loaded = False
        self._dim = 0
        self._encode = None  # (list[str]) -> list[list[float]]

    # --- 불러오기 -------------------------------------------------------
    def _load(self) -> None:
        with self._load_lock:
            if self._loaded:
                return
            try:
                if self._backend == "onnx":
                    self._load_onnx()
                elif self._backend == "torch":
                    self._load_torch()
                else:
                    raise ModelLoadError(f"알 수 없는 EMBED_BACKEND: {self._backend} (onnx|torch)")
            except ModelLoadError:
                raise
            except Exception as e:  # 파일 없음·네트워크 차단·패키지 미설치 등
                raise ModelLoadError(f"임베딩 모델을 불러오지 못했습니다({type(e).__name__}: {e})") from e
            self._dim = len(self._encode(["가"])[0])
            self._loaded = True

    def _resolve(self, filename: str) -> str:
        if os.path.isdir(self.name):
            p = os.path.join(self.name, *filename.split("/"))
            if not os.path.isfile(p):
                raise ModelLoadError(f"모델 폴더에 {filename} 이 없습니다.")
            return p
        from huggingface_hub import hf_hub_download

        return hf_hub_download(self.name, filename)

    def _load_onnx(self) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tok = Tokenizer.from_file(self._resolve("tokenizer.json"))
        tok.enable_truncation(max_length=self._max_tokens)
        pad = "<pad>" if tok.token_to_id("<pad>") is not None else "[PAD]"
        tok.enable_padding(pad_id=tok.token_to_id(pad), pad_token=pad)
        so = ort.SessionOptions()
        so.intra_op_num_threads = self._threads
        so.inter_op_num_threads = 1
        sess = ort.InferenceSession(self._resolve(self._onnx_file), so, providers=["CPUExecutionProvider"])
        names = {i.name for i in sess.get_inputs()}

        def encode(texts: list[str]) -> list[list[float]]:
            out: list[list[float]] = []
            for i in range(0, len(texts), self._batch):
                e = tok.encode_batch(texts[i : i + self._batch])
                ids = np.array([x.ids for x in e], dtype=np.int64)
                am = np.array([x.attention_mask for x in e], dtype=np.int64)
                feed = {"input_ids": ids, "attention_mask": am}
                if "token_type_ids" in names:
                    feed["token_type_ids"] = np.zeros_like(ids)
                h = sess.run(None, feed)[0]  # (배치, 길이, 차원)
                m = am[..., None].astype(np.float32)
                v = (h * m).sum(1) / np.maximum(m.sum(1), 1.0)  # 평균 풀링
                v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)
                out.extend(v.astype(np.float32).tolist())
            return out

        self._encode = encode

    def _load_torch(self) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(self._threads)
        st = SentenceTransformer(self.name, device="cpu")
        st.max_seq_length = self._max_tokens
        self._encode = lambda texts: st.encode(
            texts, batch_size=self._batch, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        ).tolist()

    # --- 사용 -----------------------------------------------------------
    @property
    def dim(self) -> int:
        self._load()
        return self._dim

    def embed(self, texts: Sequence[str], kind: str) -> list[list[float]]:
        if kind not in KINDS:
            raise ValueError(f"kind 는 query 또는 passage 여야 합니다: {kind}")
        self._load()
        pre = E5_PREFIX[kind] if self._prefixed else ""
        with self._run_lock:
            vecs = self._encode([pre + t for t in texts])
        return [l2_normalize(v) for v in vecs]  # 한 번 더 정규화(float32 오차 정리)


_singleton: EmbedModel | None = None
_singleton_lock = threading.Lock()


def get_model(settings: Settings) -> EmbedModel:
    """프로세스 안에서 하나만 만든다(모델은 처음 쓸 때 올라간다)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None or _singleton.name != settings.embed_model:
            _singleton = EmbedModel(settings)
        return _singleton
