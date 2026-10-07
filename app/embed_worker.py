"""`python -m app.embed_worker` — 임베딩 작업자(서비스 프로세스와 별개). 큐(embedding_jobs)를 작은 묶음씩 처리한다.

  python -m app.embed_worker           상시 실행: 묶음을 되풀이한다. SIGTERM·SIGINT 를 받으면 현재 작업을 마치고 종료
  python -m app.embed_worker --once    한 번 실행: EMBED_WORKER_RUN_SECONDS(기본 600초) 안에서 처리하고 종료(cron·timer 용)

필요한 환경 변수: GRANTMATCHPY_DB_URL(필수), EMBED_*·EMBED_WORKER_*(settings.py 참고). 접속 값은 로그에 남기지 않는다."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading

from .settings import Settings, load_dotenv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.embed_worker", description="임베딩 작업자")
    ap.add_argument("--once", action="store_true", help="시간 상한 안에서 한 번 돌고 종료")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("grantmatchpy.embed_worker")

    load_dotenv()
    s = Settings.from_env()
    if not s.db_url:
        log.error("환경 변수 GRANTMATCHPY_DB_URL 이 없어 시작하지 않습니다.")
        return 2

    # 스레드 수 제한: onnxruntime intra-op 스레드는 EMBED_THREADS(기본 1) 로 model.py 가 정하고, 숫자 라이브러리 스레드도 같이 묶는다
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(k, str(s.embed_threads))

    from .embed.model import get_model
    from .embed.store import PgEmbeddingStore
    from .embed.worker import EmbedWorker

    store = PgEmbeddingStore(s.db_url)
    embedder = get_model(s)  # 모델은 처음 임베딩할 때 올라간다(지연 로딩)

    if args.once:
        w = EmbedWorker(store, embedder, s)
        try:
            w.run_once()
        except Exception as e:  # noqa: BLE001
            log.error("임베딩 한 번 실행 중 오류(%s)", type(e).__name__)  # 접속 값이 섞일 수 있어 종류만
            return 1
        finally:
            store.close()
        return 0

    stop = threading.Event()

    def _stop(signum, _frame):
        log.info("종료 신호를 받았습니다 — 현재 작업을 마치고 종료합니다.")
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    w = EmbedWorker(store, embedder, s, sleep=lambda sec: stop.wait(sec))  # 쉬는 중에도 신호에 바로 깬다
    try:
        w.run_forever(stop.is_set)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
