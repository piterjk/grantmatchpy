"""GRANTMATCHPY_MODE 시험 — 모드별 라우트 등록(없는 경로 404, 있는 경로는 토큰 401/정상 200)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import MODE_ROUTES, create_app
from app.settings import Settings
from tests.conftest import AUTH, TOKEN
from tests.fakes import FakeEmbedder, FakeStore

BODIES = {
    "/internal/attachments/fetch": {"url": "https://x.example/a", "name": "a"},
    "/internal/attachments/extract": {"storedPath": "../x"},
    "/internal/embed": {"texts": ["가"], "kind": "passage"},
    "/internal/rank": {"memberId": 1, "grantIds": [1]},
}


def client(mode, storage):
    s = Settings(token=TOKEN, storage=str(storage), mode=mode)
    return TestClient(create_app(s, embedder=FakeEmbedder(), store=FakeStore()))


@pytest.mark.parametrize("mode", ["all", "api", "attach"])
def test_routes_by_mode(mode, storage):
    c = client(mode, storage)
    assert c.get("/internal/health").status_code == 200
    for path, body in BODIES.items():
        if path in MODE_ROUTES[mode]:
            assert c.post(path, json=body).status_code == 401, path  # 있다 → 토큰 검사
            assert c.post(path, json=body, headers=AUTH).status_code != 404, path
        else:
            assert c.post(path, json=body).status_code == 404, path  # 없다 → 토큰 검사 전에 404
            assert c.post(path, json=body, headers=AUTH).status_code == 404, path


def test_mode_contents():
    assert {"/internal/embed", "/internal/rank"} <= MODE_ROUTES["api"]
    assert not any("attachments" in p for p in MODE_ROUTES["api"])
    assert not ({"/internal/embed", "/internal/rank"} & MODE_ROUTES["attach"])
    assert len(MODE_ROUTES["all"]) == 5


def test_attach_mode_never_touches_model_or_db(storage):
    s = Settings(token=TOKEN, storage=str(storage), mode="attach")  # 모델·DB 설정 없음
    c = TestClient(create_app(s))
    assert c.post("/internal/embed", json=BODIES["/internal/embed"], headers=AUTH).status_code == 404
    assert c.post("/internal/rank", json=BODIES["/internal/rank"], headers=AUTH).status_code == 404


def test_api_ok_responses(storage):
    c = client("api", storage)
    assert c.post("/internal/embed", json=BODIES["/internal/embed"], headers=AUTH).status_code == 200
    assert c.post("/internal/rank", json=BODIES["/internal/rank"], headers=AUTH).status_code == 200


def test_mode_from_env(monkeypatch):
    assert Settings.from_env().mode == "all"
    monkeypatch.setenv("GRANTMATCHPY_MODE", "API")
    assert Settings.from_env().mode == "api"
    monkeypatch.setenv("GRANTMATCHPY_MODE", "x")
    with pytest.raises(RuntimeError):
        Settings.from_env()
