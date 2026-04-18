"""
src.risk.ichimoku_gate — règle d'or §2.5 / check C6 (§11.5).

API minimale :
    result = check(proposal, strategies_cfg)
    result.ok       → bool
    result.waived   → bool (True si la stratégie a `requires_ichimoku_alignment: false`)
    result.reason   → message actionnable

Principes :
- Aucun recalcul d'indicateur — on lit `proposal.ichimoku` (IchimokuPayload)
  produit par signal-crossing puis recopié par build_proposal.
- Fail-safe : si `proposal.ichimoku` est absent, le check retourne `ok=False`.
- Le waiver vient de `strategies.yaml[strategy_id].requires_ichimoku_alignment`.
  En V1 : `mean_reversion`, `divergence_hunter`, `volume_profile_scalp`
  sont waivered (cf. §11.5).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.contracts.skills import IchimokuPayload, StrategyConfig
from src.contracts.strategy import TradeProposal


@dataclass
class IchimokuGateResult:
    ok: bool
    waived: bool
    reason: str


# Type flexible : soit dict[id→StrategyConfig] (loader), soit dict brut (yaml).
StrategiesCfg = Mapping[str, Any]


def check(
    proposal: TradeProposal,
    strategies_cfg: StrategiesCfg,
) -> IchimokuGateResult:
    """Évalue l'alignement Ichimoku de la proposition.

    `strategies_cfg` peut être :
    - `dict[str, StrategyConfig]` (sortie de `load_strategies()`)
    - `dict[str, dict]`           (yaml brut `{"strategies": {...}, "defaults": {...}}`)
    """
    cfg_entry = _resolve_strategy_entry(strategies_cfg, proposal.strategy_id)
    if cfg_entry is None:
        # Stratégie inconnue : fail-closed (la règle d'or reste appliquée).
        return IchimokuGateResult(
            ok=False, waived=False,
            reason=f"stratégie inconnue : {proposal.strategy_id}",
        )

    requires = _resolve_requires(cfg_entry, strategies_cfg)

    if not requires:
        return IchimokuGateResult(
            ok=True, waived=True,
            reason=f"waiver strategies.yaml[{proposal.strategy_id}]",
        )

    # Requires=True — on contrôle l'alignement effectif.
    ich = getattr(proposal, "ichimoku", None)
    if ich is None:
        return IchimokuGateResult(
            ok=False, waived=False,
            reason="ichimoku payload missing (bug build_proposal)",
        )

    aligned_long = _get_bool(ich, "aligned_long")
    aligned_short = _get_bool(ich, "aligned_short")
    aligned = aligned_long if proposal.side == "long" else aligned_short
    if aligned:
        return IchimokuGateResult(ok=True, waived=False, reason="aligned")

    price_above = _get_bool(ich, "price_above_kumo")
    tenkan_above = _get_bool(ich, "tenkan_above_kijun")
    chikou_above = _get_bool(ich, "chikou_above_price_26")
    return IchimokuGateResult(
        ok=False, waived=False,
        reason=(
            f"ichimoku contrarien : side={proposal.side}, "
            f"price_above_kumo={price_above}, "
            f"tenkan_above_kijun={tenkan_above}, "
            f"chikou_above_price_26={chikou_above}"
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_RESERVED_YAML_KEYS = ("strategies", "defaults")


def _resolve_strategy_entry(cfg: StrategiesCfg, sid: str) -> Any | None:
    """Retourne l'entrée pour la stratégie `sid` depuis une config mixte.

    Supporte deux formats :
    - YAML brut : `{"strategies": {id: {...}}, "defaults": {...}}`
    - Loader    : `{id: StrategyConfig}` (sortie de `load_strategies()`)

    L'ordre priorise le wrapper YAML pour éviter toute collision de nom
    (une stratégie appelée "defaults" ou "strategies" serait ambiguë).
    """
    if not isinstance(cfg, Mapping):
        return None

    # Cas 1 : yaml brut avec wrapper "strategies"
    strategies_block = cfg.get("strategies")
    if isinstance(strategies_block, Mapping) and sid in strategies_block:
        return strategies_block[sid]

    # Cas 2 : dict plat {id→StrategyConfig} — exclure les clés réservées YAML.
    if sid in cfg and sid not in _RESERVED_YAML_KEYS:
        return cfg[sid]
    return None


def _resolve_requires(entry: Any, full_cfg: StrategiesCfg) -> bool:
    """Extrait le flag `requires_ichimoku_alignment`, avec fallback défaut."""
    # StrategyConfig Pydantic
    if isinstance(entry, StrategyConfig):
        return bool(entry.requires_ichimoku_alignment)
    # dict brut
    if isinstance(entry, Mapping) and "requires_ichimoku_alignment" in entry:
        return bool(entry["requires_ichimoku_alignment"])
    # Fallback global defaults[requires_ichimoku_alignment]
    defaults = full_cfg.get("defaults") if isinstance(full_cfg, Mapping) else None
    if isinstance(defaults, Mapping) and "requires_ichimoku_alignment" in defaults:
        return bool(defaults["requires_ichimoku_alignment"])
    # Sécurité max : true par défaut (règle d'or).
    return True


def _get_bool(ich: IchimokuPayload | Mapping, key: str) -> bool:
    if isinstance(ich, Mapping):
        return bool(ich.get(key, False))
    return bool(getattr(ich, key, False))


__all__ = ["IchimokuGateResult", "check"]
