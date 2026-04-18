# ROADMAP.md

Priorités court / moyen terme. Les détails par sprint vont dans les ADR
(`docs/adr/`) ou les issues — ce fichier ne liste que les jalons.

## 30 jours — Consolidation paper

- [ ] Pipeline `full_analysis` opérationnel sur les 3 sessions (pre_us,
      post_close, crypto_burst)
- [ ] 5 stratégies activables sur les 7 du catalogue (§6) — au moins
      `ichimoku_trend_following`, `breakout_momentum`, `mean_reversion`,
      `event_driven_macro`, `news_driven_momentum`
- [ ] Dashboards HTML + summary.md Telegram générés à chaque cycle (§14)
- [ ] Auto-update VPS via systemd timer rodé (aucun rollback manuel sur
      30 j)
- [ ] Tests `pytest` > 70 % couverture indicateurs + risk-gate
- [ ] 50 trades paper accumulés (seuil §13.2 pour significativité)

## 60 jours — Première boucle d'auto-amélioration

- [ ] 1ère passe `self-improve` réelle (dimanche 22:00 UTC) →
      `IMPROVEMENTS_PENDING.md` avec ≥ 1 patch validé par backtest
- [ ] 1ère PR locale auto-générée mergée par l'opérateur après revue
- [ ] `memory-consolidate` tourne chaque nuit, archivage trades > 180 j
- [ ] Backtests walk-forward automatisés hebdo via `backtest-quick`
- [ ] Premier rapport mensuel auto en PDF (via `dashboard-builder` +
      pandoc)
- [ ] Sharpe paper > 0.8 sur 30 j glissants (cible §1 = > 1 à 90 j)

## 90 jours — Décision go/no-go V2

- [ ] HMM régime entraîné sur ≥ 60 j d'historique, confidence moyenne
      > 0.6 (§12)
- [ ] Système d'attribution PnL par stratégie × régime opérationnel
- [ ] Intégration éventuelle de 2 skills ClawHub après revue sécurité
      (earnings-calendar, economic-calendar — §contratde confiance
      skills/README.md)
- [ ] ADR sur l'introduction (ou non) d'un module d'apprentissage en
      ligne léger (river / online-learning)
- [ ] **Décision V2** : le paper-trading a-t-il montré Sharpe > 1, DD < 10 %
      sur 3 mois glissants ? Si oui → rédiger ADR bascule partielle live,
      sinon → itérer 90 j supplémentaires.

---

## Garde-fous permanents

Ces items ne sortent jamais du radar (même au-delà de 90 j) :

- Budget LLM mensuel < 90 % du plafond §2.3 (opus-4-7 réservé
  self-improve + archi-review)
- Divergence LLM vs déterministe journalisée dans `observations`
  tag `bias` — revue lundi matin
- Self-improve : max 1 stratégie ajoutée/retirée de `active:` par semaine
  (garde-fou stabilité §13.3)
