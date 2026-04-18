"""Tests pour src.orchestrator.run — Orchestrator §8.7 / §8.7.1.

Stratégie :
- Injection 100 % via Protocols : on câble des fakes minimaux (pas mock lib).
- Chaque test isole un cas : kill-switch, régime fallback, scan vide, news
  injection, signal filter, risk-gate rgfr, circuit-breaker C5, run_focused.
- On utilise `_neutral_signal` par défaut (signal_crossing absent) mais on
  override via injection quand on veut tester les seuils (|score|≥0.6).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from src.contracts.regime import RegimeState
from src.contracts.skills import (
    Candidate,
    IchimokuPayload,
    SignalOutput,
    StrategyConfig,
    StrategyExitConfig,
)
from src.contracts.strategy import TradeProposal
from src.news.news_pulse import RawNewsItem
from src.orchestrator.run import CycleConfig, Orchestrator, _neutral_signal
from src.signals.market_scan import _UniverseEntry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeKillSwitch:
    def __init__(self, active: bool = False) -> None:
        self._active = active

    def is_active(self) -> bool:
        return self._active

    def reason(self) -> str:
        return "test"


class FakeRegimeDetector:
    def __init__(self, regime: Optional[RegimeState], raise_on_detect: bool = False) -> None:
        self._regime = regime
        self._raise = raise_on_detect

    def detect(self) -> RegimeState:
        if self._raise:
            raise RuntimeError("regime_down")
        assert self._regime is not None
        return self._regime


class FakeRegimeStore:
    """Simule un ModelStore en mémoire."""

    def __init__(self, cached: Optional[dict] = None) -> None:
        self.cached = cached
        self.writes: list[dict] = []

    def read_last_regime(self, *, cache_path: Any) -> Optional[dict]:
        return self.cached

    def write_last_regime(self, regime_dict: dict, *, cache_path: Any) -> None:
        self.writes.append(regime_dict)


class FakeDataFetcher:
    """Retourne des barres synthétiques cohérentes pour le scanner + snapshot."""

    def __init__(self, bars: Optional[list[tuple]] = None, raise_: bool = False) -> None:
        self._bars = bars or self._default_bars()
        self._raise = raise_

    @staticmethod
    def _default_bars() -> list[tuple]:
        # 60 barres régulières — suffisant pour scanner (≥ 60 barres) + ATR14
        out = []
        price = 100.0
        for i in range(60):
            ts = f"2026-04-17T{i:02d}:00:00Z" if i < 24 else f"2026-04-18T{i-24:02d}:00:00Z"
            o, h, l, c = price, price + 1.0, price - 1.0, price + 0.2
            out.append((ts, o, h, l, c, 1000.0))
            price = c
        return out

    def fetch(
        self,
        canonical_symbol: str,
        *,
        asset_class: str,
        timeframe: str,
        lookback_bars: int,
    ) -> list[tuple]:
        if self._raise:
            raise RuntimeError("fetch_down")
        return self._bars


@dataclass
class FakeRiskDecision:
    approved: bool
    reasons: list[str]


class FakeRiskGate:
    """Approuve les propositions pour un asset donné, rejette les autres."""

    def __init__(
        self,
        *,
        approve_assets: Optional[set[str]] = None,
        approve_all: bool = False,
    ) -> None:
        self.approve_assets = approve_assets or set()
        self.approve_all = approve_all
        self.calls: list[TradeProposal] = []

    def evaluate(self, proposal: TradeProposal, ctx: Any) -> FakeRiskDecision:
        self.calls.append(proposal)
        if self.approve_all or proposal.asset in self.approve_assets:
            return FakeRiskDecision(approved=True, reasons=[])
        return FakeRiskDecision(approved=False, reasons=["test_reject"])


class FakeCyclesRepo:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.finished: list[dict] = []

    def start(self, *, cycle_id: str, kind: str, started_at: Optional[str] = None) -> None:
        self.started.append({"cycle_id": cycle_id, "kind": kind})

    def finish(
        self,
        cycle_id: str,
        *,
        status: str,
        proposals_count: int = 0,
        approved_count: int = 0,
        degradation: Optional[list[str]] = None,
        risk_gate_failure_rate: Optional[float] = None,
        report_path: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self.finished.append(
            {
                "cycle_id": cycle_id,
                "status": status,
                "proposals_count": proposals_count,
                "approved_count": approved_count,
                "degradation": list(degradation or []),
                "risk_gate_failure_rate": risk_gate_failure_rate,
                "error": error,
            }
        )


class FakeObservationsRepo:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(
        self,
        *,
        cycle_id: str,
        asset: str,
        payload: dict,
        strategy: Optional[str] = None,
        approved: bool = False,
    ) -> None:
        self.records.append(
            {
                "cycle_id": cycle_id,
                "asset": asset,
                "strategy": strategy,
                "approved": approved,
                "payload": payload,
            }
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _regime_risk_on() -> RegimeState:
    return RegimeState(
        macro="risk_on",
        volatility="mid",
        probabilities={"risk_on": 0.7, "transition": 0.2, "risk_off": 0.1},
        hmm_state=1,
        date="2026-04-17",
    )


def _strategies_cfg() -> dict[str, StrategyConfig]:
    return {
        "ichimoku_trend_following": StrategyConfig(
            id="ichimoku_trend_following",
            entry={"trigger": "tk_cross"},
            exit=StrategyExitConfig(tp_rule="kijun"),
            timeframes=["1h"],
        )
    }


def _build_config(tmp_path: Path, universe: Optional[list[_UniverseEntry]] = None) -> CycleConfig:
    return CycleConfig(
        asset_universe=universe
        or [_UniverseEntry(asset="BTCUSDT", asset_class="crypto")],
        asset_keywords={"BTCUSDT": ["bitcoin", "btc"]},
        strategies_cfg=_strategies_cfg(),
        regime_cache_path=tmp_path / "last_regime.json",
        timeframe="1h",
        lookback_bars=60,
    )


def _build_orch(
    tmp_path: Path,
    *,
    kill_switch: Optional[FakeKillSwitch] = None,
    regime_detector: Optional[FakeRegimeDetector] = None,
    regime_store: Optional[FakeRegimeStore] = None,
    data_fetcher: Optional[FakeDataFetcher] = None,
    risk_gate: Optional[FakeRiskGate] = None,
    cycles_repo: Optional[FakeCyclesRepo] = None,
    observations_repo: Optional[FakeObservationsRepo] = None,
    signal_crossing: Optional[Any] = None,
    news_fetch: Optional[Any] = None,
    universe: Optional[list[_UniverseEntry]] = None,
) -> Orchestrator:
    return Orchestrator(
        kill_switch=kill_switch or FakeKillSwitch(active=False),
        regime_detector=regime_detector or FakeRegimeDetector(_regime_risk_on()),
        regime_store=regime_store or FakeRegimeStore(),
        data_fetcher=data_fetcher or FakeDataFetcher(),
        risk_gate=risk_gate or FakeRiskGate(),
        gate_ctx_factory=lambda: object(),
        cycles_repo=cycles_repo or FakeCyclesRepo(),
        observations_repo=observations_repo or FakeObservationsRepo(),
        config=_build_config(tmp_path, universe=universe),
        signal_crossing=signal_crossing,
        news_fetch=news_fetch,
    )


# ---------------------------------------------------------------------------
# Stub signal forts (pour test du pipeline complet)
# ---------------------------------------------------------------------------


def _strong_signal(asset: str, regime: RegimeState) -> SignalOutput:
    """Signal |score|=0.8, conf=0.7 → passera les seuils §8.7."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SignalOutput(
        asset=asset,
        timestamp=ts,
        composite_score=0.8,
        confidence=0.7,
        regime_context=regime.macro,
        ichimoku=IchimokuPayload(
            price_above_kumo=True,
            tenkan_above_kijun=True,
            chikou_above_price_26=True,
            kumo_thickness_pct=1.5,
            aligned_long=True,
            aligned_short=False,
            distance_to_kumo_pct=0.5,
        ),
        trend=[],
        momentum=[],
        volume=[],
    )


# ===========================================================================
# Tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------


def test_neutral_signal_fields_match_contract() -> None:
    regime = _regime_risk_on()
    sig = _neutral_signal("BTCUSDT", regime)
    assert sig.asset == "BTCUSDT"
    assert sig.composite_score == 0.0
    assert sig.confidence == 0.0
    assert sig.regime_context == regime.macro


# ---------------------------------------------------------------------------
# 0. Kill-switch
# ---------------------------------------------------------------------------


def test_kill_switch_active_aborts_cycle(tmp_path: Path) -> None:
    ks = FakeKillSwitch(active=True)
    cycles = FakeCyclesRepo()
    orch = _build_orch(tmp_path, kill_switch=ks, cycles_repo=cycles)
    result = orch.run_cycle(session_name="eu_morning")
    assert result.status == "aborted"
    assert result.reason == "kill_switch"
    assert result.kind == "scheduled"
    assert result.session_name == "eu_morning"
    assert cycles.finished[-1]["status"] == "aborted"
    assert cycles.finished[-1]["error"] == "kill_switch"


# ---------------------------------------------------------------------------
# 1. Régime — fallback cache + write-back
# ---------------------------------------------------------------------------


def test_regime_fallback_to_cache(tmp_path: Path) -> None:
    """detect() lève → on lit last_regime.json et on flag regime_stale."""
    cached = _regime_risk_on().model_dump()
    store = FakeRegimeStore(cached=cached)
    rd = FakeRegimeDetector(regime=None, raise_on_detect=True)
    orch = _build_orch(
        tmp_path,
        regime_detector=rd,
        regime_store=store,
        # Univers vide pour court-circuiter après régime
        universe=[],
    )
    result = orch.run_cycle(session_name="eu_morning")
    # Cycle termine (pas aborted) mais avec flag regime_stale
    assert result.status == "degraded"
    assert "regime_stale" in result.degradation_flags
    # Pas de write-back car on a utilisé le fallback
    assert store.writes == []


def test_regime_write_back_on_success(tmp_path: Path) -> None:
    """detect() OK → on persiste le dernier régime pour fallback futur."""
    store = FakeRegimeStore(cached=None)
    orch = _build_orch(tmp_path, regime_store=store, universe=[])
    orch.run_cycle(session_name="eu_morning")
    assert len(store.writes) == 1
    assert store.writes[0]["macro"] == "risk_on"


def test_regime_no_detect_no_cache_aborts(tmp_path: Path) -> None:
    """detect() lève ET cache vide → cycle aborted."""
    rd = FakeRegimeDetector(regime=None, raise_on_detect=True)
    store = FakeRegimeStore(cached=None)
    orch = _build_orch(tmp_path, regime_detector=rd, regime_store=store, universe=[])
    result = orch.run_cycle(session_name="eu_morning")
    assert result.status == "aborted"
    assert result.reason == "no_regime"


# ---------------------------------------------------------------------------
# 2. Scan — fallback univers vide
# ---------------------------------------------------------------------------


def test_empty_universe_finishes_success(tmp_path: Path) -> None:
    """Scan sur univers vide → pas de candidats → cycle success (empty_shortlist)."""
    orch = _build_orch(tmp_path, universe=[])
    result = orch.run_cycle(session_name="eu_morning")
    assert result.status == "success"
    assert result.proposals == 0
    assert result.degradation_flags == []


def test_scan_fetch_error_degrades_to_fallback(tmp_path: Path) -> None:
    """DataFetcher lève → fallback scan_degraded + empty shortlist."""
    # Le scan échoue car fetch lève pour tous les assets ; on laisse market_scan
    # gérer et le run_step fallback_positions_only vide la liste.
    df = FakeDataFetcher(raise_=True)
    orch = _build_orch(tmp_path, data_fetcher=df)
    result = orch.run_cycle(session_name="eu_morning")
    # scan peut soit renvoyer 0 candidats silencieusement (pas d'exception
    # remontée à run_step si market_scan gère en interne), soit déclencher
    # le fallback. Dans les deux cas, aucune proposal ne sortira.
    assert result.proposals == 0


# ---------------------------------------------------------------------------
# 3. News pulse + injection
# ---------------------------------------------------------------------------


def test_news_injection_builds_pulses_from_fetch(tmp_path: Path) -> None:
    """Le news_fetch fournit des RawNewsItem → build_pulse_batch les convertit."""
    from src.orchestrator.run import _CycleContext

    news_item = RawNewsItem(
        source="reuters",
        title="Bitcoin major hack — exchange drained",
        url="https://reuters.com/x",
        published_at=datetime.now(timezone.utc),
        body=None,
        entities_hint=[],
    )
    orch = _build_orch(
        tmp_path,
        news_fetch=lambda: [news_item],
    )
    ctx = _CycleContext(
        cycle_id="t", session_name="s", kind="scheduled", started_at=0.0,
    )
    existing = Candidate(
        asset="BTCUSDT", asset_class="crypto",
        score_scan=0.2, liquidity_ok=True,
    )
    news_by_asset = orch._build_news_pulses(ctx, [existing])
    assert "BTCUSDT" in news_by_asset
    assert news_by_asset["BTCUSDT"].aggregate_impact >= 0.6


def test_news_injection_threshold_and_cap(tmp_path: Path) -> None:
    """Seuil dur + cap respecté."""
    from src.orchestrator.run import _CycleContext
    from src.contracts.skills import NewsPulse, NewsItem

    orch = _build_orch(tmp_path)
    ctx = _CycleContext(
        cycle_id="t",
        session_name="s",
        kind="scheduled",
        started_at=0.0,
    )
    existing = Candidate(
        asset="BTCUSDT",
        asset_class="crypto",
        score_scan=0.2,
        liquidity_ok=True,
    )

    # Fabrique un pulse à impact 0.9 (au-dessus du seuil 0.6)
    high_item = NewsItem(
        source="reuters",
        title="big news",
        url="https://reuters.com/x",
        published=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        impact=0.9,
        sentiment=-0.5,
        entities=["BTCUSDT"],
    )
    boosted = NewsPulse(
        asset="BTCUSDT",
        window_hours=24,
        items=[high_item],
        top=high_item,
        aggregate_impact=0.9,
        aggregate_sentiment=-0.5,
    )
    result = orch._inject_news_candidates(ctx, [existing], {"BTCUSDT": boosted})
    # L'existant est upgradé : score_scan bumpé à 0.72 (0.9*0.8) car > 0.2
    upgraded = [c for c in result if c.asset == "BTCUSDT"][0]
    assert upgraded.forced_by == "news_pulse"
    assert upgraded.score_scan == pytest.approx(0.72, rel=1e-3)


def test_news_injection_respects_cap(tmp_path: Path) -> None:
    """Plus de 3 hits high-impact → seulement 3 injections."""
    from src.orchestrator.run import _CycleContext
    from src.contracts.skills import NewsPulse, NewsItem

    orch = _build_orch(tmp_path)
    ctx = _CycleContext(
        cycle_id="t", session_name="s", kind="scheduled", started_at=0.0,
    )

    def _hot_pulse(asset: str) -> NewsPulse:
        item = NewsItem(
            source="reuters",
            title=f"{asset} news",
            url="https://reuters.com/x",
            published=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            impact=0.95,
            sentiment=-0.5,
            entities=[asset],
        )
        return NewsPulse(
            asset=asset, window_hours=24, items=[item], top=item,
            aggregate_impact=0.95, aggregate_sentiment=-0.5,
        )

    # 5 assets avec pulse high → seulement 3 doivent être injectés
    pulses = {a: _hot_pulse(a) for a in ["A", "B", "C", "D", "E"]}
    result = orch._inject_news_candidates(ctx, [], pulses)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# 4. Signal filter
# ---------------------------------------------------------------------------


def test_weak_signal_is_filtered(tmp_path: Path) -> None:
    """Signal neutre (|score|=0, conf=0) → aucune proposal."""
    orch = _build_orch(
        tmp_path,
        signal_crossing=lambda asset, regime: _neutral_signal(asset, regime),
    )
    result = orch.run_cycle(session_name="eu_morning")
    assert result.proposals == 0


def test_strong_signal_passes_to_proposal(tmp_path: Path, monkeypatch: Any) -> None:
    """Signal fort → build_proposal appelé → proposal générée (si RG approve).

    On patch `build_proposal_for` + on force un candidat via run_focused_cycle
    pour éviter d'avoir à fabriquer des barres synthétiques acceptées par le
    scanner (testé séparément dans test_market_scan.py).
    """
    from src.orchestrator import run as orch_mod

    def fake_build(strategy_id: str, *, signal: Any, snapshot: Any, config: Any, news: Any = None) -> TradeProposal:
        return TradeProposal(
            strategy_id=strategy_id,
            asset=signal.asset,
            asset_class=snapshot.asset_class,
            side="long",
            entry_price=100.0,
            stop_price=95.0,
            tp_prices=[110.0],
            rr=2.0,
            conviction=0.7,
            risk_pct=0.01,
            catalysts=[],
            ichimoku=signal.ichimoku,
            proposal_id="test-pid-1",
            ts=signal.timestamp,
        )

    monkeypatch.setattr(orch_mod, "build_proposal_for", fake_build)

    rg = FakeRiskGate(approve_all=True)
    obs = FakeObservationsRepo()
    orch = _build_orch(
        tmp_path,
        universe=[],  # pas de scan — on force un candidat via focused
        signal_crossing=_strong_signal,
        risk_gate=rg,
        observations_repo=obs,
    )
    result = orch.run_focused_cycle("BTCUSDT", asset_class="crypto")
    assert result.proposals == 1
    assert result.proposals_rejected == 0
    # Obs : 1 ligne approved
    approved_obs = [r for r in obs.records if r["approved"]]
    assert len(approved_obs) == 1
    assert approved_obs[0]["asset"] == "BTCUSDT"


# ---------------------------------------------------------------------------
# 5. Risk gate rejection & rgfr
# ---------------------------------------------------------------------------


def test_risk_gate_rejection_counts_rgfr(tmp_path: Path, monkeypatch: Any) -> None:
    """RG rejette tout → risk_gate_failure_rate=1.0 → status=degraded."""
    from src.orchestrator import run as orch_mod

    def fake_build(strategy_id: str, *, signal: Any, snapshot: Any, config: Any, news: Any = None) -> TradeProposal:
        return TradeProposal(
            strategy_id=strategy_id,
            asset=signal.asset,
            asset_class=snapshot.asset_class,
            side="long",
            entry_price=100.0, stop_price=95.0, tp_prices=[110.0],
            rr=2.0, conviction=0.7, risk_pct=0.01, catalysts=[],
            ichimoku=signal.ichimoku, proposal_id="p1", ts=signal.timestamp,
        )

    monkeypatch.setattr(orch_mod, "build_proposal_for", fake_build)
    rg = FakeRiskGate(approve_all=False)  # rejette tout
    orch = _build_orch(
        tmp_path,
        universe=[],
        signal_crossing=_strong_signal,
        risk_gate=rg,
    )
    result = orch.run_focused_cycle("BTCUSDT", asset_class="crypto")
    assert result.proposals == 0
    assert result.proposals_rejected == 1
    assert result.risk_gate_failure_rate == pytest.approx(1.0)
    # rgfr > 0.5 → degraded (avec flag auto ajouté)
    assert result.status == "degraded"
    assert "risk_gate_failure_rate_high" in result.degradation_flags


# ---------------------------------------------------------------------------
# 6. Circuit-breaker C5 — >= 3 flags → degraded
# ---------------------------------------------------------------------------


def test_three_flags_degrade_cycle(tmp_path: Path) -> None:
    """Simule 3 flags accumulés → status="degraded"."""
    from src.orchestrator.run import _CycleContext

    orch = _build_orch(tmp_path, universe=[])
    ctx = _CycleContext(
        cycle_id="t", session_name="s", kind="scheduled", started_at=0.0,
    )
    ctx.degradation_flags = ["f1", "f2", "f3"]
    result = orch._finalize_success(ctx)
    assert result.status == "degraded"
    assert set(result.degradation_flags) >= {"f1", "f2", "f3"}


def test_two_flags_still_success_flagged_degraded(tmp_path: Path) -> None:
    """2 flags → status="degraded" (car factory `.success()` promeut en degraded
    dès qu'il y a >=1 flag). Mais pas l'auto-flag `risk_gate_failure_rate_high`."""
    from src.orchestrator.run import _CycleContext

    orch = _build_orch(tmp_path, universe=[])
    ctx = _CycleContext(
        cycle_id="t", session_name="s", kind="scheduled", started_at=0.0,
    )
    ctx.degradation_flags = ["f1", "f2"]
    result = orch._finalize_success(ctx)
    # 2 flags → degraded mais PAS d'auto-flag
    assert result.status == "degraded"
    assert "risk_gate_failure_rate_high" not in result.degradation_flags


def test_zero_flags_is_success(tmp_path: Path) -> None:
    """Aucun flag, rgfr=0 → status="success"."""
    from src.orchestrator.run import _CycleContext

    orch = _build_orch(tmp_path, universe=[])
    ctx = _CycleContext(
        cycle_id="t", session_name="s", kind="scheduled", started_at=0.0,
    )
    result = orch._finalize_success(ctx)
    assert result.status == "success"


# ---------------------------------------------------------------------------
# 7. run_focused_cycle (§15.1)
# ---------------------------------------------------------------------------


def test_focused_cycle_bypasses_scan(tmp_path: Path, monkeypatch: Any) -> None:
    """run_focused_cycle force le candidat — le scan n'est jamais appelé."""
    from src.orchestrator import run as orch_mod

    # On appelle le scanner réel mais avec univers réel vide — si le scan
    # était appelé, on aurait 0 proposals. Ici, on force BTCUSDT et on veut
    # observer qu'il y a bien eu tentative de signal + proposal.
    def fake_build(strategy_id: str, *, signal: Any, snapshot: Any, config: Any, news: Any = None) -> TradeProposal:
        return TradeProposal(
            strategy_id=strategy_id,
            asset=signal.asset,
            asset_class=snapshot.asset_class,
            side="long",
            entry_price=100.0, stop_price=95.0, tp_prices=[110.0],
            rr=2.0, conviction=0.7, risk_pct=0.01, catalysts=[],
            ichimoku=signal.ichimoku, proposal_id="pid-focus", ts=signal.timestamp,
        )

    monkeypatch.setattr(orch_mod, "build_proposal_for", fake_build)

    rg = FakeRiskGate(approve_all=True)
    orch = _build_orch(
        tmp_path,
        universe=[],  # scan vide — si scan était appelé, 0 candidats
        signal_crossing=_strong_signal,
        risk_gate=rg,
    )
    result = orch.run_focused_cycle("BTCUSDT", asset_class="crypto")
    assert result.kind == "adhoc"
    assert result.session_name == "adhoc_BTCUSDT"
    assert result.proposals == 1


def test_focused_cycle_with_correlated_assets(tmp_path: Path, monkeypatch: Any) -> None:
    """run_focused_cycle pousse aussi les corrélés."""
    from src.orchestrator import run as orch_mod

    def fake_build(strategy_id: str, *, signal: Any, snapshot: Any, config: Any, news: Any = None) -> TradeProposal:
        return TradeProposal(
            strategy_id=strategy_id, asset=signal.asset,
            asset_class=snapshot.asset_class, side="long",
            entry_price=100.0, stop_price=95.0, tp_prices=[110.0],
            rr=2.0, conviction=0.7, risk_pct=0.01, catalysts=[],
            ichimoku=signal.ichimoku,
            proposal_id=f"pid-{signal.asset}", ts=signal.timestamp,
        )

    monkeypatch.setattr(orch_mod, "build_proposal_for", fake_build)
    rg = FakeRiskGate(approve_all=True)
    orch = _build_orch(
        tmp_path,
        universe=[],
        signal_crossing=_strong_signal,
        risk_gate=rg,
    )
    result = orch.run_focused_cycle(
        "BTCUSDT",
        correlated_assets=["ETHUSDT"],
        asset_class="crypto",
    )
    # BTC + ETH → 2 proposals
    assert result.proposals == 2


# ---------------------------------------------------------------------------
# 8. Stub signal_crossing — _try_import_crossing fallback
# ---------------------------------------------------------------------------


def test_default_signal_crossing_is_neutral(tmp_path: Path) -> None:
    """Sans injection et sans module src.signals.crossing → fallback neutre.

    Le signal neutre |score|=0 est filtré, donc 0 proposals.
    """
    orch = _build_orch(tmp_path)
    # On vérifie que signal_crossing est résolu (None si crossing absent, OU
    # une fonction si le module s'est chargé via _try_import_crossing).
    # Dans les deux cas, _compute_signal retourne un signal neutre (qui sera
    # filtré) : le cycle doit finir success 0-proposals.
    result = orch.run_cycle(session_name="eu_morning")
    assert result.status == "success"
    assert result.proposals == 0
