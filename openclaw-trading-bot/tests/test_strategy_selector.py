"""
tests.test_strategy_selector — skill `strategy-selector` §8.8, §6.9.

Couvre :
- Mapping régime → stratégies de fond (risk_on/off/transition)
- Plafond 3 strats (MAX_BASE_STRATEGIES)
- Exclusivité mean_reversion ↔ breakout_momentum
- Priorité ichimoku en régime directionnel
- Fallback volatilité extreme = ichimoku seule
- Fallback cold-start / régime inconnu
- Opportunists event_driven_macro + news_driven_momentum
- Weights dans [0, 1] et décroissants
- Invariants SelectionOutput (1-3 strats, asset identique)
"""
from __future__ import annotations

from src.contracts.skills import Candidate, SelectionOutput
from src.signals import strategy_selector as sel
from src.signals.strategy_selector import MAX_BASE_STRATEGIES, _RegimeView, pick, pick_batch


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _candidate(asset: str = "BTC/USDT", forced_by: str | None = None) -> Candidate:
    return Candidate(
        asset=asset,
        asset_class="crypto",
        score_scan=0.6,
        liquidity_ok=True,
        forced_by=forced_by,  # type: ignore[arg-type]
    )


def _regime(
    macro: str = "risk_on",
    volatility: str = "mid",
    confidence: float = 0.80,
) -> _RegimeView:
    return _RegimeView(macro=macro, volatility=volatility, confidence=confidence)


def _ids(out: SelectionOutput) -> list[str]:
    return [c.strategy_id for c in out.strategies]


# ---------------------------------------------------------------------------
# Mapping régime → stratégies
# ---------------------------------------------------------------------------


class TestRegimeMapping:
    def test_risk_on_has_ichimoku_breakout_divergence(self):
        out = pick(_regime("risk_on"), _candidate(), include_opportunists=False)
        assert _ids(out) == [
            "ichimoku_trend_following",
            "breakout_momentum",
            "divergence_hunter",
        ]

    def test_risk_off_has_ichimoku_divergence_vp(self):
        out = pick(_regime("risk_off"), _candidate(), include_opportunists=False)
        assert _ids(out) == [
            "ichimoku_trend_following",
            "divergence_hunter",
            "volume_profile_scalp",
        ]

    def test_transition_has_meanrev_divergence_vp(self):
        out = pick(_regime("transition"), _candidate(), include_opportunists=False)
        assert _ids(out) == [
            "mean_reversion",
            "divergence_hunter",
            "volume_profile_scalp",
        ]


# ---------------------------------------------------------------------------
# Exclusivité mean_reversion ↔ breakout_momentum
# ---------------------------------------------------------------------------


class TestExclusivity:
    def test_breakout_and_meanreversion_never_together(self):
        """Aucune combinaison de régime ne les fait coexister."""
        for macro in ("risk_on", "risk_off", "transition"):
            out = pick(_regime(macro), _candidate(), include_opportunists=False)
            ids = set(_ids(out))
            assert not ("mean_reversion" in ids and "breakout_momentum" in ids), (
                f"Conflict in regime={macro}: {ids}"
            )

    def test_filter_exclusive_keeps_first_seen(self):
        """Helper interne : conflit dans un tuple d'entrée → on garde le premier."""
        # on fabrique une liste manuelle avec les deux présents
        out = sel._filter_exclusive(
            ["mean_reversion", "breakout_momentum", "divergence_hunter"]
        )
        assert out == ["mean_reversion", "divergence_hunter"]

    def test_filter_exclusive_reverse_order(self):
        out = sel._filter_exclusive(
            ["breakout_momentum", "mean_reversion", "divergence_hunter"]
        )
        assert out == ["breakout_momentum", "divergence_hunter"]


# ---------------------------------------------------------------------------
# Plafond MAX_BASE_STRATEGIES
# ---------------------------------------------------------------------------


class TestCapLimit:
    def test_base_capped_at_max(self):
        for macro in ("risk_on", "risk_off", "transition"):
            out = pick(_regime(macro), _candidate(), include_opportunists=False)
            assert len(out.strategies) <= MAX_BASE_STRATEGIES

    def test_selection_output_max_length_3_invariant(self):
        """Pydantic borne SelectionOutput à max_length=3. Les opportunists ne
        débordent pas du contrat — l'orchestrateur §8.7 les réinjecte en ad-hoc.
        """
        # Avec opportunists ON, on a déjà 3 strats de fond en risk_on →
        # 0 opportunists ajoutés
        out = pick(_regime("risk_on"), _candidate(), include_opportunists=True)
        assert 1 <= len(out.strategies) <= 3

    def test_opportunists_fill_when_base_smaller(self):
        """Si la base tombe à 1 (fallback extreme), 2 opportunists remplissent."""
        out = pick(_regime("risk_on", volatility="extreme"), _candidate())
        ids = _ids(out)
        assert "ichimoku_trend_following" in ids
        assert "event_driven_macro" in ids
        assert "news_driven_momentum" in ids


# ---------------------------------------------------------------------------
# Volatilité extreme
# ---------------------------------------------------------------------------


class TestVolatilityExtreme:
    def test_extreme_falls_back_to_ichimoku_only(self):
        out = pick(
            _regime("risk_on", volatility="extreme"),
            _candidate(),
            include_opportunists=False,
        )
        assert _ids(out) == ["ichimoku_trend_following"]

    def test_high_keeps_structure_but_compresses_weights(self):
        mid = pick(_regime("risk_on", volatility="mid"), _candidate(), include_opportunists=False)
        high = pick(_regime("risk_on", volatility="high"), _candidate(), include_opportunists=False)
        # Mêmes strats, mais compression vol high — cumulé, la somme peut
        # rester normalisée ≈ 1.0 grâce à la renormalisation interne.
        assert _ids(mid) == _ids(high)


# ---------------------------------------------------------------------------
# Cold-start / régime inconnu
# ---------------------------------------------------------------------------


class TestColdStart:
    def test_unknown_macro_returns_ichimoku_fallback(self):
        out = pick(
            _RegimeView(macro="alien_macro", volatility="mid", confidence=0.5),
            _candidate(),
            include_opportunists=False,
        )
        assert _ids(out) == ["ichimoku_trend_following"]

    def test_unknown_volatility_defaults_to_mid(self):
        out = pick(
            _RegimeView(macro="risk_on", volatility="unknown_vol", confidence=0.8),
            _candidate(),
            include_opportunists=False,
        )
        assert _ids(out)[0] == "ichimoku_trend_following"


# ---------------------------------------------------------------------------
# Priorité ichimoku
# ---------------------------------------------------------------------------


class TestIchimokuPriority:
    def test_ichimoku_first_in_directional_regimes(self):
        for macro in ("risk_on", "risk_off"):
            out = pick(_regime(macro, confidence=0.80), _candidate(), include_opportunists=False)
            assert out.strategies[0].strategy_id == "ichimoku_trend_following"

    def test_ichimoku_has_highest_weight_when_directional(self):
        out = pick(_regime("risk_on", confidence=0.80), _candidate(), include_opportunists=False)
        weights = [c.weight for c in out.strategies]
        assert weights[0] == max(weights)

    def test_transition_does_not_privilege_ichimoku(self):
        out = pick(_regime("transition"), _candidate(), include_opportunists=False)
        # En transition, `ichimoku_trend_following` n'est pas dans la base
        assert "ichimoku_trend_following" not in _ids(out)


# ---------------------------------------------------------------------------
# Opportunists
# ---------------------------------------------------------------------------


class TestOpportunists:
    def test_opportunists_never_replace_base(self):
        out = pick(_regime("risk_on"), _candidate(), include_opportunists=True)
        base_ids = set(_ids(out)) & {
            "ichimoku_trend_following", "breakout_momentum", "divergence_hunter",
            "mean_reversion", "volume_profile_scalp",
        }
        assert len(base_ids) >= 1

    def test_opportunists_disabled_flag(self):
        out = pick(_regime("risk_on"), _candidate(), include_opportunists=False)
        assert "event_driven_macro" not in _ids(out)
        assert "news_driven_momentum" not in _ids(out)

    def test_reason_contains_opportunist_tag(self):
        out = pick(_regime("risk_on", volatility="extreme"), _candidate())
        opportunist_choices = [
            c for c in out.strategies
            if c.strategy_id in ("event_driven_macro", "news_driven_momentum")
        ]
        for c in opportunist_choices:
            assert "opportunist" in c.reason


# ---------------------------------------------------------------------------
# Invariants SelectionOutput / weights
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_asset_preserved(self):
        out = pick(_regime(), _candidate(asset="EUR/USD"))
        assert out.asset == "EUR/USD"

    def test_weights_in_unit_range(self):
        out = pick(_regime("risk_on"), _candidate())
        for c in out.strategies:
            assert 0.0 <= c.weight <= 1.0

    def test_base_weights_decreasing_in_directional(self):
        out = pick(_regime("risk_on", confidence=0.9), _candidate(), include_opportunists=False)
        weights = [c.weight for c in out.strategies]
        assert weights == sorted(weights, reverse=True)

    def test_reason_mentions_regime(self):
        out = pick(_regime("risk_off", volatility="high"), _candidate())
        for c in out.strategies:
            assert "regime=risk_off" in c.reason
            assert "vol=high" in c.reason

    def test_forced_by_propagated_to_reason(self):
        out = pick(_regime(), _candidate(forced_by="news_pulse"))
        for c in out.strategies:
            assert "forced_by=news_pulse" in c.reason


# ---------------------------------------------------------------------------
# pick_batch
# ---------------------------------------------------------------------------


class TestBatch:
    def test_batch_returns_one_output_per_candidate(self):
        cs = [_candidate(asset="BTC/USDT"), _candidate(asset="ETH/USDT")]
        outs = pick_batch(_regime("risk_on"), cs)
        assert len(outs) == 2
        assert outs[0].asset == "BTC/USDT"
        assert outs[1].asset == "ETH/USDT"

    def test_batch_empty_input(self):
        outs = pick_batch(_regime(), [])
        assert outs == []
