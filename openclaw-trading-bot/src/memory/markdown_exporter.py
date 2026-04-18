"""
src.memory.markdown_exporter — régénère `MEMORY.md` depuis SQLite.

Source : TRADING_BOT_ARCHITECTURE.md §10.3.

Note importante (§10.3) : `MEMORY.md` n'est **plus** le contexte LLM — c'est
une façade humaine (diffable Git, ouvrable dans un éditeur). Le contexte LLM
est assemblé dynamiquement par `PromptBuilder` (§10.5) qui lit SQLite
directement avec retrieval FAISS sélectif.

Plafonds :
- 30 leçons récentes non archivées
- 15 hypothèses actives
- 20 trades ouverts
Au-delà, `memory-consolidate` (§10.6) fusionne et archive.

Format : Markdown simple, sections clairement délimitées pour que les diffs Git
mettent en évidence ce qui change d'un export à l'autre.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


MEMORY_MD_PATH = "data/MEMORY.md"


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def _fmt_float(v: object, digits: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_pct(v: object, digits: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return str(v)


def _parse_json(s: Optional[str]) -> object:
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s


@dataclass
class ExporterConfig:
    max_lessons: int = 30
    max_hypotheses: int = 15
    max_trades_open: int = 20


class MarkdownExporter:
    """Génère `MEMORY.md` depuis SQLite. Méthode unique : `export_to_file`."""

    def __init__(self, config: ExporterConfig | None = None) -> None:
        self.cfg = config or ExporterConfig()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def render(self, con: sqlite3.Connection) -> str:
        regime = self._latest_regime(con)
        lessons = self._recent_lessons(con, self.cfg.max_lessons)
        hypotheses = self._active_hypotheses(con, self.cfg.max_hypotheses)
        perfs = self._performance_table(con)
        open_trades = self._open_trades(con, self.cfg.max_trades_open)

        parts = [
            "# MEMORY.md",
            "",
            f"_Généré automatiquement le {_utc_now_iso()} — ne pas éditer à la main._",
            "",
            "## Régime courant",
            self._render_regime(regime),
            "",
            "## Trades ouverts",
            self._render_open_trades(open_trades),
            "",
            "## Performance par stratégie",
            self._render_perfs(perfs),
            "",
            "## Hypothèses actives",
            self._render_hypotheses(hypotheses),
            "",
            "## Leçons récentes",
            self._render_lessons(lessons),
            "",
        ]
        return "\n".join(parts)

    def export_to_file(
        self,
        con: sqlite3.Connection,
        path: str | Path = MEMORY_MD_PATH,
    ) -> Path:
        content = self.render(con)
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Écriture atomique : tmp + replace (évite un fichier partiel si crash)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(out)
        return out

    # ------------------------------------------------------------------
    # SQL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _rows_as_dicts(cur: sqlite3.Cursor) -> list[dict]:
        rows = cur.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    def _latest_regime(self, con: sqlite3.Connection) -> Optional[dict]:
        cur = con.execute(
            "SELECT * FROM regime_snapshots ORDER BY date DESC LIMIT 1"
        )
        rows = self._rows_as_dicts(cur)
        return rows[0] if rows else None

    def _recent_lessons(self, con: sqlite3.Connection, limit: int) -> list[dict]:
        cur = con.execute(
            """
            SELECT id, date, content, trade_ref, tags, confidence
              FROM lessons
             WHERE archived = 0
             ORDER BY date DESC, created_at DESC
             LIMIT ?
            """,
            (limit,),
        )
        return self._rows_as_dicts(cur)

    def _active_hypotheses(self, con: sqlite3.Connection, limit: int) -> list[dict]:
        cur = con.execute(
            """
            SELECT id, content, status, bayesian_score, started_at, last_updated
              FROM hypotheses
             WHERE archived = 0 AND status IN ('testing', 'confirmed')
             ORDER BY last_updated DESC, started_at DESC
             LIMIT ?
            """,
            (limit,),
        )
        return self._rows_as_dicts(cur)

    def _performance_table(self, con: sqlite3.Connection) -> list[dict]:
        cur = con.execute(
            """
            SELECT pm.strategy, pm.date, pm.trades_30d, pm.winrate_30d,
                   pm.profit_factor, pm.sharpe_30d, pm.max_drawdown, pm.active
              FROM performance_metrics pm
              JOIN (
                   SELECT strategy, MAX(date) AS max_date
                     FROM performance_metrics GROUP BY strategy
              ) last ON last.strategy = pm.strategy AND last.max_date = pm.date
             ORDER BY pm.strategy ASC
            """
        )
        return self._rows_as_dicts(cur)

    def _open_trades(self, con: sqlite3.Connection, limit: int) -> list[dict]:
        cur = con.execute(
            """
            SELECT id, asset, strategy, side, entry_price, entry_time,
                   stop_price, tp_prices, size_pct_equity, rr_estimated
              FROM trades
             WHERE status = 'open'
             ORDER BY entry_time DESC
             LIMIT ?
            """,
            (limit,),
        )
        return self._rows_as_dicts(cur)

    # ------------------------------------------------------------------
    # Rendering helpers — pur Markdown, pas de HTML, pas d'emoji
    # ------------------------------------------------------------------

    @staticmethod
    def _render_regime(r: Optional[dict]) -> str:
        if not r:
            return "_Aucun snapshot de régime enregistré._"
        return (
            f"- Date : **{r['date']}**  \n"
            f"- Macro : `{r['macro']}` — Volatilité : `{r['volatility']}`  \n"
            f"- Trends : equity=`{r.get('trend_equity') or '—'}`, "
            f"forex=`{r.get('trend_forex') or '—'}`, "
            f"crypto=`{r.get('trend_crypto') or '—'}`  \n"
            f"- HMM probas : risk_off={_fmt_pct(r.get('prob_risk_off'))}, "
            f"transition={_fmt_pct(r.get('prob_transition'))}, "
            f"risk_on={_fmt_pct(r.get('prob_risk_on'))}  \n"
            f"- Etat HMM : `{r.get('hmm_state')}`"
        )

    @staticmethod
    def _render_open_trades(trades: list[dict]) -> str:
        if not trades:
            return "_Aucun trade ouvert._"
        header = (
            "| ID | Actif | Strat | Side | Entry | Stop | TPs | Risk % | RR |\n"
            "|---|---|---|---|---|---|---|---|---|"
        )
        rows = []
        for t in trades:
            tps = _parse_json(t.get("tp_prices")) or []
            tps_str = ", ".join(f"{x:.4f}" for x in tps) if isinstance(tps, list) else "—"
            rows.append(
                f"| `{t['id']}` | {t['asset']} | {t['strategy']} | {t['side']} | "
                f"{_fmt_float(t['entry_price'], 4)} | {_fmt_float(t['stop_price'], 4)} | "
                f"{tps_str} | {_fmt_pct(t['size_pct_equity'])} | "
                f"{_fmt_float(t.get('rr_estimated'))} |"
            )
        return "\n".join([header, *rows])

    @staticmethod
    def _render_perfs(perfs: list[dict]) -> str:
        if not perfs:
            return "_Pas encore de métriques de performance._"
        header = (
            "| Stratégie | Date | Trades (30j) | Winrate (30j) | Profit Factor | Sharpe 30j | Max DD | Active |\n"
            "|---|---|---|---|---|---|---|---|"
        )
        rows = []
        for p in perfs:
            rows.append(
                f"| {p['strategy']} | {p['date']} | {p.get('trades_30d') or '—'} | "
                f"{_fmt_pct(p.get('winrate_30d'))} | {_fmt_float(p.get('profit_factor'))} | "
                f"{_fmt_float(p.get('sharpe_30d'))} | {_fmt_pct(p.get('max_drawdown'))} | "
                f"{'oui' if p.get('active') else 'non'} |"
            )
        return "\n".join([header, *rows])

    @staticmethod
    def _render_hypotheses(hypotheses: list[dict]) -> str:
        if not hypotheses:
            return "_Pas d'hypothèses actives._"
        lines = []
        for h in hypotheses:
            lines.append(
                f"- **{h['id']}** (`{h['status']}`, score {_fmt_float(h.get('bayesian_score'))}): "
                f"{h['content']}"
            )
        return "\n".join(lines)

    @staticmethod
    def _render_lessons(lessons: list[dict]) -> str:
        if not lessons:
            return "_Pas encore de leçons enregistrées._"
        lines = []
        for ls in lessons:
            tags = _parse_json(ls.get("tags")) or []
            tags_str = " ".join(f"`#{t}`" for t in tags) if isinstance(tags, list) else ""
            trade_ref = f" (trade {ls['trade_ref']})" if ls.get("trade_ref") else ""
            lines.append(
                f"- **{ls['id']}** — {ls['date']}{trade_ref} "
                f"conf={_fmt_float(ls.get('confidence'))}  \n"
                f"  {ls['content']}  \n"
                f"  {tags_str}".rstrip()
            )
        return "\n".join(lines)


__all__ = ["MarkdownExporter", "ExporterConfig", "MEMORY_MD_PATH"]
