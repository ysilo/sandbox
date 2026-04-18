"""
src.data.fetcher — orchestration retry + fallback + qualité des sources.

Source : TRADING_BOT_ARCHITECTURE.md §7, §8.7.1.

Stratégie :
- Par classe d'actifs, une liste ordonnée de `PriceSource` (primary puis fallbacks).
- Retry interne par source : N tentatives avec backoff exponentiel (base 1s, cap 8s).
- Si toutes les sources échouent → DATA_005 + exception remontée au fetcher.
- Chaque bascule logge DATA_004 (source KO, fallback OK).
- Data quality : détection de gap OHLCV, de staleness (dernière barre trop vieille).

Usage :
    fetcher = DataFetcher(
        equity_sources=[StooqSource(), BoursoramaSource()],
        crypto_sources=[CCXTSource("binance"), CCXTSource("kraken")],
        forex_sources=[OandaSource(), ExchangerateHostSource()],
    )
    bars = fetcher.fetch("RUI.PA", asset_class="equity", timeframe="1d", lookback_bars=200)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Sequence

from src.utils.error_codes import EC
from src.utils.logging_utils import get_logger

from .sources.base import (
    DataGap,
    DataSourceError,
    DataStale,
    OHLCVBar,
    PriceSource,
    SourceUnavailable,
    Timeframe,
    TimeframeUnsupported,
)


log = get_logger("DataFetcher")


AssetClass = Literal["equity", "forex", "crypto"]


@dataclass
class FetcherConfig:
    max_retries_per_source: int = 3
    base_backoff_s: float = 1.0
    cap_backoff_s: float = 8.0
    # Staleness : la dernière barre ne doit pas être plus vieille que N multiples
    # de la durée du timeframe (ex: pour 1h, 3× = 3 heures)
    max_staleness_multiples: int = 5


class AllSourcesExhausted(DataSourceError):
    """Toutes les sources ont échoué. → DATA_005, fail-closed (§2.2)."""

    def __init__(self, message: str = "", *, tried: list[str] | None = None) -> None:
        super().__init__(message)
        self.tried: list[str] = tried or []


class DataFetcher:
    """Orchestre les sources selon la classe d'asset."""

    def __init__(
        self,
        *,
        equity_sources: Sequence[PriceSource] = (),
        forex_sources: Sequence[PriceSource] = (),
        crypto_sources: Sequence[PriceSource] = (),
        config: FetcherConfig | None = None,
    ) -> None:
        self._sources: dict[AssetClass, Sequence[PriceSource]] = {
            "equity": equity_sources,
            "forex": forex_sources,
            "crypto": crypto_sources,
        }
        self.config = config or FetcherConfig()

    def fetch(
        self,
        canonical_symbol: str,
        *,
        asset_class: AssetClass,
        timeframe: Timeframe,
        lookback_bars: int,
    ) -> list[OHLCVBar]:
        sources = self._sources.get(asset_class, ())
        if not sources:
            raise AllSourcesExhausted(
                f"Aucune source configurée pour {asset_class}",
                tried=[],
            )

        tried: list[str] = []
        last_err: Exception | None = None

        for idx, src in enumerate(sources):
            tried.append(src.name)
            try:
                bars = self._try_source(src, canonical_symbol, timeframe, lookback_bars)
            except TimeframeUnsupported as e:
                log.warning(
                    "source_timeframe_unsupported",
                    ec=EC.DATA_008,
                    source=src.name, asset=canonical_symbol,
                    cause=str(e),
                )
                last_err = e
                continue
            except (SourceUnavailable, DataStale, DataGap) as e:
                log.warning(
                    "source_failed",
                    ec=EC.DATA_004 if idx < len(sources) - 1 else EC.DATA_005,
                    source=src.name, asset=canonical_symbol,
                    cause=str(e),
                    context={"next_fallback": sources[idx + 1].name if idx + 1 < len(sources) else None},
                )
                last_err = e
                continue

            # OK
            if idx > 0:
                # Un fallback a été utilisé : log DATA_004 explicite
                log.info(
                    "source_failed_fallback_ok",
                    ec=EC.DATA_004,
                    source=src.name,
                    asset=canonical_symbol,
                    context={"primary_failed": sources[0].name, "fallback_used": src.name},
                )
            return bars

        # Toutes les sources ont échoué → fail-closed
        log.error(
            "all_sources_exhausted",
            ec=EC.DATA_005,
            asset=canonical_symbol,
            context={"sources_tried": tried, "asset_class": asset_class},
        )
        raise AllSourcesExhausted(
            f"toutes les sources {asset_class} ont échoué pour {canonical_symbol}",
            tried=tried,
        ) from last_err

    # ------------------------------------------------------------------
    # Retry + qualité par source
    # ------------------------------------------------------------------

    def _try_source(
        self,
        src: PriceSource,
        symbol: str,
        timeframe: Timeframe,
        lookback_bars: int,
    ) -> list[OHLCVBar]:
        last_err: Exception | None = None
        for attempt in range(1, self.config.max_retries_per_source + 1):
            try:
                bars = src.fetch_ohlcv(symbol, timeframe=timeframe, lookback_bars=lookback_bars)
            except TimeframeUnsupported:
                raise  # inutile de retry
            except SourceUnavailable as e:
                last_err = e
                if attempt < self.config.max_retries_per_source:
                    backoff = min(
                        self.config.cap_backoff_s,
                        self.config.base_backoff_s * (2 ** (attempt - 1)),
                    )
                    time.sleep(backoff)
                    continue
                raise

            if not bars:
                raise SourceUnavailable(f"{src.name}: 0 bars renvoyées")
            _check_quality(bars, timeframe=timeframe, cfg=self.config, source_name=src.name)
            return bars

        if last_err:
            raise last_err
        raise SourceUnavailable(f"{src.name}: retry loop inattendue (aucune erreur capturée)")


# ---------------------------------------------------------------------------
# Qualité données
# ---------------------------------------------------------------------------


_TF_SECONDS = {
    "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400,
}


def _check_quality(
    bars: list[OHLCVBar],
    *,
    timeframe: Timeframe,
    cfg: FetcherConfig,
    source_name: str,
) -> None:
    if not bars:
        raise SourceUnavailable(f"{source_name}: série vide")

    # Staleness : dernière barre trop vieille ?
    try:
        last_ts = datetime.strptime(bars[-1].ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SourceUnavailable(f"{source_name}: ts non parseable ({bars[-1].ts!r})")
    age_s = (datetime.now(tz=timezone.utc) - last_ts).total_seconds()
    tf_s = _TF_SECONDS.get(timeframe, 86400)
    if age_s > tf_s * cfg.max_staleness_multiples:
        raise DataStale(
            f"{source_name}: dernière barre {bars[-1].ts} "
            f"trop vieille ({age_s:.0f}s > {tf_s * cfg.max_staleness_multiples}s)"
        )

    # Gap : on compte les sauts > 2× la durée du timeframe entre barres consécutives
    # Pour EOD equity/forex, on tolère les week-ends (gap jusqu'à 3 jours OK)
    max_gap_s = tf_s * 2 if timeframe != "1d" else tf_s * 5
    gaps = 0
    for prev, curr in zip(bars, bars[1:]):
        try:
            t1 = datetime.strptime(prev.ts, "%Y-%m-%dT%H:%M:%SZ")
            t2 = datetime.strptime(curr.ts, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        if (t2 - t1).total_seconds() > max_gap_s:
            gaps += 1
    if gaps > len(bars) * 0.10:                 # > 10 % de gaps → série peu fiable
        raise DataGap(f"{source_name}: {gaps} gaps sur {len(bars)} barres")


__all__ = ["DataFetcher", "FetcherConfig", "AllSourcesExhausted", "AssetClass"]
