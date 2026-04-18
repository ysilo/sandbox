"""
src.data.sources.exchangerate_host — forex EOD fallback (gratuit, sans clé).

Source : TRADING_BOT_ARCHITECTURE.md §7.1.

Fallback pour quand `OANDA_API_KEY` absent. Résolution EOD seulement :
- Les stratégies intraday ne pourront PAS utiliser ce provider
- Si timeframe < 1d, `TimeframeUnsupported` est levée (DATA_008)

API : https://api.exchangerate.host/timeseries?base=EUR&symbols=USD&start_date=...&end_date=...
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Literal

from src.utils.ticker_map import resolve_ticker

from .base import OHLCVBar, SourceUnavailable, Timeframe, TimeframeUnsupported


_BASE_URL = "https://api.exchangerate.host/timeseries"


class ExchangerateHostSource:
    """Forex EOD — fallback only (pas d'intraday)."""

    name = "exchangerate_host"
    provider_kind: Literal["primary", "fallback"] = "fallback"

    def __init__(self, timeout: float = 6.0) -> None:
        self.timeout = timeout

    def fetch_ohlcv(
        self,
        canonical_symbol: str,
        *,
        timeframe: Timeframe,
        lookback_bars: int,
    ) -> list[OHLCVBar]:
        if timeframe != "1d":
            raise TimeframeUnsupported(
                f"exchangerate_host supporte uniquement timeframe=1d (demandé: {timeframe!r})"
            )

        pair = resolve_ticker(canonical_symbol, "exchangerate_host")
        assert isinstance(pair, tuple)
        base, quote = pair

        end = datetime.now(tz=timezone.utc).date()
        start = end - timedelta(days=lookback_bars + 20)     # marge week-ends/jours fériés
        params = {
            "base": base,
            "symbols": quote,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise SourceUnavailable(f"exchangerate_host GET failed: {e}") from e

        rates = doc.get("rates", {})
        if not isinstance(rates, dict):
            raise SourceUnavailable("exchangerate_host: rates absent")

        bars: list[OHLCVBar] = []
        for date_str, kv in sorted(rates.items()):
            try:
                rate = float(kv[quote])
            except (KeyError, ValueError, TypeError):
                continue
            # Pas d'OHLC natif — on simule comme un close flat (open=high=low=close=rate)
            # Volume = 0 (pas fournisseur tick-par-tick)
            bars.append(OHLCVBar(
                ts=f"{date_str}T00:00:00Z",
                open=rate, high=rate, low=rate, close=rate, volume=0.0,
            ))
        return bars[-lookback_bars:] if len(bars) > lookback_bars else bars


__all__ = ["ExchangerateHostSource"]
