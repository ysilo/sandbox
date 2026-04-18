"""
src.data.sources.fred — séries macro pour HMM (SP500, VIXCLS, DTWEXBGS, DGS10).

Source : TRADING_BOT_ARCHITECTURE.md §12.2.

CSV gratuit, sans clé :
    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>

Format :
    DATE,<SERIES_ID>
    2026-04-15,4980.30
    2026-04-16,.            <- `.` = missing (week-end, férié)
    2026-04-17,4995.12

Les valeurs `.` sont ignorées (trou). `DataGap` peut être levée si la série
est tronquée (< 50 % de couverture sur la fenêtre demandée).
"""
from __future__ import annotations

import csv
import io
import urllib.request
from datetime import datetime

from .base import DataGap, MacroPoint, SourceUnavailable


_BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_USER_AGENT = "openclaw-trading-bot/0.1"


class FredSource:
    """Source primaire pour features macro du HMM (§12.2)."""

    name = "fred"

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def fetch_series(self, series_id: str, *, days: int) -> list[MacroPoint]:
        url = f"{_BASE_URL}?id={series_id}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            raise SourceUnavailable(f"FRED {series_id} GET failed: {e}") from e

        reader = csv.reader(io.StringIO(body))
        header = next(reader, None)
        if not header or len(header) < 2:
            raise SourceUnavailable(f"FRED {series_id}: CSV header absent/invalide")

        points: list[MacroPoint] = []
        for row in reader:
            if len(row) < 2:
                continue
            date_str, val = row[0], row[1]
            if val == "." or not val:
                continue
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
                points.append(MacroPoint(date=date_str, value=float(val)))
            except ValueError:
                continue

        if not points:
            raise SourceUnavailable(f"FRED {series_id}: aucune valeur parsée")

        trimmed = points[-days:] if len(points) > days else points
        if len(trimmed) < max(5, days // 2):
            raise DataGap(
                f"FRED {series_id}: seulement {len(trimmed)} points sur {days} demandés"
            )
        return trimmed


__all__ = ["FredSource"]
