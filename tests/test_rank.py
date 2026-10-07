"""POST /internal/rank 시험 — 가짜 저장소(DB 없이). 순위 조회는 임베딩 모델을 올리지 않는다."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.embed.store import VECTOR_DIM
from app.main import RANK_MAX_GRANTS, create_app
from tests.conftest import AUTH
from tests.fakes import FakeEmbedder, FakeStore


def unit(i: int) -> list[float]:
    v = [0.0] * VECTOR_DIM
    v[i] = 1.0
    return v


def mix(a: int, b: int, wa: float) -> list[float]:
    v = [0.0] * VECTOR_DIM
    v[a], v[b] = wa, 1 - wa
    return v


@pytest.fixture()
def store():
    s = FakeStore()
    s.put_vector("MEMBER", 1, unit(0))
    s.put_vector("GRANT", 10, mix(0, 1, 0.3))  # 중간
    s.put_vector("GRANT", 11, unit(0))  # 가장 비슷(1.0)
    s.put_vector("GRANT", 12, unit(2))  # 무관(0)
    s.put_vector("GRANT", 13, [-x for x in unit(0)])  # 반대(-1 → 0 으로 자른다)
    return s


@pytest.fixture()
def rc(settings, store):
    return TestClient(create_app(settings, store=store))


def rank(rc, body, headers=AUTH):
    return rc.post("/internal/rank", json=body, headers=headers)


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}])
def test_rank_requires_token(rc, headers):
    assert rank(rc, {"memberId": 1, "grantIds": [10]}, headers).status_code == 401


def test_rank_bad_json_without_token_is_401(rc):
    r = rc.post("/internal/rank", content=b"{no", headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_rank_sorted_desc_clamped_0_to_1(rc):
    j = rank(rc, {"memberId": 1, "grantIds": [12, 10, 13, 11]}).json()
    assert j["ok"] is True
    assert [s["grantId"] for s in j["scores"]] == [11, 10, 12, 13]
    sc = {s["grantId"]: s["score"] for s in j["scores"]}
    assert sc[11] == pytest.approx(1.0) and 0.0 < sc[10] < 1.0 and sc[12] == pytest.approx(0.0)
    assert sc[13] == 0.0  # 음수는 0 으로
    assert all(0.0 <= v <= 1.0 for v in sc.values())


def test_rank_ties_broken_by_grant_id(rc, store):
    store.put_vector("GRANT", 5, unit(0))
    ids = [s["grantId"] for s in rank(rc, {"memberId": 1, "grantIds": [11, 5]}).json()["scores"]]
    assert ids == [5, 11]


def test_rank_omits_grants_without_vector(rc):
    j = rank(rc, {"memberId": 1, "grantIds": [10, 999, 11]}).json()
    assert [s["grantId"] for s in j["scores"]] == [11, 10]


def test_rank_member_without_intro_vector_is_empty_not_error(rc):
    r = rank(rc, {"memberId": 2, "grantIds": [10, 11]})
    assert r.status_code == 200 and r.json() == {"ok": True, "scores": []}


def test_rank_empty_grant_ids_does_not_touch_store(settings):
    class Boom(FakeStore):
        def rank(self, *a):
            raise AssertionError("부르면 안 됨")

    r = rank(TestClient(create_app(settings, store=Boom())), {"memberId": 1, "grantIds": []})
    assert r.status_code == 200 and r.json() == {"ok": True, "scores": []}


def test_rank_dedupes_grant_ids(rc):
    j = rank(rc, {"memberId": 1, "grantIds": [11, 11, 10, 10]}).json()
    assert [s["grantId"] for s in j["scores"]] == [11, 10]


def test_rank_limit_boundary(rc):
    ok = rank(rc, {"memberId": 1, "grantIds": list(range(1, RANK_MAX_GRANTS + 1))})
    assert ok.status_code == 200
    over = rank(rc, {"memberId": 1, "grantIds": list(range(1, RANK_MAX_GRANTS + 2))})
    assert over.status_code == 400
    j = over.json()
    assert j["ok"] is False and j["permanent"] is True and "5000" in j["error"]


@pytest.mark.parametrize(
    "body",
    [
        {"grantIds": [1]},
        {"memberId": "1", "grantIds": [1]},
        {"memberId": True, "grantIds": [1]},
        {"memberId": 1},
        {"memberId": 1, "grantIds": "1"},
        {"memberId": 1, "grantIds": [1, "2"]},
        {"memberId": 1, "grantIds": [1.5]},
        [1, 2],
    ],
)
def test_rank_rejects_bad_body(rc, body):
    r = rank(rc, body)
    assert r.status_code == 400 and r.json()["permanent"] is True


def test_rank_broken_json_is_400(rc):
    r = rc.post("/internal/rank", content=b"{no", headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["permanent"] is True


def test_rank_db_error_is_503_not_permanent(settings, store):
    store.unavailable = True
    r = rank(TestClient(create_app(settings, store=store)), {"memberId": 1, "grantIds": [10]})
    assert r.status_code == 503
    j = r.json()
    assert j["ok"] is False and j["permanent"] is False and j["error"]


def test_rank_without_db_url_is_503(settings):
    assert settings.db_url == ""
    r = rank(TestClient(create_app(settings)), {"memberId": 1, "grantIds": [10]})
    assert r.status_code == 503 and r.json()["permanent"] is False


def test_rank_never_loads_embedding_model(settings, store):
    class Exploding:
        name = "x"

        @property
        def dim(self):
            raise AssertionError("모델을 올리면 안 됨")

        def embed(self, *a):
            raise AssertionError("모델을 올리면 안 됨")

    c = TestClient(create_app(settings, embedder=Exploding(), store=store))
    assert rank(c, {"memberId": 1, "grantIds": [10]}).status_code == 200


def test_embed_endpoint_still_works_alongside_rank(settings, store):
    c = TestClient(create_app(settings, embedder=FakeEmbedder(), store=store))
    r = c.post("/internal/embed", json={"texts": ["가"], "kind": "passage"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["dim"] == VECTOR_DIM
