"""
src.utils.config_loader — chargement + validation des YAML de `config/`.

Architecture : TRADING_BOT_ARCHITECTURE.md §3.1.

Principes :
- Lecture unique au startup (cache mémoire via lru_cache sur le chemin).
- Validation Pydantic partielle pour les fichiers dont le contrat est figé
  (`strategies.yaml` → list[StrategyConfig], `risk.yaml` → RiskConfig).
- Les fichiers moins structurés (`assets.yaml`, `sources.yaml`) restent
  retournés en dict : la validation fine se fait dans le module consommateur.
- Erreurs explicitement taguées `CFG_xxx` (cf. `src/utils/error_codes.py`).

Ordre de résolution :
1. Variable d'env `OPENCLAW_CONFIG_DIR` si positionnée (dev, tests)
2. `./config/` relatif à la racine projet (défaut)

Les chemins sont résolus une fois au premier appel puis cachés.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, confloat, conint

from src.contracts.skills import StrategyConfig


# ---------------------------------------------------------------------------
# Schémas Pydantic pour les fichiers config les plus structurés
# ---------------------------------------------------------------------------


class LLMBudget(BaseModel):
    max_daily_tokens: conint(ge=0)
    max_monthly_cost_usd: confloat(ge=0.0)
    model: str
    opus_reserved_for: list[str] = []


class AvoidWindow(BaseModel):
    name: str
    offset_min: list[int]                   # [before, after] en minutes — [neg, pos]


class RiskConfig(BaseModel):
    """Schéma de `config/risk.yaml` (§2.3, §11)."""

    max_risk_per_trade_pct_equity: confloat(ge=0.0, le=5.0)
    max_daily_loss_pct_equity: confloat(ge=0.0, le=10.0)
    max_open_positions: conint(ge=1, le=50)
    max_exposure_per_asset_class_pct: confloat(ge=0.0, le=100.0)
    min_rr: confloat(ge=1.0)
    max_correlated_positions: conint(ge=1)
    correlation_window_days: conint(ge=5)
    max_correlation_threshold: confloat(ge=0.0, le=1.0)
    kill_switch_file: str
    llm: LLMBudget
    avoid_windows: list[AvoidWindow] = []
    slippage_bps_default: confloat(ge=0.0) = 3.0
    fee_bps_default: confloat(ge=0.0) = 7.0


class ModeConfig(BaseModel):
    """Schéma de `config/mode.yaml` — V1 refuse tout sauf `paper`."""

    mode: str                               # validé plus bas : "paper" uniquement en V1
    signed_by: str = ""
    signed_at: str = ""


# ---------------------------------------------------------------------------
# Résolution de chemin + chargement brut
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    override = os.environ.get("OPENCLAW_CONFIG_DIR")
    if override:
        p = Path(override)
    else:
        p = Path(__file__).resolve().parents[2] / "config"
    if not p.is_dir():
        raise FileNotFoundError(f"CFG_001 config dir introuvable : {p}")
    return p


@lru_cache(maxsize=32)
def _load_yaml_cached(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.is_file():
        raise FileNotFoundError(f"CFG_002 YAML introuvable : {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"CFG_003 YAML racine attendue dict, reçu {type(data).__name__} : {path}")
    return data


def load_yaml(filename: str) -> dict[str, Any]:
    """Charge un YAML depuis `config/` en dict. Cache lru."""
    return _load_yaml_cached(str(_config_dir() / filename))


def reload_all() -> None:
    """Purge le cache — utile en dev ou après `SIGHUP` custom."""
    _load_yaml_cached.cache_clear()


# ---------------------------------------------------------------------------
# Loaders typés
# ---------------------------------------------------------------------------


def load_risk_config() -> RiskConfig:
    return RiskConfig.model_validate(load_yaml("risk.yaml"))


def load_strategies() -> dict[str, StrategyConfig]:
    """Charge `strategies.yaml` et renvoie un mapping `id → StrategyConfig`.

    Les entrées sans champs obligatoires fail au startup (fail-fast).
    """
    raw = load_yaml("strategies.yaml")
    defs = raw.get("strategies", {})
    if not isinstance(defs, dict):
        raise ValueError("CFG_004 strategies.yaml: `strategies` doit être un mapping id→config")

    out: dict[str, StrategyConfig] = {}
    for sid, payload in defs.items():
        payload = {**payload, "id": sid}
        out[sid] = StrategyConfig.model_validate(payload)
    return out


def load_assets() -> dict[str, Any]:
    return load_yaml("assets.yaml")


def load_sources() -> dict[str, Any]:
    return load_yaml("sources.yaml")


def load_schedules() -> dict[str, Any]:
    return load_yaml("schedules.yaml")


def load_mode(allow_live_in_v2: bool = False) -> ModeConfig:
    """Charge `config/mode.yaml` — V1 refuse tout sauf `mode: paper` (§1)."""
    raw = load_yaml("mode.yaml") if (_config_dir() / "mode.yaml").exists() else load_yaml("mode.example.yaml")
    cfg = ModeConfig.model_validate(raw)
    if not allow_live_in_v2 and cfg.mode != "paper":
        raise ValueError(
            f"CFG_005 mode={cfg.mode!r} interdit en V1 — seul `paper` est toléré (§1)."
        )
    return cfg


__all__ = [
    "RiskConfig",
    "LLMBudget",
    "AvoidWindow",
    "ModeConfig",
    "load_yaml",
    "load_risk_config",
    "load_strategies",
    "load_assets",
    "load_sources",
    "load_schedules",
    "load_mode",
    "reload_all",
]
