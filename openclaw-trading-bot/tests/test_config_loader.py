"""
tests/test_config_loader.py — validation des YAML de `config/` au startup.

Ces tests sont un filet de sécurité : toute modification d'un YAML qui casse
son schéma Pydantic fait exploser le CI avant qu'un cycle prod ne plante.
"""
from __future__ import annotations

import pytest

from src.utils.config_loader import (
    load_assets,
    load_mode,
    load_risk_config,
    load_schedules,
    load_sources,
    load_strategies,
    reload_all,
)


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    reload_all()


def test_risk_config_loads_and_has_kill_switch_file() -> None:
    rc = load_risk_config()
    assert rc.kill_switch_file == "data/KILL"
    assert rc.llm.model.startswith("claude-")
    assert rc.max_open_positions >= 1
    assert len(rc.avoid_windows) >= 2


def test_strategies_file_validates_7_entries() -> None:
    strats = load_strategies()
    expected_ids = {
        "ichimoku_trend_following",
        "breakout_momentum",
        "mean_reversion",
        "divergence_hunter",
        "volume_profile_scalp",
        "event_driven_macro",
        "news_driven_momentum",
    }
    assert set(strats.keys()) == expected_ids
    for sid, cfg in strats.items():
        assert cfg.id == sid
        assert cfg.min_rr >= 1.0
        assert 0.0 <= cfg.min_composite_score <= 1.0
        assert cfg.timeframes, f"{sid} n'a pas de timeframes"


def test_assets_has_class_budgets_summing_to_100() -> None:
    a = load_assets()
    b = a["class_budgets_pct"]
    assert sum(b.values()) == 100


def test_schedules_has_required_sessions() -> None:
    s = load_schedules()
    # §15.1 utilise `cycles` — on garde la compat `sessions` legacy
    entries = s.get("cycles") or s.get("sessions") or []
    names = {sess["name"] for sess in entries}
    # Au moins les cycles equity/forex + crypto doivent être définis
    assert any("europe" in n or "us" in n for n in names)
    assert any("crypto" in n for n in names)


def test_sources_has_price_providers() -> None:
    s = load_sources()
    assert "prices" in s or "equity_providers" in s


def test_mode_is_paper_only_in_v1() -> None:
    mc = load_mode()
    assert mc.mode == "paper"
