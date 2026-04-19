# ==============================================================================
# openclaw-trading-bot — image unique (scheduler + API + webhook Telegram)
# ------------------------------------------------------------------------------
# Multi-stage :
#   1. builder  — résolution + install des deps Python via `uv sync --frozen`
#                 dans un venv /opt/venv (rebuild reproductible via uv.lock)
#   2. runtime  — image slim, user non-root, uniquement code + venv
#
# Paramètre :
#   --build-arg OPENCLAW_VERSION=<tag>   (défaut: latest)
#       Tag logique de la release OpenClaw embarqué dans l'image (LABEL + ENV).
#       Indépendant du SHA git utilisé comme tag Docker.
#
# Note : on s'appuie sur le frontend BuildKit bundled (pas de `# syntax=...`)
# pour éviter un pull de `docker.io/docker/dockerfile` au build — le frontend
# interne supporte déjà `--mount=type=cache` utilisé plus bas. BuildKit reste
# requis (activé par défaut sur Docker Desktop ≥ 20.10 / Engine ≥ 23).
#
# Voir §17.5 + §17.7 du doc d'archi pour la rationale du choix de `uv`.
# ==============================================================================

ARG OPENCLAW_VERSION=latest

# --- Stage 1 : builder --------------------------------------------------------
FROM python:3.12-slim AS builder

# `uv` binaire copié depuis l'image officielle Astral (~10 MB, pas de Rust à compiler).
# Pin en `uv:0.4` → patch/minor OK, major breaking change verrouillé par le SHA de l'image.
COPY --from=ghcr.io/astral-sh/uv:0.4 /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 1. Deps d'abord (cache layer optimal) — on ne copie PAS le code ici
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 2. Install du project lui-même (change souvent, couche séparée)
COPY src/ ./src/
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Stage 2 : runtime --------------------------------------------------------
FROM python:3.12-slim AS runtime

# Redéclaré après le FROM — les ARG du haut ne traversent pas les stages
ARG OPENCLAW_VERSION
ARG GIT_SHA=nosha

LABEL org.openclaw.version="${OPENCLAW_VERSION}" \
      org.openclaw.git-sha="${GIT_SHA}" \
      org.opencontainers.image.title="openclaw-trading-bot" \
      org.opencontainers.image.description="Trading bot simulateur piloté par Claude" \
      org.opencontainers.image.source="https://github.com/openclaw/trading-bot"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=UTC \
    DATA_DIR=/app/data \
    KILL_FILE_PATH=/app/data/KILL \
    PORT=8080 \
    OPENCLAW_VERSION=${OPENCLAW_VERSION} \
    OPENCLAW_GIT_SHA=${GIT_SHA}

# Dépendances runtime minimales (tini pour PID 1 propre + curl pour healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
      tini \
      curl \
      ca-certificates \
      tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime

# User non-root — UID/GID 10001 aligné avec deploy.sh pour chown du volume data/
RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --home-dir /app --shell /usr/sbin/nologin bot

WORKDIR /app

# Venv pré-compilé depuis le builder (contient le project + toutes les deps,
# compiled bytecode via UV_COMPILE_BYTECODE=1 → cold-start plus rapide).
COPY --from=builder /opt/venv /opt/venv

# Code source (pas de tests, pas de data/, pas de .env — voir .dockerignore)
COPY --chown=bot:bot src/     ./src/
COPY --chown=bot:bot config/  ./config/
COPY --chown=bot:bot skills/  ./skills/
COPY --chown=bot:bot pyproject.toml README.md ./

# Pré-création des dossiers persistants — évite le crash d'init SQLite au
# premier démarrage quand le volume host est vide. KILL_FILE_PATH pointe
# désormais dans data/ — plus besoin de bind-mount fichier côté host.
RUN mkdir -p /app/data/cache /app/data/logs /app/data/analyses \
             /app/data/simulation /app/data/dashboards /app/data/models \
             /app/data/queue /app/data/patches \
    && touch /app/data/.keep \
    && chown -R bot:bot /app/data

USER bot

EXPOSE 8080

# Healthcheck natif — /healthz est l'endpoint canonique (§14.5.6).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent --max-time 4 http://localhost:${PORT}/healthz

# tini = PID 1 → signaux propres vers APScheduler / FastAPI au shutdown
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "src.main", "--serve", "--host", "0.0.0.0", "--port", "8080"]
