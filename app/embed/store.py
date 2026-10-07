"""임베딩 작업·벡터 저장소 — 작업자(worker.py)와 순위 엔드포인트(/internal/rank)가 쓰는 표 접근.

- `EmbeddingStore` 는 계약(Protocol)이다. 진짜 구현은 `PgEmbeddingStore`(PostgreSQL 16 + pgvector, psycopg 3),
  시험용 메모리 가짜는 tests/fakes.py. 핵심 로직(worker·rank)은 이 계약에만 기대므로 DB 없이 시험된다.
- 서비스 계정이 만지는 표는 embedding_jobs · grant_embeddings · member_embeddings **셋뿐**이다(grants·member_grant_profiles 는 읽지 않는다 —
  임베딩할 글은 작업 줄(embedding_jobs.text)에 이미 들어 있고, 순위에 필요한 것은 벡터 표뿐이다).
- 접속은 환경 변수 GRANTMATCHPY_DB_URL(psycopg 접속 문자열). 접속 값은 로그·오류 글에 남기지 않는다.
- 벡터는 pgvector 문자열 '[0.1,0.2,…]' 로 넘기고 SQL 에서 ::vector 로 캐스트한다(pgvector-python 패키지 불필요).
- 시각은 UTC. 트랜잭션: 작업자는 한 묶음을 한 트랜잭션으로 처리한다(꺼낸 줄은 FOR UPDATE SKIP LOCKED 로 잠겨 다른 작업자가 건드리지 않고,
  벡터 저장과 DONE 표시가 함께 확정되거나 함께 취소된다). 순위 조회는 호출마다 짧은 별도 연결을 쓴다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence

VECTOR_DIM = 384  # vector(384) — intfloat/multilingual-e5-small
TARGET_GRANT = "GRANT"
TARGET_MEMBER = "MEMBER"

# 대상 종류 → (벡터 표, 식별 열). 이 표의 값만 SQL 문장에 들어간다(사용자 입력은 절대 문장에 넣지 않는다).
_VECTOR_TABLES = {
    TARGET_GRANT: ("grant_embeddings", "grant_id"),
    TARGET_MEMBER: ("member_embeddings", "member_id"),
}


class StoreUnavailable(RuntimeError):
    """DB 에 접속하지 못하거나 연결이 끊김(순위 엔드포인트는 503 으로 알린다)."""


class StoreJobError(RuntimeError):
    """한 줄(작업)만 저장하지 못함(무결성 위반·값 오류 등) — 그 작업만 실패로 센다. 묶음 전체는 이어 간다."""


@dataclass(frozen=True)
class Job:
    id: int
    target_type: str  # GRANT | MEMBER
    target_id: int
    text: str
    text_hash: str
    priority: int
    attempts: int


class EmbeddingStore(Protocol):
    # --- 작업자 ---
    def claim_remove_jobs(self, limit: int) -> list[Job]: ...
    def claim_pending(self, limit: int) -> list[Job]: ...
    def current_vector(self, target_type: str, target_id: int) -> tuple[str, str] | None: ...
    def upsert_vector(self, target_type: str, target_id: int, text_hash: str, model: str, vector: Sequence[float]) -> None: ...
    def delete_vector(self, target_type: str, target_id: int) -> None: ...
    def delete_job(self, job_id: int) -> None: ...
    def mark_done(self, job_id: int) -> None: ...
    def mark_failed(self, job_id: int, attempts: int, error: str, final: bool) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...

    # --- 순위 ---
    def rank(self, member_id: int, grant_ids: Sequence[int]) -> list[tuple[int, float]]: ...


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector 입력 문자열 '[0.1,0.2,…]' (repr 로 부동소수 정밀도를 그대로 둔다)."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── SQL (상수 — 파라미터는 모두 %s 바인딩) ──
SQL_CLAIM = (
    "select id, target_type, target_id, text, text_hash, priority, attempts "
    "from embedding_jobs where status = %s {order} limit %s for update skip locked"
)
SQL_CLAIM_REMOVE = SQL_CLAIM.format(order="order by id")
SQL_CLAIM_PENDING = SQL_CLAIM.format(order="order by priority, id")
SQL_CURRENT = "select text_hash, model from {table} where {col} = %s"
SQL_UPSERT = (
    "insert into {table} ({col}, text_hash, model, embedding, updated_at) values (%s, %s, %s, %s::vector, %s) "
    "on conflict ({col}) do update set text_hash = excluded.text_hash, model = excluded.model, "
    "embedding = excluded.embedding, updated_at = excluded.updated_at"
)
SQL_DELETE_VECTOR = "delete from {table} where {col} = %s"
SQL_DELETE_JOB = "delete from embedding_jobs where id = %s"
SQL_DONE = "update embedding_jobs set status = 'DONE', error = null, updated_at = %s where id = %s"
SQL_FAILED = "update embedding_jobs set status = %s, attempts = %s, error = %s, updated_at = %s where id = %s"
SQL_RANK = (
    "select g.grant_id, 1 - (g.embedding <=> m.embedding) "
    "from grant_embeddings g join member_embeddings m on m.member_id = %s and g.model = m.model "
    "where g.grant_id = any(%s::bigint[])"
)


def _tbl(target_type: str) -> tuple[str, str]:
    try:
        return _VECTOR_TABLES[target_type]
    except KeyError:
        raise StoreJobError(f"알 수 없는 대상 종류: {target_type}") from None


class PgEmbeddingStore:
    """PostgreSQL(pgvector) 구현. 작업자 연결은 지연 생성하고 오류가 나면 닫아 다음 호출 때 다시 연다.

    options: 시험에서 search_path 같은 접속 옵션을 줄 때(`-c search_path=…`)."""

    def __init__(self, url: str, *, options: str | None = None, clock: Callable[[], datetime] = utc_now):
        self._url = url
        self._options = options
        self._clock = clock
        self._conn = None

    # --- 연결 ---
    def _kwargs(self) -> dict:
        kw: dict = {"connect_timeout": 5}
        if self._options:
            kw["options"] = self._options
        return kw

    def _connect(self, **extra):
        import psycopg

        try:
            return psycopg.connect(self._url, **self._kwargs(), **extra)
        except psycopg.Error as e:
            # 오류 글에는 접속 값이 섞일 수 있어 종류만 남긴다
            raise StoreUnavailable(f"DB 에 접속하지 못했습니다({type(e).__name__})") from None

    def _worker_conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()  # autocommit 끔: 첫 문장에서 트랜잭션 시작, commit()/rollback() 으로 끝낸다
        return self._conn

    def commit(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.commit()

    def rollback(self) -> None:
        if self._conn is not None and not self._conn.closed:
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001 — 끊긴 연결이면 닫고 다음에 새로 연다
                self.close()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    # --- 작업 꺼내기 ---
    def _claim(self, sql: str, status: str, limit: int) -> list[Job]:
        cur = self._worker_conn().execute(sql, (status, limit))
        return [Job(int(r[0]), r[1], int(r[2]), r[3], str(r[4]).strip(), int(r[5]), int(r[6])) for r in cur.fetchall()]

    def claim_remove_jobs(self, limit: int) -> list[Job]:
        return self._claim(SQL_CLAIM_REMOVE, "REMOVE", limit)

    def claim_pending(self, limit: int) -> list[Job]:
        return self._claim(SQL_CLAIM_PENDING, "PENDING", limit)

    # --- 벡터 ---
    def current_vector(self, target_type: str, target_id: int) -> tuple[str, str] | None:
        table, col = _tbl(target_type)
        row = self._worker_conn().execute(SQL_CURRENT.format(table=table, col=col), (target_id,)).fetchone()
        return (str(row[0]).strip(), str(row[1])) if row else None

    def upsert_vector(self, target_type: str, target_id: int, text_hash: str, model: str, vector: Sequence[float]) -> None:
        import psycopg

        table, col = _tbl(target_type)
        conn = self._worker_conn()
        try:
            with conn.transaction():  # savepoint — 이 줄만 실패해도 묶음 트랜잭션은 살아 있다
                conn.execute(
                    SQL_UPSERT.format(table=table, col=col),
                    (target_id, text_hash, model, vector_literal(vector), self._clock()),
                )
        except (psycopg.errors.IntegrityError, psycopg.errors.DataError) as e:  # 예: 지워진 회원의 외래 키 위반·차원 불일치
            raise StoreJobError(f"벡터를 저장하지 못했습니다({type(e).__name__})") from None

    def delete_vector(self, target_type: str, target_id: int) -> None:
        table, col = _tbl(target_type)
        self._worker_conn().execute(SQL_DELETE_VECTOR.format(table=table, col=col), (target_id,))

    # --- 작업 표시 ---
    def delete_job(self, job_id: int) -> None:
        self._worker_conn().execute(SQL_DELETE_JOB, (job_id,))

    def mark_done(self, job_id: int) -> None:
        self._worker_conn().execute(SQL_DONE, (self._clock(), job_id))

    def mark_failed(self, job_id: int, attempts: int, error: str, final: bool) -> None:
        self._worker_conn().execute(SQL_FAILED, ("FAILED" if final else "PENDING", attempts, error[:500], self._clock(), job_id))

    # --- 순위 ---
    def rank(self, member_id: int, grant_ids: Sequence[int]) -> list[tuple[int, float]]:
        """(공고 id, 코사인 유사도) 목록 — 벡터가 없는 공고는 빠진다. 회원 소개 벡터가 없으면 빈 목록."""
        import psycopg

        ids = [int(g) for g in grant_ids]
        if not ids:
            return []
        try:
            with self._connect(autocommit=True) as conn:
                rows = conn.execute(SQL_RANK, (member_id, ids)).fetchall()
        except psycopg.Error as e:
            raise StoreUnavailable(f"순위를 조회하지 못했습니다({type(e).__name__})") from None
        return [(int(r[0]), float(r[1])) for r in rows]
