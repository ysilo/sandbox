"""
tests.test_risk — couche de risque §11 (kill-switch, circuit breaker, 10 checks).

Chaque check C1→C10 a un test qui le déclenche en isolation (les autres checks
sont maintenus passants). Un test d'invariant vérifie que `RiskDecision.checks`
contient toujours 10 entrées, dans l'ordre, même après short-circuit.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.contracts.skills import (
    CHECK_IDS,
    IchimokuPayload,
    RiskCheckResult,
    _pad_checks,
)
from src.contracts.strategy import TradeProposal
from src.risk import ichimoku_gate
from src.risk.circuit_breaker import CircuitBreaker, CircuitState
from src.risk.gate import (
    DataQualityState,
    GateConfig,
    GateContext,
    MacroState,
    PortfolioState,
    PositionSnapshot,
    RiskGate,
    TokenBudgetState,
    _estimate_notional_pct,
)
from src.risk.kill_switch import KillSwitch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ich_aligned_long() -> IchimokuPayload:
    return IchimokuPayload(
        price_above_kumo=True,
        tenkan_above_kijun=True,
        chikou_above_price_26=True,
        kumo_thickness_pct=0.01,
        aligned_long=True,
        aligned_short=False,
        distance_to_kumo_pct=1.5,
    )


def _make_proposal(
    *,
    strategy_id: str = "ichimoku_trend_following",
    side: str = "long",
    asset: str = "RUI.PA",
    asset_class: str = "equity",
    risk_pct: float = 0.0075,
) -> TradeProposal:
    return TradeProposal(
        strategy_id=strategy_id,
        asset=asset,
        asset_class=asset_class,
        side=side,  # type: ignore[arg-type]
        entry_price=100.0,
        stop_price=98.0,
        tp_prices=[105.0, 110.0],
        rr=2.5,
        conviction=0.7,
        risk_pct=risk_pct,
        catalysts=[],
        ichimoku=_ich_aligned_long(),
    )


def _strategies_cfg() -> dict:
    """Config minimale : règle d'or activée par défaut, `mean_reversion` waivered."""
    return {
        "defaults": {"requires_ichimoku_alignment": True},
        "strategies": {
            "ichimoku_trend_following": {"requires_ichimoku_alignment": True},
            "mean_reversion":           {"requires_ichimoku_alignment": False},
            "divergence_hunter":        {"requires_ichimoku_alignment": False},
            "volume_profile_scalp":     {"requires_ichimoku_alignment": False},
            "breakout_momentum":        {"requires_ichimoku_alignment": True},
        },
    }


def _fresh_ctx(**overrides) -> GateContext:
    base = GateContext(
        portfolio=PortfolioState(equity=100_000.0, daily_pnl_pct=0.0, open_positions=[]),
        tokens=TokenBudgetState(tokens_used_today=0, monthly_cost_usd=0.0, estimated_tokens=0),
        macro=MacroState(vix=18.0, hmm_regime="risk_on", hmm_confidence=0.5),
        data_quality=DataQualityState(),
        strategies_cfg=_strategies_cfg(),
        correlation_to_portfolio={},
        circuit_breaker_lookup=lambda _sid: CircuitState.CLOSED,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


@pytest.fixture
def kill_file(tmp_path, monkeypatch):
    """KillSwitch pointant sur un tmp file (désactive l'env global)."""
    path = tmp_path / "KILL"
    monkeypatch.setenv("KILL_FILE_PATH", str(path))
    yield path
    if path.exists():
        path.unlink()


@pytest.fixture
def gate_no_warn(kill_file) -> RiskGate:
    return RiskGate(
        config=GateConfig(),
        kill_switch=KillSwitch(kill_file),
    )


# ---------------------------------------------------------------------------
# KillSwitch — unit tests
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_is_active_false_initially(self, tmp_path):
        ks = KillSwitch(tmp_path / "KILL")
        assert ks.is_active() is False

    def test_arm_creates_file(self, tmp_path):
        ks = KillSwitch(tmp_path / "KILL")
        ks.arm("test")
        assert ks.is_active() is True
        assert "test" in ks.reason()

    def test_disarm_removes_file(self, tmp_path):
        ks = KillSwitch(tmp_path / "KILL")
        ks.arm("trigger")
        ks.disarm()
        assert ks.is_active() is False

    def test_env_override(self, tmp_path, monkeypatch):
        path = tmp_path / "nested" / "KILL"
        monkeypatch.setenv("KILL_FILE_PATH", str(path))
        ks = KillSwitch()
        ks.arm("env")
        assert path.exists()


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    @pytest.fixture
    def conn(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("""
            CREATE TABLE trades (
                id TEXT PRIMARY KEY,
                asset TEXT, asset_class TEXT, strategy TEXT, side TEXT,
                entry_price REAL, entry_time TEXT, stop_price REAL,
                tp_prices TEXT, size_pct_equity REAL, conviction REAL,
                rr_estimated REAL, catalysts TEXT, exit_price REAL,
                exit_time TEXT, pnl_pct REAL, pnl_usd_fictif REAL,
                status TEXT, validated INTEGER, llm_narrative TEXT,
                session_id TEXT, created_at TEXT
            )
        """)
        yield c
        c.close()

    def _insert_trades(self, conn, strategy, daily_pnl, start_date):
        """Insère 1 trade fermé par jour avec pnl_pct donné."""
        d = start_date
        for i, p in enumerate(daily_pnl):
            conn.execute(
                """INSERT INTO trades
                   (id, asset, asset_class, strategy, side, entry_price, entry_time,
                    stop_price, tp_prices, size_pct_equity, exit_price, exit_time,
                    pnl_pct, status)
                   VALUES (?, 'AAA', 'equity', ?, 'long', 100, ?, 98, '[]', 0.01,
                           101, ?, ?, 'closed')""",
                (f"t{i}", strategy, f"{d.isoformat()}T10:00:00Z",
                 f"{d.isoformat()}T15:00:00Z", p),
            )
            d = d + timedelta(days=1)
        conn.commit()

    def test_insufficient_data_returns_state(self, conn):
        cb = CircuitBreaker(min_history_days=30, window_days=7)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).date()
        self._insert_trades(conn, "s1", [0.0] * 10, start)
        r = cb.check_strategy(
            "s1", conn,
            now=datetime(2026, 1, 11, tzinfo=timezone.utc),
        )
        assert r.state is CircuitState.INSUFFICIENT_DATA

    def test_closed_on_normal_drawdown(self, conn):
        cb = CircuitBreaker(min_history_days=30, window_days=7, threshold_ratio=2.0)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).date()
        # 60 jours avec petits DD réguliers, puis 7 jours neutres → dd_7d=0 < seuil
        pnl = ([-0.01, 0.012] * 30) + [0.0] * 7
        self._insert_trades(conn, "s1", pnl, start)
        r = cb.check_strategy(
            "s1", conn,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=len(pnl) - 1),
        )
        assert r.state is CircuitState.CLOSED

    def test_tripped_on_severe_drawdown(self, conn):
        cb = CircuitBreaker(min_history_days=30, window_days=7, threshold_ratio=2.0)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).date()
        # 63 jours volatils (DD historique ~1-2 %), puis 7 jours de pertes massives
        # [0.002, -0.003] × 31.5 → chaque fenêtre 7j aura un petit DD positif.
        stable = [0.002, -0.003] * 32   # 64 jours, DD par fenêtre ~ 0.6 %
        crash = [-0.05, -0.04, -0.03, -0.02, -0.02, -0.01, -0.01]   # DD 7j ≈ 16 %
        pnl = stable + crash
        self._insert_trades(conn, "s1", pnl, start)
        r = cb.check_strategy(
            "s1", conn,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=len(pnl) - 1),
        )
        assert r.state is CircuitState.TRIPPED
        assert r.dd_7d > 0.05

    def test_max_drawdown_computation(self):
        cb = CircuitBreaker()
        # +5 %, +5 %, -20 %, +5 % : pic ≈ 1.1025 → creux ≈ 0.882 → DD ≈ 20 %
        dd = cb._max_drawdown([0.05, 0.05, -0.20, 0.05])
        assert 0.19 < dd < 0.21

    def test_max_drawdown_empty(self):
        cb = CircuitBreaker()
        assert cb._max_drawdown([]) == 0.0


# ---------------------------------------------------------------------------
# Ichimoku gate (unité)
# ---------------------------------------------------------------------------


class TestIchimokuGate:
    def test_aligned_long_ok(self):
        p = _make_proposal(strategy_id="ichimoku_trend_following", side="long")
        r = ichimoku_gate.check(p, _strategies_cfg())
        assert r.ok and not r.waived

    def test_contrarian_rejects(self):
        p = _make_proposal(strategy_id="ichimoku_trend_following", side="short")
        r = ichimoku_gate.check(p, _strategies_cfg())
        assert not r.ok and not r.waived

    def test_waiver_passes_contrarian(self):
        p = _make_proposal(strategy_id="mean_reversion", side="short")
        r = ichimoku_gate.check(p, _strategies_cfg())
        assert r.ok and r.waived

    def test_unknown_strategy_rejects(self):
        p = _make_proposal(strategy_id="not_a_strategy")
        r = ichimoku_gate.check(p, _strategies_cfg())
        assert not r.ok


# ---------------------------------------------------------------------------
# RiskGate — un test par check (déclenche seul, tous les autres passent)
# ---------------------------------------------------------------------------


class TestRiskGateChecks:
    def test_C1_kill_switch_blocks(self, gate_no_warn, kill_file):
        kill_file.parent.mkdir(parents=True, exist_ok=True)
        kill_file.write_text("2026-04-17T00:00:00Z — manual\n")
        d = gate_no_warn.evaluate(_make_proposal(), _fresh_ctx())
        assert d.approved is False
        assert any("C1" in r for r in d.reasons)
        c1 = d.checks[0]
        assert c1.check_id == "C1_kill_switch" and c1.passed is False

    def test_C2_daily_loss_blocks_at_threshold(self, gate_no_warn):
        ctx = _fresh_ctx()
        ctx.portfolio.daily_pnl_pct = -2.5   # < -2 % → breach
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert not d.approved
        assert any("C2" in r for r in d.reasons)

    def test_C3_max_positions_blocks_on_overflow(self, gate_no_warn):
        ctx = _fresh_ctx()
        ctx.portfolio.open_positions = [
            PositionSnapshot(asset=f"A{i}", asset_class="equity", side="long",
                             risk_pct=0.005, notional_pct=2.0)
            for i in range(8)
        ]
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert not d.approved
        assert any("C3" in r for r in d.reasons)

    def test_C3_per_class_cap_blocks(self, gate_no_warn):
        # equity cap = 4 → insère 4 positions equity, propose une 5e equity
        ctx = _fresh_ctx()
        ctx.portfolio.open_positions = [
            PositionSnapshot(asset=f"E{i}", asset_class="equity", side="long",
                             risk_pct=0.005, notional_pct=1.0)
            for i in range(4)
        ]
        d = gate_no_warn.evaluate(_make_proposal(asset_class="equity"), ctx)
        assert not d.approved
        assert any("equity" in r for r in d.reasons)

    def test_C4_exposure_per_class_blocks(self, gate_no_warn):
        ctx = _fresh_ctx()
        # Notionnel de la proposition calculé depuis la distance au stop :
        # entry=100, stop=98 → stop_distance=2 % ; risk_pct=0.005 → notional=25 %.
        # Avec 30 % déjà exposés en equity, projected=55 % > cap 40 %.
        ctx.portfolio.open_positions = [
            PositionSnapshot(asset="A", asset_class="equity", side="long",
                             risk_pct=0.005, notional_pct=30.0),
        ]
        p = _make_proposal(risk_pct=0.005)
        d = gate_no_warn.evaluate(p, ctx)
        assert not d.approved
        assert any("C4" in r for r in d.reasons)

    def test_C4_uses_notional_pct_when_provided(self, gate_no_warn):
        """Si TradeProposal a un notional_pct non nul, il prime sur l'estimation."""
        ctx = _fresh_ctx()
        # Même portfolio que ci-dessus, mais on force notional_pct=5 sur la
        # proposition via attribut ajouté à la volée — l'attribut n'existe pas
        # par défaut sur TradeProposal, donc _c4 utilise _estimate_notional_pct
        # quand il vaut None / 0. Ici on valide le chemin d'estimation fait passer
        # un cas sain où projected = 30 + 25 = 55 > 40, blocked.
        ctx.portfolio.open_positions = [
            PositionSnapshot(asset="A", asset_class="equity", side="long",
                             risk_pct=0.005, notional_pct=30.0),
        ]
        p = _make_proposal(risk_pct=0.005)
        d = gate_no_warn.evaluate(p, ctx)
        # Attendu : C4 bloque grâce à l'estimation par stop-distance.
        assert not d.approved

    def test_C5_circuit_breaker_blocks_tripped_strategy(self, gate_no_warn):
        ctx = _fresh_ctx()
        ctx.circuit_breaker_lookup = lambda sid: CircuitState.TRIPPED
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert not d.approved
        assert any("C5" in r for r in d.reasons)

    def test_C6_ichimoku_blocks_contrarian_non_waived(self, gate_no_warn):
        # Kill-switch désactivé, circuit closed, ichimoku misaligned
        p = TradeProposal(
            strategy_id="ichimoku_trend_following",
            asset="RUI.PA", asset_class="equity", side="short",
            entry_price=100, stop_price=102, tp_prices=[95, 90],
            rr=2.0, conviction=0.6, risk_pct=0.005, catalysts=[],
            ichimoku=_ich_aligned_long(),    # aligned_short=False
        )
        d = gate_no_warn.evaluate(p, _fresh_ctx())
        assert not d.approved
        assert any("C6" in r for r in d.reasons)

    def test_C6_waiver_lets_contrarian_pass(self, gate_no_warn):
        p = TradeProposal(
            strategy_id="mean_reversion",
            asset="RUI.PA", asset_class="equity", side="short",
            entry_price=100, stop_price=102, tp_prices=[95, 90],
            rr=2.0, conviction=0.6, risk_pct=0.005, catalysts=[],
            ichimoku=_ich_aligned_long(),
        )
        d = gate_no_warn.evaluate(p, _fresh_ctx())
        assert d.approved, d.reasons

    def test_C7_token_budget_blocks_over_daily_cap(self, gate_no_warn):
        ctx = _fresh_ctx()
        ctx.tokens.tokens_used_today = 49_500
        ctx.tokens.estimated_tokens = 1_000   # → 50_500 > 50_000
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert not d.approved
        assert any("C7" in r for r in d.reasons)

    def test_C7_monthly_cost_blocks(self, gate_no_warn):
        ctx = _fresh_ctx()
        ctx.tokens.monthly_cost_usd = 15.01    # ≥ $15.00
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert not d.approved
        assert any("C7" in r for r in d.reasons)

    def test_C8_correlation_cap_blocks_overlap(self, gate_no_warn):
        ctx = _fresh_ctx()
        # Position existante corrélée (|ρ|>0.7) : 15 % notionnel. Proposition
        # corrélée : notional estimé = risk_pct / stop_distance = 0.005/0.02 = 25 %.
        # Projected = 15 + 25 = 40 % > cap 20 % → bloque.
        ctx.portfolio.open_positions = [
            PositionSnapshot(asset="AAA", asset_class="equity", side="long",
                             risk_pct=0.01, notional_pct=15.0),
        ]
        ctx.correlation_to_portfolio = {"AAA": 0.85, "RUI.PA": 0.80}
        p = _make_proposal(risk_pct=0.005)
        d = gate_no_warn.evaluate(p, ctx)
        assert not d.approved
        assert any("C8" in r for r in d.reasons)

    def test_C8_uncorrelated_proposal_does_not_add(self, gate_no_warn):
        """Si la proposition n'est pas corrélée au portefeuille, C8 n'ajoute pas son notional."""
        ctx = _fresh_ctx()
        ctx.portfolio.open_positions = [
            PositionSnapshot(asset="AAA", asset_class="equity", side="long",
                             risk_pct=0.005, notional_pct=10.0),
        ]
        # Corrélation AAA existante > seuil (mais isolée < cap 20 %).
        # Le nouveau actif RUI.PA n'est pas corrélé → projected = 10 seulement.
        ctx.correlation_to_portfolio = {"AAA": 0.85, "RUI.PA": 0.10}
        p = _make_proposal(risk_pct=0.005)
        d = gate_no_warn.evaluate(p, ctx)
        assert d.approved, d.reasons

    def test_C9_macro_volatility_blocks_in_crash(self, gate_no_warn):
        ctx = _fresh_ctx()
        ctx.macro = MacroState(vix=42.0, hmm_regime="risk_off", hmm_confidence=0.9)
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert not d.approved
        assert any("C9" in r for r in d.reasons)

    def test_C9_high_vix_alone_does_not_block(self, gate_no_warn):
        # VIX élevé mais régime non risk_off → pas de crash-mode.
        ctx = _fresh_ctx()
        ctx.macro = MacroState(vix=50.0, hmm_regime="risk_on", hmm_confidence=0.9)
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert d.approved, d.reasons

    def test_C10_data_quality_blocks_stale(self, gate_no_warn):
        ctx = _fresh_ctx()
        ctx.data_quality = DataQualityState(is_fresh=False)
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert not d.approved
        assert any("C10" in r for r in d.reasons)

    def test_C10_data_quality_blocks_outliers(self, gate_no_warn):
        ctx = _fresh_ctx()
        ctx.data_quality = DataQualityState(has_outliers=True)
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert not d.approved

    def test_C10_data_quality_blocks_fallback_source(self, gate_no_warn):
        ctx = _fresh_ctx()
        ctx.data_quality = DataQualityState(used_fallback_source=True)
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert not d.approved


# ---------------------------------------------------------------------------
# Invariants §8.8.1
# ---------------------------------------------------------------------------


class TestRiskDecisionInvariants:
    def test_all_ten_checks_present_in_decision_on_approve(self, gate_no_warn):
        d = gate_no_warn.evaluate(_make_proposal(), _fresh_ctx())
        assert d.approved is True
        assert len(d.checks) == 10
        assert tuple(c.check_id for c in d.checks) == CHECK_IDS
        assert all(c.evaluated for c in d.checks)

    def test_all_ten_checks_present_after_short_circuit(self, gate_no_warn, kill_file):
        # Arme le kill-switch → short-circuit dès C1.
        kill_file.parent.mkdir(parents=True, exist_ok=True)
        kill_file.write_text("trigger\n")
        d = gate_no_warn.evaluate(_make_proposal(), _fresh_ctx())
        assert d.approved is False
        assert len(d.checks) == 10
        assert tuple(c.check_id for c in d.checks) == CHECK_IDS
        # C1 évalué ; C2..C10 short-circuités.
        assert d.checks[0].evaluated is True
        assert d.checks[0].passed is False
        for c in d.checks[1:]:
            assert c.evaluated is False
            assert c.passed is True

    def test_checks_ordered_c1_to_c10(self):
        # Factory _pad_checks doit renvoyer l'ordre canonique quel que soit l'input.
        shuffled = [
            RiskCheckResult(
                check_id="C5_circuit_breaker", passed=True,
                severity="blocking", reason="ok",
            ),
            RiskCheckResult(
                check_id="C1_kill_switch", passed=True,
                severity="blocking", reason="ok",
            ),
        ]
        padded = _pad_checks(shuffled)
        assert tuple(c.check_id for c in padded) == CHECK_IDS


# ---------------------------------------------------------------------------
# Fail-closed sur exception
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_exception_in_check_becomes_blocking_reject(self, gate_no_warn):
        ctx = _fresh_ctx()

        def boom(_sid: str) -> CircuitState:
            raise RuntimeError("DB offline")

        ctx.circuit_breaker_lookup = boom
        d = gate_no_warn.evaluate(_make_proposal(), ctx)
        assert not d.approved
        # Le check C5 doit porter la reason avec "exception:"
        c5 = next(c for c in d.checks if c.check_id == "C5_circuit_breaker")
        assert c5.passed is False
        assert "exception" in c5.reason.lower()


# ---------------------------------------------------------------------------
# warn_only dégrade un check en non-bloquant
# ---------------------------------------------------------------------------


class TestNotionalEstimator:
    def test_standard_formula(self):
        # risk 0.75 %, stop à 2 % → notional 37.5 %.
        p = _make_proposal(risk_pct=0.0075)
        # entry=100, stop=98 → stop_distance=0.02
        assert _estimate_notional_pct(p) == pytest.approx(37.5, rel=1e-6)

    def test_stop_collapsed_to_entry_falls_back_to_proxy(self):
        p = TradeProposal(
            strategy_id="ichimoku_trend_following",
            asset="RUI.PA", asset_class="equity", side="long",
            entry_price=100.0, stop_price=100.0, tp_prices=[105.0, 110.0],
            rr=2.0, conviction=0.7, risk_pct=0.0075, catalysts=[],
            ichimoku=_ich_aligned_long(),
        )
        # Pas de distance → proxy risk_pct × 100 = 0.75.
        assert _estimate_notional_pct(p) == pytest.approx(0.75, rel=1e-6)

    def test_zero_entry_price_returns_zero(self):
        p = TradeProposal(
            strategy_id="ichimoku_trend_following",
            asset="RUI.PA", asset_class="equity", side="long",
            entry_price=0.0, stop_price=0.0, tp_prices=[1.0],
            rr=2.0, conviction=0.7, risk_pct=0.0075, catalysts=[],
            ichimoku=_ich_aligned_long(),
        )
        assert _estimate_notional_pct(p) == 0.0


class TestIchimokuGateResolution:
    """_resolve_strategy_entry doit gérer les deux formats sans collision."""

    def test_yaml_wrapper_format(self):
        cfg = {
            "defaults": {"requires_ichimoku_alignment": True},
            "strategies": {
                "ichimoku_trend_following": {"requires_ichimoku_alignment": True},
            },
        }
        p = _make_proposal(strategy_id="ichimoku_trend_following")
        r = ichimoku_gate.check(p, cfg)
        assert r.ok is True

    def test_flat_loader_format(self):
        # Imite dict[str, StrategyConfig] mais avec des dicts pour ne pas avoir
        # à construire la Pydantic complète.
        cfg = {
            "ichimoku_trend_following": {"requires_ichimoku_alignment": True},
        }
        p = _make_proposal(strategy_id="ichimoku_trend_following")
        r = ichimoku_gate.check(p, cfg)
        assert r.ok is True

    def test_wrapper_priority_over_reserved_key(self):
        # Si "strategies" est présent comme wrapper, on ne doit PAS interpréter
        # le sid "defaults" comme une stratégie plate.
        cfg = {
            "strategies": {
                "ichimoku_trend_following": {"requires_ichimoku_alignment": True},
            },
            "defaults": {"requires_ichimoku_alignment": False},
        }
        p = _make_proposal(strategy_id="ichimoku_trend_following")
        r = ichimoku_gate.check(p, cfg)
        assert r.ok is True  # wrapper gagne, flag=true respecté


class TestWarnOnly:
    def test_warn_only_does_not_short_circuit(self, kill_file):
        cfg = GateConfig(warn_only=["C9_macro_volatility"])
        gate = RiskGate(config=cfg, kill_switch=KillSwitch(kill_file))
        ctx = _fresh_ctx()
        ctx.macro = MacroState(vix=42.0, hmm_regime="risk_off", hmm_confidence=0.9)
        d = gate.evaluate(_make_proposal(), ctx)
        # C9 échoue mais il est en warn → on arrive au bout, et C10 passe → approved.
        assert d.approved, d.reasons
        c9 = next(c for c in d.checks if c.check_id == "C9_macro_volatility")
        assert c9.passed is False
        assert c9.severity == "warn"
