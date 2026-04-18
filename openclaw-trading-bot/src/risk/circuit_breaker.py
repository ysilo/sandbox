"""
src.risk.circuit_breaker — circuit breaker par stratégie (§11.3).

Règle : désactive temporairement une stratégie si son drawdown 7 jours
dépasse `threshold_ratio × drawdown_median_historique`.

Implémentation :
- Utilise la table `trades` pour calculer `dd_7d` (pire drawdown glissant de
  l'equity curve simulée sur les 7 derniers jours) et `dd_median` (médiane des
  drawdowns rolling 7 jours sur l'historique complet de la stratégie).
- Exige au moins `min_history_days` jours d'historique — sinon renvoie
  `INSUFFICIENT_DATA` (traité comme `CLOSED` par le gate = on laisse passer).
- Le calcul est fait à la volée, en RAM. Pas de table `circuit_state` en V1.

Fail-open côté RiskGate : si la DB est indisponible, le check C5 passe avec
`severity=warn` (on ne veut pas bloquer toutes les strats pour un blip DB).
Le fail-closed reste possible en forçant `strict_on_db_error=True`.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"                        # OK — stratégie active
    TRIPPED = "tripped"                      # drawdown 7j > seuil → désactivée
    INSUFFICIENT_DATA = "insufficient_data"  # pas assez d'historique


@dataclass
class CircuitBreakerResult:
    state: CircuitState
    dd_7d: float               # drawdown 7 jours (valeur positive, e.g. 0.05 = -5%)
    dd_median: Optional[float] # médiane historique
    sample_size_days: int      # nb jours d'historique trouvés
    reason: str


class CircuitBreaker:
    """Évalue l'état du circuit pour une stratégie donnée.

    Usage :
        cb = CircuitBreaker(threshold_ratio=2.0, min_history_days=30)
        result = cb.check_strategy("ichimoku_trend_following", conn)
        if result.state is CircuitState.TRIPPED:
            ...
    """

    def __init__(
        self,
        *,
        threshold_ratio: float = 2.0,
        min_history_days: int = 30,
        window_days: int = 7,
    ) -> None:
        if threshold_ratio <= 0:
            raise ValueError("threshold_ratio doit être > 0")
        if min_history_days < window_days:
            raise ValueError("min_history_days doit être ≥ window_days")
        self.threshold_ratio = float(threshold_ratio)
        self.min_history_days = int(min_history_days)
        self.window_days = int(window_days)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_strategy(
        self,
        strategy: str,
        conn: sqlite3.Connection,
        *,
        now: Optional[datetime] = None,
    ) -> CircuitBreakerResult:
        now_utc = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
        daily_pnl = self._load_daily_pnl(strategy, conn, now=now_utc)
        if len(daily_pnl) < self.min_history_days:
            return CircuitBreakerResult(
                state=CircuitState.INSUFFICIENT_DATA,
                dd_7d=0.0,
                dd_median=None,
                sample_size_days=len(daily_pnl),
                reason=(
                    f"historique insuffisant : {len(daily_pnl)}j < "
                    f"{self.min_history_days}j requis"
                ),
            )

        dd_7d = self._max_drawdown(daily_pnl[-self.window_days:])
        dd_median = self._rolling_dd_median(daily_pnl)

        if dd_median is None or dd_median <= 0.0:
            # Pas de variation historique significative — on tolère.
            return CircuitBreakerResult(
                state=CircuitState.CLOSED,
                dd_7d=dd_7d,
                dd_median=dd_median,
                sample_size_days=len(daily_pnl),
                reason="dd_median nul ou absent : pas de référence historique",
            )

        if dd_7d > dd_median * self.threshold_ratio:
            return CircuitBreakerResult(
                state=CircuitState.TRIPPED,
                dd_7d=dd_7d,
                dd_median=dd_median,
                sample_size_days=len(daily_pnl),
                reason=(
                    f"dd_7d={dd_7d:.2%} > {self.threshold_ratio}× médiane "
                    f"({dd_median:.2%})"
                ),
            )

        return CircuitBreakerResult(
            state=CircuitState.CLOSED,
            dd_7d=dd_7d,
            dd_median=dd_median,
            sample_size_days=len(daily_pnl),
            reason=(
                f"dd_7d={dd_7d:.2%} ≤ {self.threshold_ratio}× médiane "
                f"({dd_median:.2%})"
            ),
        )

    # ------------------------------------------------------------------
    # Helpers (protégés — testables par accès direct)
    # ------------------------------------------------------------------

    def _load_daily_pnl(
        self,
        strategy: str,
        conn: sqlite3.Connection,
        *,
        now: datetime,
    ) -> list[float]:
        """PnL quotidien de la stratégie, sur tout l'historique.

        Agrège `pnl_pct` des trades fermés (`status='closed'`) par `DATE(exit_time)`.
        Retourne une liste ordonnée chronologiquement, valeurs en proportion
        (pas en %, e.g. 0.012 = +1.2 %).
        """
        # On prend tout l'historique ; la longueur est bornée par le nb de
        # jours de trading — négligeable. Tri chronologique implicite par DATE.
        rows = conn.execute(
            """
            SELECT DATE(exit_time) AS d, COALESCE(SUM(pnl_pct), 0.0) AS p
              FROM trades
             WHERE strategy = ?
               AND status = 'closed'
               AND exit_time IS NOT NULL
             GROUP BY DATE(exit_time)
             ORDER BY DATE(exit_time) ASC
            """,
            (strategy,),
        ).fetchall()

        # Remplir les jours sans trade avec 0.0 pour que le calcul de DD soit continu.
        if not rows:
            return []
        start = datetime.fromisoformat(rows[0]["d"]).replace(tzinfo=timezone.utc)
        end_day = now.date()
        daily: dict[str, float] = {r["d"]: float(r["p"]) for r in rows}

        series: list[float] = []
        cur = start.date()
        while cur <= end_day:
            series.append(daily.get(cur.isoformat(), 0.0))
            cur = cur + timedelta(days=1)
        return series

    @staticmethod
    def _max_drawdown(daily_pnl: list[float]) -> float:
        """Drawdown max sur la fenêtre, à partir d'une equity curve cumulative.

        Retourne une valeur positive : 0.05 = -5 % de perte par rapport au pic.
        Equity initial = 1.0 (base arbitraire, on ne regarde que les ratios).
        """
        if not daily_pnl:
            return 0.0
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for pnl in daily_pnl:
            equity *= (1.0 + float(pnl))
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    def _rolling_dd_median(self, daily_pnl: list[float]) -> Optional[float]:
        """Médiane des drawdowns rolling window_days sur tout l'historique.

        Construit une liste de DD sur fenêtres glissantes (pas de recouvrement)
        puis retourne la médiane. Renvoie None si < min_history_days ou si la
        liste de DD est vide.
        """
        if len(daily_pnl) < self.min_history_days:
            return None
        w = self.window_days
        dds: list[float] = []
        # Fenêtres non chevauchantes pour éviter de sur-pondérer les blocs récents.
        for i in range(0, len(daily_pnl) - w + 1, w):
            dds.append(self._max_drawdown(daily_pnl[i : i + w]))
        if not dds:
            return None
        dds_sorted = sorted(dds)
        n = len(dds_sorted)
        mid = n // 2
        if n % 2 == 1:
            return dds_sorted[mid]
        return 0.5 * (dds_sorted[mid - 1] + dds_sorted[mid])


__all__ = ["CircuitState", "CircuitBreaker", "CircuitBreakerResult"]
