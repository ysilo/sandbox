"""
src.data.sources.base — protocoles et types partagés par toutes les sources.

Source : TRADING_BOT_ARCHITECTURE.md §7.

Principes :
- Chaque source implémente `PriceSource` (OHLCV) ou `MacroSource` (séries
  scalaires FRED/CoinGecko pour le HMM §12.2).
- Pas d'héritage de classe ; Protocol structural (duck-typing typé).
- Les erreurs remontent en exceptions `DataSourceError` / sous-classes,
  jamais en valeurs sentinelles.
- Les timeouts, retries et fallbacks sont gérés par `src/data/fetcher.py`,
  PAS par les sources elles-mêmes. Une source fait 1 tentative, propage
  l'exception; le fetcher orchestre.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Timeframe = Literal["5m", "15m", "1h", "4h", "1d"]


@dataclass(frozen=True)
class OHLCVBar:
    """Une barre OHLCV. Les sources produisent list[OHLCVBar] triées par ts asc."""

    ts: str               # ISO-8601 UTC Z
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MacroPoint:
    """Un point de série macro (FRED / CoinGecko proxy)."""

    date: str             # YYYY-MM-DD
    value: float


class DataSourceError(Exception):
    """Racine des erreurs de source. Les sous-classes mappent sur la taxonomie §7.5.2."""


class SourceUnavailable(DataSourceError):
    """La source ne répond pas (timeout, 5xx, DNS). → DATA_004 si fallback dispo."""


class DataStale(DataSourceError):
    """Données plus anciennes que le seuil acceptable. → DATA_001."""


class DataGap(DataSourceError):
    """Trou dans la série OHLCV. → DATA_002."""


class TimeframeUnsupported(DataSourceError):
    """Timeframe demandé pas géré par la source. → DATA_008."""


@runtime_checkable
class PriceSource(Protocol):
    """Interface pour fournisseur d'OHLCV (crypto / forex / equity)."""

    name: str                                               # ex: "stooq"
    provider_kind: Literal["primary", "fallback"]

    def fetch_ohlcv(
        self,
        canonical_symbol: str,
        *,
        timeframe: Timeframe,
        lookback_bars: int,
    ) -> list[OHLCVBar]:
        """Retourne les N dernières barres, oldest-first.

        Peut lever :
            SourceUnavailable, TimeframeUnsupported, DataStale, DataGap
        """
        ...


@runtime_checkable
class MacroSource(Protocol):
    """Interface pour fournisseur de séries macro scalaires (HMM §12.2)."""

    name: str

    def fetch_series(
        self,
        series_id: str,
        *,
        days: int,
    ) -> list[MacroPoint]:
        """Retourne les N derniers points, oldest-first.

        `series_id` est l'ID natif du provider (ex: "SP500" pour FRED,
        "bitcoin" pour CoinGecko). La conversion depuis le canonical
        éventuel est faite par l'appelant via `src.utils.ticker_map`.
        """
        ...


__all__ = [
    "OHLCVBar",
    "MacroPoint",
    "Timeframe",
    "PriceSource",
    "MacroSource",
    "DataSourceError",
    "SourceUnavailable",
    "DataStale",
    "DataGap",
    "TimeframeUnsupported",
]
