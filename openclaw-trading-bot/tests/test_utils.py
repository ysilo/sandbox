"""
tests/test_utils.py — couvre error_codes, logging_utils, ticker_map.
Les health_checks font des appels HTTP → testés en intégration, pas ici.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.utils.error_codes import EC, by_code
from src.utils.logging_utils import (
    configure,
    cycle_scope,
    current_cycle_id,
    get_logger,
)
from src.utils.ticker_map import (
    TickerMapError,
    infer_asset_class,
    resolve_ticker,
)


# ---------------------------------------------------------------------------
# error_codes
# ---------------------------------------------------------------------------


def test_error_codes_exhaustive_families() -> None:
    families = {code.name.split("_")[0] for code in EC}
    assert families == {"CFG", "NET", "DATA", "LLM", "RISK", "RUN"}


def test_error_codes_have_short_and_remediation() -> None:
    for code in EC:
        assert code.short, f"{code.name} n'a pas de `short`"
        assert code.default_remediation, f"{code.name} n'a pas de `default_remediation`"
        assert len(code.default_remediation) > 20  # message utile, pas vide


def test_by_code_lookup_works() -> None:
    assert by_code("NET_002") is EC.NET_002


def test_by_code_unknown_raises() -> None:
    with pytest.raises(KeyError):
        by_code("XXX_999")


# ---------------------------------------------------------------------------
# logging_utils — cycle_scope + JSON schema
# ---------------------------------------------------------------------------


def test_cycle_scope_propagates() -> None:
    assert current_cycle_id() is None
    with cycle_scope("c-test-123"):
        assert current_cycle_id() == "c-test-123"
    assert current_cycle_id() is None


def test_log_emits_valid_json_with_required_fields(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    configure(log_dir=tmp_path)
    log = get_logger("UnitTest")
    with cycle_scope("c-xyz"):
        log.error("boom", ec=EC.NET_002, asset="RUI.PA", cause="connection refused")

    # 1 event attendu : le log principal (pas de meta-warning puisque ec fourni)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    # Filtrer lignes qui sont du JSON parseable
    parsed = [json.loads(l) for l in lines if l.startswith("{")]
    record = next(r for r in parsed if r.get("event") == "boom")
    assert record["level"] == "ERROR"
    assert record["error_code"] == "NET_002"
    assert record["cycle_id"] == "c-xyz"
    assert record["component"] == "UnitTest"
    assert record["context"]["asset"] == "RUI.PA"
    # remediation auto-remplie par EC.NET_002
    assert "remediation" in record
    assert "Connection refused" in record["remediation"]


def test_log_warning_without_error_code_triggers_meta_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(log_dir=tmp_path)
    log = get_logger("UnitTest")
    log.warning("sloppy_log")  # pas d'ec/error_code → meta-warning émis

    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("{")]
    parsed = [json.loads(l) for l in lines]
    # Au moins un meta-warning présent
    metas = [r for r in parsed if r.get("component") == "LoggingInvariant"]
    assert metas, "meta-warning manquant pour log WARNING sans error_code"


# ---------------------------------------------------------------------------
# ticker_map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("canonical,provider,expected", [
    ("RUI.PA", "stooq", "rui.fr"),
    ("RUI.PA", "boursorama_scrape", "1rPRUI"),
    ("BTC/USDT", "ccxt", "BTC/USDT"),
    ("EURUSD", "oanda", "EUR_USD"),
    ("bitcoin", "coingecko", "bitcoin"),
    ("SP500", "fred", "SP500"),
])
def test_resolve_ticker_canonical_mappings(canonical: str, provider: str, expected: str) -> None:
    assert resolve_ticker(canonical, provider) == expected


def test_resolve_ticker_exchangerate_host_returns_tuple() -> None:
    result = resolve_ticker("EURUSD", "exchangerate_host")
    assert result == ("EUR", "USD")


def test_resolve_ticker_rejects_malformed() -> None:
    with pytest.raises(TickerMapError):
        resolve_ticker("NOTAPATICKER", "stooq")
    with pytest.raises(TickerMapError):
        resolve_ticker("BTCUSDT", "ccxt")  # manque /


def test_infer_asset_class_heuristic() -> None:
    assert infer_asset_class("RUI.PA") == "equity"
    assert infer_asset_class("BTC/USDT") == "crypto"
    assert infer_asset_class("EURUSD") == "forex"
    with pytest.raises(TickerMapError):
        infer_asset_class("ambiguous_string")
