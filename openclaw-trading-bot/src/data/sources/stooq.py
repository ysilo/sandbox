"""
src.data.sources.stooq — fournisseur primary Euronext Paris (gratuit, sans clé).

Source : TRADING_BOT_ARCHITECTURE.md §7.1.

Endpoint CSV direct : https://stooq.com/q/d/l?s=<symbol>.fr&i=d|5
Header :  Date, Open, High, Low, Close, Volume
Symbole  :  RUI.PA → `rui.fr` (via ticker_map)
"""
from __future__ import annotations

import csv
import io
import urllib.request
from datetime import datetime, timezone
from typing import Literal

from src.utils.ticker_map import resolve_ticker

from .base import (
    OHLCVBar,
    SourceUnavailable,
    Timeframe,
    TimeframeUnsupported,
)


_BASE_URL = "https://stooq.com/q/d/l"
_USER_AGENT = "openclaw-trading-bot/0.1 (+https://github.com/openclaw)"


class StooqSource:
    """CSV téléchargement — EOD (i=d) + intraday 5m (i=5) agrégeable en H1/H4."""

    name = "stooq"
    provider_kind: Literal["primary", "fallback"] = "primary"

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout

    # ------------------------------------------------------------------
    # PriceSource
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        canonical_symbol: str,
        *,
        timeframe: Timeframe,
        lookback_bars: int,
    ) -> list[OHLCVBar]:
        symbol = resolve_ticker(canonical_symbol, "stooq")
        assert isinstance(symbol, str)

        interval_param = self._map_interval(timeframe)
        url = f"{_BASE_URL}?s={symbol}&i={interval_param}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            raise SourceUnavailable(f"stooq GET {url} failed: {e}") from e

        bars = self._parse_csv(body, timeframe=timeframe)
        if len(bars) < lookback_bars:
            # On renvoie ce qu'on a — le fetcher décide si c'est utilisable
            return bars
        return bars[-lookback_bars:]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _map_interval(timeframe: Timeframe) -> str:
        """stooq : i=d (daily), i=w (weekly), i=5 (5-min intraday)."""
        if timeframe == "1d":
            return "d"
        if timeframe in ("1h", "4h", "5m", "15m"):
            # intraday 5m — l'agrégation vers H1/H4 se fait dans le fetcher
            return "5"
        raise TimeframeUnsupported(f"stooq ne gère pas timeframe={timeframe!r}")

    @staticmethod
    def _parse_csv(body: str, *, timeframe: Timeframe) -> list[OHLCVBar]:
        reader = csv.DictReader(io.StringIO(body))
        bars: list[OHLCVBar] = []
        for row in reader:
            try:
                # EOD CSV : Date=YYYY-MM-DD — normalise vers YYYY-MM-DDT00:00:00Z
                # Intraday CSV : Date=YYYY-MM-DD, Time=HH:MM:SS
                date_str = row["Date"]
                time_str = row.get("Time", "")
                if time_str:
                    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
                else:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                dt = dt.replace(tzinfo=timezone.utc)
                bars.append(OHLCVBar(
                    ts=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0.0) or 0.0),
                ))
            except (KeyError, ValueError):
                # Ligne corrompue — on skip, le fetcher détectera un gap si besoin
                continue
        return bars


__all__ = ["StooqSource"]
