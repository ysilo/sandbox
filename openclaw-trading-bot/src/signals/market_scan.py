"""
src.signals.market_scan — skill `market-scan` §8.8, §8.8.2, §8.8.3.

Scanner d'univers déterministe, 0 token. Produit une shortlist de `Candidate`
(Pydantic §8.8.1) admissibles pour le pipeline aval (`strategy-selector` →
`signal-crossing` → `build_proposal` → `risk-gate`).

Formule §8.8.2 :

    score_scan = 0.35 × trend_slope_50       # pente SMA50 normalisée
               + 0.25 × relative_volume_20   # vol / moyenne 20j
               + 0.20 × abs(atr_pct_20)      # volatilité annualisée
               + 0.20 × momentum_roc_20      # ROC sur 20 barres
    score_scan = clip(score_scan, -1, 1)

Seuil de shortlist : `abs(score_scan) ≥ 0.25` ET `liquidity_ok`. Taille cible
**20 candidats max** (§8.8.3).

Design :
- Pas d'I/O direct : le scanner reçoit déjà les OHLCV via un callable
  `fetch(asset, asset_class) -> list[OHLCVBar]`. L'orchestrateur branche ça
  sur `DataFetcher.fetch()`. Cela rend le module 100 % testable sans
  instancier un fetcher complet.
- Liquidity : callback `liquidity_check(asset, asset_class) -> bool` optionnel.
  Si None, considère tout le monde liquide (shortcut pour tests).
- Si l'univers filtré est vide → log WARNING (à la main du consommateur) et
  retourne `[]`. L'orchestrateur doit alors abréger le cycle avec DATA_006
  `empty_shortlist` (§8.8.3).

Pas d'appels LLM, pas d'écriture fichier : la persistance JSON + SQLite est
faite par l'orchestrateur (§8.7) qui appelle ce module.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Literal, Optional, Sequence

from src.contracts.skills import Candidate


AssetClass = Literal["equity", "forex", "crypto"]


# ---------------------------------------------------------------------------
# Constantes §8.8.2 / §8.8.3
# ---------------------------------------------------------------------------

# Pondérations score_scan
_W_TREND_SLOPE = 0.35
_W_REL_VOL = 0.25
_W_ATR_PCT = 0.20
_W_ROC_20 = 0.20

# Seuil shortlist §8.8.2
SCORE_SCAN_THRESHOLD: float = 0.25

# Plafond shortlist total §8.8.3
SHORTLIST_MAX_TOTAL: int = 20

# Plafonds par classe §8.8.3 (non-enforce ici — c'est l'orchestrateur qui
# compose l'univers ; on expose les constantes pour référence)
SHORTLIST_CAPS_BY_CLASS: dict[str, int] = {
    "equity": 10,
    "forex": 5,
    "crypto": 5,
}

# Minimum de barres pour calculer les features (SMA50 + 20 barres de contexte)
_MIN_BARS_REQUIRED: int = 60


# ---------------------------------------------------------------------------
# Types I/O minimaux — callbacks pour découpler du fetcher
# ---------------------------------------------------------------------------


@dataclass
class _UniverseEntry:
    """Un asset de l'univers à scanner avec sa classe et son symbole canonique."""

    asset: str               # "RUI.PA", "EURUSD", "BTC/USDT"
    asset_class: AssetClass


# Callback fetch OHLCV : signature minimale — bars en ordre croissant (asc).
# Chaque barre = (ts, open, high, low, close, volume)
OHLCVTuple = tuple[str, float, float, float, float, float]
FetchFn = Callable[[str, str], Sequence[OHLCVTuple]]

# Callback liquidité optionnel — §7.3
LiquidityFn = Callable[[str, str], bool]


# ---------------------------------------------------------------------------
# Features §8.8.2 — 100 % Python pur
# ---------------------------------------------------------------------------


def _sma(values: Sequence[float], window: int) -> Optional[float]:
    if len(values) < window or window <= 0:
        return None
    return sum(values[-window:]) / float(window)


def _trend_slope_50(closes: Sequence[float]) -> float:
    """Pente normalisée SMA50 : (SMA50_now - SMA50_20ago) / SMA50_20ago.

    Clippée ensuite par le score global. Retourne 0.0 si données insuffisantes.
    """
    if len(closes) < 70:  # SMA50 + 20 barres de recul
        return 0.0
    sma_now = _sma(closes, 50)
    sma_past = _sma(closes[:-20], 50)
    if sma_now is None or sma_past is None or sma_past == 0:
        return 0.0
    return (sma_now - sma_past) / sma_past


def _relative_volume_20(volumes: Sequence[float]) -> float:
    """vol_last / mean(vol, 20). Clippé [-1, 1] par normalisation tanh.

    La formule source §8.8.2 n'est pas bornée ; on applique `tanh` pour la
    contenir dans [-1, 1] sans écraser les signaux modérés.
    """
    if len(volumes) < 21:
        return 0.0
    recent = volumes[-1]
    mean_prev = sum(volumes[-21:-1]) / 20.0
    if mean_prev <= 0:
        return 0.0
    ratio = recent / mean_prev
    # Centrer autour de 1.0 (ratio=1 → 0.0 score), saturation via tanh
    return math.tanh(ratio - 1.0)


def _atr_pct_20(bars: Sequence[OHLCVTuple]) -> float:
    """ATR(20) / close, clippée [-1, 1].

    ATR = moyenne des True Range sur 20 barres.
    True Range = max(high-low, |high-close_prev|, |low-close_prev|).
    Renvoie `atr / last_close` clippé — valeur typique 0-0.10 en marchés
    normaux, peut monter à 0.3+ en crypto volatile.
    """
    if len(bars) < 21:
        return 0.0
    tr_values: list[float] = []
    for i in range(-20, 0):
        high = bars[i][2]
        low = bars[i][3]
        close_prev = bars[i - 1][4]
        tr = max(
            high - low,
            abs(high - close_prev),
            abs(low - close_prev),
        )
        tr_values.append(tr)
    atr = sum(tr_values) / len(tr_values)
    last_close = bars[-1][4]
    if last_close <= 0:
        return 0.0
    return max(-1.0, min(1.0, atr / last_close))


def _momentum_roc_20(closes: Sequence[float]) -> float:
    """Rate of Change 20 : (close_now - close_20ago) / close_20ago. Non borné
    ici ; le score global est clippé. Typiquement [-0.5, +0.5].
    """
    if len(closes) < 21:
        return 0.0
    past = closes[-21]
    now = closes[-1]
    if past == 0:
        return 0.0
    return (now - past) / past


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def compute_score_scan(bars: Sequence[OHLCVTuple]) -> float:
    """Score déterministe §8.8.2. Clippé dans [-1, 1].

    Retourne 0.0 si `len(bars) < _MIN_BARS_REQUIRED` (data insuffisante).
    Ce cas-là est distingué par `scan()` qui écarte l'asset avec la raison
    "insufficient_bars".
    """
    if len(bars) < _MIN_BARS_REQUIRED:
        return 0.0

    closes = [b[4] for b in bars]
    volumes = [b[5] for b in bars]

    trend = _trend_slope_50(closes)
    rvol = _relative_volume_20(volumes)
    atr = abs(_atr_pct_20(bars))
    roc = _momentum_roc_20(closes)

    raw = (
        _W_TREND_SLOPE * trend
        + _W_REL_VOL * rvol
        + _W_ATR_PCT * atr
        + _W_ROC_20 * roc
    )
    return _clip(raw)


# ---------------------------------------------------------------------------
# Scan principal
# ---------------------------------------------------------------------------


@dataclass
class ScanStats:
    """Statistiques descriptives du run, consommées par le dashboard §14."""

    scanned: int
    shortlisted: int
    skipped_insufficient_bars: int
    skipped_liquidity: int
    skipped_fetch_error: int


@dataclass
class ScanResult:
    """Sortie complète du scan : shortlist + stats."""

    candidates: list[Candidate]
    stats: ScanStats


def scan(
    universe: Sequence[_UniverseEntry],
    *,
    fetch: FetchFn,
    liquidity_check: Optional[LiquidityFn] = None,
    max_shortlist: int = SHORTLIST_MAX_TOTAL,
) -> ScanResult:
    """Scan de l'univers → shortlist triée par `|score_scan|` desc.

    Args:
        universe: liste d'assets à scanner. L'orchestrateur construit cette
            liste depuis `config/assets.yaml`.
        fetch: callback OHLCV. Doit retourner ≥ `_MIN_BARS_REQUIRED` barres
            (sinon l'asset est skippé avec raison `insufficient_bars`). Les
            erreurs `DataSourceError` doivent être interceptées par l'appelant
            — ici on catch juste `Exception` par défaut pour éviter qu'une
            source dégradée casse tout le scan.
        liquidity_check: optionnel. Si fourni, appelé par asset.
        max_shortlist: plafond global §8.8.3 (défaut 20).

    Returns:
        ScanResult contenant la shortlist Pydantic + stats agrégées.

    Note:
        Ne log pas — logging est la responsabilité de l'orchestrateur qui
        invoque ce scan. On retourne des stats pour permettre des métriques.
    """
    scored: list[tuple[float, _UniverseEntry]] = []

    skipped_bars = 0
    skipped_fetch = 0

    for entry in universe:
        try:
            bars = list(fetch(entry.asset, entry.asset_class))
        except Exception:  # pragma: no cover — défense en profondeur
            skipped_fetch += 1
            continue

        if len(bars) < _MIN_BARS_REQUIRED:
            skipped_bars += 1
            continue

        score = compute_score_scan(bars)
        scored.append((score, entry))

    # Filtre seuil + liquidité
    candidates: list[Candidate] = []
    skipped_liq = 0
    # Trier par |score| desc pour prioriser les signaux forts avant le cap
    scored.sort(key=lambda x: abs(x[0]), reverse=True)

    for score, entry in scored:
        if abs(score) < SCORE_SCAN_THRESHOLD:
            continue

        liquid = True
        if liquidity_check is not None:
            try:
                liquid = bool(liquidity_check(entry.asset, entry.asset_class))
            except Exception:  # pragma: no cover
                liquid = False
        if not liquid:
            skipped_liq += 1
            continue

        candidates.append(
            Candidate(
                asset=entry.asset,
                asset_class=entry.asset_class,
                score_scan=float(score),
                liquidity_ok=True,
            )
        )
        if len(candidates) >= max_shortlist:
            break

    stats = ScanStats(
        scanned=len(universe),
        shortlisted=len(candidates),
        skipped_insufficient_bars=skipped_bars,
        skipped_liquidity=skipped_liq,
        skipped_fetch_error=skipped_fetch,
    )
    return ScanResult(candidates=candidates, stats=stats)


def force_candidate(
    asset: str,
    asset_class: AssetClass,
    *,
    forced_by: Literal["news_pulse", "telegram_cmd", "correlated_to"],
    correlated_to: Optional[str] = None,
    score_scan: float = 0.0,
) -> Candidate:
    """Fabrique un `Candidate` forcé — bypasse le scoring §8.8.2.

    Utilisé par l'orchestrateur quand `NewsWatcher` (§15.1) déclenche un
    cycle focused sur un asset qui n'est pas dans la shortlist régulière,
    ou quand l'opérateur force un asset via commande Telegram.

    `liquidity_ok` forcé à True — le risk-gate C10 tranchera en aval si la
    liquidité est insuffisante au moment de l'exécution.
    """
    if forced_by == "correlated_to" and correlated_to is None:
        raise ValueError("forced_by='correlated_to' nécessite correlated_to=<asset>")
    return Candidate(
        asset=asset,
        asset_class=asset_class,
        score_scan=float(_clip(score_scan)),
        liquidity_ok=True,
        forced_by=forced_by,
        correlated_to=correlated_to,
    )


__all__ = [
    "SCORE_SCAN_THRESHOLD",
    "SHORTLIST_MAX_TOTAL",
    "SHORTLIST_CAPS_BY_CLASS",
    "_UniverseEntry",
    "ScanStats",
    "ScanResult",
    "compute_score_scan",
    "scan",
    "force_candidate",
]
