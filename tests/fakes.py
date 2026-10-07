"""시험용 가짜 — 메모리 저장소(EmbeddingStore 계약)와 가짜 임베더. DB·모델 파일 없이 작업자·순위 로직을 시험한다."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from app.embed.model import ModelLoadError
from app.embed.store import VECTOR_DIM, Job, StoreJobError, StoreUnavailable


@dataclass
class Row:
    id: int
    target_type: str
    target_id: int
    text: str
    text_hash: str
    priority: int = 5
    status: str = "PENDING"
    attempts: int = 0
    error: str | None = None


class FakeStore:
    """embedding_jobs·벡터 표를 딕셔너리로 흉내. claim 은 id 순서·priority 순서를 진짜 SQL 과 같게 낸다."""

    def __init__(self):
        self.jobs: dict[int, Row] = {}
        self.vectors: dict[tuple[str, int], tuple[str, str, list[float]]] = {}  # (종류, id) → (text_hash, model, 벡터)
        self._next = 1
        self.commits = 0
        self.rollbacks = 0
        self.unavailable = False  # True 면 rank 가 DB 접속 실패
        self.fail_upsert_for: set[int] = set()  # target_id 목록 — 저장 시 StoreJobError

    # --- 시험 도우미 ---
    def add_job(self, target_type: str, target_id: int, text: str = "글", text_hash: str | None = None,
                priority: int | None = None, status: str = "PENDING", attempts: int = 0) -> Row:
        r = Row(self._next, target_type, target_id, text, text_hash or f"h-{target_type}-{target_id}",
                priority if priority is not None else (1 if target_type == "MEMBER" else 5), status, attempts)
        self.jobs[r.id] = r
        self._next += 1
        return r

    def put_vector(self, target_type: str, target_id: int, vec: Sequence[float], text_hash: str = "x", model: str = "fake-model") -> None:
        self.vectors[(target_type, target_id)] = (text_hash, model, list(vec))

    # --- 계약 ---
    def _claim(self, status: str, limit: int, key) -> list[Job]:
        rows = sorted((r for r in self.jobs.values() if r.status == status), key=key)[:limit]
        return [Job(r.id, r.target_type, r.target_id, r.text, r.text_hash, r.priority, r.attempts) for r in rows]

    def claim_remove_jobs(self, limit: int) -> list[Job]:
        return self._claim("REMOVE", limit, lambda r: r.id)

    def claim_pending(self, limit: int) -> list[Job]:
        return self._claim("PENDING", limit, lambda r: (r.priority, r.id))

    def current_vector(self, target_type: str, target_id: int):
        v = self.vectors.get((target_type, target_id))
        return (v[0], v[1]) if v else None

    def upsert_vector(self, target_type, target_id, text_hash, model, vector) -> None:
        if target_id in self.fail_upsert_for:
            raise StoreJobError("벡터를 저장하지 못했습니다(IntegrityError)")
        self.vectors[(target_type, target_id)] = (text_hash, model, list(vector))

    def delete_vector(self, target_type: str, target_id: int) -> None:
        self.vectors.pop((target_type, target_id), None)

    def delete_job(self, job_id: int) -> None:
        self.jobs.pop(job_id, None)

    def mark_done(self, job_id: int) -> None:
        r = self.jobs[job_id]
        r.status, r.error = "DONE", None

    def mark_failed(self, job_id: int, attempts: int, error: str, final: bool) -> None:
        r = self.jobs[job_id]
        r.status, r.attempts, r.error = ("FAILED" if final else "PENDING"), attempts, error[:500]

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass

    def rank(self, member_id: int, grant_ids: Sequence[int]) -> list[tuple[int, float]]:
        if self.unavailable:
            raise StoreUnavailable("DB 에 접속하지 못했습니다(OperationalError)")
        m = self.vectors.get(("MEMBER", member_id))
        if m is None:
            return []
        out = []
        for g in grant_ids:
            v = self.vectors.get(("GRANT", g))
            if v is None or v[1] != m[1]:  # 모델이 같은 것끼리만(진짜 SQL 의 join 조건)
                continue
            out.append((g, 1 - cosine_distance(v[2], m[2])))
        return out


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 1 - dot / (na * nb)


@dataclass
class FakeEmbedder:
    """384차원 가짜 임베더. 호출을 기록한다. fail_texts 의 글은 일반 오류, load_fail=True 면 모델 로딩 실패."""

    name: str = "fake-model"
    load_fail: bool = False
    fail_texts: set[str] = field(default_factory=set)
    bad_dim: bool = False
    calls: list[tuple[list[str], str]] = field(default_factory=list)

    @property
    def dim(self) -> int:
        return VECTOR_DIM

    def embed(self, texts, kind):
        if self.load_fail:
            raise ModelLoadError("임베딩 모델을 불러오지 못했습니다(FileNotFoundError)")
        self.calls.append((list(texts), kind))
        if any(t in self.fail_texts for t in texts):
            raise RuntimeError("계산 실패")
        n = 3 if self.bad_dim else VECTOR_DIM
        return [[1.0] + [0.0] * (n - 1) for _ in texts]
