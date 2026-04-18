"""
src.data.sources.oanda — forex OHLCV via OANDA v20 REST API (OPTIONNEL).

Source : TRADING_BOT_ARCHITECTURE.md §7.1.

Cette source est **optionnelle** en V1 — OANDA demande un compte démo
(gratuit) + clé API. Si `OANDA_API_KEY` absent, le fetcher bascule sur
`exchangerate_host` (EOD seulement, moins précis).

Endpoint : https://api-fxpractice.oanda.com/v3/instruments/{instrument}/candles
Auth     : Authorization: Bearer <token>
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Literal

from src.utils.ticker_map import resolve_ticker

from .base import OHLCVBar, SourceUnavailable, Timeframe, TimeframeUnsupported


_DEMO_BASE = "https://api-fxpractice.oanda.com/v3"
_LIVE_BASE = "https://api-fxtrade.oanda.com/v3"


_GRANULARITY = {
    "5m": "M5",
    "15m": "M15",
    "1h": "H1",
    "4h": "H4",
    "1d": "D",
}


class OandaSource:
    """OANDA v20 REST — mid candles, bid/ask non consommés en V1."""

    name = "oanda"
    provider_kind: Literal["primary", "fallback"] = "primary"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        account_type: Literal["demo", "live"] = "demo",
        timeout: float = 6.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OANDA_API_KEY", "")
        if not self.api_key:
            raise SourceUnavailable("OANDA_API_KEY absent — source non initialisée")
        self.base_url = _DEMO_BASE if account_type == "demo" else _LIVE_BASE
        self.timeout = timeout

    def fetch_ohlcv(
        self,
        canonical_symbol: str,
        *,
        timeframe: Timeframe,
        lookback_bars: int,
    ) -> list[OHLCVBar]:
        instrument = resolve_ticker(canonical_symbol, "oanda")
        assert isinstance(instrument, str)

        granularity = _GRANULARITY.get(timeframe)
        if granularity is None:
            raise TimeframeUnsupported(f"OANDA granularity non supportée : {timeframe!r}")

        params = {
            "count": str(lookback_bars),
            "price": "M",                                   # mid
            "granularity": granularity,
        }
        url = f"{self.base_url}/instruments/{instrument}/candles?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise SourceUnavailable(f"OANDA GET {instrument} failed: {e}") from e

        bars: list[OHLCVBar] = []
        for c in doc.get("candles", []):
            if not c.get("complete", True):
                continue
            mid = c.get("mid", {})
            try:
                bars.append(OHLCVBar(
                    ts=c["time"][:19] + "Z",            # normalise fraction de seconde
                    open=float(mid["o"]),
                    high=float(mid["h"]),
                    low=float(mid["l"]),
                    close=float(mid["c"]),
                    volume=float(c.get("volume", 0)),
                ))
            except (KeyError, ValueError):
                continue
        return bars


__all__ = ["OandaSource"]
