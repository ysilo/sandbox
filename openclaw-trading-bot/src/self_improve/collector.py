"""
src.self_improve.collector — étape 1 du self-improve (§13.2).

Lit les trades clôturés sur une fenêtre (30 jours par défaut) depuis SQLite et
construit un DataFrame simple (list[dict]) enrichi de features utiles au
diagnostic : catégorie de régime, durée, catégorie de P&L, conviction, etc.

Contrat :
- Input : connection SQLite + fenêtre de jours (`since_days`).
- Output : `CollectedDataset` avec `trades`, `losers`, `winners`, `ratio_losers`,
  `regime_snapshots` optionnel.

Principes :
- **Pure** : pas d'appel LLM ici, pas de requête HTTP. C'est le socle partagé
  de la pipeline : si cette étape échoue, les étapes suivantes ne tournent pas.
- **Déterministe** : trié par `entry_time ASC` pour que les tests soient
  reproductibles.
- Un "loser" = `pnl_pct < -0.5 %` (seuil configurable ; cf §13.2 étape 1).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils.logging_utils import get_logger


log = get_logger("SelfImprove.Collector")

# Seuil pour classer un trade comme « losers » (cf §13.2 étape 1 DIAGNOSTIC).
DEFAULT_LOSER_PNL_PCT: float = -0.5


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


@dataclass
class CollectedDataset:
    """Résultat de l'étape 1 — trades clôturés + dérivés."""

    trades: list[dict] = field(default_factory=list)        # tous les trades clôturés
    winners: list[dict] = field(default_factory=list)
    losers: list[dict] = field(default_factory=list)
    since: str = ""                                          # ISO 8601 (début fenêtre)
    until: str = ""                                          # ISO 8601 (fin fenêtre)
    loser_threshold_pct: float = DEFAULT_LOSER_PNL_PCT
    total: int = 0

    @property
    def ratio_losers(self) -> float:
        if self.total == 0:
            return 0.0
        return len(self.losers) / self.total

    def strategies(self) -> list[str]:
        """Liste unique des stratégies représentées dans les trades clôturés."""
        return sorted({t["strategy"] for t in self.trades if t.get("strategy")})


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # SQLite stocke soit ISO avec +00:00 soit "YYYY-MM-DD HH:MM:SS".
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _duration_hours(entry_time: Optional[str], exit_time: Optional[str]) -> Optional[float]:
    a = _parse_iso(entry_time)
    b = _parse_iso(exit_time)
    if not a or not b:
        return None
    delta = b - a
    return round(delta.total_seconds() / 3600.0, 3)


def _enrich(row: dict) -> dict:
    """Ajoute quelques features pré-calculées utiles au diagnosticien."""
    enriched = dict(row)
    enriched["duration_hours"] = _duration_hours(row.get("entry_time"), row.get("exit_time"))
    # Expose les listes JSON décodées (côté SQLite elles sont stringifiées).
    for key in ("catalysts", "tp_prices"):
        raw = row.get(key)
        if isinstance(raw, str):
            try:
                enriched[key] = json.loads(raw)
            except json.JSONDecodeError:
                enriched[key] = []
        elif raw is None:
            enriched[key] = []
    # Catégorie P&L grossière (pour stats diagnostic).
    pnl = row.get("pnl_pct")
    if pnl is None:
        enriched["pnl_bucket"] = "unknown"
    elif pnl > 0.5:
        enriched["pnl_bucket"] = "win"
    elif pnl < -0.5:
        enriched["pnl_bucket"] = "loss"
    else:
        enriched["pnl_bucket"] = "scratch"
    return enriched


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def collect_closed_trades(
    con: sqlite3.Connection,
    *,
    since_days: int = 30,
    loser_threshold_pct: float = DEFAULT_LOSER_PNL_PCT,
    now: Optional[datetime] = None,
) -> CollectedDataset:
    """Étape 1 du self-improve : trades clôturés sur les `since_days` derniers jours.

    - `con` : connexion SQLite ouverte (init_db).
    - `since_days` : fenêtre temporelle (30j par défaut, cf §13.2). Clampé ≥ 1
      pour éviter une fenêtre vide silencieuse en cas de valeur < 1.
    - `loser_threshold_pct` : seuil pour classer un trade "loser" (%).
    - `now` : injectable pour tests (fige la borne haute).
    """
    since_days = max(1, int(since_days))
    now = now or datetime.now(tz=timezone.utc)
    since_dt = now - timedelta(days=since_days)
    since_iso = since_dt.replace(microsecond=0).isoformat()
    until_iso = now.replace(microsecond=0).isoformat()

    cur = con.execute(
        """
        SELECT * FROM trades
         WHERE status = 'closed'
           AND entry_time >= ?
         ORDER BY entry_time ASC
        """,
        (since_iso,),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    trades = [_enrich(r) for r in rows]

    winners = [t for t in trades if (t.get("pnl_pct") or 0.0) > 0.0]
    losers = [t for t in trades if (t.get("pnl_pct") or 0.0) <= loser_threshold_pct]

    log.info(
        "collector_done",
        total=len(trades),
        winners=len(winners),
        losers=len(losers),
        since=since_iso,
    )
    return CollectedDataset(
        trades=trades,
        winners=winners,
        losers=losers,
        since=since_iso,
        until=until_iso,
        loser_threshold_pct=loser_threshold_pct,
        total=len(trades),
    )


__all__ = ["CollectedDataset", "collect_closed_trades", "DEFAULT_LOSER_PNL_PCT"]
