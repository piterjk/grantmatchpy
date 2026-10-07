"""`python -m app` — 127.0.0.1 에만 연다(주소는 바꿀 수 없다). 포트는 GRANTMATCHPY_PORT(기본 8310)."""

import uvicorn

from .main import create_app
from .settings import Settings, load_dotenv


def main() -> None:
    load_dotenv()
    s = Settings.from_env()
    app = create_app(s)  # 토큰이 없으면 여기서 멈춘다
    uvicorn.run(app, host="127.0.0.1", port=s.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
