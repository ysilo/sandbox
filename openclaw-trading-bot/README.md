# openclaw-trading-bot

Trading bot simulateur multi-actifs (Forex, Crypto, Actions/ETF), assisté par
LLM avec validation humaine quotidienne. V1 exclusivement en **simulation**
(paper) — le trading réel est conditionné à la preuve de rentabilité simulée
sur 3 mois (cf. §1 du doc d'archi).

## Documents de référence

- **[QUICKSTART.md](QUICKSTART.md)** — clone → bot fonctionnel sur VPS en ~10 min
- **[TRADING_BOT_ARCHITECTURE.md](TRADING_BOT_ARCHITECTURE.md)** — spec complète, 18 sections, source de vérité
- **[CLAUDE.md](CLAUDE.md)** — instructions permanentes de l'agent LLM
- **[ROADMAP.md](ROADMAP.md)** — priorités court / moyen terme
- **[skills/README.md](skills/README.md)** — catalogue des skills + pipeline

## TL;DR déploiement

```bash
cd /opt
git clone https://github.com/<toi>/openclaw-trading-bot.git
cd openclaw-trading-bot
cp .env.example .env && nano .env   # remplir les 5 vars obligatoires

# Option A — docker compose direct (minimal)
sudo mkdir -p data/{cache,logs,analyses,simulation}
sudo chown -R 10001:10001 data
docker compose up -d --build

# Option B — wrapper avec validation + healthcheck + retention (recommandé)
./deploy.sh
```

`./deploy.sh` **utilise `docker compose`** — il ajoute juste les pré-checks
`.env` / NTP / permissions, l'attente du healthcheck, et la rotation des
images locales. Source : [deploy.sh](deploy.sh).

Puis ajouter le bloc `deploy/Caddyfile.snippet` au Caddy de l'hôte et
(optionnel) installer le timer d'auto-update. Le bot lui-même est dans un
conteneur ; **ces unités systemd s'installent sur l'hôte** parce qu'elles
doivent `git pull` le repo et piloter `docker compose` (même pattern que
Watchtower, détail §17.9 du doc d'archi) :

```bash
sudo cp deploy/systemd/openclaw-auto-update.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-auto-update.timer
```

Détails et troubleshooting → [QUICKSTART.md](QUICKSTART.md).

## Architecture en une phrase

Deux cycles d'analyse par jour (pre-US, post-close) + bursts crypto +
triggers ad-hoc sur breaking news. Pipeline déterministe
(`market-scan` → `strategy-selector` → `signal-crossing` → `risk-gate` →
`dashboard-builder`) avec LLM cantonné à `news-pulse` (sonnet-4-6) et
`self-improve` hebdomadaire (opus-4-7, §2.3). Mémoire SQLite + FAISS
(§10.3-10.6). Humain valide chaque proposition via Telegram ou dashboard.

## Sécurité

- **Paper-trading par défaut** (`BOT_MODE=simulation` dans `.env`). V1 ne
  supporte pas le live — bascule live réservée V2 après ADR signée.
- **Kill-switch** : `touch data/KILL` à la racine → risk-gate stoppe toute
  proposition au prochain cycle (§11.1).
- **Fail-closed** : donnée manquante, API KO, régime indéterminé → aucun trade.
- **Tout passe par `risk-gate`** (§11) — 10 contrôles déterministes (dont
  alignement Ichimoku §11.5), pas de bypass possible même par le LLM
  (§2.1 « Python décide, LLM propose » + §2.5 « Ichimoku aligné en sortie »).
- **Secrets** : `.env` chmod 600, jamais commit, jamais loggés, jamais dans
  le dashboard ni la mémoire.
- **Self-improve PR locale** — tout patch auto-généré attend validation
  humaine avant merge (§13.3).

## Stack technique

- **Python 3.12** — APScheduler, FastAPI, SQLite (WAL), pandas, numpy
- **Docker Compose V2** — un seul container (scheduler + dashboard + webhook)
- **Caddy sur l'hôte** — TLS automatique, reverse_proxy → 127.0.0.1:8080
- **LLM** — `claude-sonnet-4-6` pour cycles, `claude-opus-4-7` pour self-improve
- **Embeddings** — `BAAI/bge-small-en-v1.5` via fastembed + FAISS (§10.4)
- **Données** — ccxt (crypto), oanda (FX), alpaca (equities), CoinGecko,
  FRED, Trading Economics, NewsAPI, Finnhub (§7)

## Structure du repo

```
openclaw-trading-bot/
├── src/                         # code applicatif (orchestrator, risk, signals, ...)
├── skills/                      # 9 skills opérationnels (market-scan, risk-gate, ...)
├── config/                      # risk.yaml, strategies.yaml, sources.yaml, ...
├── data/                        # SQLite, dashboards, logs (persistant, gitignored)
├── tests/                       # pytest
├── deploy/                      # Caddyfile.snippet, auto-update.sh, systemd/
├── Dockerfile
├── docker-compose.yml
├── deploy.sh                    # entrée principale du déploiement
├── .env.example
├── QUICKSTART.md                # ← commence ici pour déployer
├── TRADING_BOT_ARCHITECTURE.md  # ← source de vérité 18 sections
└── README.md                    # ce fichier
```

Arborescence détaillée : §3 du doc d'archi.

## Licence

Usage privé uniquement. Ce code **n'est pas** un conseil en investissement.
