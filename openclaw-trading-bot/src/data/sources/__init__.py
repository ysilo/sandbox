"""
src.data.sources — fournisseurs de données (prix OHLCV + macro).

Exports :
- Types / interfaces : `PriceSource`, `MacroSource`, `OHLCVBar`, `MacroPoint`, `Timeframe`
- Exceptions : `DataSourceError`, `SourceUnavailable`, `DataStale`, `DataGap`, `TimeframeUnsupported`
- Implémentations : `StooqSource`, `BoursoramaSource`, `CCXTSource`, `OandaSource`,
  `ExchangerateHostSource`, `FredSource`, `CoinGeckoSource`
"""
from __future__ import annotations

from .base import (
    DataGap,
    DataSourceError,
    DataStale,
    MacroPoint,
    MacroSource,
    OHLCVBar,
    PriceSource,
    SourceUnavailable,
    Timeframe,
    TimeframeUnsupported,
)
from .boursorama import BoursoramaSource
from .ccxt_source import CCXTSource
from .coingecko import CoinGeckoSource
from .exchangerate_host import ExchangerateHostSource
from .fred import FredSource
from .oanda import OandaSource
from .stooq import StooqSource

__all__ = [
    # types
    "OHLCVBar", "MacroPoint", "Timeframe",
    "PriceSource", "MacroSource",
    # exceptions
    "DataSourceError", "SourceUnavailable",
    "DataStale", "DataGap", "TimeframeUnsupported",
    # sources
    "StooqSource", "BoursoramaSource", "CCXTSource",
    "OandaSource", "ExchangerateHostSource",
    "FredSource", "CoinGeckoSource",
]
