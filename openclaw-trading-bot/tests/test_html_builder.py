"""
tests/test_html_builder.py — HTMLDashboardBuilder (§14.5.4).

Couvre :
- build() écrit `data/dashboards/<YYYY-MM-DD>/<session>.html` dans tmp_path
- render() contient les blocs attendus (opportunités, coûts, cycle, footer)
- Gestion opportunités vides → message fallback
- Escape HTML par défaut (Jinja2 autoescape)
- Badge kill-switch ON/OFF
- infinity lag → affiche "n/a"
- Degradation flags chips
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.contracts.cycle import CycleResult
from src.contracts.regime import RegimeState
from src.contracts.skills import IchimokuPayload
from src.contracts.strategy import TradeProposal
from src.dashboards.cost_repo import CostRepository
from src.dashboards.html_builder import HTMLDashboardBuilder
from src.dashboards.pricing import LLMLimits, ModelPricing
from src.memory.db import init_db


_NOW = datetime(2026, 1, 20, 12, 0, 0, tzinfo=timezone.utc)


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
    return ModelPricing(
        rates={"claude-sonnet-4-6": (3.0, 15.0)},
        last_updated=date.today(),
    )


@pytest.fixture
def repo(con, pricing):
    return CostRepository(con, pricing, LLMLimits())


@pytest.fixture
def builder(repo, tmp_path):
    return HTMLDashboardBuilder(
        cost_repo=repo,
        dashboards_dir=tmp_path / "dashboards",
    )


@pytest.fixture
def regime():
    return RegimeState(
        macro="risk_on",
        volatility="mid",
        probabilities={"risk_on": 0.7, "transition": 0.2, "risk_off": 0.1},
        hmm_state=0,
        date="2026-01-20",
    )


@pytest.fixture
def cycle():
    return CycleResult.success(
        proposals=1,
        proposals_rejected=2,
        session_name="eu_morning",
        duration_s=12.34,
        risk_gate_failure_rate=0.10,
    )


@pytest.fixture
def cycle_degraded():
    return CycleResult.success(
        proposals=0,
        session_name="eu_morning",
        degradation_flags=["fred_degraded", "news_pulse_degraded"],
        duration_s=5.0,
    )


def _ichimoku() -> IchimokuPayload:
    return IchimokuPayload(
        price_above_kumo=True,
        tenkan_above_kijun=True,
        chikou_above_price_26=True,
        kumo_thickness_pct=0.5,
        aligned_long=True,
        aligned_short=False,
        distance_to_kumo_pct=1.2,
    )


def _proposal(**kw) -> TradeProposal:
    defaults = dict(
        strategy_id="breakout_momentum",
        asset="RUI.PA",
        asset_class="equity",
        side="long",
        entry_price=52.1234,
        stop_price=50.0000,
        tp_prices=[54.0, 56.0],
        rr=2.5,
        conviction=0.85,
        risk_pct=0.01,
        catalysts=["earnings"],
        ichimoku=_ichimoku(),
    )
    defaults.update(kw)
    return TradeProposal(**defaults)


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------


def test_render_contains_core_sections(builder, regime, cycle):
    html = builder.render(
        session="eu_morning",
        cycle_result=cycle,
        regime=regime,
        proposals=[_proposal()],
        cost_panel=builder.cost_repo.build_panel(now=_NOW),
        kill_switch_active=False,
        now=_NOW,
    )
    assert "<!DOCTYPE html>" in html
    assert "Openclaw" in html
    assert "eu_morning" in html
    assert "Opportunités" in html
    assert "Coûts" in html
    assert "Cycle" in html
    assert "/costs.json" in html
    assert "/healthz" in html


def test_render_empty_proposals_shows_fallback(builder, regime, cycle):
    cycle_zero = CycleResult.success(proposals=0, session_name="eu_morning")
    html = builder.render(
        session="eu_morning",
        cycle_result=cycle_zero,
        regime=regime,
        proposals=[],
        cost_panel=builder.cost_repo.build_panel(now=_NOW),
        kill_switch_active=False,
        now=_NOW,
    )
    assert "Aucune opportunité" in html


def test_render_displays_proposal_details(builder, regime, cycle):
    p = _proposal(proposal_id="tp_abc123", asset="BTC/USDT", side="short")
    html = builder.render(
        session="crypto_18utc",
        cycle_result=cycle,
        regime=regime,
        proposals=[p],
        cost_panel=builder.cost_repo.build_panel(now=_NOW),
        kill_switch_active=False,
        now=_NOW,
    )
    assert "tp_abc123" in html
    assert "BTC/USDT" in html
    assert "SHORT" in html
    # Les boutons portent le proposal_id
    assert 'data-id="tp_abc123"' in html
    assert 'data-action="validate"' in html
    assert 'data-action="reject"' in html


def test_render_kill_switch_badge(builder, regime, cycle):
    html_off = builder.render(
        session="s", cycle_result=cycle, regime=regime, proposals=[],
        cost_panel=builder.cost_repo.build_panel(now=_NOW),
        kill_switch_active=False, now=_NOW,
    )
    html_on = builder.render(
        session="s", cycle_result=cycle, regime=regime, proposals=[],
        cost_panel=builder.cost_repo.build_panel(now=_NOW),
        kill_switch_active=True, now=_NOW,
    )
    assert "kill-switch off" in html_off
    assert "kill-switch ON" in html_on


def test_render_degradation_flags_chips(builder, regime, cycle_degraded):
    html = builder.render(
        session="s", cycle_result=cycle_degraded, regime=regime, proposals=[],
        cost_panel=builder.cost_repo.build_panel(now=_NOW),
        kill_switch_active=False, now=_NOW,
    )
    assert "fred_degraded" in html
    assert "news_pulse_degraded" in html


def test_render_escapes_html_injection(builder, regime, cycle):
    # Inject XSS dans le session name → doit être échappé
    html = builder.render(
        session="<script>alert(1)</script>",
        cycle_result=cycle, regime=regime, proposals=[],
        cost_panel=builder.cost_repo.build_panel(now=_NOW),
        kill_switch_active=False, now=_NOW,
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_infinity_lag_shown_as_na(builder, regime, cycle):
    # DB vide → lag = inf → footer "n/a"
    html = builder.render(
        session="s", cycle_result=cycle, regime=regime, proposals=[],
        cost_panel=builder.cost_repo.build_panel(now=_NOW),
        kill_switch_active=False, now=_NOW,
    )
    assert "n/a" in html


# ---------------------------------------------------------------------------
# build() (écrit sur disque)
# ---------------------------------------------------------------------------


def test_build_writes_file(builder, regime, cycle, tmp_path):
    out = builder.build(
        session="eu_morning",
        cycle_result=cycle,
        regime=regime,
        proposals=[_proposal()],
        kill_switch_active=False,
        now=_NOW,
    )
    assert out.exists()
    assert out.name == "eu_morning.html"
    assert out.parent.name == "2026-01-20"
    content = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "eu_morning" in content


def test_build_idempotent_overwrites(builder, regime, cycle):
    out1 = builder.build(
        session="eu_morning", cycle_result=cycle, regime=regime,
        proposals=[_proposal()], now=_NOW,
    )
    first_size = out1.stat().st_size
    out2 = builder.build(
        session="eu_morning", cycle_result=cycle, regime=regime,
        proposals=[_proposal(), _proposal(asset="EURUSD", asset_class="forex")],
        now=_NOW,
    )
    assert out1 == out2
    assert out2.stat().st_size >= first_size
