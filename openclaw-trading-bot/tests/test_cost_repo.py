"""
tests/test_cost_repo.py — CostRepository (§14.5.5).

Couvre :
- schéma compat V1 (ts / tokens_in / tokens_out / status / cached)
- Q1 tokens_today / Q2 cost_mtd
- Q3 breakdown agents / Q4 breakdown modèles
- Q5 api_health (p95, err_rate, state green/amber/red)
- Q6 trend 30j / Q7 top consumers
- DB vide → panel zéro + alerte `no_llm_data` + source_data_lag_seconds=+inf
- ModelPricing.is_stale() & cost_usd()
- CostPanel.to_dict() remplace +inf par None
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from src.dashboards.cost_repo import CostPanel, CostRepository
from src.dashboards.pricing import LLMLimits, ModelPricing
from src.memory.db import init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def con():
    c = init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def pricing():
    # Tarifs connus, dernière MAJ aujourd'hui → pas `pricing_stale`
    return ModelPricing(
        rates={
            "claude-sonnet-4-6": (3.0, 15.0),
            "claude-opus-4-7": (15.0, 75.0),
        },
        last_updated=date.today(),
    )


@pytest.fixture
def limits():
    return LLMLimits(
        max_daily_tokens=10_000,
        max_monthly_cost_usd=10.0,
    )


@pytest.fixture
def repo(con, pricing, limits):
    return CostRepository(con, pricing, limits)


# Un "now" de référence — fin janvier pour éviter les effets de bord month boundary
_NOW = datetime(2026, 1, 20, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _insert_llm(con, *, ts: datetime, agent: str, model: str,
                tin: int, tout: int, cost: float,
                session_id: str | None = "s1") -> None:
    con.execute(
        """INSERT INTO llm_usage
            (ts, agent, model, tokens_in, tokens_out, cost_usd, session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_iso(ts), agent, model, tin, tout, cost, session_id),
    )


def _insert_api(con, *, ts: datetime, source: str, kind: str,
                status: int, latency_ms: int, cached: int = 0,
                cost: float = 0.0) -> None:
    con.execute(
        """INSERT INTO api_usage
            (ts, source, kind, status, latency_ms, cached, cost_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_iso(ts), source, kind, status, latency_ms, cached, cost),
    )


# ---------------------------------------------------------------------------
# Empty DB — fail-open + alerte no_llm_data
# ---------------------------------------------------------------------------


def test_build_panel_empty_db(repo):
    panel = repo.build_panel(now=_NOW)
    assert isinstance(panel, CostPanel)
    assert panel.tokens_today == 0
    assert panel.cost_month_usd == 0.0
    assert panel.forecast_month_usd == 0.0
    assert panel.by_agent == []
    assert panel.by_model == []
    assert panel.by_api_source == []
    assert panel.trend_30d == []
    assert panel.top_consumers == []
    assert math.isinf(panel.source_data_lag_seconds)
    assert any(a.code == "no_llm_data" for a in panel.alerts)


def test_empty_panel_to_dict_replaces_inf(repo):
    panel = repo.build_panel(now=_NOW)
    d = panel.to_dict()
    assert d["source_data_lag_seconds"] is None


# ---------------------------------------------------------------------------
# Q1 tokens_today & Q2 cost_mtd
# ---------------------------------------------------------------------------


def test_tokens_today_only_counts_today(con, repo):
    # Aujourd'hui
    _insert_llm(con, ts=_NOW - timedelta(hours=1), agent="scan",
                model="claude-sonnet-4-6", tin=100, tout=50, cost=0.001)
    # Hier
    _insert_llm(con, ts=_NOW - timedelta(days=1, hours=2), agent="scan",
                model="claude-sonnet-4-6", tin=999, tout=999, cost=0.999)
    panel = repo.build_panel(now=_NOW)
    assert panel.tokens_today == 150


def test_cost_mtd_only_counts_current_month(con, repo):
    _insert_llm(con, ts=_NOW - timedelta(days=2), agent="a",
                model="claude-sonnet-4-6", tin=1, tout=1, cost=0.50)
    _insert_llm(con, ts=_NOW - timedelta(days=5), agent="a",
                model="claude-sonnet-4-6", tin=1, tout=1, cost=0.25)
    # Hors mois (décembre)
    _insert_llm(con, ts=datetime(2025, 12, 20, tzinfo=timezone.utc),
                agent="a", model="claude-sonnet-4-6",
                tin=1, tout=1, cost=999.0)
    panel = repo.build_panel(now=_NOW)
    assert panel.cost_month_usd == pytest.approx(0.75, rel=1e-6)


# ---------------------------------------------------------------------------
# Q3 breakdown agents
# ---------------------------------------------------------------------------


def test_breakdown_by_agent_aggregates_24h(con, repo):
    _insert_llm(con, ts=_NOW - timedelta(hours=2), agent="scan",
                model="claude-sonnet-4-6", tin=100, tout=50, cost=0.10)
    _insert_llm(con, ts=_NOW - timedelta(hours=3), agent="scan",
                model="claude-sonnet-4-6", tin=200, tout=80, cost=0.20)
    _insert_llm(con, ts=_NOW - timedelta(hours=1), agent="news_pulse",
                model="claude-opus-4-7", tin=500, tout=300, cost=0.80)
    # > 24h
    _insert_llm(con, ts=_NOW - timedelta(hours=30), agent="scan",
                model="claude-sonnet-4-6", tin=1, tout=1, cost=0.001)
    panel = repo.build_panel(now=_NOW)
    by_agent = {r.agent: r for r in panel.by_agent}
    assert by_agent["scan"].calls_24h == 2
    assert by_agent["scan"].tokens_in_24h == 300
    assert by_agent["scan"].tokens_out_24h == 130
    assert by_agent["scan"].cost_24h_usd == pytest.approx(0.30)
    assert by_agent["news_pulse"].calls_24h == 1
    # Tri par cost_24h_usd DESC → news_pulse en premier
    assert panel.by_agent[0].agent == "news_pulse"


# ---------------------------------------------------------------------------
# Q4 breakdown modèles
# ---------------------------------------------------------------------------


def test_breakdown_by_model_sum_month(con, repo):
    _insert_llm(con, ts=_NOW - timedelta(days=3), agent="a",
                model="claude-sonnet-4-6", tin=100, tout=50, cost=0.10)
    _insert_llm(con, ts=_NOW - timedelta(days=5), agent="b",
                model="claude-sonnet-4-6", tin=200, tout=80, cost=0.20)
    _insert_llm(con, ts=_NOW - timedelta(days=1), agent="a",
                model="claude-opus-4-7", tin=300, tout=100, cost=2.00)
    panel = repo.build_panel(now=_NOW)
    by_model = {r.model: r for r in panel.by_model}
    assert by_model["claude-sonnet-4-6"].tokens_in == 300
    assert by_model["claude-sonnet-4-6"].tokens_out == 130
    assert by_model["claude-sonnet-4-6"].cost_usd == pytest.approx(0.30)
    assert by_model["claude-opus-4-7"].cost_usd == pytest.approx(2.00)


# ---------------------------------------------------------------------------
# Q5 api_health
# ---------------------------------------------------------------------------


def test_api_health_green_when_clean(con, repo):
    for i in range(20):
        _insert_api(con, ts=_NOW - timedelta(minutes=i),
                    source="stooq", kind="equity", status=200,
                    latency_ms=100 + i, cached=0)
    panel = repo.build_panel(now=_NOW)
    assert len(panel.by_api_source) == 1
    row = panel.by_api_source[0]
    assert row.source == "stooq"
    assert row.calls_24h == 20
    assert row.error_rate_pct == 0.0
    assert row.state == "green"
    assert row.latency_p95_ms < 1500


def test_api_health_red_on_high_latency(con, repo):
    # p95 > 2000ms → red
    for i in range(20):
        lat = 100 if i < 18 else 3000
        _insert_api(con, ts=_NOW - timedelta(minutes=i),
                    source="slow_api", kind="news", status=200,
                    latency_ms=lat)
    panel = repo.build_panel(now=_NOW)
    row = panel.by_api_source[0]
    assert row.latency_p95_ms >= 2000
    assert row.state == "red"


def test_api_health_cached_counts_as_success(con, repo):
    # 5 cachés (status=0) + 5 OK (200) + 10 erreurs (500) → err pct = 50%
    for i in range(5):
        _insert_api(con, ts=_NOW - timedelta(minutes=i),
                    source="api_a", kind="crypto",
                    status=0, latency_ms=10, cached=1)
    for i in range(5, 10):
        _insert_api(con, ts=_NOW - timedelta(minutes=i),
                    source="api_a", kind="crypto",
                    status=200, latency_ms=100)
    for i in range(10, 20):
        _insert_api(con, ts=_NOW - timedelta(minutes=i),
                    source="api_a", kind="crypto",
                    status=500, latency_ms=50)
    panel = repo.build_panel(now=_NOW)
    row = panel.by_api_source[0]
    assert row.cache_hit_pct == pytest.approx(25.0, rel=0.01)
    assert row.error_rate_pct == pytest.approx(50.0, rel=0.01)
    assert row.state == "red"  # > 10% err


# ---------------------------------------------------------------------------
# Q6 trend 30j
# ---------------------------------------------------------------------------


def test_trend_30d_groups_by_day(con, repo):
    _insert_llm(con, ts=_NOW - timedelta(days=2, hours=1), agent="a",
                model="claude-sonnet-4-6", tin=100, tout=50, cost=0.10)
    _insert_llm(con, ts=_NOW - timedelta(days=2, hours=5), agent="a",
                model="claude-sonnet-4-6", tin=200, tout=100, cost=0.20)
    _insert_llm(con, ts=_NOW - timedelta(days=1), agent="a",
                model="claude-sonnet-4-6", tin=50, tout=50, cost=0.05)
    panel = repo.build_panel(now=_NOW)
    days = {p.day: p for p in panel.trend_30d}
    assert len(days) == 2
    two_days_ago = (_NOW - timedelta(days=2)).strftime("%Y-%m-%d")
    assert days[two_days_ago].cost_usd == pytest.approx(0.30)
    assert days[two_days_ago].tokens == 450


# ---------------------------------------------------------------------------
# Q7 top_consumers
# ---------------------------------------------------------------------------


def test_top_consumers_sorted_by_cost(con, repo):
    for i, (cost, sess) in enumerate([(0.1, "s1"), (0.5, "s2"), (0.3, "s3")]):
        _insert_llm(con, ts=_NOW - timedelta(days=1, hours=i),
                    agent="scan", model="claude-sonnet-4-6",
                    tin=10, tout=10, cost=cost, session_id=sess)
    panel = repo.build_panel(now=_NOW)
    assert len(panel.top_consumers) == 3
    assert panel.top_consumers[0].cycle_id == "s2"
    assert panel.top_consumers[0].cost_usd == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Alertes budget
# ---------------------------------------------------------------------------


def test_alerts_daily_tokens_warn(con, repo, limits):
    # 82% du budget 10 000
    _insert_llm(con, ts=_NOW - timedelta(hours=1), agent="a",
                model="claude-sonnet-4-6", tin=5000, tout=3200, cost=0.10)
    panel = repo.build_panel(now=_NOW)
    codes = {a.code for a in panel.alerts}
    assert "tokens_daily_warn" in codes


def test_alerts_daily_tokens_crit(con, repo):
    _insert_llm(con, ts=_NOW - timedelta(hours=1), agent="a",
                model="claude-sonnet-4-6", tin=6000, tout=4000, cost=0.10)
    panel = repo.build_panel(now=_NOW)
    codes = {a.code for a in panel.alerts}
    assert "tokens_daily_crit" in codes


def test_alerts_cost_month_warn(con, repo):
    # 75% de 10$
    _insert_llm(con, ts=_NOW - timedelta(days=2), agent="a",
                model="claude-sonnet-4-6", tin=1, tout=1, cost=7.50)
    panel = repo.build_panel(now=_NOW)
    codes = {a.code for a in panel.alerts}
    assert "cost_month_warn" in codes


def test_alerts_pricing_stale(con, limits):
    stale = ModelPricing(
        rates={"claude-sonnet-4-6": (3.0, 15.0)},
        last_updated=date(2025, 1, 1),
    )
    repo = CostRepository(con, stale, limits)
    # Inject au moins 1 point pour éviter alerte no_llm_data
    _insert_llm(con, ts=_NOW - timedelta(hours=1), agent="a",
                model="claude-sonnet-4-6", tin=1, tout=1, cost=0.001)
    panel = repo.build_panel(now=_NOW)
    codes = {a.code for a in panel.alerts}
    assert "pricing_stale" in codes


# ---------------------------------------------------------------------------
# ModelPricing
# ---------------------------------------------------------------------------


def test_model_pricing_cost_usd():
    p = ModelPricing(rates={"claude-sonnet-4-6": (3.0, 15.0)}, last_updated=date.today())
    # 1000 input × 3 + 500 output × 15 par Mtok
    expected = (1000 / 1_000_000) * 3.0 + (500 / 1_000_000) * 15.0
    assert p.cost_usd("claude-sonnet-4-6", 1000, 500) == pytest.approx(expected)


def test_model_pricing_unknown_model_fail_open():
    p = ModelPricing(rates={}, last_updated=date.today())
    assert p.cost_usd("unknown", 1000, 500) == 0.0


def test_model_pricing_is_stale_true_for_old():
    p = ModelPricing(rates={}, last_updated=date(2020, 1, 1))
    assert p.is_stale() is True


def test_model_pricing_is_stale_false_for_recent():
    p = ModelPricing(rates={}, last_updated=date.today())
    assert p.is_stale() is False


def test_model_pricing_load_from_file(tmp_path):
    yaml_content = """\
last_updated: 2026-01-01
source: https://example.com
models:
  claude-sonnet-4-6:
    input_per_mtok_usd: 3.0
    output_per_mtok_usd: 15.0
"""
    f = tmp_path / "pricing.yaml"
    f.write_text(yaml_content)
    p = ModelPricing.load(path=f)
    assert p.last_updated == date(2026, 1, 1)
    assert p.rates["claude-sonnet-4-6"] == (3.0, 15.0)
    assert p.source_url == "https://example.com"


# ---------------------------------------------------------------------------
# forecast + computed_at
# ---------------------------------------------------------------------------


def test_forecast_projects_to_month_end(con, repo):
    # Jour 20 du mois, 2$ dépensés → forecast = 2 / 20 * 31 = 3.1
    _insert_llm(con, ts=_NOW - timedelta(days=5), agent="a",
                model="claude-sonnet-4-6", tin=1, tout=1, cost=2.0)
    panel = repo.build_panel(now=_NOW)
    assert panel.forecast_month_usd == pytest.approx(2.0 / 20 * 31, rel=0.01)


def test_computed_at_iso_utc(repo):
    panel = repo.build_panel(now=_NOW)
    assert panel.computed_at.endswith("Z") or "+00:00" in panel.computed_at
