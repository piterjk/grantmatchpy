"""임베딩 작업자 시험 — 가짜 저장소·가짜 임베더(DB·모델 없이). 진짜 PostgreSQL 시험은 test_embed_store_pg.py."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.embed.store import VECTOR_DIM, vector_literal
from app.embed.worker import KST, EmbedWorker, is_night
from app.embed_worker import main as worker_main
from app.settings import Settings
from tests.fakes import FakeEmbedder, FakeStore


class Clock:
    """시계·monotonic·sleep 을 한꺼번에 가짜로. sleep 하면 시간이 그만큼 흐른다."""

    def __init__(self, hour: int = 12):
        self.t = 0.0
        self.hour = hour
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 10, 6, self.hour, 0, tzinfo=KST)

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make(store=None, emb=None, hour=12, **kw):
    store = store or FakeStore()
    emb = emb or FakeEmbedder()
    clock = Clock(hour)
    s = Settings(**kw)
    return EmbedWorker(store, emb, s, clock=clock.now, monotonic=clock.monotonic, sleep=clock.sleep), store, emb, clock


# ── 시간대·묶음 크기 ──


@pytest.mark.parametrize(
    "hour,night",
    [(0, False), (1, True), (3, True), (5, True), (6, False), (12, False), (23, False)],
)
def test_is_night_default_window(hour, night):
    assert is_night(datetime(2026, 10, 6, hour, 30, tzinfo=KST), 1, 6) is night


def test_is_night_uses_korean_time_not_utc():
    # UTC 17:00 = 한국 02:00(다음 날) → 밤
    assert is_night(datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc), 1, 6) is True
    assert is_night(datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc), 1, 6) is False  # 한국 07:00


def test_is_night_wrapping_window():
    assert is_night(datetime(2026, 10, 6, 23, 0, tzinfo=KST), 22, 6) is True
    assert is_night(datetime(2026, 10, 6, 7, 0, tzinfo=KST), 22, 6) is False


def test_batch_size_changes_by_time_of_day():
    day, *_ = make(hour=14)
    night, *_ = make(hour=2)
    assert (day.batch_size(), night.batch_size()) == (4, 16)  # 기본값
    custom, *_ = make(hour=2, embed_worker_batch_night=7, embed_worker_batch_day=3)
    assert custom.batch_size() == 7


def test_batch_takes_only_batch_size_jobs_day_and_night():
    for hour, expect in ((12, 4), (3, 16)):
        w, store, emb, _ = make(hour=hour)
        for i in range(30):
            store.add_job("GRANT", i)
        r = w.run_batch()
        assert r.done == expect and len(emb.calls) == expect
        assert sum(1 for j in store.jobs.values() if j.status == "DONE") == expect


# ── 순서·종류 ──


def test_member_intro_first_and_kind_by_target():
    w, store, emb, _ = make()
    for g in (10, 11, 12):
        store.add_job("GRANT", g, text=f"공고{g}")
    store.add_job("MEMBER", 7, text="우리 회사 소개")  # id 가 제일 뒤라도 priority 1 이라 먼저
    w.run_batch()
    assert emb.calls[0] == (["우리 회사 소개"], "query")
    assert [c[1] for c in emb.calls[1:]] == ["passage"] * 3
    assert [c[0][0] for c in emb.calls[1:]] == ["공고10", "공고11", "공고12"]  # 같은 priority 는 id 순
    assert ("MEMBER", 7) in store.vectors and ("GRANT", 10) in store.vectors


def test_stored_vector_uses_job_hash_and_embedder_model():
    w, store, *_ = make()
    store.add_job("GRANT", 1, text_hash="abc")
    w.run_batch()
    h, model, vec = store.vectors[("GRANT", 1)]
    assert (h, model, len(vec)) == ("abc", "fake-model", VECTOR_DIM)


# ── 건너뜀·재임베딩 ──


def test_same_hash_and_model_skips_embedding():
    w, store, emb, _ = make()
    store.put_vector("GRANT", 1, [1.0] * VECTOR_DIM, text_hash="same", model="fake-model")
    j = store.add_job("GRANT", 1, text_hash="same")
    r = w.run_batch()
    assert (r.skipped, r.done, r.failed) == (1, 0, 0)
    assert emb.calls == []  # 임베딩 호출 0회
    assert store.jobs[j.id].status == "DONE"


def test_different_hash_reembeds():
    w, store, emb, _ = make()
    store.put_vector("GRANT", 1, [0.5] * VECTOR_DIM, text_hash="old", model="fake-model")
    store.add_job("GRANT", 1, text_hash="new")
    r = w.run_batch()
    assert (r.done, r.skipped) == (1, 0) and len(emb.calls) == 1
    assert store.vectors[("GRANT", 1)][0] == "new"


def test_different_model_reembeds_even_with_same_hash():
    w, store, emb, _ = make()
    store.put_vector("GRANT", 1, [0.5] * VECTOR_DIM, text_hash="same", model="old-model")
    store.add_job("GRANT", 1, text_hash="same")
    r = w.run_batch()
    assert r.done == 1 and len(emb.calls) == 1
    assert store.vectors[("GRANT", 1)][1] == "fake-model"


# ── 실패 ──


def test_failure_three_times_then_failed():
    emb = FakeEmbedder(fail_texts={"나쁜 글"})
    w, store, _, _ = make(emb=emb)
    j = store.add_job("GRANT", 1, text="나쁜 글")
    for expected_status, expected_attempts in (("PENDING", 1), ("PENDING", 2), ("FAILED", 3)):
        r = w.run_batch()
        assert r.failed == 1
        assert (store.jobs[j.id].status, store.jobs[j.id].attempts) == (expected_status, expected_attempts)
    assert "RuntimeError" in store.jobs[j.id].error
    assert w.run_batch().idle  # FAILED 는 더 꺼내지 않는다


def test_failure_does_not_stop_other_jobs_and_error_truncated():
    emb = FakeEmbedder(fail_texts={"x" * 800})
    w, store, _, _ = make(emb=emb)
    bad = store.add_job("GRANT", 1, text="x" * 800)
    store.add_job("GRANT", 2, text="좋은 글")
    r = w.run_batch()
    assert (r.failed, r.done) == (1, 1)
    # 오류 글에 입력 글 전체가 들어가지 않아도 500자를 넘지 않는다
    assert len(store.jobs[bad.id].error) <= 500


def test_wrong_dimension_is_a_failure_not_saved():
    w, store, _, _ = make(emb=FakeEmbedder(bad_dim=True))
    j = store.add_job("GRANT", 1)
    r = w.run_batch()
    assert r.failed == 1 and ("GRANT", 1) not in store.vectors and store.jobs[j.id].attempts == 1


def test_store_job_error_counts_as_failure():
    w, store, _, _ = make()
    store.fail_upsert_for = {1}
    j = store.add_job("MEMBER", 1)
    store.add_job("GRANT", 2)
    r = w.run_batch()
    assert (r.failed, r.done) == (1, 1) and store.jobs[j.id].attempts == 1


def test_unknown_target_type_fails():
    w, store, _, _ = make()
    j = store.add_job("OTHER", 1)
    assert w.run_batch().failed == 1 and store.jobs[j.id].attempts == 1


def test_model_load_failure_uses_no_attempts_and_stops_batch():
    w, store, emb, _ = make(emb=FakeEmbedder(load_fail=True))
    a = store.add_job("MEMBER", 1)
    b = store.add_job("GRANT", 2)
    r = w.run_batch()
    assert r.model_unavailable and r.failed == 0 and r.done == 0
    assert all(j.status == "PENDING" and j.attempts == 0 for j in (store.jobs[a.id], store.jobs[b.id]))
    emb.load_fail = False  # 다음 주기에는 처리된다
    r2 = w.run_batch()
    assert r2.done == 2


def test_model_load_failure_midbatch_keeps_earlier_results():
    emb = FakeEmbedder()
    w, store, _, _ = make(emb=emb)
    store.put_vector("MEMBER", 1, [1.0] * VECTOR_DIM, text_hash="h-MEMBER-1")  # 건너뛰는 일은 모델 없이 된다
    store.add_job("MEMBER", 1)
    store.add_job("GRANT", 2)
    emb.load_fail = True
    r = w.run_batch()
    assert (r.skipped, r.model_unavailable, r.failed) == (1, True, 0)


def test_db_error_rolls_back_and_raises():
    w, store, _, _ = make()
    store.add_job("GRANT", 1)

    def boom(*a, **k):
        raise ConnectionError("끊김")

    store.mark_done = boom
    with pytest.raises(ConnectionError):
        w.run_batch()
    assert store.rollbacks == 1


# ── REMOVE ──


def test_remove_jobs_delete_vector_and_row_up_to_100():
    w, store, emb, _ = make()
    for i in range(120):
        store.put_vector("GRANT", i, [1.0] * VECTOR_DIM)
        store.add_job("GRANT", i, status="REMOVE")
    store.put_vector("MEMBER", 5, [1.0] * VECTOR_DIM)
    store.add_job("MEMBER", 5, status="REMOVE")
    r = w.run_batch()
    assert r.removed == 100 and emb.calls == []
    assert sum(1 for j in store.jobs.values() if j.status == "REMOVE") == 21
    assert sum(1 for k in store.vectors if k[0] == "GRANT") == 20
    r = w.run_batch()
    assert r.removed == 21 and not store.jobs and not store.vectors  # 회원 벡터도 지워짐


def test_remove_runs_before_pending_in_same_batch():
    w, store, *_ = make()
    store.put_vector("GRANT", 1, [1.0] * VECTOR_DIM)
    store.add_job("GRANT", 1, status="REMOVE")
    store.add_job("GRANT", 2)
    r = w.run_batch()
    assert (r.removed, r.done) == (1, 1) and ("GRANT", 1) not in store.vectors


# ── 한 번 실행·상시 실행 ──


def test_run_once_respects_time_limit():
    w, store, emb, clock = make(embed_worker_sleep_seconds=5)
    for i in range(100):
        store.add_job("GRANT", i)
    total = w.run_once(run_seconds=12)
    # t=0·5·10 에 묶음 3번(4건씩), 12초 상한이라 더 돌지 않는다
    assert total.done == 12 and clock.t <= 12
    assert sum(1 for j in store.jobs.values() if j.status == "PENDING") == 88
    assert clock.sleeps == [5.0, 5.0, 2.0]  # 마지막 쉬는 시간은 남은 시간까지만


def test_run_once_stops_when_idle_and_uses_default_limit():
    w, store, emb, clock = make(embed_worker_run_seconds=600)
    for i in range(6):
        store.add_job("GRANT", i)
    total = w.run_once()
    assert total.done == 6 and clock.t < 600  # 일이 끝나면 상한을 채우지 않고 종료


def test_run_once_stops_on_model_failure():
    w, store, emb, clock = make(emb=FakeEmbedder(load_fail=True))
    store.add_job("GRANT", 1)
    total = w.run_once(run_seconds=600)
    assert total.model_unavailable and clock.sleeps == []  # 되풀이하며 계속 두드리지 않는다


def test_run_once_deadline_stops_inside_a_batch():
    w, store, emb, clock = make(hour=3)  # 밤: 묶음 16

    class SlowEmbedder(FakeEmbedder):
        def embed(self, texts, kind):
            clock.t += 4  # 한 건에 4초
            return super().embed(texts, kind)

    w.embedder = SlowEmbedder()
    for i in range(16):
        store.add_job("GRANT", i)
    total = w.run_once(run_seconds=10)
    assert total.done == 3  # 4·8·12초 — 상한(10)을 넘으면 남은 작업은 손대지 않는다
    assert sum(1 for j in store.jobs.values() if j.status == "PENDING") == 13


def test_run_forever_loops_waits_and_stops_on_signal():
    w, store, emb, clock = make(embed_worker_sleep_seconds=5, embed_worker_idle_seconds=30)
    for i in range(6):
        store.add_job("GRANT", i)
    state = {"stop": False}
    orig_sleep = clock.sleep

    def sleep(s):
        orig_sleep(s)
        if len(clock.sleeps) >= 3:
            state["stop"] = True

    w._sleep = sleep
    w.run_forever(lambda: state["stop"])
    # 4건 → 5초 쉼 → 2건 → 5초 쉼 → 할 일 없음 → 30초 쉼(여기서 신호)
    assert clock.sleeps == [5.0, 5.0, 30.0]
    assert sum(1 for j in store.jobs.values() if j.status == "DONE") == 6


def test_run_forever_stop_finishes_current_job_only():
    w, store, emb, _ = make(hour=3)
    for i in range(16):
        store.add_job("GRANT", i)
    n = {"c": 0}

    class StoppingEmbedder(FakeEmbedder):
        def embed(self, texts, kind):
            n["c"] += 1
            return super().embed(texts, kind)

    w.embedder = StoppingEmbedder()
    w.run_forever(lambda: n["c"] >= 2)  # 2번째 작업을 마친 뒤 신호가 보인다
    assert n["c"] == 2
    assert sum(1 for j in store.jobs.values() if j.status == "DONE") == 2  # 끝낸 작업은 확정


def test_run_forever_survives_db_error():
    w, store, *_ = make(embed_worker_idle_seconds=30)
    calls = {"n": 0}
    real = store.claim_remove_jobs

    def flaky(limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("끊김 host=secret-host")
        return real(limit)

    store.claim_remove_jobs = flaky
    store.add_job("GRANT", 1)
    state = {"stop": False}
    orig = w._sleep

    def sleep(s):
        orig(s)
        if calls["n"] >= 2:
            state["stop"] = True

    w._sleep = sleep
    w.run_forever(lambda: state["stop"])
    assert store.rollbacks == 1 and store.jobs[1].status == "DONE"


def test_log_summary_has_counts_only(caplog):
    caplog.set_level("INFO", logger="grantmatchpy.embed_worker")
    w, store, *_ = make()
    store.add_job("GRANT", 1, text="비밀스러운 공고 글")
    w.run_once(run_seconds=60)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "처리 1건·건너뜀 0건·실패 0건·삭제 0건" in text
    assert "비밀스러운" not in text


def test_run_forever_log_does_not_leak_error_message(caplog):
    caplog.set_level("INFO", logger="grantmatchpy.embed_worker")
    w, store, *_ = make()

    def boom(limit):
        raise ConnectionError("postgresql://user:pw@host/db")

    store.claim_remove_jobs = boom
    n = {"c": 0}

    def sleep(s):
        n["c"] += 1

    w._sleep = sleep
    w.run_forever(lambda: n["c"] >= 1)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ConnectionError" in text and "pw@host" not in text


# ── 설정·진입점 ──


def test_worker_settings_from_env(monkeypatch):
    s = Settings.from_env()
    assert (s.embed_worker_batch_day, s.embed_worker_batch_night, s.night_start_hour, s.night_end_hour) == (4, 16, 1, 6)
    assert (s.embed_worker_sleep_seconds, s.embed_worker_idle_seconds, s.embed_worker_run_seconds) == (5, 30, 600)
    for k, v in {
        "EMBED_WORKER_BATCH_DAY": "2", "EMBED_WORKER_BATCH_NIGHT": "8", "NIGHT_START_HOUR": "0", "NIGHT_END_HOUR": "5",
        "EMBED_WORKER_SLEEP_SECONDS": "1", "EMBED_WORKER_IDLE_SECONDS": "9", "EMBED_WORKER_RUN_SECONDS": "60",
    }.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert (s.embed_worker_batch_day, s.embed_worker_batch_night, s.night_start_hour, s.night_end_hour) == (2, 8, 0, 5)
    assert (s.embed_worker_sleep_seconds, s.embed_worker_idle_seconds, s.embed_worker_run_seconds) == (1, 9, 60)


def test_worker_refuses_to_start_without_db_url(monkeypatch, tmp_path):
    monkeypatch.delenv("GRANTMATCHPY_DB_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # .env 가 없는 빈 폴더
    assert worker_main(["--once"]) == 2


def test_vector_literal_format():
    assert vector_literal([0.1, 0.25, -1.0]) == "[0.1,0.25,-1.0]"
    assert vector_literal([1, 2]) == "[1.0,2.0]"
