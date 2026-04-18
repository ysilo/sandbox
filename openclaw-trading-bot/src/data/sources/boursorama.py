"""
src.data.sources.boursorama — fournisseur fallback Euronext Paris (scrape).

Source : TRADING_BOT_ARCHITECTURE.md §7.1 + §7.2.1.

Principe : scrape public HTML de pages cours Boursorama, avec detection
de changement de layout (lève DATA_007 si la structure HTML diverge).

URL : https://www.boursorama.com/cours/<ticker>/  (ex: 1rPRUI)
Parse : BeautifulSoup + lxml, chaque quote ligne avec 7 colonnes attendues.

⚠ Rate limit 1 req/s (politesse, pas contractuel). Si Boursorama détecte
un bot, la page renvoie un 403 ou une page challenge — l'appelant devra
basculer sur un autre fallback (il n'y en a pas en V1 → fail-closed §2.2).
"""
from __future__ import annotations

import time
import urllib.request
from datetime import datetime, timezone
from typing import Literal

from src.utils.error_codes import EC
from src.utils.logging_utils import get_logger
from src.utils.ticker_map import resolve_ticker

from .base import (
    OHLCVBar,
    SourceUnavailable,
    Timeframe,
    TimeframeUnsupported,
)


_BASE_URL = "https://www.boursorama.com/cours"
_HISTO_URL = "https://www.boursorama.com/bourse/action/graph/ajax/api/v1/quote/chart/historique"
_USER_AGENT = "Mozilla/5.0 (compatible; openclaw-trading-bot/0.1)"

log = get_logger("BoursoramaScrape")


class BoursoramaSource:
    """Scrape Boursorama — EOD uniquement en V1 (intraday différé V2).

    Le scrape intraday fin sort du scope MVP : la page publique ne renvoie
    que les derniers jours à résolution ~1min qui n'est pas agrégée
    fiablement en H1/H4. On se contente d'EOD pour le fallback.
    """

    name = "boursorama_scrape"
    provider_kind: Literal["primary", "fallback"] = "fallback"

    def __init__(self, timeout: float = 8.0, sleep_s: float = 1.0) -> None:
        self.timeout = timeout
        self.sleep_s = sleep_s

    def fetch_ohlcv(
        self,
        canonical_symbol: str,
        *,
        timeframe: Timeframe,
        lookback_bars: int,
    ) -> list[OHLCVBar]:
        if timeframe != "1d":
            raise TimeframeUnsupported(
                f"boursorama V1 supporte uniquement timeframe=1d (demandé: {timeframe!r})"
            )

        ticker = resolve_ticker(canonical_symbol, "boursorama_scrape")
        assert isinstance(ticker, str)
        url = f"{_HISTO_URL}?symbol={ticker}&length={lookback_bars}&period=0"

        time.sleep(self.sleep_s)                # politesse
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            raise SourceUnavailable(f"boursorama GET {url} failed: {e}") from e

        if status != 200:
            raise SourceUnavailable(f"boursorama status={status} for {url}")

        return self._parse_histo_json(body)

    # ------------------------------------------------------------------
    # Parsing — la réponse AJAX Boursorama est du JSON
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_histo_json(body: str) -> list[OHLCVBar]:
        import json as _json

        try:
            doc = _json.loads(body)
        except ValueError as e:
            # Probablement changement de layout / réponse HTML
            log.error(
                "scrape_layout_changed",
                ec=EC.DATA_007,
                cause=f"JSON parse failed: {e}",
            )
            raise SourceUnavailable("boursorama: réponse non-JSON (layout changé ?)") from e

        quotes = (
            doc.get("d", {}).get("QuoteTab")
            if isinstance(doc, dict) and isinstance(doc.get("d"), dict)
            else None
        )
        if not isinstance(quotes, list) or not quotes:
            log.error(
                "scrape_layout_changed",
                ec=EC.DATA_007,
                cause="structure JSON attendue (`d.QuoteTab`) absente ou vide",
            )
            raise SourceUnavailable("boursorama: structure JSON inattendue")

        bars: list[OHLCVBar] = []
        for q in quotes:
            try:
                # Format Boursorama : {"d": "2026-04-17", "o": 40.12, "h": ..., "l": ..., "c": ..., "v": 12345}
                dt = datetime.strptime(q["d"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                bars.append(OHLCVBar(
                    ts=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    open=float(q["o"]),
                    high=float(q["h"]),
                    low=float(q["l"]),
                    close=float(q["c"]),
                    volume=float(q.get("v", 0.0) or 0.0),
                ))
            except (KeyError, ValueError, TypeError):
                continue

        bars.sort(key=lambda b: b.ts)
        return bars


__all__ = ["BoursoramaSource"]
