"""FastAPI 서비스 — 127.0.0.1 전용. 모든 /internal/* 요청은 X-Internal-Token 헤더가 GRANTMATCHPY_TOKEN 과 같아야 한다.

  POST /internal/attachments/fetch    {"url","name"}     → 첨부 내려받기
  POST /internal/attachments/extract  {"storedPath"}     → 저장된 첨부에서 글자 추출
  POST /internal/embed                {"texts","kind"}   → 문장 임베딩(kind = passage | query, 한 번에 최대 32개·각 8000자)
  POST /internal/rank                 {"memberId","grantIds"} → 회원 소개와 공고의 유사도 순위(벡터 표만 읽는다, 한 번에 최대 5000개)
  GET  /internal/health                                  → {"status":"ok"} (토큰 필요 없음)

실행: python -m app  (또는 uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8310)"""

from __future__ import annotations

import hmac
import os
import re
import threading
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .embed.model import Embedder, ModelLoadError, get_model
from .embed.store import EmbeddingStore, StoreUnavailable
from .extract import ExtractError, sandbox
from .fetch import Downloader, FetchError
from .settings import Settings, load_dotenv

# PHP GrantTextService 와 같은 기준: 다시 해도 같은 실패 → 바로 SKIPPED
_FETCH_PERMANENT = re.compile(r"너무 큽니다|허용 목록|허용하지 않는")
_EXTRACT_PERMANENT = re.compile(r"지원하지 않는 형식|암호|배포용|빈 파일|너무 큼|너무 많")


def fetch_permanent(msg: str) -> bool:
    return bool(_FETCH_PERMANENT.search(msg))


def extract_permanent(msg: str) -> bool:
    return bool(_EXTRACT_PERMANENT.search(msg))


class FetchRequest(BaseModel):
    url: str
    name: str = ""


class ExtractRequest(BaseModel):
    storedPath: str


EMBED_MAX_TEXTS = 32  # 한 번에 보낼 수 있는 글 수
EMBED_MAX_CHARS = 8000  # 글 하나의 최대 글자 수
RANK_MAX_GRANTS = 5000  # /internal/rank 한 번에 받는 공고 id 최대 수

_HEALTH = "/internal/health"
_FETCH, _EXTRACT = "/internal/attachments/fetch", "/internal/attachments/extract"
_EMBED, _RANK = "/internal/embed", "/internal/rank"
_ALL_ROUTES = {_HEALTH, _FETCH, _EXTRACT, _EMBED, _RANK}
MODE_ROUTES = {  # all=모두(기본) · api=순위·임베딩(모델·DB) · attach=첨부 내려받기·추출(모델·DB 접속 안 함)
    "all": _ALL_ROUTES,
    "api": {_HEALTH, _EMBED, _RANK},
    "attach": {_HEALTH, _FETCH, _EXTRACT},
}

_JSON_ERROR_PATHS =("/internal/embed", "/internal/rank")  # 깨진 JSON 도 같은 400 모양으로 답하는 경로


def validate_rank_body(body: object) -> str | None:
    """/internal/rank 요청 본문 검사. 문제가 있으면 한국어 이유를, 없으면 None."""
    if not isinstance(body, dict):
        return "요청 본문은 {memberId, grantIds} 객체여야 합니다."

    def is_int(v: object) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)

    if not is_int(body.get("memberId")):
        return "memberId 는 정수여야 합니다."
    ids = body.get("grantIds")
    if not isinstance(ids, list) or not all(is_int(i) for i in ids):
        return "grantIds 는 정수 목록이어야 합니다."
    if len(ids) > RANK_MAX_GRANTS:
        return f"grantIds 는 최대 {RANK_MAX_GRANTS}개입니다({len(ids)}개)."
    return None


def validate_embed_body(body: object) -> str | None:
    """/internal/embed 요청 본문 검사. 문제가 있으면 한국어 이유를, 없으면 None."""
    if not isinstance(body, dict):
        return "요청 본문은 {texts, kind} 객체여야 합니다."
    if body.get("kind") not in ("passage", "query"):
        return "kind 는 passage 또는 query 여야 합니다."
    texts = body.get("texts")
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
        return "texts 는 글(문자열) 목록이어야 합니다."
    if len(texts) > EMBED_MAX_TEXTS:
        return f"한 번에 보낼 수 있는 글은 최대 {EMBED_MAX_TEXTS}개입니다({len(texts)}개)."
    if any(len(t) > EMBED_MAX_CHARS for t in texts):
        return f"글 하나는 최대 {EMBED_MAX_CHARS}자입니다."
    return None


def resolve_stored_path(storage: str, stored: str) -> str:
    """저장소 기준 상대 경로를 실제 경로로. 저장소 밖으로 나가는 경로(.. · 절대 경로 · 심볼릭 링크)는 거부."""
    bad = ValueError("저장소 밖으로 나가는 경로는 쓸 수 없습니다.")
    if (
        stored == ""
        or "\0" in stored
        or "\\" in stored
        or stored.startswith("/")
        or re.match(r"^[A-Za-z]:", stored)
        or any(seg in ("", ".", "..") for seg in stored.split("/"))
    ):
        raise bad
    root = os.path.realpath(storage)
    full = os.path.realpath(os.path.join(root, *stored.split("/")))
    if os.path.commonpath([root, full]) != root or full == root:
        raise bad
    return full


def create_app(settings: Settings | None = None, embedder: Embedder | None = None, store: EmbeddingStore | None = None) -> FastAPI:
    """embedder·store 는 시험에서 가짜를 끼우는 자리. 비우면 설정의 진짜 모델(EMBED_*)이 처음 쓸 때 올라가고,
    저장소는 GRANTMATCHPY_DB_URL 이 있을 때 /internal/rank 가 처음 불릴 때 만든다(순위 조회는 임베딩 모델을 올리지 않는다)."""
    if settings is None:
        load_dotenv()
        settings = Settings.from_env()
    if not settings.token:
        # 토큰 없이 열린 내부 API 는 만들지 않는다
        raise RuntimeError("환경 변수 GRANTMATCHPY_TOKEN 이 없어 시작하지 않습니다.")
    token_b = settings.token.encode("utf-8")
    sem = threading.BoundedSemaphore(settings.extract_concurrency)  # 추출 프로세스를 한꺼번에 많이 띄우지 않는다
    downloader = Downloader(settings)

    app = FastAPI(title="grantmatchpy", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.downloader = downloader
    app.state.embedder = embedder
    app.state.store = store
    store_lock = threading.Lock()

    def get_store() -> EmbeddingStore:
        with store_lock:
            if app.state.store is None:
                if not settings.db_url:
                    raise StoreUnavailable("DB 접속 설정(GRANTMATCHPY_DB_URL)이 없습니다.")
                from .embed.store import PgEmbeddingStore  # psycopg 는 필요할 때만 불러온다

                app.state.store = PgEmbeddingStore(settings.db_url)
            return app.state.store

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        if request.url.path in _JSON_ERROR_PATHS:  #깨진 JSON 등도 같은 400 모양으로(토큰이 틀리면 먼저 401)
            got = (request.headers.get("x-internal-token") or "").encode("latin-1", errors="replace")
            if not hmac.compare_digest(got, token_b):
                return JSONResponse(status_code=401, content={"detail": "unauthorized"})
            return JSONResponse(status_code=400, content={"ok": False, "error": "요청 본문이 올바른 JSON 이 아닙니다.", "permanent": True})
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    def require_token(x_internal_token: str | None = Header(default=None)) -> None:
        got = (x_internal_token or "").encode("latin-1", errors="replace")  # 헤더는 latin-1 로 풀려 온다
        if not hmac.compare_digest(got, token_b):  # 상수 시간 비교
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/internal/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/internal/attachments/fetch", dependencies=[Depends(require_token)])
    def fetch(req: FetchRequest) -> dict:
        try:
            f = downloader.fetch(req.url, req.name)
        except FetchError as e:
            msg = str(e)
            return {"ok": False, "error": msg, "permanent": fetch_permanent(msg)}
        return {"ok": True, "sha256": f.sha256, "size": f.size, "contentType": f.content_type, "storedPath": f.stored_path}

    @app.post("/internal/attachments/extract", dependencies=[Depends(require_token)])
    def extract(req: ExtractRequest):
        try:
            full = resolve_stored_path(settings.storage, req.storedPath)
        except ValueError as e:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(e), "permanent": True})
        try:
            with sem:
                r = sandbox.extract(full, timeout_sec=settings.extract_timeout, memory_mb=settings.extract_memory_mb)
        except ExtractError as e:
            msg = str(e)
            return {"ok": False, "error": msg, "permanent": extract_permanent(msg)}
        return {"ok": True, **r}

    @app.post("/internal/embed", dependencies=[Depends(require_token)])
    def embed(body: Any = Body(...)):
        err = validate_embed_body(body)
        if err:
            return JSONResponse(status_code=400, content={"ok": False, "error": err, "permanent": True})
        try:
            model = app.state.embedder or get_model(settings)
            vectors = model.embed(body["texts"], body["kind"])
            dim = model.dim
        except ModelLoadError as e:
            return JSONResponse(status_code=503, content={"ok": False, "error": str(e), "permanent": False})
        return {"ok": True, "model": model.name, "dim": dim, "vectors": vectors}

    @app.post("/internal/rank", dependencies=[Depends(require_token)])
    def rank(body: Any = Body(...)):
        """회원 소개와 공고의 유사도(0~1, 높은 순). 벡터가 없는 공고는 빠지고, 소개 벡터가 없으면 빈 목록(오류 아님).
        DB 에 못 닿으면 503 — Java 는 이때 유사도 없이 정렬한다."""
        err = validate_rank_body(body)
        if err:
            return JSONResponse(status_code=400, content={"ok": False, "error": err, "permanent": True})
        ids = list(dict.fromkeys(body["grantIds"]))  # 중복 제거(순서 유지)
        if not ids:
            return {"ok": True, "scores": []}
        try:
            rows = get_store().rank(body["memberId"], ids)
        except StoreUnavailable as e:
            return JSONResponse(status_code=503, content={"ok": False, "error": str(e), "permanent": False})
        scores = [{"grantId": g, "score": min(1.0, max(0.0, float(s)))} for g, s in rows]
        scores.sort(key=lambda x: (-x["score"], x["grantId"]))
        return {"ok": True, "scores": scores}

    # 서비스 모드(GRANTMATCHPY_MODE): 모드에 없는 엔드포인트는 등록하지 않아 토큰 검사 전에 404 가 난다
    allowed = MODE_ROUTES[settings.mode]
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) not in _ALL_ROUTES or r.path in allowed]
    return app
