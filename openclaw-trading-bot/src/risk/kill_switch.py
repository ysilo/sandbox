"""
src.risk.kill_switch — sentinelle `data/KILL` (§11.1).

Contrat :
- `is_active()` : vrai si le fichier sentinelle existe.
- `arm(reason)` : crée le fichier (parents compris) avec timestamp + raison.
- `disarm()`   : supprime le fichier si présent.

Le fichier vit dans le seul dossier writable du container (§17.3). Le chemin
est résolu depuis `KILL_FILE_PATH` (défaut `data/KILL`) pour permettre override
en tests et en prod (le Dockerfile fixe `/app/data/KILL`).

Le kill-switch est évalué EN PREMIER par le risk-gate (check C1, §11.6) : si
armé, toute proposition est immédiatement rejetée avec `passed=False, severity=blocking`.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _resolve_kill_file(override: Optional[str | Path] = None) -> Path:
    if override is not None:
        return Path(override)
    return Path(os.environ.get("KILL_FILE_PATH", "data/KILL"))


class KillSwitch:
    """Sentinelle on-disk. Stateless : relit le filesystem à chaque appel."""

    def __init__(self, kill_file: Optional[str | Path] = None) -> None:
        self._kill_file = _resolve_kill_file(kill_file)

    @property
    def kill_file(self) -> Path:
        # Figé au __init__ : pour tester un override dynamique, instancier un
        # nouveau KillSwitch (ou passer `kill_file=...` explicite).
        return self._kill_file

    def is_active(self) -> bool:
        try:
            return self._kill_file.exists()
        except OSError as exc:
            # Fail-closed : si on ne peut pas statuer, on considère le kill armé.
            log.error("KillSwitch.is_active : erreur filesystem (%s) → fail-closed", exc)
            return True

    def arm(self, reason: str) -> None:
        try:
            self._kill_file.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._kill_file.write_text(f"{ts} — {reason}\n", encoding="utf-8")
            log.critical("Kill-switch armé : %s", reason)
        except OSError as exc:
            log.error("KillSwitch.arm : impossible d'écrire %s (%s)", self._kill_file, exc)
            raise

    def disarm(self) -> None:
        try:
            if self._kill_file.exists():
                self._kill_file.unlink()
                log.warning("Kill-switch désarmé manuellement (%s)", self._kill_file)
        except OSError as exc:
            log.error("KillSwitch.disarm : erreur (%s)", exc)
            raise

    def reason(self) -> str:
        """Contenu du fichier (timestamp + raison) pour diagnostic."""
        try:
            return self._kill_file.read_text(encoding="utf-8") if self._kill_file.exists() else ""
        except OSError:
            return ""


__all__ = ["KillSwitch"]
