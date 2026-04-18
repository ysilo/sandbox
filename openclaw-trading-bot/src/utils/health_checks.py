"""
src.utils.health_checks — startup fail-fast checks (§7.5.3).

Règle d'or §2.6 : on refuse de démarrer si l'environnement est cassé,
plutôt que d'émettre un cycle à moitié fonctionnel qui pollue les métriques.

Usage (à appeler depuis `src/orchestrator/run.py` AVANT le scheduler) :

    from src.utils.health_checks import run_startup_checks, main_or_exit
    main_or_exit()   # sys.exit(78) si un check critique échoue

Stratégie :
- Checks *toujours obligatoires* : env vars cores, HTTP Anthropic + Telegram,
  FS writable, YAML parseable.
- Checks *conditionnels* selon `enabled_markets` dans assets.yaml :
  equity → stooq + boursorama réseau ; crypto → binance ping ; forex → OANDA
  optionnel (fallback exchangerate_host).

Les checks n'échouent PAS sur des warnings — c'est le main() qui agrège et
décide de `sys.exit(78)` selon le niveau (CRITICAL bloque, WARNING passe).
"""
from __future__ import annotations

import http.client
import os
import socket
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml

from .error_codes import EC
from .logging_utils import get_logger


log = get_logger("HealthCheck")


@dataclass
class CheckResult:
    name: str
    passed: bool
    cause: Optional[str] = None
    remediation: Optional[str] = None
    severity: str = "CRITICAL"       # WARNING | CRITICAL (only CRITICAL blocks startup)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def _check_env_var(name: str, *, required: bool = True) -> CheckResult:
    val = os.environ.get(name, "")
    passed = bool(val)
    if passed:
        return CheckResult(name=f"env:{name}", passed=True)
    return CheckResult(
        name=f"env:{name}",
        passed=False,
        cause=f"Variable {name} absente ou vide dans l'environnement.",
        remediation=EC.CFG_001.default_remediation,
        severity="CRITICAL" if required else "WARNING",
    )


def _check_http_reachable(
    url: str,
    *,
    timeout: float = 5.0,
    expect: Iterable[int] = (200, 401, 405),
) -> CheckResult:
    """Petit HEAD/GET sans deps externes (stdlib uniquement)."""
    try:
        scheme, _, rest = url.partition("://")
        host, _, path = rest.partition("/")
        path = "/" + path if path else "/"

        if scheme == "https":
            conn = http.client.HTTPSConnection(host, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(host, timeout=timeout)
        try:
            conn.request("HEAD", path)
            resp = conn.getresponse()
            status = resp.status
        finally:
            conn.close()

        if status in tuple(expect):
            return CheckResult(name=f"http:{url}", passed=True)
        return CheckResult(
            name=f"http:{url}",
            passed=False,
            cause=f"HTTP {status} inattendu (accepté: {list(expect)})",
            remediation=EC.NET_002.default_remediation,
            severity="CRITICAL",
        )
    except (socket.timeout, TimeoutError) as e:
        return CheckResult(
            name=f"http:{url}", passed=False,
            cause=f"timeout {timeout}s : {e}",
            remediation=EC.NET_001.default_remediation,
        )
    except (socket.gaierror,) as e:
        return CheckResult(
            name=f"http:{url}", passed=False,
            cause=f"DNS fail : {e}",
            remediation=EC.NET_003.default_remediation,
        )
    except Exception as e:
        return CheckResult(
            name=f"http:{url}", passed=False,
            cause=f"{type(e).__name__}: {e}",
            remediation=EC.NET_002.default_remediation,
        )


def _check_writable(directory: Path) -> CheckResult:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=True):
            pass
        return CheckResult(name=f"writable:{directory}", passed=True)
    except OSError as e:
        return CheckResult(
            name=f"writable:{directory}", passed=False,
            cause=f"écriture impossible : {e}",
            remediation=f"chmod u+w {directory} ou corriger UID/GID du volume Docker.",
        )


def _check_sqlite_openable(db_path: Path) -> CheckResult:
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.close()
        return CheckResult(name=f"sqlite:{db_path.name}", passed=True)
    except sqlite3.Error as e:
        return CheckResult(
            name=f"sqlite:{db_path.name}", passed=False,
            cause=f"SQLite error : {e}",
            remediation=EC.RUN_001.default_remediation,
        )


def _check_yaml_loadable(path: str) -> CheckResult:
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as f:
            yaml.safe_load(f)
        return CheckResult(name=f"yaml:{p.name}", passed=True)
    except FileNotFoundError:
        return CheckResult(
            name=f"yaml:{p.name}", passed=False,
            cause=f"fichier absent : {p}",
            remediation=EC.CFG_002.default_remediation,
        )
    except yaml.YAMLError as e:
        return CheckResult(
            name=f"yaml:{p.name}", passed=False,
            cause=f"YAML invalide : {e}",
            remediation=EC.CFG_002.default_remediation,
        )


# ---------------------------------------------------------------------------
# Assembleur de checks
# ---------------------------------------------------------------------------


def _load_enabled_markets(assets_yaml: str) -> set[str]:
    """Lit assets.yaml et renvoie l'ensemble des classes activées."""
    p = Path(assets_yaml)
    if not p.is_file():
        return {"equity", "forex", "crypto"}
    with p.open("r", encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    # heuristique simple : si class_budgets_pct contient une classe avec budget>0, elle est enabled
    budgets = (d.get("class_budgets_pct") or {}) if isinstance(d, dict) else {}
    return {cls for cls, pct in budgets.items() if pct and pct > 0}


def run_startup_checks(
    data_dir: Path | None = None,
    config_dir: Path | None = None,
) -> list[CheckResult]:
    data_dir = data_dir or Path(os.environ.get("DATA_DIR", "data"))
    config_dir = config_dir or Path("config")
    enabled_markets = _load_enabled_markets(str(config_dir / "assets.yaml"))

    checks: list[CheckResult] = [
        # ---- Core : toujours obligatoire ----
        _check_env_var("ANTHROPIC_API_KEY", required=True),
        _check_env_var("TELEGRAM_BOT_TOKEN", required=True),
        _check_env_var("TELEGRAM_CHAT_ID", required=True),
        _check_http_reachable("https://api.anthropic.com", timeout=5, expect=(200, 401, 405, 403)),
        _check_http_reachable("https://api.telegram.org", timeout=5, expect=(200, 301, 302, 404)),
        # Filesystem
        _check_writable(data_dir / "logs"),
        _check_writable(data_dir / "cache"),
        _check_writable(data_dir / "simulation"),
        _check_sqlite_openable(data_dir / "memory.db"),
        # Config
        _check_yaml_loadable(str(config_dir / "risk.yaml")),
        _check_yaml_loadable(str(config_dir / "strategies.yaml")),
        _check_yaml_loadable(str(config_dir / "assets.yaml")),
        _check_yaml_loadable(str(config_dir / "sources.yaml")),
        _check_yaml_loadable(str(config_dir / "schedules.yaml")),
    ]

    # ---- Conditionnel : selon marchés activés ----
    if "equity" in enabled_markets:
        checks.append(_check_http_reachable("https://stooq.com", timeout=5, expect=(200, 301, 302, 403)))
        checks.append(_check_http_reachable("https://www.boursorama.com", timeout=5, expect=(200, 301, 302, 403)))

    if "forex" in enabled_markets:
        oanda_key = _check_env_var("OANDA_API_KEY", required=False)
        checks.append(oanda_key)
        if not oanda_key.passed:
            checks.append(_check_http_reachable("https://api.exchangerate.host", timeout=5, expect=(200, 301, 302, 403, 404)))

    if "crypto" in enabled_markets:
        checks.append(_check_http_reachable("https://api.binance.com/api/v3/ping", timeout=5, expect=(200, 401, 403, 418)))

    return checks


def summarize(results: list[CheckResult]) -> tuple[int, int, int]:
    """Renvoie (n_passed, n_warning_failed, n_critical_failed)."""
    n_pass = sum(1 for r in results if r.passed)
    n_warn = sum(1 for r in results if not r.passed and r.severity == "WARNING")
    n_crit = sum(1 for r in results if not r.passed and r.severity == "CRITICAL")
    return n_pass, n_warn, n_crit


def main_or_exit() -> None:
    """Exécute les checks, log chaque résultat, sys.exit(78) si CRITICAL KO.

    Exit code 78 = EX_CONFIG (BSD sysexits) — cohérent avec le doc §7.5.3.
    """
    results = run_startup_checks()
    for r in results:
        if r.passed:
            log.info("startup_check", name=r.name, passed=True)
        elif r.severity == "CRITICAL":
            log.critical(
                "startup_check",
                ec=EC.CFG_001,
                name=r.name,
                cause=r.cause,
                remediation=r.remediation,
            )
        else:
            log.warning(
                "startup_check",
                ec=EC.CFG_001,
                name=r.name,
                cause=r.cause,
                remediation=r.remediation,
            )

    n_pass, n_warn, n_crit = summarize(results)
    if n_crit > 0:
        log.critical(
            "startup_aborted",
            ec=EC.CFG_001,
            cause=f"{n_crit} check(s) CRITICAL KO, {n_warn} WARNING, {n_pass} OK",
            remediation="Corriger les erreurs CRITICAL ci-dessus puis relancer.",
        )
        sys.exit(78)
    log.info("startup_checks_ok", n_pass=n_pass, n_warn=n_warn)


__all__ = [
    "CheckResult",
    "run_startup_checks",
    "summarize",
    "main_or_exit",
]
