"""
tests/test_dashboard_api.py — FastAPI app factory §14.5.6.

Endpoints testés via TestClient :
- GET /healthz
- GET /costs.json
- POST /validate/{proposal_id}
- POST /reject/{proposal_id}

Tests sans réseau : `CostRepository(:memory:)` + callbacks in-memory.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.dashboards.api import create_app
from src.dashboards.cost_repo import CostRepository
from src.dashboards.pricing import LLMLimits, ModelPricing
from src.memory.db import init_db


@pytest.fixture
def con():
    c = init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def pricing():
    return ModelPricing(
        rates={"claude-sonnet-4-6": (3.0, 15.0)},
        last_updated=date.today(),
    )


@pytest.fixture
def pricing_stale():
    return ModelPricing(
        rates={"claude-sonnet-4-6": (3.0, 15.0)},
        last_updated=date(2020, 1, 1),
    )


@pytest.fixture
def repo(con, pricing):
    return CostRepository(con, pricing, LLMLimits())


@pytest.fixture
def validated_store():
    return []


@pytest.fixture
def rejected_store():
    return []


@pytest.fixture
def client(repo, validated_store, rejected_store):
    known_ids = {"tp_known"}

    def on_val(pid: str, reason):
        if pid not in known_ids:
            return False
        validated_store.append((pid, reason))
        return True

    def on_rej(pid: str, reason):
        if pid not in known_ids:
            return False
        rejected_store.append((pid, reason))
        return True

    app = create_app(cost_repo=repo, on_validate=on_val, on_reject=on_rej)
    return TestClient(app)


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["pricing_stale"] is False


def test_healthz_pricing_stale_reported(con, pricing_stale):
    repo = CostRepository(con, pricing_stale, LLMLimits())
    app = create_app(cost_repo=repo)
    c = TestClient(app)
    body = c.get("/healthz").json()
    assert body["pricing_stale"] is True


# ---------------------------------------------------------------------------
# /costs.json
# ---------------------------------------------------------------------------


def test_costs_json_empty_db(client):
    r = client.get("/costs.json")
    assert r.status_code == 200
    body = r.json()
    assert body["tokens_today"] == 0
    assert body["cost_month_usd"] == 0.0
    # CostPanel.to_dict() remplace +inf par None → JSON-safe
    assert body["source_data_lag_seconds"] is None
    # Alerte no_llm_data présente
    codes = {a["code"] for a in body["alerts"]}
    assert "no_llm_data" in codes


def test_costs_json_structure(client):
    body = client.get("/costs.json").json()
    expected_keys = {
        "tokens_today", "tokens_daily_budget", "cost_month_usd",
        "cost_month_budget_usd", "forecast_month_usd", "by_agent",
        "by_model", "by_api_source", "trend_30d", "top_consumers",
        "pricing_last_updated", "alerts", "computed_at",
        "source_data_lag_seconds",
    }
    assert expected_keys.issubset(body.keys())


# ---------------------------------------------------------------------------
# /validate
# ---------------------------------------------------------------------------


def test_validate_known(client, validated_store):
    r = client.post("/validate/tp_known")
    assert r.status_code == 200
    assert r.json() == {"status": "validated", "proposal_id": "tp_known"}
    assert validated_store == [("tp_known", None)]


def test_validate_unknown_returns_404(client):
    r = client.post("/validate/tp_missing")
    assert r.status_code == 404
    assert r.json()["detail"] == "proposal_not_found"


def test_validate_with_reason_body(client, validated_store):
    r = client.post("/validate/tp_known", json={"reason": "ok pour moi"})
    assert r.status_code == 200
    assert validated_store[0] == ("tp_known", "ok pour moi")


def test_validate_tolerant_to_empty_body(client):
    r = client.post("/validate/tp_known")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /reject
# ---------------------------------------------------------------------------


def test_reject_known(client, rejected_store):
    r = client.post("/reject/tp_known")
    assert r.status_code == 200
    assert r.json() == {"status": "rejected", "proposal_id": "tp_known"}
    assert rejected_store == [("tp_known", None)]


def test_reject_unknown_returns_404(client):
    r = client.post("/reject/tp_xxx")
    assert r.status_code == 404


def test_reject_with_reason(client, rejected_store):
    r = client.post("/reject/tp_known", json={"reason": "bad RR"})
    assert r.status_code == 200
    assert rejected_store[0] == ("tp_known", "bad RR")


# ---------------------------------------------------------------------------
# Callback exceptions → 500
# ---------------------------------------------------------------------------


def test_validate_callback_exception_returns_500(repo):
    def boom(pid, reason):
        raise RuntimeError("db down")
    app = create_app(cost_repo=repo, on_validate=boom)
    c = TestClient(app)
    # TestClient ne raise pas par défaut
    r = c.post("/validate/tp_any")
    assert r.status_code == 500


# ---------------------------------------------------------------------------
# Default callbacks fonctionnent sans injection
# ---------------------------------------------------------------------------


def test_default_callbacks_accept_any_id(repo):
    app = create_app(cost_repo=repo)
    c = TestClient(app)
    r = c.post("/validate/whatever")
    assert r.status_code == 200
    r = c.post("/reject/whatever")
    assert r.status_code == 200
