"""
src.memory.lesson_index — retrieval sémantique top-K des leçons.

Source : TRADING_BOT_ARCHITECTURE.md §10.4.

Architecture :
- Backend **FAISS + fastembed** (BAAI/bge-small-en-v1.5, ONNX, 384 dim L2-norm)
  comme décrit dans §10.4. Chargement lazy (premier `query()` : ~2 s, appels
  suivants ~5-10 ms).
- Fallback **`_BagOfWordsBackend`** : implémentation pure-stdlib (similarité
  Jaccard sur tokens) activée automatiquement si `fastembed`/`faiss` ne sont
  pas disponibles. Permet de ne jamais bloquer le bot en cold-start et reste
  correct à l'échelle V1 (<500 leçons).
- Persistance : `data/lesson_index.faiss` (binaire) + `data/lesson_index.meta.json`.
  Le fallback BOW n'a pas besoin de fichier : il se rebuild en RAM à chaque
  démarrage (quelques ms).

Document indexé pour une leçon (§10.4) :
    "{tags_space_separated}. {content}"
Requête :
    "{asset} {strategy} {regime_tags} {free_text?}"
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, Sequence


log = logging.getLogger("LessonIndex")


@dataclass(frozen=True)
class LessonHit:
    lesson_id: str
    content: str
    tags: list[str]
    confidence: float
    score: float                          # similarité cosinus ou Jaccard (0..1)


@dataclass
class _LessonDoc:
    lesson_id: str
    content: str
    tags: list[str]
    confidence: float

    def indexable_text(self) -> str:
        return f"{' '.join(self.tags)}. {self.content}".strip()


class _Backend(Protocol):
    name: str

    def build(self, docs: Sequence[_LessonDoc]) -> None: ...
    def query(self, q: str, *, k: int) -> list[tuple[int, float]]: ...


# ---------------------------------------------------------------------------
# Fallback pur stdlib — similarité Jaccard sur tokens normalisés
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")


def _tokenize(s: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(s) if len(t) > 1}


class _BagOfWordsBackend:
    """Sac de mots + similarité Jaccard. Rapide, déterministe, zéro dépendance.

    Convient parfaitement tant que la base de leçons reste <~500 entrées et que
    la langue est majoritairement monolingue. Les seules différences notables
    avec un embedder dense : moins bon sur les synonymes et la paraphrase.
    """

    name = "bow-jaccard"

    def __init__(self) -> None:
        self._tokens: list[set[str]] = []

    def build(self, docs: Sequence[_LessonDoc]) -> None:
        self._tokens = [_tokenize(d.indexable_text()) for d in docs]

    def query(self, q: str, *, k: int) -> list[tuple[int, float]]:
        if not self._tokens:
            return []
        qt = _tokenize(q)
        if not qt:
            return []
        scores: list[tuple[int, float]] = []
        for idx, tks in enumerate(self._tokens):
            if not tks:
                continue
            inter = len(qt & tks)
            if inter == 0:
                continue
            union = len(qt | tks) or 1
            scores.append((idx, inter / union))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]


# ---------------------------------------------------------------------------
# FAISS + fastembed backend (lazy import, activé si les deps sont installées)
# ---------------------------------------------------------------------------


class _FaissBackend:
    """FAISS IndexFlatIP + embeddings fastembed BGE-small. §10.4."""

    name = "faiss-bge-small"

    def __init__(self) -> None:
        self._faiss = None
        self._embedder = None
        self._index = None
        self._dim = 0

    def _lazy_load(self) -> None:
        if self._embedder is not None and self._faiss is not None:
            return
        import faiss  # type: ignore[import-not-found]
        from fastembed import TextEmbedding  # type: ignore[import-not-found]

        self._faiss = faiss
        self._embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        # On récupère la dimension d'un embed de probe pour construire l'index
        probe = list(self._embedder.embed(["dim probe"]))
        self._dim = len(probe[0])

    def _embed(self, texts: Sequence[str]):
        assert self._embedder is not None
        import numpy as np  # type: ignore[import-not-found]

        vecs = list(self._embedder.embed(list(texts)))
        return np.asarray(vecs, dtype="float32")

    def build(self, docs: Sequence[_LessonDoc]) -> None:
        self._lazy_load()
        if not docs:
            self._index = self._faiss.IndexFlatIP(self._dim)  # type: ignore[union-attr]
            return
        vecs = self._embed([d.indexable_text() for d in docs])
        self._index = self._faiss.IndexFlatIP(self._dim)  # type: ignore[union-attr]
        self._index.add(vecs)

    def query(self, q: str, *, k: int) -> list[tuple[int, float]]:
        if self._index is None or self._index.ntotal == 0:
            return []
        self._lazy_load()
        qv = self._embed([q])
        sims, idxs = self._index.search(qv, min(k, self._index.ntotal))
        return [
            (int(idxs[0][i]), float(sims[0][i]))
            for i in range(idxs.shape[1])
            if idxs[0][i] != -1
        ]


def _pick_backend(prefer: str | None = None) -> _Backend:
    """Choisit le backend disponible. `prefer='faiss'|'bow'|None`."""
    if prefer == "bow":
        return _BagOfWordsBackend()
    if prefer in (None, "faiss"):
        # Probe dépendances optionnelles sans pollution de pyflakes.
        from importlib.util import find_spec
        if find_spec("faiss") is not None and find_spec("fastembed") is not None:
            return _FaissBackend()
        if prefer == "faiss":
            raise ImportError("faiss/fastembed requis pour le backend 'faiss'")
        return _BagOfWordsBackend()
    raise ValueError(f"backend inconnu : {prefer!r}")


# ---------------------------------------------------------------------------
# LessonIndex public
# ---------------------------------------------------------------------------


@dataclass
class _IndexMeta:
    docs: list[_LessonDoc] = field(default_factory=list)


class LessonIndex:
    """Index de retrieval lazy, déterministe, re-buildable.

    Usage :
        idx = LessonIndex()
        idx.build_from_rows(lessons_repo.all_active())
        hits = idx.query(asset="EURUSD", regime_tags=["risk_on"], strategy="breakout_momentum")
    """

    def __init__(self, *, backend_preference: str | None = None) -> None:
        self._backend = _pick_backend(backend_preference)
        self._meta = _IndexMeta()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def size(self) -> int:
        return len(self._meta.docs)

    def build_from_rows(self, rows: Iterable[dict]) -> None:
        docs = [_row_to_doc(r) for r in rows]
        self._backend.build(docs)
        self._meta = _IndexMeta(docs=docs)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        asset: str,
        regime_tags: Sequence[str],
        strategy: str,
        free_text: str | None = None,
        k: int = 6,
    ) -> list[LessonHit]:
        if self.size == 0:
            return []
        q = f"{asset} {strategy} {' '.join(regime_tags)}"
        if free_text:
            q = f"{q} {free_text}"
        ranked = self._backend.query(q, k=k)
        hits: list[LessonHit] = []
        for idx, score in ranked:
            if idx < 0 or idx >= self.size:
                continue
            d = self._meta.docs[idx]
            hits.append(LessonHit(
                lesson_id=d.lesson_id,
                content=d.content,
                tags=list(d.tags),
                confidence=d.confidence,
                score=float(score),
            ))
        return hits

    # ------------------------------------------------------------------
    # Persistance optionnelle du meta (utile pour `memory-consolidate`)
    # ------------------------------------------------------------------

    def save_meta(self, path: str | Path) -> None:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "backend": self._backend.name,
            "docs": [d.__dict__ for d in self._meta.docs],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def load_meta(self, path: str | Path) -> bool:
        """Charge les docs depuis disque et reconstruit l'index en RAM.

        Renvoie `True` si le fichier existe et a été chargé avec succès, sinon
        `False` (le caller peut alors faire un `build_from_rows` pour rebuild).
        """
        p = Path(path)
        if not p.exists():
            return False
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            docs = [_LessonDoc(**d) for d in payload.get("docs", [])]
        except (json.JSONDecodeError, TypeError, KeyError):
            return False
        self._backend.build(docs)
        self._meta = _IndexMeta(docs=docs)
        return True


def _row_to_doc(row: dict) -> _LessonDoc:
    tags_raw = row.get("tags")
    tags: list[str]
    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw]
    elif isinstance(tags_raw, str) and tags_raw:
        try:
            parsed = json.loads(tags_raw)
            tags = [str(t) for t in parsed] if isinstance(parsed, list) else [tags_raw]
        except json.JSONDecodeError:
            tags = [tags_raw]
    else:
        tags = []
    return _LessonDoc(
        lesson_id=str(row.get("id") or row.get("lesson_id") or ""),
        content=str(row.get("content") or ""),
        tags=tags,
        confidence=float(row.get("confidence") or 1.0),
    )


__all__ = ["LessonIndex", "LessonHit"]
