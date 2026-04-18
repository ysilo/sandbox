"""
tests.test_strategies — 7 build_proposal (§6 TRADING_BOT_ARCHITECTURE.md).

Approche :
- Les modules stratégie sont **purs** et déterministes : on leur injecte
  un SignalOutput + MarketSnapshot + StrategyConfig + NewsPulse fabriqués à la main.
- Pour chaque stratégie, on vérifie :
    1. Accept path (toutes conditions vertes) → TradeProposal non-None
    2. Reject composite_score < seuil
    3. Reject Ichimoku non aligné (pour strats qui requiert alignment)
    4. Reject par condition stratégie-spécifique (ex: ADX trop faible, pas de news…)
    5. RR < min_rr → None (via tp trop proche du stop)
    6. Invariants TradeProposal : strategy_id, side, rr >= min_rr, conviction clamp

Un test invariants global vérifie que chaque (strategy_id, proposal) respecte :
- proposal.strategy_id == NAME du module
- proposal.rr >= config.min_rr
- 0 <= proposal.conviction <= 1
- proposal.risk_pct <= config.max_risk_pct_equity
- len(proposal.tp_prices) >= 1
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from src.contracts.skills import (
    IchimokuPayload,
    IndicatorScore,
    MarketSnapshot,
    NewsItem,
    NewsPulse,
    SignalOutput,
    StrategyConfig,
    StrategyExitConfig,
)
from src.contracts.strategy import TradeProposal
from src.strategies import (
    STRATEGY_REGISTRY,
    breakout_momentum,
    build_proposal_for,
    divergence_hunter,
    event_driven_macro,
    ichimoku_trend_following,
    mean_reversion,
    news_driven_momentum,
    volume_profile_scalp,
)
from src.strategies import _common as common


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ichimoku(aligned: str = "long") -> IchimokuPayload:
    """`aligned` ∈ {long, short, neutral}."""
    is_long = aligned == "long"
    is_short = aligned == "short"
    return IchimokuPayload(
        price_above_kumo=is_long,
        tenkan_above_kijun=is_long,
        chikou_above_price_26=is_long,
        kumo_thickness_pct=0.5,
        aligned_long=is_long,
        aligned_short=is_short,
        distance_to_kumo_pct=(1.2 if is_long else -1.2 if is_short else 0.0),
    )


def _ind(name: str, score: float, conf: float = 0.9) -> IndicatorScore:
    return IndicatorScore(name=name, score=score, confidence=conf)


def _signal(
    *,
    composite: float = 0.90,
    confidence: float = 0.90,
    aligned: str = "long",
    trend: Optional[list[IndicatorScore]] = None,
    momentum: Optional[list[IndicatorScore]] = None,
    volume: Optional[list[IndicatorScore]] = None,
    regime: str = "risk_on",
) -> SignalOutput:
    return SignalOutput(
        asset="BTC/USDT",
        timestamp=_utc(),
        composite_score=composite,
        confidence=confidence,
        regime_context=regime,  # type: ignore[arg-type]
        ichimoku=_ichimoku(aligned),
        trend=trend or [],
        momentum=momentum or [],
        volume=volume or [],
    )


def _snapshot(*, last_close: float = 100.0, atr: float = 2.0,
              asset_class: str = "crypto") -> MarketSnapshot:
    # 5 barres OHLCV minimum ; on met le close à la dernière.
    ohlcv: list[tuple[str, float, float, float, float, float]] = [
        (_utc(), last_close - 1, last_close + 0.5, last_close - 1.5, last_close - 0.5, 1000.0),
        (_utc(), last_close - 0.5, last_close + 0.8, last_close - 1.0, last_close, 1200.0),
    ]
    return MarketSnapshot(
        asset="BTC/USDT",
        asset_class=asset_class,  # type: ignore[arg-type]
        ts=_utc(),
        ohlcv=ohlcv,
        timeframe="4h",
        atr_14=atr,
    )


def _cfg(
    strategy_id: str,
    *,
    min_composite: float = 0.80,
    min_rr: float = 1.5,
    requires_ichimoku: bool = True,
    atr_mult: float = 1.5,
    tp_rule: str = "r_multiple",
    tp_r: Optional[list[float]] = None,
    entry: Optional[dict] = None,
) -> StrategyConfig:
    return StrategyConfig(
        id=strategy_id,
        enabled=True,
        requires_ichimoku_alignment=requires_ichimoku,
        max_risk_pct_equity=0.01,
        min_rr=min_rr,
        min_composite_score=min_composite,
        coef_self_improve=1.0,
        entry=entry or {},
        exit=StrategyExitConfig(
            atr_stop_mult=atr_mult,
            tp_rule=tp_rule,  # type: ignore[arg-type]
            tp_r_multiples=tp_r or [2.0, 3.0],
            trailing=None,
        ),
        timeframes=["4h"],
    )


def _news(
    *,
    impact: float = 0.80,
    sentiment: float = 0.70,
    source: str = "reuters_rss",
    title: str = "Fed raises rates",
    entities: Optional[list[str]] = None,
) -> NewsPulse:
    item = NewsItem(
        source=source,
        title=title,
        url="https://example.com/news",
        published=_utc(),
        impact=impact,
        sentiment=sentiment,
        entities=entities or ["USD"],
    )
    return NewsPulse(
        asset="BTC/USDT",
        window_hours=24,
        items=[item],
        top=item,
        aggregate_impact=impact,
        aggregate_sentiment=sentiment,
    )


# ---------------------------------------------------------------------------
# Helpers — Checkers invariants
# ---------------------------------------------------------------------------


def assert_proposal_valid(p: TradeProposal, cfg: StrategyConfig, expected_id: str):
    assert isinstance(p, TradeProposal)
    assert p.strategy_id == expected_id
    assert p.side in ("long", "short")
    assert p.rr >= cfg.min_rr - 1e-9
    assert 0.0 <= p.conviction <= 1.0
    assert 0.0 <= p.risk_pct <= cfg.max_risk_pct_equity + 1e-9
    assert len(p.tp_prices) >= 1
    # Direction-coherent stop & tp
    if p.side == "long":
        assert p.stop_price < p.entry_price
        assert all(tp > p.entry_price for tp in p.tp_prices)
    else:
        assert p.stop_price > p.entry_price
        assert all(tp < p.entry_price for tp in p.tp_prices)


# ---------------------------------------------------------------------------
# _common helpers
# ---------------------------------------------------------------------------


class TestCommonHelpers:
    def test_compute_stop_long(self):
        s = common.compute_stop(side="long", entry=100.0, atr=2.0, atr_mult=1.5)
        assert s == pytest.approx(97.0)

    def test_compute_stop_short(self):
        s = common.compute_stop(side="short", entry=100.0, atr=2.0, atr_mult=1.5)
        assert s == pytest.approx(103.0)

    def test_compute_rr(self):
        r = common.compute_rr(entry=100.0, stop=97.0, tp_list=[106.0, 112.0])
        assert r == pytest.approx(2.0)

    def test_compute_rr_rejects_zero_risk(self):
        with pytest.raises(ValueError):
            common.compute_rr(entry=100.0, stop=100.0, tp_list=[105.0])

    def test_adjusted_conviction_clamps(self):
        assert common.adjusted_conviction(0.9, 1.3) == pytest.approx(1.0)
        assert common.adjusted_conviction(0.1, 0.5) == pytest.approx(0.05)
        assert common.adjusted_conviction(-0.5, 1.0) == 0.0

    def test_tp_from_config_r_multiple(self):
        exit_cfg = StrategyExitConfig(
            atr_stop_mult=1.5, tp_rule="r_multiple",
            tp_r_multiples=[2.0, 3.0], trailing=None,
        )
        tp = common.tp_from_config(
            side="long", entry=100.0, stop=97.0,
            exit_cfg=exit_cfg, ichimoku=_ichimoku("long"),
        )
        assert tp == pytest.approx([106.0, 109.0])

    def test_tp_from_config_kijun(self):
        exit_cfg = StrategyExitConfig(
            atr_stop_mult=1.5, tp_rule="kijun",
            tp_r_multiples=[2.0, 3.0], trailing=None,
        )
        tp = common.tp_from_config(
            side="long", entry=100.0, stop=97.0,
            exit_cfg=exit_cfg, ichimoku=_ichimoku("long"),
            kijun=108.0,
        )
        # TP1 = kijun, TP2 = r_multiple[-1] * R = 3.0 * 3 + 100 = 109
        assert tp == pytest.approx([108.0, 109.0])

    def test_tp_from_config_kijun_fallback_when_unusable(self):
        exit_cfg = StrategyExitConfig(
            atr_stop_mult=1.5, tp_rule="kijun",
            tp_r_multiples=[2.0, 3.0], trailing=None,
        )
        # Kijun en-dessous du prix de long → inutilisable, fallback r_multiple
        tp = common.tp_from_config(
            side="long", entry=100.0, stop=97.0,
            exit_cfg=exit_cfg, ichimoku=_ichimoku("long"),
            kijun=95.0,
        )
        assert tp == pytest.approx([106.0, 109.0])

    def test_infer_side_long(self):
        sig = _signal(composite=0.8, aligned="long")
        assert common.infer_side(sig) == "long"

    def test_infer_side_neutral_ichimoku_returns_none(self):
        sig = _signal(composite=0.8, aligned="neutral")
        assert common.infer_side(sig) is None

    def test_passes_composite_gate(self):
        sig = _signal(composite=0.70, confidence=0.70)
        cfg = _cfg("x", min_composite=0.80)
        assert not common.passes_composite_gate(sig, cfg)

    def test_passes_ichimoku_gate_waiver(self):
        """Waiver : accepte tout sauf alignement inverse strict."""
        cfg = _cfg("x", requires_ichimoku=False)
        sig_neutral = _signal(aligned="neutral")
        # neutre + long candidat → accepté (pas d'alignment inverse)
        assert common.passes_ichimoku_gate(sig_neutral, cfg, "long")
        # aligned_short + long candidat → REJETÉ (anti-golden-rule)
        sig_short = _signal(aligned="short")
        assert not common.passes_ichimoku_gate(sig_short, cfg, "long")


# ---------------------------------------------------------------------------
# ichimoku_trend_following
# ---------------------------------------------------------------------------


class TestIchimokuTrendFollowing:
    NAME = ichimoku_trend_following.NAME

    def _base_inputs(self, *, aligned="long"):
        signal = _signal(
            composite=0.90, confidence=0.90, aligned=aligned,
            trend=[_ind("supertrend", 0.7), _ind("adx_14", 0.30)],
        )
        snap = _snapshot()
        cfg = _cfg(
            self.NAME, min_composite=0.82, min_rr=2.0,
            requires_ichimoku=True, atr_mult=1.5, tp_rule="kijun",
            tp_r=[1.5, 3.0], entry={"adx_min": 25},
        )
        return signal, snap, cfg

    def test_accepts_full_long_setup(self):
        signal, snap, cfg = self._base_inputs()
        p = ichimoku_trend_following.build_proposal(
            signal=signal, snapshot=snap, config=cfg, kijun=110.0,
        )
        assert p is not None
        assert_proposal_valid(p, cfg, self.NAME)
        assert p.side == "long"
        assert "ichimoku_aligned" in p.catalysts

    def test_rejects_when_composite_below_min(self):
        signal, snap, cfg = self._base_inputs()
        signal = _signal(
            composite=0.70, confidence=0.70, aligned="long",
            trend=[_ind("supertrend", 0.7), _ind("adx_14", 0.30)],
        )
        p = ichimoku_trend_following.build_proposal(
            signal=signal, snapshot=snap, config=cfg, kijun=110.0,
        )
        assert p is None

    def test_rejects_without_ichimoku_alignment(self):
        signal, snap, cfg = self._base_inputs(aligned="neutral")
        p = ichimoku_trend_following.build_proposal(
            signal=signal, snapshot=snap, config=cfg, kijun=110.0,
        )
        assert p is None

    def test_rejects_when_adx_below_min(self):
        signal = _signal(
            composite=0.90, confidence=0.90, aligned="long",
            trend=[_ind("supertrend", 0.7), _ind("adx_14", 0.10)],  # ADX trop bas
        )
        snap = _snapshot()
        cfg = _cfg(
            self.NAME, min_composite=0.82, min_rr=2.0,
            entry={"adx_min": 25},
        )
        p = ichimoku_trend_following.build_proposal(
            signal=signal, snapshot=snap, config=cfg, kijun=110.0,
        )
        assert p is None

    def test_rejects_when_rr_below_min(self):
        """Stop très proche de l'entry + tp serré → RR < 2.0."""
        signal = _signal(
            composite=0.90, confidence=0.90, aligned="long",
            trend=[_ind("supertrend", 0.7), _ind("adx_14", 0.30)],
        )
        snap = _snapshot(last_close=100.0, atr=5.0)  # gros ATR → gros stop
        cfg = _cfg(
            self.NAME, min_composite=0.82, min_rr=2.0,
            atr_mult=1.5, tp_rule="r_multiple", tp_r=[1.0, 1.5],  # RR<2.0
            entry={"adx_min": 25},
        )
        p = ichimoku_trend_following.build_proposal(
            signal=signal, snapshot=snap, config=cfg,
        )
        assert p is None


# ---------------------------------------------------------------------------
# breakout_momentum
# ---------------------------------------------------------------------------


class TestBreakoutMomentum:
    NAME = breakout_momentum.NAME

    def _inputs(self, *, aligned="long"):
        signal = _signal(
            composite=0.85, confidence=0.85, aligned=aligned,
            trend=[
                _ind("bollinger", 0.6),
                _ind("aroon", 0.7),
                _ind("macd", 0.5),
            ],
            volume=[_ind("obv", 0.6)],
        )
        snap = _snapshot()
        cfg = _cfg(
            self.NAME, min_composite=0.80, min_rr=2.0,
            requires_ichimoku=True, atr_mult=2.0, tp_rule="r_multiple",
            tp_r=[2.0, 4.0], entry={"volume_ratio_min": 1.8},
        )
        return signal, snap, cfg

    def test_accepts_full_setup(self):
        s, snap, cfg = self._inputs()
        # volume_ratio kwarg optionnel : fourni par l'orchestrateur
        p = breakout_momentum.build_proposal(
            signal=s, snapshot=snap, config=cfg, volume_ratio=2.0,
        )
        assert p is not None
        assert_proposal_valid(p, cfg, self.NAME)

    def test_accepts_without_volume_ratio_kwarg(self):
        """Si orchestrateur ne calcule pas le ratio, gate désactivé."""
        s, snap, cfg = self._inputs()
        p = breakout_momentum.build_proposal(signal=s, snapshot=snap, config=cfg)
        assert p is not None

    def test_rejects_without_bollinger_break(self):
        signal = _signal(
            composite=0.85, confidence=0.85, aligned="long",
            trend=[_ind("bollinger", 0.0), _ind("aroon", 0.7), _ind("macd", 0.5)],
            volume=[_ind("obv", 0.6)],
        )
        snap = _snapshot()
        cfg = _cfg(self.NAME, min_composite=0.80, min_rr=2.0)
        p = breakout_momentum.build_proposal(signal=signal, snapshot=snap, config=cfg)
        assert p is None

    def test_rejects_when_volume_ratio_too_low(self):
        s, snap, cfg = self._inputs()
        # volume_ratio=1.2 < volume_ratio_min=1.8 → reject
        p = breakout_momentum.build_proposal(
            signal=s, snapshot=snap, config=cfg, volume_ratio=1.2,
        )
        assert p is None


# ---------------------------------------------------------------------------
# mean_reversion
# ---------------------------------------------------------------------------


class TestMeanReversion:
    NAME = mean_reversion.NAME

    def _inputs(self):
        # RSI très bas → signal long contrarian
        signal = _signal(
            composite=0.85, confidence=0.85, aligned="neutral",  # waiver
            momentum=[
                _ind("rsi_14", -0.7),     # survente
                _ind("cci", -0.7),         # extrême
                _ind("stochastic", 0.5),   # cross up
            ],
            trend=[_ind("adx_14", 0.20)],  # ADX modéré
        )
        snap = _snapshot()
        cfg = _cfg(
            self.NAME, min_composite=0.80, min_rr=1.5,
            requires_ichimoku=False, atr_mult=1.2, tp_rule="r_multiple",
            tp_r=[1.5, 2.5],
        )
        return signal, snap, cfg

    def test_accepts_oversold_long(self):
        s, snap, cfg = self._inputs()
        p = mean_reversion.build_proposal(signal=s, snapshot=snap, config=cfg)
        assert p is not None
        assert p.side == "long"
        assert_proposal_valid(p, cfg, self.NAME)

    def test_rejects_when_adx_too_high(self):
        signal = _signal(
            composite=0.85, confidence=0.85, aligned="neutral",
            momentum=[
                _ind("rsi_14", -0.7), _ind("cci", -0.7), _ind("stochastic", 0.5),
            ],
            trend=[_ind("adx_14", 0.50)],  # tendance forte
        )
        snap = _snapshot()
        cfg = _cfg(self.NAME, requires_ichimoku=False, min_rr=1.5)
        p = mean_reversion.build_proposal(signal=signal, snapshot=snap, config=cfg)
        assert p is None

    def test_rejects_neutral_rsi(self):
        signal = _signal(
            composite=0.85, confidence=0.85, aligned="neutral",
            momentum=[_ind("rsi_14", 0.0), _ind("cci", 0.0), _ind("stochastic", 0.0)],
            trend=[_ind("adx_14", 0.20)],
        )
        snap = _snapshot()
        cfg = _cfg(self.NAME, requires_ichimoku=False)
        p = mean_reversion.build_proposal(signal=signal, snapshot=snap, config=cfg)
        assert p is None

    def test_rejects_when_ichimoku_opposed(self):
        """Long contrarian mais ichimoku aligned_short → anti-golden-rule."""
        signal = _signal(
            composite=0.85, confidence=0.85, aligned="short",
            momentum=[_ind("rsi_14", -0.7), _ind("cci", -0.7), _ind("stochastic", 0.5)],
            trend=[_ind("adx_14", 0.20)],
        )
        snap = _snapshot()
        cfg = _cfg(self.NAME, requires_ichimoku=False, min_rr=1.5)
        p = mean_reversion.build_proposal(signal=signal, snapshot=snap, config=cfg)
        assert p is None


# ---------------------------------------------------------------------------
# divergence_hunter
# ---------------------------------------------------------------------------


class TestDivergenceHunter:
    NAME = divergence_hunter.NAME

    def _inputs(self):
        signal = _signal(
            composite=0.80, confidence=0.80, aligned="neutral",
            momentum=[
                _ind("divergence_rsi", 0.6),   # bullish divergence
                _ind("rsi_14", -0.3),           # zone extrême
            ],
            volume=[_ind("cmf", 0.4)],          # CMF contrarian
        )
        snap = _snapshot()
        cfg = _cfg(
            self.NAME, min_composite=0.70, min_rr=2.0,
            requires_ichimoku=False, atr_mult=1.8,
            tp_rule="r_multiple", tp_r=[2.0, 3.0],
        )
        return signal, snap, cfg

    def test_accepts_bullish_divergence(self):
        s, snap, cfg = self._inputs()
        p = divergence_hunter.build_proposal(signal=s, snapshot=snap, config=cfg)
        assert p is not None
        assert p.side == "long"
        assert_proposal_valid(p, cfg, self.NAME)

    def test_rejects_without_divergence_score(self):
        signal = _signal(
            composite=0.80, confidence=0.80, aligned="neutral",
            momentum=[_ind("rsi_14", -0.3)],  # pas de divergence_rsi
            volume=[_ind("cmf", 0.4)],
        )
        snap = _snapshot()
        cfg = _cfg(self.NAME, min_composite=0.70, requires_ichimoku=False)
        p = divergence_hunter.build_proposal(signal=signal, snapshot=snap, config=cfg)
        assert p is None

    def test_rejects_macd_divergence_opposed(self):
        signal = _signal(
            composite=0.80, confidence=0.80, aligned="neutral",
            momentum=[
                _ind("divergence_rsi", 0.6),
                _ind("divergence_macd", -0.5),  # OPPOSE
                _ind("rsi_14", -0.3),
            ],
            volume=[_ind("cmf", 0.4)],
        )
        snap = _snapshot()
        cfg = _cfg(self.NAME, min_composite=0.70, requires_ichimoku=False, min_rr=2.0)
        p = divergence_hunter.build_proposal(signal=signal, snapshot=snap, config=cfg)
        assert p is None


# ---------------------------------------------------------------------------
# volume_profile_scalp
# ---------------------------------------------------------------------------


class TestVolumeProfileScalp:
    NAME = volume_profile_scalp.NAME

    def _inputs(self):
        signal = _signal(
            composite=0.75, confidence=0.75, aligned="neutral",
            momentum=[_ind("rsi_14", 0.1)],  # neutre
            volume=[_ind("volume_profile", 0.5), _ind("vwap", 0.4)],
        )
        # ATR 1.0 + atr_mult 1.0 → stop_distance=1.0, HVN à 102.5 → RR=2.5
        snap = _snapshot(atr=1.0)
        cfg = _cfg(
            self.NAME, min_composite=0.70, min_rr=1.5,
            requires_ichimoku=False, atr_mult=1.0,
            tp_rule="hvn", tp_r=[1.5, 2.0],
        )
        return signal, snap, cfg

    def test_accepts_vp_bounce(self):
        s, snap, cfg = self._inputs()
        p = volume_profile_scalp.build_proposal(
            signal=s, snapshot=snap, config=cfg, hvn_levels=[102.5, 105.0],
        )
        assert p is not None
        assert_proposal_valid(p, cfg, self.NAME)

    def test_rejects_when_rsi_extreme(self):
        signal = _signal(
            composite=0.75, confidence=0.75, aligned="neutral",
            momentum=[_ind("rsi_14", 0.7)],  # extrême, pas scalp material
            volume=[_ind("volume_profile", 0.5), _ind("vwap", 0.4)],
        )
        snap = _snapshot()
        cfg = _cfg(self.NAME, min_composite=0.70, requires_ichimoku=False)
        p = volume_profile_scalp.build_proposal(signal=signal, snapshot=snap, config=cfg)
        assert p is None


# ---------------------------------------------------------------------------
# event_driven_macro
# ---------------------------------------------------------------------------


class TestEventDrivenMacro:
    NAME = event_driven_macro.NAME

    def _inputs(self):
        signal = _signal(
            composite=0.85, confidence=0.85, aligned="long",
            trend=[_ind("macd", 0.6)],
            volume=[_ind("obv", 0.7)],
        )
        snap = _snapshot()
        cfg = _cfg(
            self.NAME, min_composite=0.77, min_rr=2.0,
            requires_ichimoku=True, atr_mult=1.8,
            tp_rule="r_multiple", tp_r=[2.0, 3.0],  # tp_r[0]=2.0 → RR=2.0
            entry={"event_tags": "fomc,ecb_meeting,cpi_us,nfp"},
        )
        news = _news(
            impact=0.85, sentiment=0.7,
            source="fomc_release", title="FOMC decision: rates unchanged",
            entities=["USD", "FOMC"],
        )
        return signal, snap, cfg, news

    def test_accepts_post_event(self):
        s, snap, cfg, news = self._inputs()
        p = event_driven_macro.build_proposal(
            signal=s, snapshot=snap, config=cfg, news=news,
        )
        assert p is not None
        assert_proposal_valid(p, cfg, self.NAME)

    def test_rejects_without_news(self):
        s, snap, cfg, _ = self._inputs()
        p = event_driven_macro.build_proposal(
            signal=s, snapshot=snap, config=cfg, news=None,
        )
        assert p is None

    def test_rejects_unmatched_event_tag(self):
        s, snap, cfg, _ = self._inputs()
        news = _news(
            impact=0.85, sentiment=0.7,
            source="reuters_rss", title="Tesla earnings beat",
            entities=["TSLA"],
        )
        p = event_driven_macro.build_proposal(
            signal=s, snapshot=snap, config=cfg, news=news,
        )
        assert p is None

    def test_rejects_when_sentiment_opposed_direction(self):
        s, snap, cfg, _ = self._inputs()
        # Long direction but negative sentiment
        news = _news(
            impact=0.85, sentiment=-0.7,
            source="fomc_release", title="FOMC decision hawkish",
            entities=["USD", "FOMC"],
        )
        p = event_driven_macro.build_proposal(
            signal=s, snapshot=snap, config=cfg, news=news,
        )
        assert p is None


# ---------------------------------------------------------------------------
# news_driven_momentum
# ---------------------------------------------------------------------------


class TestNewsDrivenMomentum:
    NAME = news_driven_momentum.NAME

    def _inputs(self):
        signal = _signal(
            composite=0.85, confidence=0.85, aligned="long",
            volume=[_ind("obv", 0.6), _ind("cmf", 0.5)],
        )
        snap = _snapshot()
        cfg = _cfg(
            self.NAME, min_composite=0.79, min_rr=2.0,
            requires_ichimoku=True, atr_mult=1.5,
            tp_rule="r_multiple", tp_r=[2.0, 3.0],   # tp_r[0]=2.0 → RR=2.0
            entry={"news_impact_min": 0.70, "news_sentiment_abs": 0.50},
        )
        news = _news(impact=0.85, sentiment=0.70)
        return signal, snap, cfg, news

    def test_accepts_strong_positive_news(self):
        s, snap, cfg, news = self._inputs()
        p = news_driven_momentum.build_proposal(
            signal=s, snapshot=snap, config=cfg, news=news,
        )
        assert p is not None
        assert p.side == "long"
        assert_proposal_valid(p, cfg, self.NAME)

    def test_rejects_weak_impact(self):
        s, snap, cfg, _ = self._inputs()
        news = _news(impact=0.50, sentiment=0.70)   # impact < 0.70
        p = news_driven_momentum.build_proposal(
            signal=s, snapshot=snap, config=cfg, news=news,
        )
        assert p is None

    def test_rejects_weak_sentiment(self):
        s, snap, cfg, _ = self._inputs()
        news = _news(impact=0.85, sentiment=0.20)   # |sent| < 0.50
        p = news_driven_momentum.build_proposal(
            signal=s, snapshot=snap, config=cfg, news=news,
        )
        assert p is None

    def test_short_signal_on_negative_sentiment(self):
        signal = _signal(
            composite=0.85, confidence=0.85, aligned="short",
            volume=[_ind("obv", -0.6), _ind("cmf", -0.5)],
        )
        snap = _snapshot()
        cfg = _cfg(
            self.NAME, min_composite=0.79, min_rr=2.0,
            requires_ichimoku=True, atr_mult=1.5,
            tp_rule="r_multiple", tp_r=[2.0, 3.0],
            entry={"news_impact_min": 0.70, "news_sentiment_abs": 0.50},
        )
        news = _news(impact=0.85, sentiment=-0.70)
        p = news_driven_momentum.build_proposal(
            signal=signal, snapshot=snap, config=cfg, news=news,
        )
        assert p is not None
        assert p.side == "short"


# ---------------------------------------------------------------------------
# Registry + dispatcher
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_registry_has_all_7_strategies(self):
        expected = {
            "ichimoku_trend_following",
            "breakout_momentum",
            "mean_reversion",
            "divergence_hunter",
            "volume_profile_scalp",
            "event_driven_macro",
            "news_driven_momentum",
        }
        assert set(STRATEGY_REGISTRY.keys()) == expected

    def test_dispatcher_routes_to_correct_module(self):
        signal = _signal(
            composite=0.90, confidence=0.90, aligned="long",
            trend=[_ind("supertrend", 0.7), _ind("adx_14", 0.30)],
        )
        snap = _snapshot()
        cfg = _cfg(
            "ichimoku_trend_following", min_composite=0.82, min_rr=2.0,
            requires_ichimoku=True, tp_rule="r_multiple", tp_r=[2.0, 3.0],
            entry={"adx_min": 25},
        )
        p = build_proposal_for(
            "ichimoku_trend_following",
            signal=signal, snapshot=snap, config=cfg,
        )
        assert p is not None
        assert p.strategy_id == "ichimoku_trend_following"

    def test_dispatcher_unknown_strategy_raises(self):
        with pytest.raises(KeyError):
            build_proposal_for(
                "nonexistent", signal=_signal(), snapshot=_snapshot(),
                config=_cfg("nonexistent"),
            )


# ---------------------------------------------------------------------------
# Invariants globaux
# ---------------------------------------------------------------------------


class TestGlobalInvariants:
    """Pour chaque stratégie, si elle retourne un proposal, invariants OK."""

    def test_all_accept_paths_produce_valid_proposals(self):
        """Teste 7 chemins heureux, un par stratégie."""
        cases = [
            # (module, cfg_override, signal_kwargs, news, kijun)
            (
                ichimoku_trend_following,
                {"min_composite": 0.82, "min_rr": 2.0, "requires_ichimoku": True,
                 "tp_rule": "r_multiple", "tp_r": [2.0, 3.0],
                 "entry": {"adx_min": 25}},
                {"composite": 0.90, "confidence": 0.90, "aligned": "long",
                 "trend": [_ind("supertrend", 0.7), _ind("adx_14", 0.30)]},
                None, None,
            ),
            (
                breakout_momentum,
                {"min_composite": 0.80, "min_rr": 2.0, "requires_ichimoku": True,
                 "tp_rule": "r_multiple", "tp_r": [2.0, 4.0]},
                {"composite": 0.85, "confidence": 0.85, "aligned": "long",
                 "trend": [_ind("bollinger", 0.6), _ind("aroon", 0.7), _ind("macd", 0.5)],
                 "volume": [_ind("obv", 0.6)]},
                None, None,
            ),
            (
                mean_reversion,
                {"min_composite": 0.80, "min_rr": 1.5, "requires_ichimoku": False,
                 "tp_rule": "r_multiple", "tp_r": [1.5, 2.5]},
                {"composite": 0.85, "confidence": 0.85, "aligned": "neutral",
                 "momentum": [_ind("rsi_14", -0.7), _ind("cci", -0.7),
                              _ind("stochastic", 0.5)],
                 "trend": [_ind("adx_14", 0.20)]},
                None, None,
            ),
            (
                divergence_hunter,
                {"min_composite": 0.70, "min_rr": 2.0, "requires_ichimoku": False,
                 "tp_rule": "r_multiple", "tp_r": [2.0, 3.0]},
                {"composite": 0.80, "confidence": 0.80, "aligned": "neutral",
                 "momentum": [_ind("divergence_rsi", 0.6), _ind("rsi_14", -0.3)],
                 "volume": [_ind("cmf", 0.4)]},
                None, None,
            ),
            (
                volume_profile_scalp,
                {"min_composite": 0.70, "min_rr": 1.5, "requires_ichimoku": False,
                 "tp_rule": "r_multiple", "tp_r": [1.5, 2.0]},
                {"composite": 0.75, "confidence": 0.75, "aligned": "neutral",
                 "momentum": [_ind("rsi_14", 0.1)],
                 "volume": [_ind("volume_profile", 0.5), _ind("vwap", 0.4)]},
                None, None,
            ),
            (
                event_driven_macro,
                {"min_composite": 0.77, "min_rr": 2.0, "requires_ichimoku": True,
                 "tp_rule": "r_multiple", "tp_r": [2.0, 3.0],
                 "entry": {"event_tags": "fomc,nfp"}},
                {"composite": 0.85, "confidence": 0.85, "aligned": "long",
                 "trend": [_ind("macd", 0.6)], "volume": [_ind("obv", 0.7)]},
                _news(impact=0.85, sentiment=0.7, source="fomc_release",
                      title="FOMC rates", entities=["USD", "FOMC"]),
                None,
            ),
            (
                news_driven_momentum,
                {"min_composite": 0.79, "min_rr": 2.0, "requires_ichimoku": True,
                 "tp_rule": "r_multiple", "tp_r": [2.0, 3.0],
                 "entry": {"news_impact_min": 0.70, "news_sentiment_abs": 0.50}},
                {"composite": 0.85, "confidence": 0.85, "aligned": "long",
                 "volume": [_ind("obv", 0.6), _ind("cmf", 0.5)]},
                _news(impact=0.85, sentiment=0.70),
                None,
            ),
        ]
        for module, cfg_over, sig_kw, news, kijun in cases:
            cfg = _cfg(module.NAME, **cfg_over)
            signal = _signal(**sig_kw)
            snap = _snapshot()
            p = module.build_proposal(
                signal=signal, snapshot=snap, config=cfg, news=news, kijun=kijun,
            )
            assert p is not None, f"{module.NAME} should accept base case"
            assert_proposal_valid(p, cfg, module.NAME)
