"""
src.data.sources.ccxt_source — crypto OHLCV via ccxt (Binance, Kraken, Bybit…).

Source : TRADING_BOT_ARCHITECTURE.md §7.1.

Principes :
- Binance par défaut (liquidité + rate limit généreux), Kraken en fallback
  (diversité juridictionnelle — stable en cas d'événement réglementaire).
- L'instanciation de ccxt est faite paresseusement pour éviter la dépendance
  en démarrage si crypto est désactivé.
- Symbols ccxt utilisent le format `BASE/QUOTE` (ex: BTC/USDT) — le canonical
  de `config/assets.yaml` utilise déjà ce format (cf. ticker_map §7.1).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from src.utils.ticker_map import resolve_ticker

from .base import OHLCVBar, SourceUnavailable, Timeframe, TimeframeUnsupported


class CCXTSource:
    """Wrapper thin autour de ccxt pour ne pas fuiter ses types dans l'orchestrateur."""

    provider_kind: Literal["primary", "fallback"] = "primary"

    def __init__(self, exchange_id: str = "binance", timeout_ms: int = 8000) -> None:
        self.exchange_id = exchange_id
        self.name = f"ccxt:{exchange_id}"
        self._timeout_ms = timeout_ms
        self._client = None  # lazy

    def _ensure_client(self) -> object:
        if self._client is None:
            try:
                import ccxt  # import paresseux — évite la dépendance si crypto désactivé
            except ImportError as e:
                raise SourceUnavailable(f"ccxt non installé : {e}") from e
            try:
                klass = getattr(ccxt, self.exchange_id)
            except AttributeError as e:
                raise SourceUnavailable(f"ccxt.{self.exchange_id} introuvable") from e
            self._client = klass({
                "enableRateLimit": True,
                "timeout": self._timeout_ms,
            })
        return self._client

    def fetch_ohlcv(
        self,
        canonical_symbol: str,
        *,
        timeframe: Timeframe,
        lookback_bars: int,
    ) -> list[OHLCVBar]:
        symbol = resolve_ticker(canonical_symbol, "ccxt")
        assert isinstance(symbol, str)

        if timeframe in ("5m", "15m", "1h", "4h", "1d"):
            ccxt_tf = timeframe
        else:
            raise TimeframeUnsupported(f"ccxt timeframe non supporté : {timeframe!r}")

        client = self._ensure_client()
        try:
            raw = client.fetch_ohlcv(  # type: ignore[attr-defined]
                symbol=symbol,
                timeframe=ccxt_tf,
                limit=lookback_bars,
            )
        except Exception as e:
            raise SourceUnavailable(f"ccxt {self.exchange_id} fetch_ohlcv failed: {e}") from e

        bars: list[OHLCVBar] = []
        for row in raw:
            try:
                ms, o, h, l, c, v = row
                dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
                bars.append(OHLCVBar(
                    ts=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    open=float(o), high=float(h), low=float(l), close=float(c),
                    volume=float(v),
                ))
            except (ValueError, TypeError):
                continue
        return bars


__all__ = ["CCXTSource"]
