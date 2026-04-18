"""
src.dashboards.pricing — chargement de `config/pricing.yaml` (§14.3.3).

Usage :
    pricing = ModelPricing.load()                    # lit config/pricing.yaml
    pricing.cost_usd("claude-sonnet-4-6", 1000, 500) # → float
    pricing.is_stale()                               # True si last_updated > 90 j

Design :
- Immutable dataclass chargée au démarrage.
- Lookup par nom de modèle exact (alignée sur OPENCLAW_MODEL_*, voir doc YAML).
- Si le modèle est inconnu → log warning + retour 0.0 (fail-open ; la télémétrie
  ne doit jamais bloquer une décision).
- Pas de tarif "par défaut" pour éviter les drifts silencieux si un nouveau
  modèle est utilisé sans config.

`LLMLimits` est un petit support container utilisé par CostRepository pour les
tuiles "budget tokens/jour" et "budget coût/mois". Les valeurs proviennent de
§2.3 (budgets LLM) et §11.4 (TokenBudgetGate).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from src.utils.config_loader import load_yaml

log = logging.getLogger(__name__)


_STALE_DAYS = 90
_PER_MTOK = 1_000_000


@dataclass(frozen=True)
class ModelPricing:
    """Tarifs $/Mtok/modèle + date de dernière mise à jour."""

    # modèle → (input_per_mtok_usd, output_per_mtok_usd)
    rates: dict[str, tuple[float, float]]
    last_updated: date
    source_url: Optional[str] = None

    @classmethod
    def load(cls, *, path: Optional[Path] = None) -> "ModelPricing":
        """Charge `config/pricing.yaml`. Si `path` fourni, lit ce fichier-là."""
        if path is not None:
            import yaml
            with path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = load_yaml("pricing.yaml")

        models_raw = raw.get("models", {}) or {}
        rates: dict[str, tuple[float, float]] = {}
        for name, payload in models_raw.items():
            if not isinstance(payload, dict):
                continue
            try:
                inp = float(payload.get("input_per_mtok_usd", 0.0))
                out = float(payload.get("output_per_mtok_usd", 0.0))
            except (TypeError, ValueError):
                log.warning("pricing_malformed", extra={"model": name})
                continue
            rates[name] = (inp, out)

        last_updated_raw = raw.get("last_updated")
        if isinstance(last_updated_raw, date):
            last_updated = last_updated_raw
        elif isinstance(last_updated_raw, str):
            try:
                last_updated = datetime.strptime(last_updated_raw, "%Y-%m-%d").date()
            except ValueError:
                log.warning("pricing_last_updated_invalid")
                last_updated = date.min
        else:
            last_updated = date.min

        return cls(
            rates=rates,
            last_updated=last_updated,
            source_url=raw.get("source"),
        )

    def cost_usd(self, model: str, tokens_in: int, tokens_out: int) -> float:
        """Calcule le coût $ pour un appel — fail-open si modèle inconnu."""
        if model not in self.rates:
            log.warning("pricing_unknown_model", extra={"model": model})
            return 0.0
        inp_rate, out_rate = self.rates[model]
        return (tokens_in / _PER_MTOK) * inp_rate + (tokens_out / _PER_MTOK) * out_rate

    def is_stale(self, *, today: Optional[date] = None) -> bool:
        """True si `last_updated` > `_STALE_DAYS` jours (défaut 90)."""
        ref = today or date.today()
        if self.last_updated == date.min:
            return True
        return (ref - self.last_updated).days > _STALE_DAYS


@dataclass(frozen=True)
class LLMLimits:
    """Plafonds de consommation LLM (§2.3 + §11.4)."""

    max_daily_tokens: int = 50_000
    max_monthly_cost_usd: float = 15.00
    # Pourcentages du budget déclenchant les alertes (§14.3.7)
    daily_tokens_warn_pct: float = 0.80
    daily_tokens_crit_pct: float = 0.95
    monthly_cost_warn_pct: float = 0.70
    monthly_cost_crit_pct: float = 0.90


__all__ = ["ModelPricing", "LLMLimits"]
