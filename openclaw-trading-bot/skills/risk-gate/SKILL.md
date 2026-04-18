---
name: risk-gate
description: |
  Passage obligé avant toute proposition d'ordre. Vérifie kill-switch,
  drawdown jour, exposition totale et par classe, **alignement Ichimoku**
  (règle d'or §2.5 — check #6, waiver par stratégie), corrélation
  portefeuille, R/R minimal, fenêtres d'évitement macro (§11.2), budget
  LLM/API mensuel. Retourne `approve`, `reduce_size`, ou `reject` avec un
  motif explicite. 100 % déterministe, 0 token. Sources de vérité :
  `config/risk.yaml` + `config/strategies.yaml` (flag
  `requires_ichimoku_alignment`).

  DÉCLENCHE CE SKILL quand l'utilisateur demande s'il peut prendre un trade,
  ouvrir une position, valider une proposition, ou vérifier un "risk check".
  Active aussi sur les formules : "puis-je trader", "est-ce safe", "feu vert
  pour BTC", "avant d'acheter". Chaque `signal-crossing` → proposition DOIT
  passer par ce skill avant émission.

triggers:
  - "avant d'acheter"
  - "puis-je trader"
  - "risk check"
  - "feu vert"
  - "est-ce safe"
  - sortie `signal-crossing` classée long/short (automatique dans le pipeline)

allowed_tools:
  - read

spec_refs:
  - "§11 — Risk Management (chapitre entier)"
  - "§11.1 — Kill-switch"
  - "§11.2 — Fenêtres d'évitement"
  - "§2.3 — Budget LLM & APIs"
  - "§2.1 — Règle d'or #1 : Python décide, LLM propose"
  - "§2.5 — Règle d'or #2 : Ichimoku aligné + risk-gate OK en sortie"

budget:
  tokens_per_run: 0       # 100 % Python
  wallclock_target_ms: 50

code_paths:
  - src/risk/risk_gate.py        # orchestration des 10 contrôles
  - src/risk/budget_gate.py      # sous-contrôle budget LLM/API
  - src/risk/correlation.py      # calcul corrélation fenêtre glissante
  - src/risk/ichimoku_gate.py    # check alignement Ichimoku (§2.5 règle d'or)
  - config/risk.yaml             # seuils (source de vérité)
  - config/strategies.yaml       # waiver requires_ichimoku_alignment
---

# risk-gate

## Pourquoi ce skill existe

C'est le **dernier garde-fou déterministe** avant qu'une proposition soit
émise vers l'opérateur. Ni le LLM, ni les stratégies, ni le signal-crossing
ne peuvent le bypasser — c'est la règle d'or §2.1 (« Python décide, LLM
propose ») combinée à §2.5 (contrat de sortie Ichimoku + risk-gate).
Si le risk-gate reject, aucune trace utilisateur n'est laissée
(silence radio), sauf si un `reject` déclenche une alerte Telegram de
sécurité (KILL présent, DD journalier dépassé).

## Contrôles (ordre fixe, fail-fast)

Chaque contrôle est évalué dans cet ordre. Le premier qui échoue arrête le
pipeline et fixe la sortie.

### 1. Kill-switch (§11.1)
```python
Path(os.environ["KILL_FILE_PATH"]).exists()   # défaut: /app/data/KILL
```
Si présent → `reject` + alerte Telegram `SEV:CRIT`. Opérateur doit retirer
manuellement le fichier pour réactiver.

### 2. Drawdown jour
`equity_drop_today_pct > config.max_daily_loss_pct_equity` (actuellement 2.0)
→ `reject` + alerte Telegram `SEV:WARN`. Le bot reste passif jusqu'au
lendemain 00:00 UTC.

### 3. Fenêtres d'évitement (§11.2)
Source : `config.avoid_windows`. Valeurs actuelles — **ne pas les dupliquer
ici**, toujours lire `risk.yaml` :

| name | offset_min |
|---|---|
| fomc | [-45, 60] |
| cpi_us | [-30, 45] |
| nfp | [-30, 45] |
| ecb_meeting | [-30, 45] |
| weekly_close_crypto_sunday | [-60, 60] |

Si une fenêtre active concerne la classe de l'actif proposé → `reject`.

### 4. Nombre de positions ouvertes
`len(open_positions) >= config.max_open_positions` (actuellement 8) → `reject`.

### 5. Exposition par classe
`exposure_class[class] + proposed_size > config.max_exposure_per_asset_class_pct`
→ `reject` (actuellement 40 % par classe).

### 6. Alignement Ichimoku (§2.5 — règle d'or #2)

Implémente littéralement la règle d'or §2.5 « une proposition ne sort du
pipeline que si le risk-gate passe ET Ichimoku est cohérent avec la
direction proposée ». Avant ce check, la règle n'était qu'un énoncé — elle
est désormais exécutée.

```python
strategy_cfg = load("strategies").strategies[proposal.strategy_id]
requires = strategy_cfg.get(
    "requires_ichimoku_alignment",
    load("strategies").defaults["requires_ichimoku_alignment"],  # défaut true
)

if not requires:
    return Check(name="ichimoku_alignment", ok=True, waived=True,
                 reason=f"waiver strategies.yaml[{proposal.strategy_id}]")

ich = proposal.ichimoku  # copié depuis signal-crossing (§8.9)
aligned = (
    ich["aligned_long"]  if proposal.side == "long"
    else ich["aligned_short"]
)
if not aligned:
    return Check(name="ichimoku_alignment", ok=False,
                 reason=f"ichimoku contrarien (side={proposal.side}, "
                        f"price_above_kumo={ich['price_above_kumo']})")
```

**Waiver explicite via `config/strategies.yaml`** :
- Par défaut : `requires_ichimoku_alignment: true` (bloc `defaults:`)
- Stratégies contrariennes (mean_reversion, divergence_hunter) et scalp
  court-horizon (volume_profile_scalp) : `false` déclaré explicitement
  avec commentaire justificatif. Pas de waiver silencieux.
- Trend-following, breakout, event-driven, news-driven : `true` explicite
  (redondant avec le défaut mais rend la politique auditable).

**Règles d'alignement** (copiées depuis `signal.ichimoku`, pas recalculées
ici — cf. §5.1 pour la définition) :
- `aligned_long = price_above_kumo AND tenkan_above_kijun AND chikou_above_price_26`
- `aligned_short = price_below_kumo AND tenkan_below_kijun AND chikou_below_price_26`

Un `reject` ici référence le `strategy_id` + la direction + l'état Ichimoku,
pour que `self-improve` puisse détecter une stratégie structurellement
désalignée (signe d'un bug de setup ou d'une heuristique qui mérite waiver).

### 7. R/R minimal
`proposal.rr < config.min_rr` (actuellement 1.8) → `reject`.

### 8. Corrélation portefeuille
Si `max_correlation(new_asset, open_positions) > config.max_correlation_threshold`
(actuellement 0.7 sur fenêtre 30 j) :
- Si `correlated_open_count >= config.max_correlated_positions` (3) → `reject`
- Sinon → `reduce_size` (× 0.5) avec raison `correlation_high_but_under_cap`

### 9. Taille trade individuel
`proposal.risk_pct > config.max_risk_per_trade_pct_equity` (actuellement 0.75)
→ `reduce_size` à la taille max autorisée.

### 10. Budget LLM / API (§2.3)
- `monthly_cost_usd > config.llm.max_monthly_cost_usd * 0.95` → `reject`
  (quasi-plafond atteint, on arrête les nouvelles propositions pour garder
  la marge à la maintenance mémoire et self-improve)
- `daily_tokens > config.llm.max_daily_tokens * 0.90` → `reduce_size` (on
  limite la taille pour éviter de claquer le budget en fin de journée)

### 11. Approve
Si toutes les barrières sont franchies → `approve` avec
`final_size_pct_equity` (taille originale éventuellement réduite).

## Contrat de sortie

```json
{
  "action": "approve",
  "reason": "ok — 10 checks passed",
  "final_size_pct_equity": 0.75,
  "checks": [
    {"name": "kill_switch", "ok": true},
    {"name": "daily_drawdown", "ok": true, "equity_drop_today_pct": 0.3},
    {"name": "avoid_windows", "ok": true, "active": []},
    {"name": "open_positions", "ok": true, "count": 5, "max": 8},
    {"name": "class_exposure", "ok": true, "class": "crypto", "used_pct": 18, "max_pct": 40},
    {"name": "ichimoku_alignment", "ok": true, "side": "long", "aligned": true, "waived": false},
    {"name": "rr", "ok": true, "rr": 2.4, "min": 1.8},
    {"name": "correlation", "ok": true, "max_corr": 0.42, "threshold": 0.7},
    {"name": "trade_size", "ok": true, "requested": 0.75, "cap": 0.75},
    {"name": "budget", "ok": true, "daily_tokens_used_pct": 34, "monthly_cost_pct": 52}
  ]
}
```

En cas de `reject` ou `reduce_size`, le `reason` référence le check qui a
échoué et la valeur précise. Le dashboard (§14.2 cartes opportunités) affiche
la chaîne de checks pour audit humain.

## Règles

- **Jamais de valeur hardcodée ici.** Tous les seuils viennent de
  `config/risk.yaml`. Modifier un seuil = éditer risk.yaml + ADR pour tout
  seuil majeur (max_daily_loss_pct_equity, max_risk_per_trade_pct_equity,
  max_correlation_threshold, min_rr).
- **Ordre des contrôles = fail-fast, pas optimisation.** Kill-switch en
  premier car c'est le signal humain d'urgence. DD en 2 car c'est le critère
  objectif le plus grave. Budget en dernier car c'est une contrainte
  économique, pas de sécurité.
- **Pas de LLM.** Jamais. Même pour formater la `reason` — c'est un
  template f-string.

## Traçabilité SQLite

| Table | Écriture | Usage aval |
|---|---|---|
| `risk_decisions` | 1 ligne / proposal évaluée | dashboard §14.2, audit |
| `observations` | si `reject` / `reduce_size` | feed `self-improve` (patterns de rejet fréquents) |

## Garde-fous

- **Un check flakky** (ex : calcul corrélation qui explose sur un actif
  nouveau sans historique) doit retourner `reject` avec `reason = "check_X
  unable to evaluate"`. Le fail-safe par défaut c'est `reject`, jamais
  `approve`.
- **Mode KILL ne bypasse JAMAIS**. Même avec une variable d'env
  `OVERRIDE=true`, le check 1 reste actif. Pour redémarrer : retirer le
  fichier `KILL` sur le host.

## Commandes manuelles

```bash
# Évaluer une proposition ad-hoc (JSON en stdin)
echo '{"asset":"BTCUSDT","risk_pct":0.5,"rr":2.1,"class":"crypto"}' \
  | python -m src.risk.risk_gate --stdin

# Afficher l'état courant des 10 contrôles (sans proposition)
python -m src.risk.risk_gate --status
```
