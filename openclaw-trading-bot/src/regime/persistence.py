"""
src.regime.persistence — stockage versionné des modèles HMM (§12.2.3).

Layout :
    {models_dir}/regime_hmm_active.pkl        # symlink vers version active
    {models_dir}/regime_hmm_v{n}.pkl          # modèles archivés
    {models_dir}/regime_hmm_v{n}.meta.json    # métadonnées
    {models_dir}/regime_hmm.lock              # lock file (optionnel, V1 non utilisé)

Note : le symlink a un nom distinct de `v{n}` pour éviter toute collision
avec le fichier réel d'une version (sinon `set_active(1)` supprimerait le
pkl qui vient d'être écrit pour écrire un symlink sur lui-même).

Rétention : les `keep_n` versions les plus récentes sont conservées (défaut 6).

Sur systèmes sans symlink (CI Windows), on fallback sur un pointeur JSON
`active.json` qui contient `{"active_version": n}`.

`last_regime.json` est géré dans le même module (simple atomique write).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_MODEL_PATTERN = re.compile(r"^regime_hmm_v(\d+)\.pkl$")


# ---------------------------------------------------------------------------
# Modèle meta
# ---------------------------------------------------------------------------


@dataclass
class ModelMeta:
    version: int
    trained_at: str
    training_window: dict[str, str]
    n_observations: int
    feature_means: list[float]
    feature_stds: list[float]
    state_map: dict[str, str]                # {"0": "risk_on", ...}
    accuracy_backtest_30d: Optional[float] = None
    hmm_params: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "ModelMeta":
        data = json.loads(text)
        return cls(**data)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ModelStore:
    """Filesystem store pour les modèles HMM + métadonnées."""

    ACTIVE_LINK = "regime_hmm_active.pkl"      # symlink (nom distinct des v{n})
    ACTIVE_JSON = "active.json"                # fallback sans symlink

    def __init__(self, models_dir: str | Path, *, keep_n: int = 6) -> None:
        self.dir = Path(models_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep_n = int(keep_n)

    # ---- versions ----------------------------------------------------

    def list_versions(self) -> list[int]:
        versions = []
        for p in self.dir.iterdir():
            m = _MODEL_PATTERN.match(p.name)
            if m:
                versions.append(int(m.group(1)))
        return sorted(versions)

    def next_version(self) -> int:
        vs = self.list_versions()
        return (vs[-1] + 1) if vs else 1

    def path_for(self, version: int) -> Path:
        return self.dir / f"regime_hmm_v{version}.pkl"

    def meta_path_for(self, version: int) -> Path:
        return self.dir / f"regime_hmm_v{version}.meta.json"

    # ---- symlink / active pointer -----------------------------------

    def _active_link(self) -> Path:
        return self.dir / self.ACTIVE_LINK

    def _active_json(self) -> Path:
        return self.dir / self.ACTIVE_JSON

    def set_active(self, version: int) -> None:
        target = self.path_for(version)
        if not target.exists():
            raise FileNotFoundError(f"version {version} introuvable : {target}")
        link = self._active_link()
        # ACTIVE_LINK a un nom distinct de v{n}.pkl — pas de collision possible.
        # On supprime le symlink existant (ou un ancien fichier orphelin du même
        # nom) avant d'en créer un neuf.
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(target.name)
            return
        except (OSError, NotImplementedError) as exc:
            log.warning("symlink non dispo (%s), fallback active.json", exc)

        # Fallback : active.json
        self._atomic_write_text(
            self._active_json(),
            json.dumps({"active_version": version}),
        )

    def active_version(self) -> Optional[int]:
        # Priorité symlink
        link = self._active_link()
        if link.is_symlink():
            try:
                target = os.readlink(link)
                m = _MODEL_PATTERN.match(Path(target).name)
                if m:
                    return int(m.group(1))
            except OSError:
                pass
        # Fallback JSON
        j = self._active_json()
        if j.is_file():
            try:
                data = json.loads(j.read_text(encoding="utf-8"))
                return int(data.get("active_version"))
            except (OSError, ValueError, TypeError):
                pass
        # Dernier recours : version la plus récente
        vs = self.list_versions()
        return vs[-1] if vs else None

    # ---- save / load -------------------------------------------------

    def save(self, *, model: Any, meta: ModelMeta, set_active: bool = True) -> Path:
        import joblib  # lazy import — joblib tire scipy indirect

        version = meta.version
        target = self.path_for(version)
        # Écriture atomique du pkl : tmp → replace
        tmp = target.with_suffix(".pkl.tmp")
        joblib.dump(model, tmp)
        os.replace(tmp, target)

        self._atomic_write_text(self.meta_path_for(version), meta.to_json())
        if set_active:
            self.set_active(version)

        self._prune_old_versions()
        return target

    def load_active(self) -> tuple[Any, ModelMeta]:
        import joblib

        v = self.active_version()
        if v is None:
            raise FileNotFoundError("aucun modèle HMM disponible")
        model_path = self.path_for(v)
        meta_path = self.meta_path_for(v)
        if not model_path.is_file() or not meta_path.is_file():
            raise FileNotFoundError(
                f"version {v} incomplète : {model_path.exists()=}, {meta_path.exists()=}"
            )
        model = joblib.load(model_path)
        meta = ModelMeta.from_json(meta_path.read_text(encoding="utf-8"))
        return model, meta

    def rollback_to(self, version: int) -> None:
        if version not in self.list_versions():
            raise FileNotFoundError(f"version {version} absente")
        self.set_active(version)
        log.critical("regime_model_rollback to v%d", version)

    # ---- last_regime cache (sérialisation JSON simple) --------------

    def write_last_regime(self, regime_dict: dict, *, cache_path: str | Path) -> None:
        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_text(path, json.dumps(regime_dict, sort_keys=True))

    def read_last_regime(self, *, cache_path: str | Path) -> Optional[dict]:
        path = Path(cache_path)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("last_regime.json corrompu (%s)", exc)
            return None

    # ---- internes ----------------------------------------------------

    def _prune_old_versions(self) -> None:
        vs = self.list_versions()
        if len(vs) <= self.keep_n:
            return
        to_drop = vs[: len(vs) - self.keep_n]
        for v in to_drop:
            for path in (self.path_for(v), self.meta_path_for(v)):
                if path.exists():
                    try:
                        path.unlink()
                    except OSError as exc:
                        log.warning("prune v%d : %s", v, exc)

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


__all__ = ["ModelMeta", "ModelStore"]
