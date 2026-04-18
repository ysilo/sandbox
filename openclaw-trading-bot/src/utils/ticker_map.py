"""
src.utils.ticker_map — conversion de tickers Euronext Paris entre providers.

Source : TRADING_BOT_ARCHITECTURE.md §7.1.

Format canonique (config/assets.yaml) :  <SYMBOL>.PA   ex : RUI.PA
Mappings gérés :
    Provider              Format           Exemple Rubis
    --------              ------           --------------
    stooq                 <symbol>.fr      rui.fr
    boursorama_scrape     1rP<SYMBOL>      1rPRUI
    ccxt                  <BASE>/<QUOTE>   BTC/USDT          (crypto only)
    oanda                 <BASE>_<QUOTE>   EUR_USD           (forex only)

Toute nouvelle source doit ajouter sa fonction ici — JAMAIS de conversion
ad-hoc ailleurs dans le code (§2.6 piège connu CLAUDE_AGENT_MEMORY.md §16).
"""
from __future__ import annotations

import re
from typing import Literal

AssetClass = Literal["equity", "forex", "crypto"]
Provider = Literal[
    "stooq",
    "boursorama_scrape",
    "ccxt",
    "oanda",
    "exchangerate_host",
    "coingecko",
    "fred",
]


_EUR_SYMBOL_PATTERN = re.compile(r"^(?P<sym>[A-Z0-9]{1,6})\.PA$")


class TickerMapError(ValueError):
    """Levée quand un symbole ne peut pas être converti vers un provider."""


# ---------------------------------------------------------------------------
# Equity — Euronext Paris
# ---------------------------------------------------------------------------


def _to_stooq_equity(canonical: str) -> str:
    """RUI.PA → rui.fr (CSV download)."""
    m = _EUR_SYMBOL_PATTERN.match(canonical)
    if not m:
        raise TickerMapError(f"symbole Euronext non reconnu : {canonical!r}")
    return f"{m.group('sym').lower()}.fr"


def _to_boursorama_equity(canonical: str) -> str:
    """RUI.PA → 1rPRUI (page cours URL)."""
    m = _EUR_SYMBOL_PATTERN.match(canonical)
    if not m:
        raise TickerMapError(f"symbole Euronext non reconnu : {canonical!r}")
    return f"1rP{m.group('sym')}"


# ---------------------------------------------------------------------------
# Crypto — ccxt
# ---------------------------------------------------------------------------


def _to_ccxt(canonical: str) -> str:
    """BTC/USDT → BTC/USDT (identité). Valide le format."""
    if "/" not in canonical:
        raise TickerMapError(f"symbole crypto non ccxt-conforme : {canonical!r}")
    base, quote = canonical.split("/", 1)
    if not (base.isalnum() and quote.isalnum()):
        raise TickerMapError(f"symbole crypto invalide : {canonical!r}")
    return canonical


# ---------------------------------------------------------------------------
# Forex — OANDA / exchangerate_host
# ---------------------------------------------------------------------------


_FX_PATTERN = re.compile(r"^(?P<base>[A-Z]{3})(?P<quote>[A-Z]{3})$")


def _to_oanda_forex(canonical: str) -> str:
    """EURUSD → EUR_USD."""
    m = _FX_PATTERN.match(canonical)
    if not m:
        raise TickerMapError(f"symbole forex non reconnu : {canonical!r}")
    return f"{m.group('base')}_{m.group('quote')}"


def _to_exchangerate_host_forex(canonical: str) -> tuple[str, str]:
    """EURUSD → (EUR, USD). Utilisé pour construire l'URL ?base=...&symbols=..."""
    m = _FX_PATTERN.match(canonical)
    if not m:
        raise TickerMapError(f"symbole forex non reconnu : {canonical!r}")
    return m.group("base"), m.group("quote")


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def resolve_ticker(
    canonical: str,
    provider: Provider,
    asset_class: AssetClass | None = None,
) -> str | tuple[str, str]:
    """Convertit le symbole canonique vers le format du provider.

    Args:
        canonical: symbole canonique (ex: "RUI.PA", "EURUSD", "BTC/USDT")
        provider: nom du provider cible
        asset_class: (optionnel) aide à disambiguer si le canonical est ambigu

    Returns:
        - str pour la plupart des providers
        - tuple[str, str] pour exchangerate_host forex (base, quote)

    Raises:
        TickerMapError si le symbole n'est pas reconnu pour ce provider.
    """
    if provider == "stooq":
        return _to_stooq_equity(canonical)
    if provider == "boursorama_scrape":
        return _to_boursorama_equity(canonical)
    if provider == "ccxt":
        return _to_ccxt(canonical)
    if provider == "oanda":
        return _to_oanda_forex(canonical)
    if provider == "exchangerate_host":
        return _to_exchangerate_host_forex(canonical)
    if provider in ("coingecko", "fred"):
        # Ces providers utilisent des IDs opaques (ex: "bitcoin", "SP500") — pas de mapping
        # auto depuis canonical. L'appelant passe l'ID natif directement.
        return canonical
    raise TickerMapError(f"provider inconnu : {provider!r}")


def infer_asset_class(canonical: str) -> AssetClass:
    """Heuristique simple pour deviner la classe depuis le format."""
    if canonical.endswith(".PA"):
        return "equity"
    if "/" in canonical:
        return "crypto"
    if _FX_PATTERN.match(canonical):
        return "forex"
    raise TickerMapError(f"impossible de déterminer la classe pour {canonical!r}")


__all__ = [
    "AssetClass",
    "Provider",
    "TickerMapError",
    "resolve_ticker",
    "infer_asset_class",
]
