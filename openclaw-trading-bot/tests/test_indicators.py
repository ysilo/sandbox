"""
tests/test_indicators.py — indicateurs & signal crossing.

Couvre :
- Ichimoku : forme de sortie, in_kumo détecté, score ∈ [-1, +1].
- Trend : ADX<20 force score=0, uptrend pur → score > 0, downtrend → <0.
- Momentum : RSI plausible (uptrend monotone → > 50), CCI signal.
- Volume : OBV cumulé cohérent, CMF, VP POC dans [low_min, high_max].
- Volatility : ATR positif, BB bandwidth croît avec la variance.
- SignalCrossing : produit un SignalOutput valide, is_proposal figé à False.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.indicators.ichimoku import compute_ichimoku, ichimoku_payload
from src.indicators.momentum import (
    MomentumScore,
    compute_cci,
    compute_momentum,
    compute_rsi,
    compute_stochastic,
    compute_trix,
    rsi_signal,
)
from src.indicators.trend import (
    TrendScore,
    compute_adx,
    compute_aroon,
    compute_bollinger,
    compute_macd,
    compute_parabolic_sar,
    compute_supertrend,
)
from src.indicators.volatility import (
    compute_atr,
    compute_atr_pct,
    compute_bb_bandwidth,
    compute_realized_vol,
    compute_volatility_profile,
)
from src.indicators.volume import (
    VolumeScore,
    compute_cmf,
    compute_obv,
    compute_volume_profile,
    compute_vwap,
    volume_profile_signal,
)
from src.indicators.weights import (
    DEFAULT_MOMENTUM,
    DEFAULT_TREND,
    DEFAULT_VOLUME,
)
from src.signals.signal_crossing import SignalCrossing


# ---------------------------------------------------------------------------
# Fixtures : séries OHLCV synthétiques
# ---------------------------------------------------------------------------


def _make_uptrend(n: int = 120, start: float = 100.0, step: float = 0.5,
                  noise: float = 0.0, seed: int = 42) -> pd.DataFrame:
    """Série haussière régulière avec option de bruit gaussien."""
    rng = np.random.default_rng(seed)
    closes = start + step * np.arange(n) + rng.normal(0, noise, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    df = pd.DataFrame({
        "open":  closes - 0.1,
        "high":  closes + 0.3,
        "low":   closes - 0.3,
        "close": closes,
        "volume": np.linspace(1_000, 2_000, n),
    }, index=idx)
    return df


def _make_downtrend(n: int = 120) -> pd.DataFrame:
    df = _make_uptrend(n)
    df = df.iloc[::-1].reset_index(drop=True)
    df.index = pd.date_range("2025-01-01", periods=n, freq="D")
    return df


def _make_flat(n: int = 120, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open":  np.full(n, price),
        "high":  np.full(n, price + 0.05),
        "low":   np.full(n, price - 0.05),
        "close": np.full(n, price),
        "volume": np.full(n, 1_000.0),
    }, index=idx)


def _make_sideways_with_spike(n: int = 120) -> pd.DataFrame:
    """Flat avec un spike de volatilité vers la fin pour BB bandwidth."""
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    closes = np.full(n, 100.0)
    closes[-20:] += np.linspace(0, 10, 20)      # breakout en toute fin
    return pd.DataFrame({
        "open":  closes,
        "high":  closes + 0.5,
        "low":   closes - 0.5,
        "close": closes,
        "volume": np.full(n, 1_000.0),
    }, index=idx)


# ---------------------------------------------------------------------------
# Ichimoku
# ---------------------------------------------------------------------------


def test_ichimoku_shape_uptrend() -> None:
    df = _make_uptrend()
    r = compute_ichimoku(df)
    assert not r.in_kumo, "uptrend marqué : prix doit être au-dessus du kumo"
    assert r.score > 0.5
    assert r.score <= 1.0
    assert len(r.tenkan) == len(df)


def test_ichimoku_score_downtrend() -> None:
    df = _make_downtrend()
    r = compute_ichimoku(df)
    assert r.score < -0.5
    assert r.score >= -1.0


def test_ichimoku_in_kumo_flat_series() -> None:
    df = _make_flat()
    r = compute_ichimoku(df)
    # Marché parfaitement plat → prix dans le nuage → score=0
    assert r.in_kumo is True
    assert r.score == 0.0


def test_ichimoku_payload_is_valid_pydantic() -> None:
    df = _make_uptrend()
    payload = ichimoku_payload(df)
    assert payload.aligned_long is True
    assert payload.aligned_short is False
    assert payload.kumo_thickness_pct >= 0.0


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


def test_supertrend_uptrend_is_positive() -> None:
    df = _make_uptrend(n=200)
    st = compute_supertrend(df)
    assert st.iloc[-1] == 1.0


def test_supertrend_downtrend_is_negative() -> None:
    df = _make_downtrend(n=200)
    st = compute_supertrend(df)
    assert st.iloc[-1] == -1.0


def test_macd_histogram_positive_in_uptrend() -> None:
    df = _make_uptrend(n=200)
    m = compute_macd(df)
    assert m.histogram.iloc[-1] > 0


def test_psar_uptrend_positive() -> None:
    df = _make_uptrend(n=200, noise=0.05)
    p = compute_parabolic_sar(df)
    assert p.iloc[-1] == 1.0


def test_aroon_uptrend_high_up_low_down() -> None:
    df = _make_uptrend(n=200)
    a = compute_aroon(df, period=14)
    assert a.up.iloc[-1] == pytest.approx(100.0, abs=1e-6)
    assert a.down.iloc[-1] <= 30.0


def test_adx_uptrend_strong() -> None:
    df = _make_uptrend(n=200, noise=0.1)
    r = compute_adx(df, period=14)
    assert r.adx.iloc[-1] > 25.0
    assert r.plus_di.iloc[-1] > r.minus_di.iloc[-1]


def test_bollinger_bandwidth_expands_with_volatility() -> None:
    df = _make_sideways_with_spike(n=200)
    bb = compute_bollinger(df, period=20)
    # bandwidth doit être plus large en fin de série qu'en plein flat
    assert bb.bandwidth.iloc[-1] > bb.bandwidth.iloc[50]


def test_trend_score_forced_zero_when_adx_below_20() -> None:
    df = _make_flat(n=200)
    s = TrendScore.compute(df, DEFAULT_TREND)
    assert s.score == 0.0
    assert "filtered" in s.components


def test_trend_score_positive_uptrend() -> None:
    df = _make_uptrend(n=200, noise=0.2)
    s = TrendScore.compute(df, DEFAULT_TREND)
    assert s.score > 0.3, f"score={s.score} sur un uptrend marqué"
    assert s.adx_strength > 20


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------


def test_rsi_uptrend_above_50() -> None:
    df = _make_uptrend(n=100)
    r = compute_rsi(df, period=14)
    assert r.iloc[-1] > 50.0


def test_rsi_signal_zones() -> None:
    assert rsi_signal(25) == 0.8
    assert rsi_signal(32) == 0.4
    assert rsi_signal(75) == -0.8
    assert rsi_signal(50) == 0.0


def test_stochastic_in_uptrend_bias_high() -> None:
    df = _make_uptrend(n=80)
    s = compute_stochastic(df)
    # En uptrend soutenu, le %K doit flirter avec la zone haute
    assert s.k.iloc[-1] > 50.0


def test_trix_positive_uptrend() -> None:
    df = _make_uptrend(n=200)
    t = compute_trix(df)
    assert t.line.iloc[-1] > 0


def test_cci_can_go_extreme() -> None:
    df = _make_uptrend(n=200, step=1.0)        # step large → mouvement agressif
    c = compute_cci(df, period=20)
    assert c.iloc[-1] > 50.0


def test_momentum_positive_uptrend() -> None:
    df = _make_uptrend(n=50)
    m = compute_momentum(df, period=12)
    assert m.iloc[-1] > 0


def test_momentum_score_positive_uptrend() -> None:
    df = _make_uptrend(n=150, noise=0.2)
    ms = MomentumScore.compute(df, DEFAULT_MOMENTUM)
    assert ms.score > 0.0
    assert 0 <= ms.rsi <= 100


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def test_obv_monotone_in_uptrend() -> None:
    df = _make_uptrend(n=50)
    obv = compute_obv(df)
    # OBV doit croître globalement (dernière valeur > milieu)
    assert obv.iloc[-1] > obv.iloc[25]


def test_vwap_near_close_uptrend() -> None:
    df = _make_uptrend(n=50)
    vwap = compute_vwap(df)
    assert not pd.isna(vwap.iloc[-1])
    assert vwap.iloc[-1] > 0


def test_cmf_positive_uptrend_close_near_high() -> None:
    df = _make_uptrend(n=50)
    # Forçage : close près du high → MFM positif → CMF > 0
    df["close"] = df["high"] - 0.01
    cmf = compute_cmf(df, period=20)
    assert cmf.iloc[-1] > 0


def test_volume_profile_poc_in_range() -> None:
    df = _make_uptrend(n=200)
    vp = compute_volume_profile(df, lookback=150, bins=30)
    assert df["low"].min() <= vp.poc <= df["high"].max()
    assert vp.val <= vp.poc <= vp.vah


def test_volume_profile_signal_neutral_in_value_area() -> None:
    from src.indicators.volume import VolumeProfileResult
    vp = VolumeProfileResult(poc=100, vah=105, val=95, hvn_levels=[], lvn_levels=[])
    assert volume_profile_signal(100.0, vp) == 0.0
    assert volume_profile_signal(110.0, vp) == 0.5
    assert volume_profile_signal(80.0, vp) == -0.5


def test_volume_score_neutral_when_volume_missing() -> None:
    df = _make_uptrend(n=100)
    df["volume"] = 0.0
    vs = VolumeScore.compute(df, DEFAULT_VOLUME)
    assert vs.score == 0.0
    assert "filtered" in vs.components


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------


def test_atr_positive_on_uptrend() -> None:
    df = _make_uptrend(n=100)
    atr = compute_atr(df, period=14)
    assert atr.iloc[-1] > 0


def test_atr_pct_small_for_large_prices() -> None:
    df = _make_uptrend(n=100, start=1000.0, step=0.5)
    pct = compute_atr_pct(df)
    assert pct.iloc[-1] < 0.01


def test_realized_vol_non_negative() -> None:
    df = _make_uptrend(n=100)
    rv = compute_realized_vol(df["close"])
    assert rv.iloc[-1] >= 0


def test_bb_bandwidth_increases_after_spike() -> None:
    df = _make_sideways_with_spike(n=200)
    bw = compute_bb_bandwidth(df)
    assert bw.iloc[-1] > bw.iloc[100]


def test_volatility_profile_bundle() -> None:
    df = _make_uptrend(n=300)
    p = compute_volatility_profile(df)
    assert p.atr > 0
    assert p.atr_pct > 0
    assert 0 <= p.percentile_1y <= 100


# ---------------------------------------------------------------------------
# SignalCrossing
# ---------------------------------------------------------------------------


def test_signal_crossing_produces_valid_signal_output() -> None:
    df = _make_uptrend(n=200, noise=0.2)
    sc = SignalCrossing()
    out = sc.score(df, asset="RUI.PA", regime_context="risk_on")
    assert out.is_proposal is False                     # invariant figé
    assert out.composite_score > 0.0
    assert -1.0 <= out.composite_score <= 1.0
    assert 0.0 <= out.confidence <= 1.0
    assert out.asset == "RUI.PA"
    assert out.ichimoku.aligned_long is True
    assert len(out.trend) >= 5
    assert len(out.momentum) >= 5
    assert len(out.volume) >= 4


def test_signal_crossing_zero_when_in_kumo() -> None:
    df = _make_flat(n=200)
    sc = SignalCrossing()
    out = sc.score(df, asset="X", regime_context="risk_on")
    assert out.composite_score == 0.0
    assert out.confidence == 0.0
    assert out.ichimoku.aligned_long is False
    assert out.ichimoku.aligned_short is False


def test_signal_crossing_regime_penalty() -> None:
    df = _make_uptrend(n=200, noise=0.3)
    sc = SignalCrossing()
    ok = sc.score(df, asset="X", regime_context="risk_on")
    off = sc.score(df, asset="X", regime_context="risk_off")
    # Même signal long : confiance pénalisée en risk_off
    assert off.confidence < ok.confidence


def test_signal_crossing_rejects_incomplete_ohlcv() -> None:
    df = _make_uptrend(n=200).drop(columns=["volume"])
    sc = SignalCrossing()
    with pytest.raises(ValueError, match="manquantes"):
        sc.score(df, asset="X", regime_context="risk_on")


def test_signal_crossing_rejects_too_short_series() -> None:
    df = _make_uptrend(n=10)
    sc = SignalCrossing()
    with pytest.raises(ValueError, match="52 barres"):
        sc.score(df, asset="X", regime_context="risk_on")
