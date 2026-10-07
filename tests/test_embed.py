"""임베딩 서비스 시험.

- 기본: 진짜 모델 없이 가짜 모델을 끼워 서비스 계약(토큰·상한·모양·정규화·503)을 시험한다.
- EMBED_REAL=1 일 때만: 진짜 모델(.models 캐시의 multilingual-e5-small int8 ONNX)로 의미 순위를 시험한다.
    PowerShell:  $env:EMBED_REAL="1"; .venv\\Scripts\\python.exe -m pytest tests/test_embed.py"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.embed.model import EmbedModel, ModelLoadError, l2_normalize
from app.embed.passage_text import build_passage_text, condition_sentence
from app.main import EMBED_MAX_CHARS, EMBED_MAX_TEXTS, create_app
from app.settings import Settings
from tests.conftest import AUTH, TOKEN

ROOT = Path(__file__).resolve().parent.parent


class FakeModel:
    """글자 해시로 4차원 벡터를 만드는 가짜(정규화는 EmbedModel 몫이라 여기서는 일부러 안 한다 → 계약 시험은 EmbedModel 로)."""

    name = "fake-model"
    dim = 4

    def __init__(self):
        self.calls: list[tuple[list[str], str]] = []

    def embed(self, texts, kind):
        self.calls.append((list(texts), kind))
        out = []
        for t in texts:
            h = hashlib.sha256((kind + t).encode()).digest()
            out.append(l2_normalize([h[i] + 1.0 for i in range(4)]))
        return out


class BrokenModel:
    name = "broken"
    dim = 0

    def embed(self, texts, kind):
        raise ModelLoadError("임베딩 모델을 불러오지 못했습니다(FileNotFoundError)")


def _client(settings, model) -> TestClient:
    return TestClient(create_app(settings, embedder=model))


@pytest.fixture()
def fake():
    return FakeModel()


@pytest.fixture()
def ec(settings, fake):
    return _client(settings, fake)


# ── 토큰 ──


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}, {"X-Internal-Token": ""}])
def test_embed_requires_token(ec, fake, headers):
    r = ec.post("/internal/embed", json={"texts": ["가"], "kind": "passage"}, headers=headers)
    assert r.status_code == 401
    assert fake.calls == []  # 모델은 건드리지도 않는다


def test_embed_bad_body_without_token_is_401(ec):
    assert ec.post("/internal/embed", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 401


# ── 정상 ──


def test_embed_ok_contract(ec, fake):
    r = ec.post("/internal/embed", json={"texts": ["반려동물 앱", "제조 기업"], "kind": "passage"}, headers=AUTH)
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["model"] == "fake-model" and j["dim"] == 4
    assert len(j["vectors"]) == 2 and all(len(v) == 4 for v in j["vectors"])
    assert all(math.isclose(sum(x * x for x in v), 1.0, abs_tol=1e-6) for v in j["vectors"])
    assert fake.calls == [(["반려동물 앱", "제조 기업"], "passage")]


def test_embed_kind_query_and_deterministic(ec):
    a = ec.post("/internal/embed", json={"texts": ["같은 글"], "kind": "query"}, headers=AUTH).json()["vectors"]
    b = ec.post("/internal/embed", json={"texts": ["같은 글"], "kind": "query"}, headers=AUTH).json()["vectors"]
    c = ec.post("/internal/embed", json={"texts": ["같은 글"], "kind": "passage"}, headers=AUTH).json()["vectors"]
    assert a == b and a != c


def test_embed_limits_boundary_ok(ec):
    texts = ["가" * EMBED_MAX_CHARS] * EMBED_MAX_TEXTS
    r = ec.post("/internal/embed", json={"texts": texts, "kind": "passage"}, headers=AUTH)
    assert r.status_code == 200 and len(r.json()["vectors"]) == EMBED_MAX_TEXTS


# ── 상한·잘못된 요청 → 400 + permanent ──


@pytest.mark.parametrize(
    "body",
    [
        {"texts": ["가"] * (EMBED_MAX_TEXTS + 1), "kind": "passage"},  # 33개
        {"texts": ["가" * (EMBED_MAX_CHARS + 1)], "kind": "passage"},  # 8001자
        {"texts": [], "kind": "passage"},
        {"texts": ["가"], "kind": "other"},
        {"texts": ["가"]},
        {"texts": "가", "kind": "passage"},
        {"texts": [1, 2], "kind": "passage"},
        {"kind": "passage"},
        ["가"],
    ],
)
def test_embed_rejects_bad_request(ec, fake, body):
    r = ec.post("/internal/embed", json=body, headers=AUTH)
    assert r.status_code == 400
    j = r.json()
    assert j["ok"] is False and j["permanent"] is True and j["error"]
    assert fake.calls == []


def test_embed_rejects_broken_json(ec):
    r = ec.post("/internal/embed", content=b"{not json", headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["permanent"] is True


# ── 모델 로딩 실패 → 503 ──


def test_embed_model_load_failure_is_503(settings):
    r = _client(settings, BrokenModel()).post("/internal/embed", json={"texts": ["가"], "kind": "passage"}, headers=AUTH)
    assert r.status_code == 503
    j = r.json()
    assert j["ok"] is False and j["permanent"] is False and "모델" in j["error"]


def test_embed_real_model_missing_files_is_503(tmp_path):
    """설정의 진짜 모델 경로가 비어 있으면(파일 없음) 503 — 네트워크 접속 없이 확인."""
    s = Settings(token=TOKEN, embed_model=str(tmp_path))
    r = TestClient(create_app(s)).post("/internal/embed", json={"texts": ["가"], "kind": "passage"}, headers=AUTH)
    assert r.status_code == 503 and r.json()["permanent"] is False


# ── EmbedModel: 접두어·정규화·kind 검사 ──


def _stub_model(name="intfloat/multilingual-e5-small") -> tuple[EmbedModel, list[list[str]]]:
    m = EmbedModel(Settings(token=TOKEN, embed_model=name))
    seen: list[list[str]] = []

    def enc(texts):
        seen.append(list(texts))
        return [[3.0, 4.0, 0.0] for _ in texts]  # 정규화 안 된 벡터

    m._encode, m._dim, m._loaded = enc, 3, True
    return m, seen


def test_e5_prefix_by_kind():
    m, seen = _stub_model()
    m.embed(["가", "나"], "passage")
    m.embed(["다"], "query")
    assert seen == [["passage: 가", "passage: 나"], ["query: 다"]]


def test_no_prefix_for_non_e5():
    m, seen = _stub_model("jhgan/ko-sroberta-multitask")
    m.embed(["가"], "passage")
    assert seen == [["가"]]


def test_embed_output_is_l2_normalized_floats():
    m, _ = _stub_model()
    v = m.embed(["가"], "passage")[0]
    assert v == pytest.approx([0.6, 0.8, 0.0]) and all(isinstance(x, float) for x in v)
    assert math.isclose(sum(x * x for x in v), 1.0)


def test_embed_rejects_unknown_kind():
    m, _ = _stub_model()
    with pytest.raises(ValueError):
        m.embed(["가"], "doc")


def test_l2_normalize_zero_vector_stays_zero():
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("EMBED_THREADS", "2")
    monkeypatch.setenv("EMBED_BATCH", "4")
    monkeypatch.setenv("EMBED_MODEL", "x/y")
    s = Settings.from_env()
    assert (s.embed_threads, s.embed_batch, s.embed_model, s.embed_backend) == (2, 4, "x/y", "onnx")
    monkeypatch.delenv("EMBED_THREADS")
    monkeypatch.delenv("EMBED_BATCH")
    monkeypatch.delenv("EMBED_MODEL")
    s = Settings.from_env()
    assert (s.embed_threads, s.embed_batch, s.embed_model) == (1, 1, "intfloat/multilingual-e5-small")


# ── 공고 글 만들기 ──


def test_passage_text_format_and_empty_fields():
    t = build_passage_text(
        "사업", "중소기업", "기술 > 사업화", ["서울", "경기"], "요약 글",
        [("AGE_MAX", 39), ("INDUSTRY", ["ICT"]), ("OTHER", "체납 중인 자는 제외된다."), ("COMMON", "TAX")],
    )
    lines = t.split("\n")
    assert lines[0] == "[사업명: 사업] [지원대상: 중소기업] [분야: 기술 > 사업화] [지역: 서울, 경기]"
    assert lines[1:4] == ["사업", "중소기업", "요약 글"]
    assert "만 39세 이하" in lines and "업종: ICT" in lines and "체납 중인 자는 제외된다." in lines
    e = build_passage_text("사업", None, "", [], "", [("OTHER", "")])
    assert e.split("\n") == ["[사업명: 사업]", "사업"]  # 빈 필드는 줄·항목을 뺀다
    assert len(build_passage_text("가" * 3000, None, None, None, None)) <= 900


def test_condition_sentence_unknown_kind_is_empty():
    assert condition_sentence("???", "x") == ""


# ── 진짜 모델 (EMBED_REAL=1 일 때만) ──

real = pytest.mark.skipif(os.environ.get("EMBED_REAL") != "1", reason="EMBED_REAL=1 일 때만(모델 캐시 .models 필요)")


@pytest.fixture(scope="module")
def real_model():
    os.environ.setdefault("HF_HOME", str(ROOT / ".models"))
    return EmbedModel(Settings(token=TOKEN))


@real
def test_real_model_dim_norm_and_service(real_model, settings):
    c = _client(settings, real_model)
    r = c.post("/internal/embed", json={"texts": ["반려동물 앱을 만드는 회사"], "kind": "query"}, headers=AUTH).json()
    assert r["ok"] and r["dim"] == 384 and r["model"] == "intfloat/multilingual-e5-small"
    assert math.isclose(sum(x * x for x in r["vectors"][0]), 1.0, abs_tol=1e-4)


@real
def test_real_model_pet_company_closer_to_pet_grants(real_model):
    """회사 소개(반려동물 앱)가 반려동물 공고를 무관한 공고보다 더 가깝게 본다(평가 세트의 같은 글 사용)."""
    ev = ROOT / "tools" / "eval"
    grants = {g["id"]: g for g in json.loads((ev / "grants_50.json").read_text(encoding="utf-8"))}
    comp = next(c for c in json.loads((ev / "companies.json").read_text(encoding="utf-8")) if c["key"] == "pet")
    pet_ids, other_ids = [325, 190], [1219, 1106, 1022, 897]  # 반려동물 2건, 특례보증·10시 출근제·방사광가속기·소공인센터
    q = real_model.embed([comp["text"]], "query")[0]
    ps = real_model.embed([grants[i]["text"] for i in pet_ids + other_ids], "passage")
    sims = [sum(a * b for a, b in zip(q, p)) for p in ps]
    assert min(sims[:2]) > max(sims[2:]), sims
