# QUICKSTART

Déploiement en < 5 min sur un VPS avec Docker + Caddy déjà installés.
Détails, options avancées, troubleshooting → [TRADING_BOT_ARCHITECTURE.md §17](TRADING_BOT_ARCHITECTURE.md#17-déploiement).

## 0. Développement local (avant le VPS)

Le projet utilise **[uv](https://github.com/astral-sh/uv)** comme package manager
(cf. §17.5.1). Install de uv (une fois par machine) :

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # macOS / Linux
# ou : powershell -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows
```

Setup projet local :

```bash
git clone https://github.com/<toi>/openclaw-trading-bot.git
cd openclaw-trading-bot
uv sync                                     # crée .venv/, installe runtime + dev
cp .env.example .env && nano .env           # clés API
uv run pytest                               # tests
uv run python -m src.orchestrator.run --once   # cycle unique
```

Détails complets : [§17.5.2](TRADING_BOT_ARCHITECTURE.md#1752-workflow-de-développement-local-avec-uv).

## 1. Clone + config (sur le VPS)

```bash
cd /opt
git clone https://github.com/<toi>/openclaw-trading-bot.git
cd openclaw-trading-bot
cp .env.example .env && nano .env   # ANTHROPIC_API_KEY, TELEGRAM_*, DASHBOARD_BASIC_AUTH_PASS
chmod 600 .env
```

## 2. Deploy

```bash
./deploy.sh       # wrapper docker compose + validation .env + healthcheck + retention
```

Variante minimale sans wrapper :

```bash
sudo mkdir -p data/{cache,logs,analyses,simulation} && sudo chown -R 10001:10001 data
docker compose up -d --build
```

## 3. Caddy (sur l'hôte)

```bash
sudo tee -a /etc/caddy/Caddyfile < deploy/Caddyfile.snippet
sudo nano /etc/caddy/Caddyfile     # remplacer openclaw-bot.easy-flow.site par ton domaine
sudo systemctl reload caddy
```

## 4. Auto-update (optionnel, recommandé)

Le bot tourne dans un conteneur ; seul le *mécanisme* d'update vit sur l'hôte
(il doit `git pull` dans `/opt/...` + parler au démon Docker — même raison
que Watchtower). D'où les unités systemd :

```bash
sudo cp deploy/systemd/openclaw-auto-update.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-auto-update.timer
```

Tous les jours à 04:00 UTC : `git fetch` + `./deploy.sh` si nouveau commit
sur `main`. Déclenchement manuel en cas d'urgence :
`sudo systemctl start openclaw-auto-update.service`.

## 5. logrotate (recommandé)

```bash
sudo cp deploy/logrotate.conf /etc/logrotate.d/openclaw-trading-bot
sudo logrotate -d /etc/logrotate.d/openclaw-trading-bot   # dry-run
```

Rotation quotidienne des `.log` et `.jsonl` sous `data/logs/`, 14 jours
de rétention, compression.

## 6. Vérif

```bash
docker compose ps                         # healthy
curl -s http://127.0.0.1:8080/healthz     # {"status":"ok"}
curl -I https://<ton-domaine>/healthz     # 200 OK (après propagation DNS)
```

## Kill-switch

```bash
touch data/KILL        # gèle toute proposition au prochain cycle (§11.1)
rm data/KILL           # réactive
```

## Rollback

```bash
./deploy.sh --rollback                    # image précédente, sans rebuild
```
