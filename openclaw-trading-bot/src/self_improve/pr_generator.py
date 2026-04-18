"""
src.self_improve.pr_generator — étape 6 du self-improve (§13.2).

Écrit `IMPROVEMENTS_PENDING.md` à la racine du projet pour soumettre le patch
sélectionné à revue humaine. En V1 : pas de git push, pas d'ouverture de PR —
juste un fichier markdown versionné à côté du code.

Contrat :
    write_improvements_pending(selection, path) -> str (chemin écrit)

Format du fichier (stable, lisible par un humain) :

    # Improvements Pending — <date>

    ## Patch sélectionné : <patch_id>
    - **Scope** : strategy:breakout_momentum
    - **Kind** : param_tuning
    - **Score** : 0.82
    - **Description** : ...
    - **Changement proposé** : ...
    - **Métriques backtest** : sharpe +0.35, t-stat 2.7, DSR 0.97
    - **Pattern d'origine** : ...

    ## Actions
    - [ ] `/approve <patch_id>` — merge local + canary 14j
    - [ ] `/reject  <patch_id>` — archive sans merger
    - [ ] `/defer   <patch_id>` — reporter à la semaine prochaine

    ## Autres candidats rejetés
    - P-YYY : rejeté (validation_failed, t_stat 1.2)

Principe : le fichier est **idempotent** (régénéré à chaque run du pipeline) —
on ne trouve donc jamais deux patchs en attente en même temps.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.self_improve.selector import ScoredPatch, SelectionResult
from src.utils.logging_utils import get_logger


log = get_logger("SelfImprove.PRGenerator")

DEFAULT_PATH: str = "IMPROVEMENTS_PENDING.md"


# ---------------------------------------------------------------------------
# Helpers de rendu
# ---------------------------------------------------------------------------


def _fmt_score(sp: ScoredPatch) -> str:
    v = sp.validation
    return (
        f"sharpe {v.sharpe_baseline:.2f} → {v.sharpe_patch:.2f} "
        f"(Δ {v.sharpe_delta:+.2f}), "
        f"t-stat {v.t_stat:.2f}, "
        f"DD {v.dd_baseline:.3f} → {v.dd_patch:.3f}, "
        f"trades {v.trade_count}, DSR {v.dsr:.3f}"
    )


def _render_selected_block(sp: ScoredPatch) -> str:
    p = sp.patch
    pat = p.source_pattern
    pattern_line = (
        f"pattern=`{pat.pattern}` (freq {pat.frequency}, severity {pat.severity})"
        if pat
        else "pattern=inconnu"
    )
    change = ", ".join(f"{k}={v}" for k, v in p.change.items()) or "-"
    return (
        f"## Patch sélectionné : `{p.patch_id}`\n"
        f"- **Scope** : `{p.target}`\n"
        f"- **Kind** : `{p.kind}`\n"
        f"- **Score composite** : `{sp.score:.3f}`\n"
        f"- **Description** : {p.description}\n"
        f"- **Changement proposé** : {change}\n"
        f"- **Métriques backtest** : {_fmt_score(sp)}\n"
        f"- **Pattern d'origine** : {pattern_line}\n"
    )


def _render_actions(patch_id: str) -> str:
    return (
        "## Actions\n"
        f"- [ ] `/approve {patch_id}` — merge local + entrée en canary 14 jours.\n"
        f"- [ ] `/reject {patch_id}` — archiver sans merger (le pipeline respectera).\n"
        f"- [ ] `/defer {patch_id}` — reporter à la semaine prochaine.\n"
    )


def _render_rejected_block(
    rejected: list[ScoredPatch],
    blacklisted: list[ScoredPatch],
) -> str:
    if not rejected and not blacklisted:
        return ""
    lines = ["## Autres candidats"]
    for sp in rejected:
        reason = sp.block_reason or "rejected"
        lines.append(
            f"- `{sp.patch.patch_id}` — {reason} "
            f"(t_stat={sp.validation.t_stat:.2f}, dsr={sp.validation.dsr:.2f})"
        )
    for sp in blacklisted:
        lines.append(
            f"- `{sp.patch.patch_id}` — blacklist : {sp.block_reason}"
        )
    return "\n".join(lines) + "\n"


def _render_empty() -> str:
    return (
        "_Aucun patch ne remplit les critères §13.3.2 cette semaine._\n"
        "Pas d'action requise.\n"
    )


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def write_improvements_pending(
    selection: SelectionResult,
    *,
    path: str = DEFAULT_PATH,
    now: Optional[datetime] = None,
) -> str:
    """Écrit `IMPROVEMENTS_PENDING.md` et retourne le chemin final.

    - Le fichier est systématiquement réécrit (idempotent).
    - Si aucun patch n'est sélectionné ni rejeté, un message explicite est posé
      pour que le dashboard/Telegram puisse tout de même indiquer
      « cette semaine : rien à valider ».
    """
    now = now or datetime.now(tz=timezone.utc)
    header = f"# Improvements Pending — {now.strftime('%Y-%m-%d')}\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    parts: list[str] = [header]
    top = selection.top
    if top is None:
        parts.append(_render_empty())
    else:
        parts.append(_render_selected_block(top))
        parts.append(_render_actions(top.patch.patch_id))
    extra = _render_rejected_block(selection.rejected, selection.blacklisted)
    if extra:
        parts.append(extra)

    content = "\n".join(parts)

    # Écriture atomique : tmp puis os.replace (pas de fichier corrompu si crash).
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=target.name + ".",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    os.replace(tmp_path, str(target))

    log.info("improvements_pending_written", path=str(target), has_top=top is not None)
    return str(target)


__all__ = ["write_improvements_pending", "DEFAULT_PATH"]
