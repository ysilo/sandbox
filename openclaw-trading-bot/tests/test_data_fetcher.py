"""
tests/test_data_fetcher.py — orchestration retry/fallback, sans appel réseau.

Les sources réelles (Stooq, CCXT, OANDA, FRED…) sont testées en intégration
séparément, avec un tag pytest et les clés API en env. Ici, on mocke
l'interface `PriceSource` pour vérifier la logique du fetcher uniquement.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import pytest

from src.data.fetcher import (
    AllSourcesExhausted,
    DataFetcher,
    FetcherConfig,
)
from src.data.sources.base import (
    OHLCVBar,
    SourceUnavailable,
    Timeframe,
)


# ---------------------------------------------------------------------------
# Fake PriceSource pour tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeSource:
    name: str
    provider_kind: Literal["primary", "fallback"] = "primary"
    fail_n_times: int = 0                            # échoue les N premières tentatives
    always_fail: bool = False
    return_bars: list[OHLCVBar] | None = None
    _calls: int = 0

    def fetch_ohlcv(
        self,
        canonical_symbol: str,
        *,
        timeframe: Timeframe,
        lookback_bars: int,
    ) -> list[OHLCVBar]:
        self._calls += 1
        if self.always_fail or self._calls <= self.fail_n_times:
            raise SourceUnavailable(f"{self.name} simulated failure attempt={self._calls}")
        if self.return_bars is None:
            return _make_bars(n=lookback_bars, tf=timeframe)
        return self.return_bars


def _make_bars(n: int, tf: Timeframe) -> list[OHLCVBar]:
    """Série factice mais plausible : chaque barre à l'intervalle attendu."""
    from datetime import timedelta
    tf_delta = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[tf]
    now = datetime.now(tz=timezone.utc).replace(microsecond=0, second=0)
    bars: list[OHLCVBar] = []
    for i in range(n):
        ts = (now - timedelta(seconds=tf_delta * (n - i - 1))).strftime("%Y-%m-%dT%H:%M:%SZ")
        bars.append(OHLCVBar(ts=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0))
    return bars


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_primary_succeeds_on_first_try() -> None:
    primary = _FakeSource(name="primary")
    fb = _FakeSource(name="fallback", provider_kind="fallback")
    fetcher = DataFetcher(equity_sources=[primary, fb])

    bars = fetcher.fetch("RUI.PA", asset_class="equity", timeframe="1d", lookback_bars=30)
    assert len(bars) == 30
    assert primary._calls == 1
    assert fb._calls == 0                             # fallback jamais appelé


def test_primary_retries_then_succeeds() -> None:
    primary = _FakeSource(name="primary", fail_n_times=2)  # échoue 2×, 3e OK
    fetcher = DataFetcher(
        equity_sources=[primary],
        config=FetcherConfig(max_retries_per_source=3, base_backoff_s=0.0, cap_backoff_s=0.0),
    )
    bars = fetcher.fetch("RUI.PA", asset_class="equity", timeframe="1d", lookback_bars=20)
    assert primary._calls == 3
    assert len(bars) == 20


def test_fallback_used_when_primary_exhausted() -> None:
    primary = _FakeSource(name="primary", always_fail=True)
    fb = _FakeSource(name="fallback", provider_kind="fallback")
    fetcher = DataFetcher(
        equity_sources=[primary, fb],
        config=FetcherConfig(max_retries_per_source=2, base_backoff_s=0.0, cap_backoff_s=0.0),
    )
    bars = fetcher.fetch("RUI.PA", asset_class="equity", timeframe="1d", lookback_bars=10)
    assert primary._calls == 2                        # épuisé les 2 retries
    assert fb._calls == 1                             # fallback a fonctionné
    assert len(bars) == 10


def test_all_sources_exhausted_raises() -> None:
    primary = _FakeSource(name="primary", always_fail=True)
    fb = _FakeSource(name="fallback", always_fail=True, provider_kind="fallback")
    fetcher = DataFetcher(
        equity_sources=[primary, fb],
        config=FetcherConfig(max_retries_per_source=1, base_backoff_s=0.0, cap_backoff_s=0.0),
    )
    with pytest.raises(AllSourcesExhausted) as exc_info:
        fetcher.fetch("RUI.PA", asset_class="equity", timeframe="1d", lookback_bars=10)
    assert exc_info.value.tried == ["primary", "fallback"]


def test_no_sources_configured_raises() -> None:
    fetcher = DataFetcher()
    with pytest.raises(AllSourcesExhausted):
        fetcher.fetch("RUI.PA", asset_class="equity", timeframe="1d", lookback_bars=10)


def test_stale_data_is_caught() -> None:
    # Série avec dernière barre très ancienne → DataStale → fallback tenté
    from datetime import timedelta
    old_bars = [OHLCVBar(
        ts=(datetime.now(tz=timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        open=100, high=100, low=100, close=100, volume=0,
    )]
    stale = _FakeSource(name="stale", return_bars=old_bars)
    fresh = _FakeSource(name="fresh", provider_kind="fallback")
    fetcher = DataFetcher(
        equity_sources=[stale, fresh],
        config=FetcherConfig(max_retries_per_source=1, base_backoff_s=0.0, cap_backoff_s=0.0),
    )
    bars = fetcher.fetch("RUI.PA", asset_class="equity", timeframe="1d", lookback_bars=5)
    assert stale._calls == 1
    assert fresh._calls == 1                          # fallback utilisé
    assert len(bars) == 5
