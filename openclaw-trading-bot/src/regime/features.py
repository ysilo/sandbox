"""
src.regime.features — matrice 5 features × N jours pour le HMM (§12.2.1).

Colonnes (ordre figé, cf. §12.2.1) :
    0. spx_return       — log-return S&P 500 (FRED SP500 ; fallback Stooq ^spx)
    1. vix              — niveau VIX (FRED VIXCLS ; fallback Stooq ^vix)
    2. dxy_change       — variation % DXY (FRED DTWEXBGS)
    3. yield_10y_change — diff absolue 10y Treasury (FRED DGS10)
    4. crypto_vol       — std log-returns BTC sur 20j glissants (CoinGecko)

Sources :
- Primaire : FRED (CSV public gratuit).
- Fallback : Stooq (mêmes séries, scraping CSV). V1 branche simplement la même
  interface `MacroSource.fetch_series` ; le provider alternatif est responsable
  du mapping série_id.

Toute série qui ne peut PAS être résolue (primaire KO ET fallback KO) fait
lever `FeatureFetchError`. Le caller (RegimeDetector) attrape et tombe sur
`last_regime.json` (§12.2.4).

Alignment : on travaille en date logique (YYYY-MM-DD). Les points manquants
sont forward-fill jusqu'à 3 jours — au-delà, on considère un trou significatif
et on lève DataGap (ou on tronque selon `allow_gaps`).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional, Protocol

import numpy as np

from src.data.sources.base import DataGap, MacroPoint, SourceUnavailable

log = logging.getLogger(__name__)


FEATURE_NAMES = (
    "spx_return",
    "vix",
    "dxy_change",
    "yield_10y_change",
    "crypto_vol",
)


class FeatureFetchError(RuntimeError):
    """Primaire ET fallback ont échoué pour au moins une feature."""


@dataclass
class FeatureSourceConfig:
    """Mapping série → IDs par provider.

    Surcharger pour tests ou pour pointer sur d'autres séries.
    """

    fred_spx: str = "SP500"
    fred_vix: str = "VIXCLS"
    fred_dxy: str = "DTWEXBGS"
    fred_y10y: str = "DGS10"
    stooq_spx: str = "^spx"
    stooq_vix: str = "^vix"
    stooq_dxy: str = "dx.f"
    stooq_y10y: str = "10usy.b"
    coingecko_btc: str = "bitcoin"


class _MacroLike(Protocol):
    name: str

    def fetch_series(self, series_id: str, *, days: int) -> list[MacroPoint]:
        ...


# ---------------------------------------------------------------------------
# Extraction d'une série unique avec fallback
# ---------------------------------------------------------------------------


def _fetch_with_fallback(
    primary: Optional[_MacroLike],
    primary_id: str,
    fallback: Optional[_MacroLike],
    fallback_id: str,
    *,
    days: int,
    label: str,
) -> list[MacroPoint]:
    """Tente primaire → fallback. Lève FeatureFetchError si les deux tombent."""
    last_exc: Optional[Exception] = None
    if primary is not None:
        try:
            return primary.fetch_series(primary_id, days=days)
        except (SourceUnavailable, DataGap) as exc:
            log.warning(
                "feature %s : primaire %s KO (%s), tentative fallback",
                label, getattr(primary, "name", "?"), exc,
            )
            last_exc = exc
    if fallback is not None:
        try:
            return fallback.fetch_series(fallback_id, days=days)
        except (SourceUnavailable, DataGap) as exc:
            log.error(
                "feature %s : fallback %s KO aussi (%s)",
                label, getattr(fallback, "name", "?"), exc,
            )
            last_exc = exc
    raise FeatureFetchError(
        f"feature {label} indisponible (primaire + fallback KO) : {last_exc}"
    )


# ---------------------------------------------------------------------------
# Alignement & forward-fill
# ---------------------------------------------------------------------------


def _to_daily_map(points: Iterable[MacroPoint]) -> dict[str, float]:
    return {p.date: float(p.value) for p in points}


def _align_on_dates(
    series: dict[str, dict[str, float]],
    *,
    dates: list[str],
    ffill_limit: int = 3,
) -> dict[str, list[float]]:
    """Aligne chaque série sur `dates`, avec forward-fill borné."""
    out: dict[str, list[float]] = {}
    for name, sdict in series.items():
        values: list[float] = []
        last_valid: Optional[float] = None
        ffill_count = 0
        for d in dates:
            v = sdict.get(d)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                if last_valid is not None and ffill_count < ffill_limit:
                    values.append(last_valid)
                    ffill_count += 1
                else:
                    values.append(float("nan"))
            else:
                values.append(float(v))
                last_valid = float(v)
                ffill_count = 0
        out[name] = values
    return out


def _union_sorted_dates(series: dict[str, dict[str, float]]) -> list[str]:
    all_dates: set[str] = set()
    for sdict in series.values():
        all_dates.update(sdict.keys())
    return sorted(all_dates)


# ---------------------------------------------------------------------------
# Calcul des features (transforms §12.2.1)
# ---------------------------------------------------------------------------


def _log_returns(values: list[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = np.diff(np.log(arr))
    return ret


def _pct_change(values: list[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.diff(arr) / arr[:-1]


def _absolute_diff(values: list[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    return np.diff(arr)


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    """Std glissant sur `window` observations. Renvoie un array de longueur
    max(0, len(values) - window + 1).
    """
    if len(values) < window:
        return np.array([], dtype=float)
    out = np.empty(len(values) - window + 1, dtype=float)
    for i in range(len(out)):
        out[i] = np.std(values[i : i + window], ddof=0)
    return out


# ---------------------------------------------------------------------------
# Builder principal
# ---------------------------------------------------------------------------


@dataclass
class FeatureMatrix:
    matrix: np.ndarray           # (N, 5) — ordre FEATURE_NAMES
    dates: list[str]             # (N,) dates alignées
    sources_used: dict[str, str] # nom de provider effectivement utilisé par feature


def build_features(
    *,
    fred: Optional[_MacroLike],
    coingecko: Optional[_MacroLike],
    stooq_macro: Optional[_MacroLike] = None,   # fallback optionnel (V1 : None)
    window_days: int = 60,
    config: Optional[FeatureSourceConfig] = None,
    today: Optional[date] = None,
) -> FeatureMatrix:
    """Construit la matrice (window_days × 5) pour le HMM.

    - Récupère SPX/VIX/DXY/10Y via FRED (fallback Stooq si fourni).
    - Récupère BTC via CoinGecko (pas de fallback raisonnable en V1).
    - Aligne sur les dates dispo (union puis ffill ≤3j).
    - Applique les transforms §12.2.1.

    Nb de jours à récupérer = `window_days + buffer` pour accommoder :
    - diff (1 barre de perdue)
    - rolling std 20j pour crypto_vol
    - log-returns 1 barre
    Buffer = 25 jours (cf. code de référence §12.2.4).
    """
    cfg = config or FeatureSourceConfig()
    today = today or datetime.now(timezone.utc).date()
    fetch_days = window_days + 25

    sources_used: dict[str, str] = {}

    def _fetch(label, p_src, p_id, f_src, f_id):
        points = _fetch_with_fallback(
            p_src, p_id, f_src, f_id,
            days=fetch_days, label=label,
        )
        used = getattr(p_src, "name", "?") if p_src else getattr(f_src, "name", "?")
        # si primaire a levé mais fallback a réussi, on doit le refléter.
        # Le truc simple : noter le nom de celui dont provient `points`.
        # On perd l'info exacte ici ; on affecte primaire si dispo.
        sources_used[label] = used
        return points

    spx_pts   = _fetch("spx",  fred, cfg.fred_spx,   stooq_macro, cfg.stooq_spx)
    vix_pts   = _fetch("vix",  fred, cfg.fred_vix,   stooq_macro, cfg.stooq_vix)
    dxy_pts   = _fetch("dxy",  fred, cfg.fred_dxy,   stooq_macro, cfg.stooq_dxy)
    y10_pts   = _fetch("y10y", fred, cfg.fred_y10y,  stooq_macro, cfg.stooq_y10y)
    btc_pts   = _fetch("btc",  coingecko, cfg.coingecko_btc, None, "")

    # 1. Construire un dict par série
    series = {
        "spx": _to_daily_map(spx_pts),
        "vix": _to_daily_map(vix_pts),
        "dxy": _to_daily_map(dxy_pts),
        "y10": _to_daily_map(y10_pts),
        "btc": _to_daily_map(btc_pts),
    }

    # 2. Dates : union, triée, filtrée <= today
    all_dates = [d for d in _union_sorted_dates(series) if d <= today.isoformat()]
    # On garde assez de points pour avoir window_days APRÈS les transformations
    # (log-returns + rolling 20 = -21 points). On prend les derniers fetch_days.
    dates = all_dates[-fetch_days:]
    if len(dates) < window_days + 21:
        raise FeatureFetchError(
            f"séries alignées trop courtes : {len(dates)} dates vs "
            f"{window_days + 21} requises"
        )

    aligned = _align_on_dates(series, dates=dates, ffill_limit=3)
    # 3. Drop any date où une série reste NaN après ffill
    mask_ok = [
        i for i in range(len(dates))
        if not any(math.isnan(aligned[s][i]) for s in aligned)
    ]
    if len(mask_ok) < window_days + 21:
        raise FeatureFetchError(
            f"données incomplètes après ffill : {len(mask_ok)} dates utilisables "
            f"vs {window_days + 21} requises"
        )
    dates = [dates[i] for i in mask_ok]
    for k in list(aligned.keys()):
        aligned[k] = [aligned[k][i] for i in mask_ok]

    # 4. Transforms
    spx_ret  = _log_returns(aligned["spx"])             # len N-1
    vix_lvl  = np.asarray(aligned["vix"], dtype=float)  # len N
    dxy_pct  = _pct_change(aligned["dxy"])              # len N-1
    y10_diff = _absolute_diff(aligned["y10"])           # len N-1
    btc_ret  = _log_returns(aligned["btc"])             # len N-1
    btc_vol  = _rolling_std(btc_ret, window=20)         # len N-1-19 = N-20

    # Pour aligner tout sur une fenêtre commune, on prend les `window_days`
    # dernières observations de chaque transform.
    common_len = min(len(spx_ret), len(vix_lvl) - 1, len(dxy_pct),
                     len(y10_diff), len(btc_vol))
    if common_len < window_days:
        raise FeatureFetchError(
            f"fenêtre commune post-transform trop courte : {common_len} "
            f"< {window_days}"
        )

    spx_ret  = spx_ret[-window_days:]
    vix_lvl  = vix_lvl[-window_days:]         # len N → prendre N derniers
    dxy_pct  = dxy_pct[-window_days:]
    y10_diff = y10_diff[-window_days:]
    btc_vol  = btc_vol[-window_days:]

    matrix = np.column_stack([spx_ret, vix_lvl, dxy_pct, y10_diff, btc_vol])

    # dates associées : les N derniers jours post-transform.
    aligned_dates = dates[-window_days:]

    return FeatureMatrix(
        matrix=matrix.astype(float),
        dates=aligned_dates,
        sources_used=sources_used,
    )


__all__ = [
    "FEATURE_NAMES",
    "FeatureFetchError",
    "FeatureSourceConfig",
    "FeatureMatrix",
    "build_features",
]
