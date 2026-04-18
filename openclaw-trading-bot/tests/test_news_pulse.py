"""
tests.test_news_pulse — skill `news-pulse` §6.7, §7, §15.1.

Couvre le pipeline déterministe 0-token (stub LLM) :
- Normalisation de titre + hash (dedupe)
- NER via asset_keywords
- Sentiment lexique (positif/négatif/neutre, accents FR)
- Détection catalyst par pattern (fomc/cpi/hack/earnings/...)
- Impact = baseline × proximité + boost sentiment
- Build pulse : fenêtre temporelle, cap max_items, fail-closed LLM
- Stub `PassthroughSummarizer` n'incrémente pas `llm_calls`
- Batch multi-assets
- triggers_ad_hoc selon seuil
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.contracts.skills import NewsPulse
from src.news import news_pulse as np
from src.news.news_pulse import (
    PassthroughSummarizer,
    RawNewsItem,
    build_pulse,
    build_pulse_batch,
    triggers_ad_hoc,
)


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


def _raw(
    title: str,
    *,
    source: str = "reuters_rss",
    url: str = "https://example.com/a",
    minutes_ago: int = 30,
    body: str | None = None,
    entities_hint: list[str] | None = None,
) -> RawNewsItem:
    return RawNewsItem(
        source=source,
        title=title,
        url=url,
        published_at=_now() - timedelta(minutes=minutes_ago),
        body=body,
        entities_hint=list(entities_hint or []),
    )


def _keywords() -> dict[str, list[str]]:
    return {
        "BTCUSDT": ["bitcoin", "btc"],
        "ETHUSDT": ["ethereum", "eth"],
        "RUI.PA":  ["rubis", "rui"],
        "SPY":     ["s&p 500", "sp500", "spx", "spy"],
    }


# ---------------------------------------------------------------------------
# Normalisation / hash
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_same_after_punctuation_change(self):
        assert np._normalize_title("Bitcoin rallies!") == np._normalize_title("bitcoin rallies")

    def test_accent_insensitive(self):
        assert np._normalize_title("Résultats dépassent") == np._normalize_title("resultats depassent")

    def test_collapses_whitespace(self):
        assert np._normalize_title("Fed   hints    at   pause") == "fed hints at pause"

    def test_hash_is_stable(self):
        h1 = np._hash_title("BTC surges 10%")
        h2 = np._hash_title("btc  surges  10 %")
        assert h1 == h2


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_keeps_single_occurrence(self):
        items = [_raw("Fed pauses", minutes_ago=30), _raw("Fed pauses", minutes_ago=15)]
        out = np._dedupe(items)
        assert len(out) == 1

    def test_keeps_oldest_version(self):
        items = [
            _raw("Fed pauses", minutes_ago=30, source="reuters"),
            _raw("Fed pauses!", minutes_ago=45, source="bloomberg"),
        ]
        out = np._dedupe(items)
        assert len(out) == 1
        assert out[0].source == "bloomberg"  # plus ancien

    def test_preserves_distinct_titles(self):
        items = [
            _raw("Bitcoin rallies"),
            _raw("Ethereum falls"),
            _raw("Fed pauses"),
        ]
        out = np._dedupe(items)
        assert len(out) == 3


# ---------------------------------------------------------------------------
# NER entities
# ---------------------------------------------------------------------------


class TestEntities:
    def test_detects_via_keyword(self):
        it = _raw("Bitcoin hits new all-time high")
        ents = np._extract_entities(it, asset_keywords=_keywords())
        assert "BTCUSDT" in ents

    def test_detects_multiple(self):
        it = _raw("Bitcoin and Ethereum both surge")
        ents = np._extract_entities(it, asset_keywords=_keywords())
        assert "BTCUSDT" in ents
        assert "ETHUSDT" in ents

    def test_uses_body(self):
        it = _raw("Market surges", body="Bitcoin leading the rally")
        ents = np._extract_entities(it, asset_keywords=_keywords())
        assert "BTCUSDT" in ents

    def test_case_insensitive(self):
        it = _raw("BTC soars")
        ents = np._extract_entities(it, asset_keywords={"BTCUSDT": ["BTC"]})
        assert "BTCUSDT" in ents

    def test_merges_hints(self):
        it = _raw("Market news", entities_hint=["CUSTOM_TICKER"])
        ents = np._extract_entities(it, asset_keywords=_keywords())
        assert "CUSTOM_TICKER" in ents

    def test_no_match(self):
        it = _raw("Weather report for Paris")
        ents = np._extract_entities(it, asset_keywords=_keywords())
        assert ents == []


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------


class TestSentiment:
    def test_positive(self):
        assert np._score_sentiment("Bitcoin surges to record highs") > 0.2

    def test_negative(self):
        assert np._score_sentiment("Crypto hack drains exchange") < -0.2

    def test_neutral_empty(self):
        assert np._score_sentiment("") == 0.0

    def test_neutral_no_lexicon_hits(self):
        # Phrase sans mots du lexique
        assert np._score_sentiment("The chair of the board") == 0.0

    def test_french_positive(self):
        assert np._score_sentiment("Rubis dépasse les attentes") > 0.0

    def test_french_negative(self):
        assert np._score_sentiment("Forte chute du titre") < 0.0

    def test_bounded(self):
        # Très long message très positif
        text = " ".join(["surge"] * 50)
        s = np._score_sentiment(text)
        assert -1.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# Catalyst detection
# ---------------------------------------------------------------------------


class TestCatalyst:
    @pytest.mark.parametrize("title,expected", [
        ("FOMC meeting results tomorrow",      "fomc"),
        ("CPI inflation data beats forecast",  "cpi"),
        ("NFP report: strong job growth",      "nfp"),
        ("Earnings beat for Q1",                "earnings"),
        ("Major hack drains $100M from DEX",   "hack"),
        ("Bitcoin halving approaches",          "halving"),
        ("Merger announced between A and B",    "merger"),
        ("XYZ listing on Binance tomorrow",    "listing"),
        ("SEC lawsuit filed against Ripple",    "regulation"),
        ("Local news : Paris weather",          "other"),
    ])
    def test_patterns(self, title, expected):
        assert np._detect_catalyst(title) == expected

    def test_priority_order(self):
        # FOMC + CPI dans le même titre — FOMC est premier dans l'ordre
        assert np._detect_catalyst("FOMC discusses CPI trends") == "fomc"


# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------


class TestImpact:
    def test_baseline_fomc_high(self):
        impact = np._compute_impact(
            catalyst="fomc",
            published_at=_now() - timedelta(minutes=30),
            now=_now(),
            sentiment=0.0,
        )
        assert impact >= 0.85

    def test_baseline_other_low(self):
        impact = np._compute_impact(
            catalyst="other",
            published_at=_now() - timedelta(minutes=30),
            now=_now(),
            sentiment=0.0,
        )
        assert impact <= 0.35

    def test_proximity_decays(self):
        fresh = np._compute_impact(
            catalyst="fomc", published_at=_now() - timedelta(minutes=15),
            now=_now(), sentiment=0.0,
        )
        old = np._compute_impact(
            catalyst="fomc", published_at=_now() - timedelta(hours=24),
            now=_now(), sentiment=0.0,
        )
        assert fresh > old

    def test_sentiment_boost(self):
        without = np._compute_impact(
            catalyst="earnings", published_at=_now(), now=_now(), sentiment=0.0,
        )
        with_high_sent = np._compute_impact(
            catalyst="earnings", published_at=_now(), now=_now(), sentiment=1.0,
        )
        assert with_high_sent > without

    def test_bounded(self):
        impact = np._compute_impact(
            catalyst="hack", published_at=_now(), now=_now(), sentiment=-1.0,
        )
        assert 0.0 <= impact <= 1.0


# ---------------------------------------------------------------------------
# build_pulse
# ---------------------------------------------------------------------------


class TestBuildPulse:
    def test_empty_input_returns_empty_pulse(self):
        pulse, stats = build_pulse(
            "BTCUSDT", [], asset_keywords=_keywords(), now=_now(),
        )
        assert isinstance(pulse, NewsPulse)
        assert pulse.items == []
        assert pulse.aggregate_impact == 0.0
        assert pulse.aggregate_sentiment == 0.0
        assert pulse.top is None
        assert stats.fetched == 0

    def test_no_matching_asset_returns_empty_pulse(self):
        # News sur EUR mais on demande BTC
        items = [_raw("ECB cuts rates unexpectedly")]
        pulse, stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        assert pulse.items == []
        assert stats.fetched == 1
        assert stats.scored == 0

    def test_filters_outside_window(self):
        items = [
            _raw("Bitcoin surges", minutes_ago=30),   # in window
            _raw("Bitcoin news old", minutes_ago=60 * 48),  # 48h — out of 24h window
        ]
        pulse, stats = build_pulse(
            "BTCUSDT", items,
            asset_keywords=_keywords(),
            window_hours=24, now=_now(),
        )
        assert len(pulse.items) == 1
        assert stats.fetched == 2

    def test_caps_at_max_items(self):
        # 30 news différentes sur BTC, max=5
        items = [_raw(f"Bitcoin news {i}", minutes_ago=i) for i in range(30)]
        pulse, _stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(),
            now=_now(), max_items=5,
        )
        assert len(pulse.items) == 5

    def test_sorted_by_impact_desc(self):
        items = [
            _raw("Bitcoin price update",               minutes_ago=30),   # "other"
            _raw("Bitcoin hack on exchange reported",  minutes_ago=60),   # "hack" high
            _raw("Bitcoin in the news",                minutes_ago=15),   # "other"
        ]
        pulse, _stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        impacts = [i.impact for i in pulse.items]
        assert impacts == sorted(impacts, reverse=True)
        # Le hack doit être en tête
        assert "hack" in pulse.items[0].title.lower()

    def test_top_field_set(self):
        items = [_raw("Bitcoin breaking news", minutes_ago=15)]
        pulse, _stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        assert pulse.top is not None
        assert pulse.top == pulse.items[0]

    def test_aggregates_bounded(self):
        items = [
            _raw("Bitcoin hack exchange",  minutes_ago=15),
            _raw("Bitcoin surges record",  minutes_ago=30),
        ]
        pulse, _stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        assert 0.0 <= pulse.aggregate_impact <= 1.0
        assert -1.0 <= pulse.aggregate_sentiment <= 1.0

    def test_dedupe_applied(self):
        items = [
            _raw("Bitcoin surges",  minutes_ago=30),
            _raw("bitcoin surges!", minutes_ago=15),  # dupe
        ]
        pulse, stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        assert len(pulse.items) == 1
        assert stats.after_dedupe == 1

    def test_with_catalyst_count(self):
        items = [
            _raw("Bitcoin price check",    minutes_ago=30),  # other
            _raw("Bitcoin halving event",  minutes_ago=45),  # halving
            _raw("FOMC ahead for Bitcoin", minutes_ago=60),  # fomc
        ]
        _pulse, stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        assert stats.with_catalyst == 2

    def test_news_item_published_has_z_suffix(self):
        items = [_raw("Bitcoin breaks resistance", minutes_ago=10)]
        pulse, _stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        assert pulse.items[0].published.endswith("Z")


# ---------------------------------------------------------------------------
# LLM stub
# ---------------------------------------------------------------------------


class TestLLMStub:
    def test_passthrough_does_not_count_as_llm_call(self):
        items = [_raw("Bitcoin rallies strongly", minutes_ago=10)]
        _pulse, stats = build_pulse(
            "BTCUSDT", items,
            asset_keywords=_keywords(),
            now=_now(),
            summarizer=PassthroughSummarizer(),
        )
        assert stats.llm_calls == 0

    def test_default_summarizer_is_passthrough(self):
        items = [_raw("Bitcoin rallies strongly", minutes_ago=10)]
        _pulse, stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        assert stats.llm_calls == 0

    def test_custom_summarizer_counts(self):
        class _MySummarizer:
            def summarize(self, items):
                return list(items)

        items = [_raw("Bitcoin rallies strongly", minutes_ago=10)]
        _pulse, stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(),
            now=_now(), summarizer=_MySummarizer(),
        )
        assert stats.llm_calls == 1

    def test_summarizer_exception_falls_back(self):
        class _BrokenSummarizer:
            def summarize(self, items):
                raise RuntimeError("LLM unavailable")

        items = [_raw("Bitcoin hack exchange", minutes_ago=10)]
        pulse, _stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(),
            now=_now(), summarizer=_BrokenSummarizer(),
        )
        # Fallback : les items enrichis déterministes sont conservés
        assert len(pulse.items) == 1


# ---------------------------------------------------------------------------
# build_pulse_batch
# ---------------------------------------------------------------------------


class TestBatch:
    def test_batch_multiple_assets(self):
        items = [
            _raw("Bitcoin and Ethereum both rally", minutes_ago=15),
            _raw("Rubis posts strong quarterly results", minutes_ago=30),
        ]
        out = build_pulse_batch(
            ["BTCUSDT", "ETHUSDT", "RUI.PA"],
            items, asset_keywords=_keywords(), now=_now(),
        )
        assert set(out.keys()) == {"BTCUSDT", "ETHUSDT", "RUI.PA"}
        assert len(out["BTCUSDT"][0].items) == 1
        assert len(out["ETHUSDT"][0].items) == 1
        assert len(out["RUI.PA"][0].items) == 1

    def test_batch_empty_assets(self):
        out = build_pulse_batch(
            [], [_raw("News")], asset_keywords=_keywords(), now=_now(),
        )
        assert out == {}


# ---------------------------------------------------------------------------
# triggers_ad_hoc (§15.1)
# ---------------------------------------------------------------------------


class TestAdHocTrigger:
    def test_high_impact_triggers(self):
        items = [_raw("Major Bitcoin hack drains exchange", minutes_ago=5)]
        pulse, _stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        assert triggers_ad_hoc(pulse) is True

    def test_low_impact_does_not_trigger(self):
        items = [_raw("Bitcoin price fluctuates slightly", minutes_ago=60)]
        pulse, _stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        assert triggers_ad_hoc(pulse) is False

    def test_empty_pulse_does_not_trigger(self):
        pulse = NewsPulse.empty("BTCUSDT")
        assert triggers_ad_hoc(pulse) is False

    def test_custom_threshold(self):
        items = [_raw("Bitcoin earnings approach", minutes_ago=30)]
        pulse, _stats = build_pulse(
            "BTCUSDT", items, asset_keywords=_keywords(), now=_now(),
        )
        # Seuil bas → trigger ; seuil très haut → non
        assert triggers_ad_hoc(pulse, impact_threshold=0.3) is True
        assert triggers_ad_hoc(pulse, impact_threshold=0.99) is False
