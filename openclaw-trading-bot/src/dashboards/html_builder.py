"""
src.dashboards.html_builder — `HTMLDashboardBuilder` (§14.5.4).

MVP single-file Tailwind CDN. Pas de Chart.js interactif ni de heatmap dans
cette phase — ils peuvent être ajoutés via override du template Jinja2 sans
toucher au builder.

Layout §14.1 (desktop ≥ 1280 px) :
    - En-tête : session, régime, horodatage, kill-switch
    - Bloc 1 : Opportunités (cartes)
    - Bloc 2 : Portefeuille (V1: placeholder — alimenté dès §14.4.1)
    - Bloc 3 : Coûts & APIs (via CostRepository)
    - Bloc 4 : Risque (placeholder)
    - Footer : liens `/costs.json`, `/healthz`

Écriture :
    `data/dashboards/<YYYY-MM-DD>/<session>.html`

Le template est embarqué dans le module (zéro fichier externe pour V1) afin
de rester portable en tests. Il utilise Jinja2 avec `autoescape=True`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, select_autoescape

from src.contracts.cycle import CycleResult
from src.contracts.regime import RegimeState
from src.contracts.strategy import TradeProposal
from src.dashboards.cost_repo import CostPanel, CostRepository

log = logging.getLogger(__name__)


DASHBOARDS_DIR = Path("data/dashboards")


# ---------------------------------------------------------------------------
# Template Jinja2 (MVP — single-file, Tailwind Play CDN)
# ---------------------------------------------------------------------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="fr" class="dark">
<head>
<meta charset="utf-8"/>
<meta http-equiv="refresh" content="300"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Openclaw — {{ session }} — {{ ts_short }}</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">
<header class="border-b border-slate-800 px-6 py-4 flex flex-wrap items-center justify-between gap-3">
  <div class="flex items-center gap-4">
    <div class="font-bold tracking-tight text-lg">openclaw</div>
    <div class="text-slate-400 text-sm">{{ session }} · {{ ts_full }}</div>
  </div>
  <div class="flex items-center gap-3">
    <span class="px-2 py-1 rounded text-xs font-medium
        {% if regime.macro == 'risk_on' %}bg-emerald-900 text-emerald-200
        {% elif regime.macro == 'risk_off' %}bg-rose-900 text-rose-200
        {% else %}bg-amber-900 text-amber-200{% endif %}">
      régime : {{ regime.macro }} · vol {{ regime.volatility }}
    </span>
    <span class="px-2 py-1 rounded text-xs font-medium
        {% if kill_switch_active %}bg-rose-600 text-white{% else %}bg-emerald-700 text-white{% endif %}">
      kill-switch {% if kill_switch_active %}ON{% else %}off{% endif %}
    </span>
    <span class="px-2 py-1 rounded text-xs font-medium
        {% if cycle.status == 'success' %}bg-emerald-800 text-emerald-100
        {% elif cycle.status == 'degraded' %}bg-amber-800 text-amber-100
        {% else %}bg-rose-800 text-rose-100{% endif %}">
      cycle : {{ cycle.status }}
      {% if cycle.degradation_flags %}
        · {{ cycle.degradation_flags | length }} flag{{ 's' if cycle.degradation_flags|length>1 else '' }}
      {% endif %}
    </span>
  </div>
</header>

<main class="grid grid-cols-1 lg:grid-cols-3 gap-6 p-6">
  {# -------- Bloc 1 : Opportunités -------- #}
  <section class="lg:col-span-2 space-y-4">
    <h2 class="text-xl font-semibold">Opportunités <span class="text-slate-400 text-sm font-normal">({{ opportunities|length }})</span></h2>
    {% if opportunities %}
      {% for p in opportunities %}
      <article class="rounded-lg border border-slate-800 bg-slate-900 p-4 space-y-2">
        <div class="flex justify-between items-center">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="px-2 py-0.5 rounded text-xs font-bold
                {% if p.side == 'long' %}bg-emerald-700{% else %}bg-rose-700{% endif %}">
              {{ p.side | upper }}
            </span>
            <span class="font-bold">{{ p.asset }}</span>
            <span class="text-slate-400 text-xs">{{ p.asset_class }}</span>
            <span class="text-slate-400 text-xs">· {{ p.strategy_id }}</span>
          </div>
          <div class="text-slate-500 text-xs">{{ p.proposal_id }}</div>
        </div>
        <div class="font-mono text-sm text-slate-300">
          Entry {{ '%.4f'|format(p.entry_price) }} ·
          Stop {{ '%.4f'|format(p.stop_price) }} ·
          TP {{ p.tp_prices | join(' / ') }} ·
          R/R {{ '%.1f'|format(p.rr) }} ·
          Size {{ '%.1f'|format(p.risk_pct * 100) }} %
        </div>
        <div class="text-sm text-slate-400">Conviction : {{ '%.2f'|format(p.conviction) }}</div>
        <div class="flex gap-2 pt-1">
          <button data-action="validate" data-id="{{ p.proposal_id }}"
            class="bg-emerald-600 hover:bg-emerald-500 px-3 py-1 rounded text-sm font-medium">
            ✓ Valider
          </button>
          <button data-action="reject" data-id="{{ p.proposal_id }}"
            class="bg-slate-700 hover:bg-slate-600 px-3 py-1 rounded text-sm font-medium">
            ✗ Rejeter
          </button>
        </div>
      </article>
      {% endfor %}
    {% else %}
      <div class="rounded-lg border border-slate-800 bg-slate-900 p-6 text-center text-slate-400">
        Aucune opportunité ce cycle — rester en cash est aussi une décision.
      </div>
    {% endif %}
  </section>

  {# -------- Colonne droite (2 & 3 & 4) -------- #}
  <aside class="space-y-6">
    {# -------- Bloc 3 : Coûts -------- #}
    <section class="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 class="text-lg font-semibold mb-3">Coûts &amp; APIs</h2>
      <div class="grid grid-cols-2 gap-3 text-sm">
        <div>
          <div class="text-slate-400 text-xs uppercase">Tokens aujourd'hui</div>
          <div class="font-mono">{{ cost_panel.tokens_today }} / {{ cost_panel.tokens_daily_budget }}</div>
        </div>
        <div>
          <div class="text-slate-400 text-xs uppercase">Coût mois</div>
          <div class="font-mono">$ {{ '%.2f'|format(cost_panel.cost_month_usd) }}
            / $ {{ '%.2f'|format(cost_panel.cost_month_budget_usd) }}</div>
        </div>
        <div class="col-span-2">
          <div class="text-slate-400 text-xs uppercase">Forecast fin de mois</div>
          <div class="font-mono
              {% if cost_panel.forecast_month_usd > cost_panel.cost_month_budget_usd %}text-rose-400{% endif %}">
            $ {{ '%.2f'|format(cost_panel.forecast_month_usd) }}
          </div>
        </div>
      </div>
      {% if cost_panel.by_agent %}
      <table class="w-full text-xs mt-4 border-t border-slate-800 pt-2">
        <thead class="text-slate-400"><tr>
          <th class="text-left py-1">Agent</th>
          <th class="text-right">Calls 24h</th>
          <th class="text-right">Coût 24h</th>
          <th class="text-right">Coût mois</th>
        </tr></thead>
        <tbody>
        {% for r in cost_panel.by_agent %}
          <tr class="border-t border-slate-800">
            <td class="py-1">{{ r.agent }}</td>
            <td class="text-right font-mono">{{ r.calls_24h }}</td>
            <td class="text-right font-mono">$ {{ '%.4f'|format(r.cost_24h_usd) }}</td>
            <td class="text-right font-mono">$ {{ '%.4f'|format(r.cost_month_usd) }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      {% endif %}
      {% if cost_panel.by_api_source %}
      <table class="w-full text-xs mt-4 border-t border-slate-800 pt-2">
        <thead class="text-slate-400"><tr>
          <th class="text-left py-1">Source</th>
          <th class="text-right">Calls</th>
          <th class="text-right">Err%</th>
          <th class="text-right">p95</th>
          <th class="text-center">État</th>
        </tr></thead>
        <tbody>
        {% for s in cost_panel.by_api_source %}
          <tr class="border-t border-slate-800">
            <td class="py-1">{{ s.source }}</td>
            <td class="text-right font-mono">{{ s.calls_24h }}</td>
            <td class="text-right font-mono">{{ '%.1f'|format(s.error_rate_pct) }}</td>
            <td class="text-right font-mono">{{ s.latency_p95_ms }}ms</td>
            <td class="text-center">
              {% if s.state == 'green' %}🟢{% elif s.state == 'amber' %}🟠{% else %}🔴{% endif %}
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      {% endif %}
      {% if cost_panel.alerts %}
      <ul class="mt-4 space-y-1 text-xs border-t border-slate-800 pt-2">
        {% for a in cost_panel.alerts %}
        <li class="
            {% if a.level == 'critical' %}text-rose-400
            {% elif a.level == 'warning' %}text-amber-400
            {% else %}text-slate-400{% endif %}">
          [{{ a.level }}] {{ a.code }} — {{ a.message }}
        </li>
        {% endfor %}
      </ul>
      {% endif %}
    </section>

    {# -------- Bloc 2 : Portefeuille (placeholder V1) -------- #}
    <section class="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 class="text-lg font-semibold mb-2">Portefeuille</h2>
      <div class="text-slate-500 text-sm">Panneau alimenté en Phase 12 (§14.4.1).</div>
    </section>

    {# -------- Bloc 4 : Cycle -------- #}
    <section class="rounded-lg border border-slate-800 bg-slate-900 p-4 text-sm">
      <h2 class="text-lg font-semibold mb-2">Cycle</h2>
      <div class="grid grid-cols-2 gap-1">
        <div class="text-slate-400">proposals</div>
        <div class="font-mono">{{ cycle.proposals }}</div>
        <div class="text-slate-400">rejected</div>
        <div class="font-mono">{{ cycle.proposals_rejected }}</div>
        <div class="text-slate-400">rgfr</div>
        <div class="font-mono">{{ '%.2f'|format(cycle.risk_gate_failure_rate) }}</div>
        <div class="text-slate-400">durée</div>
        <div class="font-mono">{{ '%.2f'|format(cycle.duration_s) }}s</div>
      </div>
      {% if cycle.degradation_flags %}
      <div class="mt-2 flex flex-wrap gap-1">
        {% for f in cycle.degradation_flags %}
          <span class="px-2 py-0.5 rounded bg-amber-900 text-amber-200 text-xs">{{ f }}</span>
        {% endfor %}
      </div>
      {% endif %}
    </section>
  </aside>
</main>

<footer class="px-6 py-3 border-t border-slate-800 text-slate-500 text-xs flex justify-between">
  <div>
    Généré à {{ ts_full }} · lag llm_usage :
    {% if cost_panel.source_data_lag_seconds is none %}n/a
    {% elif cost_panel.source_data_lag_seconds == infinity %}n/a
    {% else %}{{ '%.0f'|format(cost_panel.source_data_lag_seconds / 60) }} min{% endif %}
  </div>
  <div class="space-x-3">
    <a href="/costs.json" class="hover:text-slate-300">/costs.json</a>
    <a href="/healthz" class="hover:text-slate-300">/healthz</a>
  </div>
</footer>

<script>
  // Helper ≤ 40 lignes (§14.5.7)
  document.querySelectorAll('button[data-action]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      const action = btn.dataset.action;
      btn.disabled = true;
      btn.textContent = '…';
      try {
        const r = await fetch('/' + action + '/' + encodeURIComponent(id), {method: 'POST'});
        const ok = r.ok;
        btn.textContent = ok ? '✓ ' + action + 'é' : '✗ erreur';
      } catch (e) {
        btn.textContent = '✗ réseau';
      }
    });
  });
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


@dataclass
class _Opportunity:
    """Vue simplifiée d'un TradeProposal pour le template (évite d'exposer
    toute la structure interne)."""

    proposal_id: str
    asset: str
    asset_class: str
    strategy_id: str
    side: str
    entry_price: float
    stop_price: float
    tp_prices: list[float]
    rr: float
    conviction: float
    risk_pct: float


class HTMLDashboardBuilder:
    """Construit un HTML statique pour un cycle — une page par session."""

    def __init__(
        self,
        *,
        cost_repo: CostRepository,
        dashboards_dir: Path = DASHBOARDS_DIR,
    ) -> None:
        self.cost_repo = cost_repo
        self.dashboards_dir = Path(dashboards_dir)
        self._env = Environment(
            autoescape=select_autoescape(["html", "xml"]),
        )
        # Expose `infinity` au template (pour comparer source_data_lag_seconds)
        self._env.globals["infinity"] = float("inf")
        self._template = self._env.from_string(_TEMPLATE)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def build(
        self,
        *,
        session: str,
        cycle_result: CycleResult,
        regime: RegimeState,
        proposals: list[TradeProposal],
        kill_switch_active: bool = False,
        now: Optional[datetime] = None,
    ) -> Path:
        """Génère le HTML et l'écrit sur disque. Retourne le chemin."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cost_panel = self.cost_repo.build_panel(now=now)
        html = self.render(
            session=session,
            cycle_result=cycle_result,
            regime=regime,
            proposals=proposals,
            cost_panel=cost_panel,
            kill_switch_active=kill_switch_active,
            now=now,
        )
        out_path = (
            self.dashboards_dir
            / now.strftime("%Y-%m-%d")
            / f"{session}.html"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        log.info("dashboard_written", extra={"path": str(out_path)})
        return out_path

    def render(
        self,
        *,
        session: str,
        cycle_result: CycleResult,
        regime: RegimeState,
        proposals: list[TradeProposal],
        cost_panel: CostPanel,
        kill_switch_active: bool,
        now: datetime,
    ) -> str:
        """Rend le HTML sans l'écrire. Utile pour tests / preview."""
        return self._template.render(
            session=session,
            ts_full=now.strftime("%Y-%m-%d %H:%M UTC"),
            ts_short=now.strftime("%H:%M"),
            regime=regime,
            cycle=cycle_result,
            opportunities=[self._opp(p) for p in proposals],
            cost_panel=cost_panel,
            kill_switch_active=kill_switch_active,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _opp(p: TradeProposal) -> _Opportunity:
        return _Opportunity(
            proposal_id=p.proposal_id,
            asset=p.asset,
            asset_class=p.asset_class,
            strategy_id=p.strategy_id,
            side=p.side,
            entry_price=float(p.entry_price),
            stop_price=float(p.stop_price),
            tp_prices=[float(x) for x in p.tp_prices],
            rr=float(p.rr),
            conviction=float(p.conviction),
            risk_pct=float(p.risk_pct),
        )


__all__ = ["HTMLDashboardBuilder", "DASHBOARDS_DIR"]
