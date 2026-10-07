"""설정은 환경 변수로 (`.env` 가 있으면 읽되, 이미 있는 환경 변수를 덮어쓰지 않는다). 비밀 값은 코드·저장소에 넣지 않는다."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v and v.strip() else default


def _list(name: str, default: list[str]) -> list[str]:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return list(default)
    return [x.strip().lower() for x in v.split(",") if x.strip()]


@dataclass
class Settings:
    token: str = ""
    storage: str = "./storage/attachments"
    port: int = 8310
    mode: str = "all"  # GRANTMATCHPY_MODE: all(모두) | api(health·rank·embed) | attach(health·첨부 내려받기·추출)
    # 내려받기
    allow_http: bool = False
    allowed_hosts: list[str] = field(default_factory=lambda: ["bizinfo.go.kr", "k-startup.go.kr"])
    allowed_ports: list[int] = field(default_factory=lambda: [80, 443])
    allow_private_ip: bool = False  # 시험용: 켜면 사설·내부 IP 도 허용(운영에서는 끈다)
    fetch_timeout: int = 120
    ca_bundle: str = ""  # 사설 인증서 시험용(비우면 기본 신뢰 목록)
    fetch_max_bytes: int = 30_000_000
    user_agent: str = "jiwonnavi-collector/1.0"
    # 글자 추출
    extract_timeout: int = 120
    extract_memory_mb: int = 1024
    extract_concurrency: int = 2
    # 임베딩
    embed_model: str = "intfloat/multilingual-e5-small"  # Hugging Face 저장소 이름 또는 모델 폴더 경로
    embed_backend: str = "onnx"  # onnx(기본, 메모리 약 550MB) | torch(약 1.3GB)
    embed_onnx_file: str = "onnx/model_qint8_avx512_vnni.onnx"
    embed_threads: int = 1
    embed_batch: int = 1  # 배치가 클수록 패딩 낭비·메모리가 늘고 int8 벡터가 배치 구성에 따라 조금씩 달라진다(README 참조)
    embed_max_tokens: int = 512
    # 임베딩 작업자·순위(PostgreSQL + pgvector). 접속 값은 환경 변수로만 받는다(코드·로그에 남기지 않는다)
    db_url: str = ""  # GRANTMATCHPY_DB_URL — psycopg 접속 문자열. 비어 있으면 /internal/rank 는 503, 작업자는 시작하지 않는다
    embed_worker_batch_day: int = 4  # 낮(밤 시간대 밖)에 한 묶음에서 꺼낼 작업 수 — 작게 해서 다른 서비스를 방해하지 않는다
    embed_worker_batch_night: int = 16  # 밤(한국 시간)에는 크게 — 사람이 적은 시간에 밀린 공고를 처리
    night_start_hour: int = 1  # 밤 시간대 시작(한국 시간, 포함)
    night_end_hour: int = 6  # 밤 시간대 끝(한국 시간, 미포함)
    embed_worker_sleep_seconds: int = 5  # 묶음 사이 쉬는 시간
    embed_worker_idle_seconds: int = 30  # 처리할 일이 없을 때(또는 모델·DB 문제로 멈췄을 때) 대기
    embed_worker_run_seconds: int = 600  # --once 로 실행할 때 한 번 실행의 시간 상한

    @staticmethod
    def from_env() -> "Settings":
        s = Settings()
        s.token = os.environ.get("GRANTMATCHPY_TOKEN", "")
        s.storage = os.environ.get("GRANTMATCHPY_STORAGE", s.storage)
        s.port = _int("GRANTMATCHPY_PORT", s.port)
        s.mode = (os.environ.get("GRANTMATCHPY_MODE", "").strip() or s.mode).lower()
        if s.mode not in ("all", "api", "attach"):
            raise RuntimeError(f"GRANTMATCHPY_MODE 는 all|api|attach 여야 합니다: {s.mode}")
        s.allow_http = _bool("ALLOW_HTTP", s.allow_http)
        s.allowed_hosts = _list("ALLOWED_HOSTS", s.allowed_hosts)
        s.allowed_ports = [int(p) for p in _list("ALLOWED_PORTS", [str(p) for p in s.allowed_ports])]
        s.allow_private_ip = _bool("FETCH_ALLOW_PRIVATE_IP", s.allow_private_ip)
        s.fetch_timeout = _int("FETCH_TIMEOUT", s.fetch_timeout)
        s.ca_bundle = os.environ.get("FETCH_CA_BUNDLE", "")
        s.fetch_max_bytes = _int("FETCH_MAX_BYTES", s.fetch_max_bytes)
        s.extract_timeout = _int("EXTRACT_TIMEOUT", s.extract_timeout)
        s.extract_memory_mb = _int("EXTRACT_MEMORY_MB", s.extract_memory_mb)
        s.extract_concurrency = max(1, _int("EXTRACT_CONCURRENCY", s.extract_concurrency))
        s.embed_model = os.environ.get("EMBED_MODEL", "").strip() or s.embed_model
        s.embed_backend = (os.environ.get("EMBED_BACKEND", "").strip() or s.embed_backend).lower()
        s.embed_onnx_file = os.environ.get("EMBED_ONNX_FILE", "").strip() or s.embed_onnx_file
        s.embed_threads = max(1, _int("EMBED_THREADS", s.embed_threads))
        s.embed_batch = max(1, _int("EMBED_BATCH", s.embed_batch))
        s.embed_max_tokens = max(8, _int("EMBED_MAX_TOKENS", s.embed_max_tokens))
        s.db_url = os.environ.get("GRANTMATCHPY_DB_URL", "").strip()
        s.embed_worker_batch_day = max(1, _int("EMBED_WORKER_BATCH_DAY", s.embed_worker_batch_day))
        s.embed_worker_batch_night = max(1, _int("EMBED_WORKER_BATCH_NIGHT", s.embed_worker_batch_night))
        s.night_start_hour = _int("NIGHT_START_HOUR", s.night_start_hour) % 24
        s.night_end_hour = _int("NIGHT_END_HOUR", s.night_end_hour) % 24
        s.embed_worker_sleep_seconds = max(0, _int("EMBED_WORKER_SLEEP_SECONDS", s.embed_worker_sleep_seconds))
        s.embed_worker_idle_seconds = max(1, _int("EMBED_WORKER_IDLE_SECONDS", s.embed_worker_idle_seconds))
        s.embed_worker_run_seconds = max(1, _int("EMBED_WORKER_RUN_SECONDS", s.embed_worker_run_seconds))
        return s
