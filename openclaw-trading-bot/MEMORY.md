# MEMORY.md — Façade humaine de la mémoire SQLite

> **Ce fichier n'est pas la source de vérité.** La mémoire réelle vit dans
> `data/memory.db` (SQLite) — tables `trades`, `lessons`, `hypotheses`,
> `observations`, `regime_snapshots`, `performance_metrics`, `llm_usage`,
> etc. Voir [§10.3 du doc d'archi](TRADING_BOT_ARCHITECTURE.md#103-schéma-sqlite).
>
> Ce `.md` est **régénéré automatiquement** chaque nuit à 02:00 UTC par le
> skill [`memory-consolidate`](skills/memory-consolidate/SKILL.md) à partir
> des données SQLite. Ne pas éditer à la main — toute édition sera
> écrasée au prochain passage.
>
> Objectif : donner à l'opérateur humain une vue condensée et lisible de
> la mémoire, sans avoir à interroger SQLite. Les agents ne lisent **pas**
> ce fichier — ils lisent directement la base.

---

## 0. Index

Voir [MEMORY_INDEX.md](MEMORY_INDEX.md) pour la table des IDs
(`H-xxx` hypothèses, `L-xxxx` leçons, `T-xxxxxx` trades, `B-xx` biais,
`TD-xx` TODOs) et leur emplacement.

---

## 1. Règles invariantes

Statut actuel (réplique de §2.5 + §11 du doc d'archi — ne pas modifier
ici ; modifier la spec puis régénérer) :

- `BOT_MODE=simulation` en V1. Bascule live interdite sans ADR signée.
- Kill-switch : présence de `data/KILL` → risk-gate rejette tout (§11.1).
- Fail-closed sur donnée manquante ou régime indéterminé.
- `risk-gate` est l'unique chemin vers une proposition d'ordre (§2.5).
- Jamais de secret (clé, token, adresse) ici ni dans les logs.
- `trades`, `lessons`, `hypotheses`, `observations` = append-only côté SQL.
- Max 3 stratégies actives simultanément (§6) ; max 60 % d'allocation par
  stratégie.

---

## 2. Régime de marché courant

_Régénéré depuis `regime_snapshots` (dernière ligne)._

- **Dernière mise à jour** : cold-start
- **Macro** : indéterminé
- **Volatilité** : indéterminée
- **Confidence HMM** : — (cold-start, seuil d'activation 50 ticks)
- **Benchmarks** : SPX —, BTC —, DXY —, VIX —

---

## 3. Hypothèses actives

_Régénéré depuis `hypotheses` WHERE `status IN ('testing','pending')`._

| ID | Énoncé | Statut | Score bayésien | Trades testés | Depuis |
|---|---|---|---|---|---|

_Aucune hypothèse — `self-improve` en produira à partir du dimanche 22:00
UTC suivant les 50 premiers trades paper (§13.2)._

---

## 4. Leçons apprises (top 20 récentes)

_Régénéré depuis `lessons` ORDER BY `ts DESC LIMIT 20`._

| ID | Date | Tag | Énoncé | Trades refs |
|---|---|---|---|---|
| L-0000 | cold-start | `system` | Initialisation du bot. Aucune leçon préalable — les premières viendront après 20 trades paper. | — |

Archivage : les leçons > 180 jours sont déplacées dans `lessons_archive`
par `memory-consolidate` (§10.6).

---

## 5. Trades récents (top 20)

_Régénéré depuis `trades` ORDER BY `ts_entry DESC LIMIT 20`._

| ID | Entry | Asset | Side | Strat | PnL | Commentaire |
|---|---|---|---|---|---|---|

_Aucun trade enregistré._

---

## 6. Performance des stratégies (30 jours glissants)

_Régénéré depuis `performance_metrics` WHERE `window='30d'`._

Catalogue source de vérité : `config/strategies.yaml` + §6 du doc d'archi.

| Stratégie | Trades 30j | Winrate | PF | Sharpe | Max DD | Statut |
|---|---|---|---|---|---|---|
| ichimoku_trend_following | 0 | — | — | — | — | enabled |
| breakout_momentum | 0 | — | — | — | — | enabled |
| mean_reversion | 0 | — | — | — | — | enabled |
| divergence_hunter | 0 | — | — | — | — | disabled (cold-start) |
| volume_profile_scalp | 0 | — | — | — | — | disabled (cold-start) |
| event_driven_macro | 0 | — | — | — | — | enabled |
| news_driven_momentum | 0 | — | — | — | — | enabled |

---

## 7. Biais personnels détectés

_Régénéré depuis `observations` WHERE `tag='bias' AND ts > now()-30d`._

_Liste vide au démarrage. `self-improve` enrichira cette section en
analysant les écarts entre décision LLM et décision déterministe
(règle d'or §2.1)._

---

## 8. TODO auto-améliorations

_Régénéré depuis `observations` WHERE `tag='todo' AND status='open'`._

- [ ] Valider fonctionnement du pipeline `full_analysis` sur la première
      session réelle
- [ ] Accumuler 50 trades paper avant de solliciter `self-improve` (§13.2
      exige ≥ 50 pour significativité statistique)
- [ ] Rédiger la première ADR après 30 jours (choix des stratégies
      initiales maintenues vs désactivées)
- [ ] Vérifier qualité des feeds news (latence, doublons, impact=high
      taux de faux positifs)

---

## 9. Journal méta (évolutions de la spec mémoire)

_Régénéré depuis `meta_events` WHERE `type='schema_change'`._

- _À remplir au premier passage de `memory-consolidate`._
