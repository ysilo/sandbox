"""
src.data.sources.coingecko — BTC OHLC pour la feature `crypto_vol` du HMM.

Source : TRADING_BOT_ARCHITECTURE.md §12.2.

Endpoint gratuit (rate limit 10-30 req/min) :
    https://api.coingecko.com/api/v3/coins/{id}/ohlc?vs_currency=usd&days={d}

Response :
    [[timestamp_ms, open, high, low, close], ...]

On ne récupère que BTC (id `bitcoin`) ; les autres coins ne servent pas en V1.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .base import MacroPoint, SourceUnavailable


_BASE_URL = "https://api.coingecko.com/api/v3"
_USER_AGENT = "openclaw-trading-bot/0.1"


class CoinGeckoSource:
    """BTC OHLC → feature crypto_vol (std log-returns 20j) calculée dans `src/regime/`."""

    name = "coingecko"

    def __init__(self, timeout: float = 6.0) -> None:
        self.timeout = timeout

    def fetch_series(self, series_id: str, *, days: int) -> list[MacroPoint]:
        """`series_id` = coingecko id (ex: "bitcoin"). Renvoie close-only en MacroPoint."""
        # L'endpoint /ohlc accepte days ∈ {1, 7, 14, 30, 90, 180, 365, max}. On arrondit up.
        coingecko_days = self._round_up_days(days)
        params = urllib.parse.urlencode({"vs_currency": "usd", "days": coingecko_days})
        url = f"{_BASE_URL}/coins/{series_id}/ohlc?{params}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise SourceUnavailable(f"coingecko {series_id} GET failed: {e}") from e

        if not isinstance(doc, list):
            raise SourceUnavailable(f"coingecko {series_id}: format inattendu")

        points: list[MacroPoint] = []
        for row in doc:
            try:
                ms = int(row[0])
                close = float(row[4])
                date_str = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
                points.append(MacroPoint(date=date_str, value=close))
            except (ValueError, IndexError, TypeError):
                continue

        return points[-days:] if len(points) > days else points

    @staticmethod
    def _round_up_days(n: int) -> int:
        for allowed in (1, 7, 14, 30, 90, 180, 365):
            if n <= allowed:
                return allowed
        return 365


__all__ = ["CoinGeckoSource"]
