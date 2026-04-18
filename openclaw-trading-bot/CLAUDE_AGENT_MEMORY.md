# CLAUDE_AGENT_MEMORY.md — Contexte agent Claude (openclaw-trading-bot)

> Mémoire persistante de l'agent Claude (moi) pour ce projet.
> **Distinct** de `MEMORY.md` qui est la mémoire du bot lui-même, régénérée chaque nuit par `memory-consolidate` (§13.1).
> Objet : me permettre de reprendre le contexte instantanément après une compaction ou un cold-start de session.

---

## 1. Identité du projet

- **Nom** : `openclaw-trading-bot`
- **Langue** : français (architecture, code comments, docs)
- **Utilisateur** : Say (`contact.ysilo@gmail.com`)
- **Stade** : MVP, architecture figée à 6477 lignes, **prête à implémenter**
- **Document central** : `TRADING_BOT_ARCHITECTURE.md`

## 2. Objectif produit

- Simulateur de trading (paper-trading, **pas d'exécution réelle en V1**)
- Multi-assets : **Euronext Paris (focus V1, dont Rubis RUI.PA)**, Forex, Crypto
- **Pas de cycles actions/forex le weekend** (règle dure §15.1)
- Humain unique dans la boucle : validation via dashboard + Telegram

## 3. Règles d'or (§2 — jamais enfreindre)

1. **Déterminisme d'abord** : tout ce qui peut être Python pur l'est. Le LLM ne fait QUE news analysis, self-improve diagnostic, memory consolidation.
2. **Fail-closed sur le risque** : pas de proposition si risk gate KO ni si toutes les sources data KO.
3. **Logs d'erreurs actionnables** : taxonomie `CFG/NET/DATA/LLM/RISK/RUN` (§7.5), code explicite par condition.

## 3b. Outillage Python (§17.5.1)

- **Package manager** : `uv` (Astral) — remplace pip + venv + pip-tools
- `pyproject.toml` = source de vérité, `uv.lock` = lockfile engagée, `requirements.txt` = artefact `uv export`
- Commandes : `uv sync` / `uv run pytest` / `uv add <pkg>` / `uv lock --check`
- Dockerfile builder stage utilise `uv sync --frozen --no-dev`
- Ne **jamais** utiliser `pip install` dans les scripts ou le Dockerfile

## 4. Stack technique MVP (100 % gratuit)

| Domaine | Primaire | Fallback | Note |
|---|---|---|---|
| Equity (Euronext) | **Stooq CSV** (`rui.fr`) | **Boursorama scrape** (`1rPRUI`) | mapping dans `ticker_map.py` |
| Forex | OANDA v20 (**optionnel**) | `exchangerate_host` | OANDA non-requis pour démarrer |
| Crypto | ccxt (Binance/Kraken) | autre exchange ccxt | 24/7 |
| Macro HMM | **FRED** (SP500, VIXCLS, DTWEXBGS, DGS10) | Stooq | CoinGecko pour BTC |
| News | RSS multi-sources | — | scoring LLM sonnet-4-6 |
| LLM | `claude-sonnet-4-6` (cycles) | `claude-opus-4-7` (self-improve) | prompt caching 5-layer |

**Refusé** : Alpaca, yfinance (pas de couverture Euronext), EODHD (payant).

## 5. Arborescence clé

```
src/
  ├── orchestrator/run.py
  ├── regime/hmm_detector.py
  ├── signals/signal_crossing.py
  ├── strategies/                  # 7 stratégies, build_proposal() chacune
  ├── news/
  ├── risk/                        # 10 contrôles C1-C10
  ├── simulator/
  ├── dashboards/                  # HTML + FastAPI + CostRepository
  ├── self_improve/                # validator + rollback
  └── utils/{ticker_map,error_codes,health_checks}.py
config/{risk,assets,strategies}.yaml   # schémas complets §3.1
data/
  ├── models/regime_hmm_v{n}.pkl   # versioning HMM
  ├── cache/last_regime.json       # fallback cold-start
  ├── queue/pending_*.jsonl        # backpressure offline
  └── patches/                     # self-improve history
```

## 6. 7 stratégies (§6)

`ichimoku_trend_following` (défaut conservateur) · `breakout_momentum` · `mean_reversion` · `divergence_hunter` · `volume_profile_scalp` · `event_driven_macro` · `news_driven_momentum`

**Pivot central** : Ichimoku Kinko Hyo + 5 familles d'indicateurs (tendance, momentum, volume, volatilité, macro).

## 7. 10 contrôles risk gate (§11.6)

C1 kill_switch · C2 daily_loss · C3 max_open_positions · C4 exposure_per_class · C5 circuit_breaker · C6 ichimoku_alignment · C7 token_budget · C8 correlation_cap · C9 macro_volatility · C10 data_quality

## 8. HMM régime (§12.2)

- **3 états** : `risk_on` / `transition` / `risk_off`
- **5 features** : `spx_return`, `vix`, `dxy_change`, `yield_10y_change`, `crypto_vol` (std log-returns 20j BTC)
- **Training** : 5 ans daily, min 3 ans pour bootstrap
- **Re-train mensuel** (1er du mois 02:00 UTC) + Telegram `/regime retrain`
- **Versioning** : `regime_hmm_v{n}.pkl` + `.meta.json`, rollback auto si accuracy -5pts
- **SPX/VIX/DXY/yields = features macro uniquement, PAS tradables en V1**

## 9. Contrats Pydantic (§8.8.1) — frontière skills

`Candidate` · `SelectionOutput` · `SignalOutput` (**diagnostic, PAS une proposition**) · `IchimokuPayload` · `NewsItem` · `NewsPulse` · `RiskCheckResult` · `RiskDecision` · `BacktestReport`

**Invariant** : `SignalOutput ≠ TradeProposal`. Seul `src/strategies/<id>.build_proposal(...)` crée un `TradeProposal` (déterministe, 0 token).

## 10. Orchestrateur résilience (§8.7.1)

Chaque étape : timeout + retry + fallback explicites. Points clés :
- `regime_detector` KO → `last_regime.json` (mode dégradé)
- `news_agent` KO → `NewsPulse.empty()`, pas d'injection
- `risk_manager` KO → **fail-closed**, proposal rejetée
- `CycleResult.degradation_flags` exposé dashboard + Telegram
- `len(degradation_flags) >= 3` OU `risk_gate_failure_rate > 50%` → circuit breaker C5

**Injection news** : seuil `impact_score >= 0.60`, cap 3/cycle, dé-doublonnage avec upgrade de score.

## 11. Self-improve (§13) — Dimanche 22:00 UTC

- **Max 1 patch mergé / semaine**
- **Blacklist** : `src/risk/`, `kill_switch.py`, `config/risk.yaml`, contrat `RiskDecision` → jamais auto-modifiables
- **Critères validation** : t-stat > 2.0 · Sharpe > baseline · DSR > 0.95 · trades ≥ 50 · DD ≤ baseline × 1.10
- Validation Telegram `/approve` (admin unique, expire 7j)
- **Canary 14j** paper-trading : KS-test, delta Sharpe ≤ 30%, 0 exception runtime
- Rollback auto si critères canary échouent → `git revert` + notification

## 12. Dashboard & Telegram (§14)

- HTML single-file/cycle, Tailwind CDN + Chart.js + Heroicons inline
- FastAPI : `POST /validate/{id}`, `POST /reject/{id}`, `GET /healthz`, `GET /costs.json`
- `CostRepository` (§14.5.5) : 7 requêtes SQL canoniques, pull-only, `computed_at` + `source_data_lag_seconds`
- Badge "stale" si lag > 3600s

## 13. Schedule (§15.1)

| Cycle | Cadence | Fenêtre |
|---|---|---|
| Equity / Forex | 2×/jour | **Lun-Ven uniquement** |
| Crypto | 4×/jour (00/06/12/18 UTC) | 24/7 |
| NewsWatcher | continu (ad-hoc) | cooldown + max_adhoc_per_day |
| Memory consolidate | 23:30 UTC | daily |
| Self-improve | Dim 22:00 UTC | weekly |
| HMM retrain | 1er du mois 02:00 UTC | monthly |

## 14. État d'avancement

- Architecture : 6477 lignes, **8 fixes de readiness appliqués** (contrats Pydantic, schémas YAML, API parsing, HMM détaillé, orchestrateur résilient, self-improve rollback, requêtes CostRepository)
- Tous les audits de cohérence sont passés
- Prochaine étape logique (si demandée) : **début de l'implémentation** (probablement depuis `utils/error_codes.py`, `config/*.yaml`, puis `risk/` en premier)

## 15. Préférences utilisateur observées

- Veut des fixes **courts et actionnables**, pas de réécriture massive
- Rejette les solutions payantes au stade MVP
- Demande **vérification systématique des incohérences** après chaque changement
- Utilise `/fix` laconique en comptant sur le contexte conservé
- Préfère que je détaille pièges/invariants plutôt que le happy path

## 16. Pièges connus (ne jamais faire)

- Proposer Alpaca ou yfinance (pas de couverture Euronext)
- Lancer des cycles equity/forex le weekend
- Laisser un skill LLM créer un `TradeProposal` (déterministe only)
- Écrire `log.info if x else log.critical)(...)` (parenthèses manquantes)
- Rendre OANDA obligatoire au démarrage (reste optionnel §7.1)
- Traiter SPX/VIX comme actifs tradables (features HMM uniquement)
- Écraser `MEMORY.md` qui est la mémoire du bot (ce fichier-ci est pour moi)

## 17. Alias utiles (si l'utilisateur écrit laconiquement)

- "fix" / "Fix" → appliquer les corrections identifiées dans le dernier audit
- "vérif" / "vérification" → Explore agent sur cohérence + cross-refs
- "archi" → `TRADING_BOT_ARCHITECTURE.md`
- "MVP" → stack gratuit, rien de payant

---

*Dernière mise à jour : 2026-04-17. Généré à la demande de Say après reprise de session post-compaction et application des 8 fixes de readiness.*
