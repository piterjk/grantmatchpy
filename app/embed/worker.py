"""임베딩 작업자 — embedding_jobs 큐를 작은 묶음씩 꺼내 벡터로 만든다(서비스 프로세스와 별개 프로세스, 모델은 이 프로세스에서 올린다).

한 번의 "묶음":
  1. REMOVE 작업(마감·숨김·소개 삭제)을 한 번에 최대 100건 — 벡터를 지우고 작업 줄을 지운다.
  2. PENDING 작업을 priority·id 순(회원 소개=1 이 공고=5 보다 먼저)으로 batch 건 꺼내 하나씩 처리한다.
       a. 저장소의 현재 (text_hash, model) 이 작업의 text_hash·현재 모델과 같으면 임베딩 없이 DONE(건너뜀)
       b. 아니면 embed([글], kind) — GRANT=passage, MEMBER=query(회원 소개는 검색 질의 쪽) — 로 벡터를 만들어 저장하고 DONE
  묶음 크기: 한국 시간 밤(night_start_hour 이상 night_end_hour 미만)에는 batch_night, 아니면 batch_day.
실패: 시도 횟수 +1·error 기록(500자). 3번째 실패면 FAILED, 아니면 PENDING 으로 되돌린다.
  단 모델 로딩 실패(ModelLoadError, 서비스의 503 에 해당)는 시도 횟수를 쓰지 않고 이번 묶음을 중단 — 다음 주기에 다시.
로그는 한국어 한 줄 요약(건수만). 글 내용·접속 값은 남기지 않는다."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..settings import Settings
from .model import Embedder, ModelLoadError
from .store import VECTOR_DIM, EmbeddingStore, Job, StoreJobError

log = logging.getLogger("grantmatchpy.embed_worker")

MAX_ATTEMPTS = 3  # 3번 실패하면 FAILED
REMOVE_LIMIT = 100  # 한 묶음에서 지울 최대 건수
KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없어 고정 오프셋이면 충분하다
KIND_BY_TARGET = {"GRANT": "passage", "MEMBER": "query", "KNOWLEDGE": "passage"}  # 지식 자료 청크는 검색 대상 글(passage)


@dataclass
class BatchResult:
    done: int = 0  # 벡터를 새로 만들어 저장
    skipped: int = 0  # 같은 지문·모델이라 임베딩 없이 DONE
    failed: int = 0  # 실패(재시도 대기 또는 FAILED)
    removed: int = 0  # 벡터·작업 줄 삭제
    fetched: int = 0  # 꺼낸 PENDING 작업 수
    model_unavailable: bool = False  # 모델 로딩 실패로 중단

    @property
    def idle(self) -> bool:
        """처리할 일이 아무것도 없었음."""
        return self.fetched == 0 and self.removed == 0

    def add(self, o: "BatchResult") -> None:
        self.done += o.done
        self.skipped += o.skipped
        self.failed += o.failed
        self.removed += o.removed
        self.fetched += o.fetched
        self.model_unavailable = self.model_unavailable or o.model_unavailable

    def summary(self) -> str:
        s = f"처리 {self.done}건·건너뜀 {self.skipped}건·실패 {self.failed}건·삭제 {self.removed}건"
        return s + "(모델을 불러오지 못해 중단)" if self.model_unavailable else s


def is_night(now: datetime, start_hour: int, end_hour: int) -> bool:
    """한국 시간 기준 밤 시간대인가(start 이상 end 미만, start > end 면 자정을 넘는 구간)."""
    h = now.astimezone(KST).hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


class EmbedWorker:
    def __init__(
        self,
        store: EmbeddingStore,
        embedder: Embedder,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.embedder = embedder
        self.s = settings
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep

    def batch_size(self) -> int:
        night = is_night(self._clock(), self.s.night_start_hour, self.s.night_end_hour)
        return self.s.embed_worker_batch_night if night else self.s.embed_worker_batch_day

    # --- 묶음 하나 ---
    def run_batch(self, should_stop: Callable[[], bool] = lambda: False, deadline: float | None = None) -> BatchResult:
        """묶음 하나를 처리한다. 작업 사이에 should_stop 이 참이거나 deadline(monotonic) 이 지나면 남은 작업은 두고 끝낸다
        (꺼낸 줄은 건드리지 않았으므로 PENDING 그대로 — 다음에 다시 꺼낸다). DB 오류는 롤백하고 그대로 올린다."""
        res = BatchResult()
        try:
            for j in self.store.claim_remove_jobs(REMOVE_LIMIT):
                self.store.delete_vector(j.target_type, j.target_id)
                self.store.delete_job(j.id)
                res.removed += 1
            self.store.commit()
            if should_stop():
                return res

            jobs = self.store.claim_pending(self.batch_size())
            res.fetched = len(jobs)
            for j in jobs:
                if should_stop() or (deadline is not None and self._monotonic() >= deadline):
                    break
                if not self._process(j, res):  # 모델 로딩 실패 — 이번 묶음 중단
                    res.model_unavailable = True
                    break
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return res

    def _process(self, j: Job, res: BatchResult) -> bool:
        """작업 하나. False 면 모델을 못 올려 묶음을 중단해야 한다."""
        kind = KIND_BY_TARGET.get(j.target_type)
        if kind is None:
            self._fail(j, "알 수 없는 대상 종류", res)
            return True
        cur = self.store.current_vector(j.target_type, j.target_id)
        if cur is not None and cur == (j.text_hash, self.embedder.name):
            self.store.mark_done(j.id)  # 글도 모델도 같다 — 다시 만들지 않는다
            res.skipped += 1
            return True
        try:
            vec = self.embedder.embed([j.text], kind)[0]
        except ModelLoadError:
            return False  # 시도 횟수를 쓰지 않는다 — 줄은 PENDING 그대로
        except Exception as e:  # noqa: BLE001 — 글 하나의 계산 실패
            self._fail(j, f"{type(e).__name__}: {e}", res)
            return True
        if len(vec) != VECTOR_DIM:
            self._fail(j, f"벡터 차원이 {VECTOR_DIM} 이 아닙니다({len(vec)})", res)
            return True
        try:
            self.store.upsert_vector(j.target_type, j.target_id, j.text_hash, self.embedder.name, vec)
        except StoreJobError as e:
            self._fail(j, str(e), res)
            return True
        self.store.mark_done(j.id)
        res.done += 1
        return True

    def _fail(self, j: Job, error: str, res: BatchResult) -> None:
        attempts = j.attempts + 1
        self.store.mark_failed(j.id, attempts, error[:500], attempts >= MAX_ATTEMPTS)
        res.failed += 1

    # --- 한 번 실행(--once) ---
    def run_once(self, run_seconds: int | None = None) -> BatchResult:
        """시간 상한 안에서 묶음을 되풀이한다. 할 일이 없거나 모델을 못 올리면 일찍 끝낸다."""
        limit = self.s.embed_worker_run_seconds if run_seconds is None else run_seconds
        deadline = self._monotonic() + limit
        total = BatchResult()
        while self._monotonic() < deadline:
            r = self.run_batch(deadline=deadline)
            total.add(r)
            if not r.idle:
                log.info("임베딩 묶음: %s", r.summary())
            if r.idle or r.model_unavailable:
                break
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleep(min(float(self.s.embed_worker_sleep_seconds), remaining))
        log.info("임베딩 한 번 실행 끝: %s", total.summary())
        return total

    # --- 상시 실행 ---
    def run_forever(self, should_stop: Callable[[], bool]) -> None:
        """should_stop 이 참이 될 때까지 되풀이한다(현재 작업을 마치고 끝낸다). 쉬는 시간은 sleep 으로(상시 실행에서는 중단 신호에 깨는 대기)."""
        while not should_stop():
            wait = float(self.s.embed_worker_sleep_seconds)
            try:
                r = self.run_batch(should_stop=should_stop)
                if not r.idle:
                    log.info("임베딩 묶음: %s", r.summary())
                if r.idle or r.model_unavailable:
                    wait = float(self.s.embed_worker_idle_seconds)
            except Exception as e:  # noqa: BLE001 — DB 끊김 등. 접속 값이 섞일 수 있어 종류만 남기고 다음 주기에 다시
                log.warning("임베딩 묶음 오류(%s) — %d초 뒤 다시 시도합니다.", type(e).__name__, self.s.embed_worker_idle_seconds)
                wait = float(self.s.embed_worker_idle_seconds)
            if should_stop():
                break
            self._sleep(wait)
        log.info("임베딩 작업자를 종료합니다.")
