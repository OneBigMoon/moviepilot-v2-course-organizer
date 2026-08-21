import asyncio
import math
import json
import sys
import types
from typing import Any
from collections import namedtuple
from pathlib import Path

import pytest

from tests.courseorganizer_testkit import load_courseorganizer


Module = load_courseorganizer()
naming = Module.naming
providers = Module.providers
resolver = Module.resolver

PLUGIN_PATH = Path(__file__).parents[1] / "plugins.v2" / "courseorganizer"

MoviePilotAIReviewer = providers.MoviePilotAIReviewer
MoviePilotMetadataProvider = providers.MoviePilotMetadataProvider
ProviderSearchResult = providers.ProviderSearchResult
NamingConfig = resolver.NamingConfig
SmartNamingResolver = resolver.SmartNamingResolver
NamingDecision = resolver.NamingDecision


def _patch_resolver_evaluate_candidates(monkeypatch: pytest.MonkeyPatch, evaluator):
    monkeypatch.setattr(naming, "evaluate_candidates", evaluator)
FakeMediaInfo = namedtuple("FakeMediaInfo", [
    "source",
    "media_id",
    "type",
    "title",
    "en_title",
    "original_title",
    "names",
    "year",
    "tmdb_id",
    "douban_id",
    "detail_link",
])


@pytest.mark.parametrize(
    (
        "raw",
        "title",
        "year",
        "seasons",
        "queries",
    ),
    [
        ("《飘零叶 Tumble Leaf》儿童早教动画，全美获奖冒险故事启蒙精品", "飘零叶 Tumble Leaf", None, (), ("飘零叶 Tumble Leaf", "Tumble Leaf", "飘零叶")),
        ("黑冰 (2001) 高清修复版 未删减", "黑冰", 2001, (), ("黑冰",)),
        ("《少年得到爆笑唐诗》全集第1-3季：趣味解读，轻松学古诗", "少年得到爆笑唐诗", None, (1, 2, 3), ("少年得到爆笑唐诗",)),
        ("100堂动画课带孩子穿越唐诗大世界，趣味唐诗启蒙系统课程", "100堂动画课带孩子穿越唐诗大世界", None, (), ("100堂动画课带孩子穿越唐诗大世界",)),
        ("孩子的第一堂宇宙课，探索太阳系与星辰大海", "孩子的第一堂宇宙课", None, (), ("孩子的第一堂宇宙课",)),
        ("世界地理探秘儿童课，10个颠覆认知的地理奇闻", "世界地理探秘儿童课", None, (), ("世界地理探秘儿童课",)),
        ("唐宋八大家动画，走近古文运动领袖的文学与人生", "唐宋八大家动画", None, (), ("唐宋八大家动画",)),
        ("熊孩子国学故事", "熊孩子国学故事", None, (), ("熊孩子国学故事",)),
    ],
)
def test_parse_user_folder_examples(raw, title, year, seasons, queries):
    hints = naming.parse_title(raw)
    assert hints.local_title == title
    assert hints.year == year
    assert hints.season_hints == seasons
    assert tuple(item.text for item in hints.query_candidates) == queries


def test_parse_title_normalizes_nfkc_whitespace_and_limits_queries():
    raw = " TUMBLE\u3000LEAF  "
    hints = naming.parse_title(raw)
    assert hints.local_title == "TUMBLE LEAF"
    assert len(hints.query_candidates) <= 3


def test_parse_title_with_chinese_season_range():
    hints = naming.parse_title("课程《少年》第一季")
    assert 1 in hints.season_hints


def test_parse_title_with_s01_s03_range():
    hints = naming.parse_title("S01-S03 课程集")
    assert hints.season_hints == (1, 2, 3)


def test_parse_title_ignores_short_noise_infix_only():
    hints = naming.parse_title("黑冰 (2001) 高清修复版 主题 未删减")
    assert hints.local_title == "黑冰"


def test_parse_title_removes_duplicate_queries_and_caps_three_queries():
    hints = naming.parse_title("Tumble Leaf 《书名》", manual_query="Tumble Leaf")
    assert len(hints.query_candidates) <= 3
    assert hints.query_candidates[0].origin == "manual"


def test_parse_manual_overrides_with_validation_errors():
    text = "\n".join(
        [
            "A => local:A",
            "A => query:duplicate",
            "B => malformed",
            "C => candidate:xx",
            "D => candidate:themoviedb::tv",
            "E => candidate:themoviedb:xx:unsupported",
            " => local:empty",
        ]
    )
    result = naming.parse_manual_overrides(text)
    assert len(result.overrides) == 1
    assert result.overrides[0].action == "local"
    assert any("duplicate_left" in item for item in result.errors)
    assert any("invalid_rule" in item for item in result.errors)
    assert any("invalid_candidate" in item for item in result.errors)
    assert any("empty_left" in item for item in result.errors)


def test_score_exact_bonus_composition():
    hints = naming.TitleHints(
        raw_title="飘零叶 Tumble Leaf",
        local_title="飘零叶 Tumble Leaf",
        year=None,
        season_hints=(),
        query_candidates=(naming.QueryCandidate("Tumble Leaf", "latin_segment"),),
        reason_codes=("manual",),
    )
    candidate = naming.MetadataCandidate(
        key="themoviedb:65047:tv",
        source="themoviedb",
        media_id="65047",
        media_type="tv",
        title="Tumble Leaf",
        en_title="Tumble Leaf",
        original_title="",
        aliases=(),
        year=2013,
    )
    score = naming.score_candidates(
        hints,
        naming.DirectoryHints(media_count=3, seasons=(1,), episodic=True),
        [candidate],
    )[0]
    assert score.score == 93


def test_score_alias_exact_and_fuzzy_fallback():
    hints = naming.TitleHints(
        raw_title="黑冰",
        local_title="黑冰",
        year=2001,
        season_hints=(),
        query_candidates=(naming.QueryCandidate("黑冰", "first_clause"),),
        reason_codes=(),
    )
    exact = naming.MetadataCandidate(
        key="douban:1:movie",
        source="douban",
        media_id="1",
        media_type="movie",
        title="不匹配",
        en_title="",
        original_title="",
        aliases=("黑冰",),
        year=2001,
    )
    scored = naming.score_candidates(
        hints,
        naming.DirectoryHints(media_count=1, seasons=(1,), episodic=True),
        [exact],
    )[0]
    assert scored.rejected_reason == "episodic_movie"


def test_short_title_requires_year_or_cross_source_for_auto():
    hints = naming.parse_title("小学")
    candidate = naming.MetadataCandidate(
        key="themoviedb:1:tv",
        source="themoviedb",
        media_id="1",
        media_type="tv",
        title="小学",
        year=None,
    )
    decision = naming.evaluate_candidates(
        hints,
        naming.DirectoryHints(media_count=3, seasons=(1,), episodic=True),
        [candidate],
        auto_threshold=90,
        min_margin=12,
    )
    assert decision.status in {"review", "local_fallback"}


class FakeChain:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def search(self, text, source=None):
        self.calls.append((text, source))
        if source == "boom":
            raise RuntimeError("source disabled")
        if source == "fail":
            raise RuntimeError("down")
        return None, self.payload[source]


def test_provider_resolves_sources_and_budget():
    fake = FakeChain(
        {
            "themoviedb": [
                FakeMediaInfo(
                    "themoviedb",
                    "65047",
                    "tv",
                    "Tumble Leaf",
                    "Tumble Leaf",
                    "Tumble",
                    ["Tumble", "Leaf"],
                    2013,
                    "65047",
                    "",
                    "",
                )
            ],
            "douban": [],
        }
    )
    provider = MoviePilotMetadataProvider(chain=fake, search_source="themoviedb,douban")
    resolved = provider.resolve_sources(["themoviedb", "douban", "bangumi"])
    assert resolved == ("themoviedb", "douban")
    result = provider.search(
        (
            naming.QueryCandidate("飘零叶", "book_title"),
            naming.QueryCandidate("Tumble", "latin_segment"),
            naming.QueryCandidate("Leaf", "latin_segment"),
        ),
        resolved,
    )
    assert isinstance(result, ProviderSearchResult)
    assert len(result.candidates) == 1
    assert result.candidates[0].key == "themoviedb:65047:tv"
    assert len(fake.calls) <= 6


def test_provider_marks_all_failed_only_when_every_attempt_fails():
    class FailChain:
        def __init__(self):
            self.calls = 0

        def search(self, text, source=None):
            self.calls += 1
            if source == "themoviedb":
                raise RuntimeError("boom")
            return None, []

    chain = FailChain()
    provider = MoviePilotMetadataProvider(chain=chain, search_source="themoviedb,douban")
    result = provider.search((naming.QueryCandidate("a", "first_clause"),), ("themoviedb", "douban"))
    assert result.all_failed is False


def test_provider_marks_all_failed_when_every_source_raises():
    class FailingChain:
        def __init__(self):
            self.calls: list[str] = []

        def search(self, text, source=None):
            self.calls.append(source)
            raise RuntimeError(f"{source} failed")

    chain = FailingChain()
    provider = MoviePilotMetadataProvider(chain=chain, search_source="themoviedb,douban")
    result = provider.search((naming.QueryCandidate("a", "first_clause"),), ("themoviedb", "douban"))
    assert result.all_failed is True
    assert result.attempted_sources == ("themoviedb", "douban")
    assert len(result.errors) == 2


def test_provider_keeps_earlier_success_when_later_query_fails():
    class PartiallyFailingChain:
        def __init__(self):
            self.calls = []

        def search(self, text, source=None):
            self.calls.append((text, source))
            if text == "second":
                raise RuntimeError("later query failed")
            return None, []

    chain = PartiallyFailingChain()
    provider = MoviePilotMetadataProvider(chain=chain, search_source="themoviedb")
    result = provider.search(
        (
            naming.QueryCandidate("first", "first_clause"),
            naming.QueryCandidate("second", "book_title"),
        ),
        ("themoviedb",),
    )

    assert result.candidates == ()
    assert result.all_failed is False
    assert result.attempted_sources == ("themoviedb",)
    assert any("second" in error for error in result.errors)


def test_ai_reviewer_accepts_only_whitelisted_high_confidence():
    candidate = naming.MetadataCandidate(
        key="themoviedb:65047:tv",
        source="themoviedb",
        media_id="65047",
        media_type="tv",
        title="Tumble Leaf",
        year=2013,
    )
    scored = naming.ScoredCandidate(candidate=candidate, score=95, reason_codes=("x",))

    def invoke(prompt: str):
        return json.dumps(
            {
                "decision": "choose",
                "candidate_key": "themoviedb:65047:tv",
                "confidence": 0.9,
                "reason_codes": ["good"],
            }
        )

    reviewer = MoviePilotAIReviewer(invoke_fn=invoke)
    result = reviewer.review(
        "飘零叶 Tumble Leaf",
        naming.parse_title("飘零叶 Tumble Leaf"),
        [scored],
        {"themoviedb:65047:tv": 95},
    )
    assert result.accepted is True


def test_ai_reviewer_review_payload_includes_semantic_fields_and_bounds():
    long_text = "长文本" * 80
    long_alias = "别名" + ("A" * 180)
    candidate_records = [
        naming.ScoredCandidate(
            candidate=naming.MetadataCandidate(
                key="themoviedb:65047:tv",
                source="S" * 80,
                media_id="65047",
                media_type=("M" * 80),
                title=long_text,
                en_title=long_text,
                original_title=long_text,
                aliases=(
                    "Alpha",
                    "",
                    "Alpha",
                    "Beta",
                    long_alias,
                    long_alias,
                    "Gamma",
                    "Delta",
                    "Epsilon",
                ),
                year=2013,
                matched_query=long_text,
                query_origin="O" * 80,
            ),
            score=95,
            reason_codes=(
                "good" * 17,
                "x",
                "y",
                "z",
                "a",
                "b",
                "c",
                "d",
                "e",
                "f",
            ),
        ),
        naming.ScoredCandidate(
            candidate=naming.MetadataCandidate(
                key="themoviedb:65048:tv",
                source="douban",
                media_id="65048",
                media_type="tv",
                title="B",
                year=2013,
            ),
            score=90,
            reason_codes=("a",),
        ),
        naming.ScoredCandidate(
            candidate=naming.MetadataCandidate(
                key="themoviedb:65049:tv",
                source="douban",
                media_id="65049",
                media_type="tv",
                title="C",
                year=2013,
            ),
            score=90,
            reason_codes=("b",),
        ),
        naming.ScoredCandidate(
            candidate=naming.MetadataCandidate(
                key="themoviedb:65050:tv",
                source="douban",
                media_id="65050",
                media_type="tv",
                title="D",
                year=2013,
            ),
            score=90,
            reason_codes=("c",),
        ),
        naming.ScoredCandidate(
            candidate=naming.MetadataCandidate(
                key="themoviedb:65051:tv",
                source="douban",
                media_id="65051",
                media_type="tv",
                title="E",
                year=2013,
            ),
            score=90,
            reason_codes=("d",),
        ),
        naming.ScoredCandidate(
            candidate=naming.MetadataCandidate(
                key="themoviedb:65052:tv",
                source="douban",
                media_id="65052",
                media_type="tv",
                title="F",
                year=2013,
            ),
            score=90,
            reason_codes=("e",),
        ),
    ]

    captured: list[dict[str, Any]] = []

    def invoke(payload: dict[str, Any]):
        captured.append(payload)
        return {
            "decision": "choose",
            "candidate_key": "themoviedb:65047:tv",
            "confidence": 0.94,
            "reason_codes": ["good"],
        }

    reviewer = MoviePilotAIReviewer(invoke_fn=invoke)
    result = reviewer.review(
        "飘零叶 Tumble Leaf",
        naming.parse_title("飘零叶 Tumble Leaf"),
        candidate_records,
        {record.candidate.key: 95 for record in candidate_records},
    )
    assert result.accepted is True
    assert len(captured) == 1
    payload = captured[0]
    assert len(payload["candidates"]) == 5
    assert payload["candidates"][0]["key"] == "themoviedb:65047:tv"
    assert payload["candidates"][0]["score"] == 95
    assert tuple(item["key"] for item in payload["candidates"]) == (
        "themoviedb:65047:tv",
        "themoviedb:65048:tv",
        "themoviedb:65049:tv",
        "themoviedb:65050:tv",
        "themoviedb:65051:tv",
    )
    assert payload["candidates"][0]["source"] == "S" * 64
    assert payload["candidates"][0]["media_type"] == "M" * 64
    assert payload["candidates"][0]["title"] == long_text[:160]
    assert payload["candidates"][0]["en_title"] == long_text[:160]
    assert payload["candidates"][0]["original_title"] == long_text[:160]
    assert payload["candidates"][0]["aliases"] == (
        "Alpha",
        "Beta",
        long_alias[:160],
        "Gamma",
        "Delta",
    )
    assert payload["candidates"][0]["year"] == 2013
    assert payload["candidates"][0]["matched_query"] == long_text[:160]
    assert payload["candidates"][0]["query_origin"] == "O" * 64
    assert payload["candidates"][0]["reason_codes"] == (
        ("good" * 17)[:64],
        "x",
        "y",
        "z",
        "a",
        "b",
        "c",
        "d",
    )
    assert len(payload["candidates"][0]["reason_codes"]) == 8
    assert "detail_link" not in payload["candidates"][0]
    assert "media_id" not in payload["candidates"][0]


def test_ai_reviewer_rejects_choice_not_in_presented_candidates():
    scored_candidates = []
    for i in range(6):
        candidate = naming.MetadataCandidate(
            key=f"themoviedb:{65040+i}:tv",
            source="themoviedb",
            media_id=str(65040 + i),
            media_type="tv",
            title=f"候选{i}",
            year=2013,
        )
        scored_candidates.append(
            naming.ScoredCandidate(candidate=candidate, score=90 + i, reason_codes=(f"r{i}",))
        )

    captured: list[dict[str, Any]] = []

    def invoke(payload: dict[str, Any]):
        captured.append(payload)
        return {
            "decision": "choose",
            "candidate_key": scored_candidates[-1].candidate.key,
            "confidence": 0.95,
            "reason_codes": ["late"],
        }

    reviewer = MoviePilotAIReviewer(invoke_fn=invoke, timeout_seconds=1)
    result = reviewer.review(
        "课程A",
        naming.parse_title("课程A"),
        scored_candidates,
        {record.candidate.key: 95 for record in scored_candidates},
    )
    assert result.accepted is False
    assert result.decision == "local"
    assert result.error == "candidate_not_whitelisted"
    assert result.reason_codes == ("candidate_not_whitelisted",)
    assert len(captured) == 1
    assert len(captured[0]["candidates"]) == 5
    assert scored_candidates[-1].candidate.key not in {
        item["key"] for item in captured[0]["candidates"]
    }


def test_resolver_rejects_invalid_ai_result_choices(monkeypatch):
    class FakeProvider:
        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:1:tv",
                        source="themoviedb",
                        media_id="1",
                        media_type="tv",
                        title="可信候选",
                        year=2013,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    class LyingReviewer:
        def review(self, raw_title, hints, candidates, score_lookup):
            return providers.AIReviewResult(
                accepted=True,
                decision="choose",
                candidate_key="nonexistent",
                confidence=0.4,
                reason_codes=("bad",),
                error="",
            )

    def fake_evaluate_candidates(hints, directory, candidates, auto_threshold=90, min_margin=12):
        return naming.MatchEvaluation(
            status="review",
            top=naming.ScoredCandidate(
                candidate=naming.MetadataCandidate(
                    key="themoviedb:1:tv",
                    source="themoviedb",
                    media_id="1",
                    media_type="tv",
                    title="可信候选",
                    year=2013,
                ),
                score=90,
                reason_codes=("review",),
            ),
            eligible=(
                naming.ScoredCandidate(
                    candidate=naming.MetadataCandidate(
                        key="themoviedb:1:tv",
                        source="themoviedb",
                        media_id="1",
                        media_type="tv",
                        title="可信候选",
                        year=2013,
                    ),
                    score=90,
                    reason_codes=("review",),
                ),
            ),
            margin=12,
            reason_codes=("review_needed",),
        )

    resolver = SmartNamingResolver(
        load_data=lambda key, default=None: {},
        save_data=lambda key, value: None,
        provider=FakeProvider(),
        ai_reviewer=LyingReviewer(),
    )

    _patch_resolver_evaluate_candidates(monkeypatch, fake_evaluate_candidates)
    decision = resolver.resolve(
        "课程A",
        naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False),
        NamingConfig(mode="apply", ai_review=True),
    )
    assert decision.status == "local_fallback"
def test_resolver_uses_cache_without_new_network_searches(tmp_path):
    class SpyProvider:
        def __init__(self):
            self.calls = []

        def resolve_sources(self, requested):
            return requested

        def search(self, queries, sources):
            self.calls.append((tuple(q.text for q in queries), tuple(sources)))
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:65047:tv",
                        source="themoviedb",
                        media_id="65047",
                        media_type="tv",
                        title="Tumble Leaf",
                        year=2013,
                        aliases=("飘零叶",),
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    provider = SpyProvider()
    store = {}

    def load_data(key, default=None):
        return store.get(key, default)

    def save_data(key, value):
        store[key] = value

    resolver = SmartNamingResolver(load_data=load_data, save_data=save_data, provider=provider, clock=lambda: 10)
    config = NamingConfig(
        mode="apply",
        sources=("themoviedb", "douban"),
        auto_threshold=90,
        min_margin=12,
        uncertain_policy="local",
        append_tmdb_id=False,
        ai_review=False,
        manual_overrides="",
    )

    d1 = naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False)
    first = resolver.resolve("《飘零叶 Tumble Leaf》课程", d1, config)
    second = resolver.resolve("《飘零叶 Tumble Leaf》课程", d1, config)
    assert first.status == "auto_external"
    assert second.status == "auto_external"
    assert len(provider.calls) == 1
    assert provider.calls[0][1] == ("themoviedb", "douban")


def test_resolver_forces_review_to_local_when_ai_disabled_and_no_uncertain_review(monkeypatch):
    class SpyProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            self.calls += 1
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:65047:tv",
                        source="themoviedb",
                        media_id="65047",
                        media_type="tv",
                        title="Tumble Leaf",
                        year=2013,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    resolver = SmartNamingResolver(
        load_data=lambda key, default=None: {},
        save_data=lambda key, value: None,
        provider=SpyProvider(),
    )

    def fake_evaluate_candidates(hints, directory, candidates, auto_threshold=90, min_margin=12):
        return naming.MatchEvaluation(
            status="review",
            top=naming.ScoredCandidate(
                candidate=naming.MetadataCandidate(
                    key="themoviedb:65047:tv",
                    source="themoviedb",
                    media_id="65047",
                    media_type="tv",
                    title="Tumble Leaf",
                    year=2013,
                ),
                score=92,
                reason_codes=("review",),
            ),
            eligible=(),
            margin=10,
            reason_codes=("needs_ai",),
        )

    _patch_resolver_evaluate_candidates(monkeypatch, fake_evaluate_candidates)

    first = resolver.resolve(
        "课程A",
        naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False),
        NamingConfig(mode="apply", uncertain_policy="local"),
    )
    assert first.status == "local_fallback"
    assert first.reason_codes == ("needs_ai", "review")
    assert first.final_root == "课程A"
    assert first.final_prefix == "课程A"

    second = resolver.resolve(
        "课程A",
        naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False),
        NamingConfig(mode="apply", uncertain_policy="hold"),
    )
    assert second.status == "manual_review"


def test_resolver_review_status_is_not_moveable_by_default():
    decision = NamingDecision(
        status="review",
        raw_title="课程A",
        local_title="课程A",
        final_root="课程A",
        final_prefix="课程A",
    )
    assert decision.allowed_to_move is False


def test_resolver_rescores_without_search_on_new_episode_or_season(tmp_path):
    class SpyProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return requested

        def search(self, queries, sources):
            self.calls += 1
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:1:tv",
                        source="themoviedb",
                        media_id="1",
                        media_type="tv",
                        title="Tumble Leaf",
                        year=2013,
                        aliases=("飘零叶",),
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    provider = SpyProvider()
    store = {}

    def load_data(key, default=None):
        return store.get(key, default)

    def save_data(key, value):
        store[key] = value

    resolver = SmartNamingResolver(load_data=load_data, save_data=save_data, provider=provider, clock=lambda: 20)
    config = NamingConfig(mode="apply")

    d1 = naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False)
    d2 = naming.DirectoryHints(media_count=2, seasons=(1, 2), episodic=True)
    resolver.resolve("课程目录", d1, config)
    provider.calls = 0
    resolver.resolve("课程目录", d2, config)
    assert provider.calls == 0


def _build_resolver_with_rows(store: dict):
    def load_data(key, default=None):
        return store.get(key, default)

    def save_data(key, value):
        store[key] = value

    return load_data, save_data


def _fixture_course_organizer(tmp_path, mode="apply"):
    class FakeChain:
        def __init__(self):
            self.calls = []

        def search(self, text, source=None):
            if source == "themoviedb":
                return None, [
                    FakeMediaInfo(
                        "themoviedb",
                        "65047",
                        "tv",
                        "Tumble Leaf",
                        "Tumble Leaf",
                        "",
                        ("Tumble Leaf",),
                        2013,
                        "65047",
                        "",
                        "",
                    )
                ]
            return None, []

    chain = FakeChain()
    provider = MoviePilotMetadataProvider(chain=chain)
    module = Module
    organizer = module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(tmp_path / "incoming"),
            "output": str(tmp_path / "output"),
            "naming_mode": mode,
            "naming_sources": "themoviedb,douban",
            "naming_auto_threshold": 90,
            "naming_min_margin": 12,
            "naming_uncertain_policy": "local",
        },
        metadata_provider=provider,
    )
    return organizer


def test_preview_does_not_move_files(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course = incoming / "《飘零叶 Tumble Leaf》儿童早教动画"
    course.mkdir(parents=True)
    (course / "lesson.mp4").write_bytes(b"x")

    organizer = _fixture_course_organizer(tmp_path, mode="preview")

    assert organizer._process_course(str(course.name), str(course), str(output)) is False
    assert organizer._process_course(str(course.name), str(course), str(output)) is False
    assert not any(output.iterdir())


def test_preview_preserves_exact_filesystem_title_when_nfkc_changes_punctuation(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "少儿数学启蒙动画，《数学荒岛历险记》1-3季合集"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")
    organizer = _fixture_course_organizer(tmp_path, mode="preview")

    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is False

    rows = organizer._get_resolver().preview_rows()
    assert any(row["raw_title"] == course_name for row in rows)
    assert not any(row["raw_title"] == course_name.replace("，", ",") for row in rows)


def test_apply_uses_expected_tumble_leaf_root_and_filename(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "《飘零叶 Tumble Leaf》儿童早教动画"
    course = incoming / course_name
    course.mkdir(parents=True)
    (course / "lesson.mp4").write_bytes(b"x")

    organizer = _fixture_course_organizer(tmp_path, mode="apply")
    def fake_evaluate_candidates(hints, directory, candidates, auto_threshold=90, min_margin=12):
        candidates_tuple = tuple(candidates)
        assert candidates_tuple
        return naming.MatchEvaluation(
            status="auto_external",
            top=naming.ScoredCandidate(
                candidate=candidates_tuple[0],
                score=92,
                reason_codes=("title",),
            ),
            eligible=tuple(
                naming.ScoredCandidate(candidate=item, score=92, reason_codes=("title",))
                for item in candidates_tuple
            ),
            margin=20,
            reason_codes=("title",),
        )

    _patch_resolver_evaluate_candidates(monkeypatch, fake_evaluate_candidates)
    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is True

    assert (output / "Tumble Leaf (2013)" / "Season 1").is_dir()
    assert (output / "Tumble Leaf (2013)" / "Season 1" / "Tumble Leaf - S01E001.mp4").read_bytes() == b"x"


def test_apply_keeps_weak_bear_child_local_fallback_with_top_candidate(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "熊孩子国学故事"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    class WeakProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            self.calls += 1
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:999:tv",
                        source="themoviedb",
                        media_id="999",
                        media_type="tv",
                        title="无关课程",
                        year=2020,
                        aliases=("不是课程",),
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    provider = WeakProvider()

    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "apply",
            "naming_append_tmdb_id": True,
        },
            metadata_provider=provider,
    )

    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert provider.calls == 0
    assert organizer._process_course(course_name, str(course), str(output)) is True
    assert provider.calls == 1
    rows = organizer._get_resolver().preview_rows()
    assert rows[-1]["status"] == "local_fallback"
    assert rows[-1]["final_title"] == course_name
    assert rows[-1]["source"] == "themoviedb"
    assert rows[-1]["media_id"] == "999"
    assert rows[-1]["score"] < 70
    if "candidate_key" in rows[-1]:
        assert rows[-1]["candidate_key"] == "themoviedb:999:tv"
    assert (output / course_name / "Season 1" / f"{course_name} - S01E001.mp4").read_bytes() == b"x"


@pytest.mark.parametrize("raw_confidence", [math.nan, math.inf, -0.1, 1.1, 0.84, "nan"])
def test_ai_reviewer_rejects_invalid_confidence(raw_confidence):
    candidate = naming.MetadataCandidate(
        key="themoviedb:65047:tv",
        source="themoviedb",
        media_id="65047",
        media_type="tv",
        title="Tumble Leaf",
        year=2013,
    )
    scored = naming.ScoredCandidate(candidate=candidate, score=95, reason_codes=("x",))

    def invoke(payload: str):
        return {
            "decision": "choose",
            "candidate_key": "themoviedb:65047:tv",
            "confidence": raw_confidence,
            "reason_codes": ["ai"],
        }

    reviewer = MoviePilotAIReviewer(invoke_fn=invoke)
    result = reviewer.review(
        "飘零叶 Tumble Leaf",
        naming.parse_title("飘零叶 Tumble Leaf"),
        [scored],
        {"themoviedb:65047:tv": 95},
    )
    assert result.accepted is False
    assert result.decision == "local"


def test_resolver_rejects_invalid_ai_confidence(monkeypatch):
    class SpyProvider:
        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:1:tv",
                        source="themoviedb",
                        media_id="1",
                        media_type="tv",
                        title="课程A",
                        year=2020,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    class LyingReviewer:
        def review(self, raw_title, hints, candidates, score_lookup):
            return providers.AIReviewResult(
                accepted=True,
                decision="choose",
                candidate_key="themoviedb:1:tv",
                confidence=math.inf,
                reason_codes=("bad",),
                error="",
            )

    def fake_evaluate_candidates(hints, directory, candidates, auto_threshold=90, min_margin=12):
        return naming.MatchEvaluation(
            status="review",
            top=naming.ScoredCandidate(
                candidate=naming.MetadataCandidate(
                    key="themoviedb:1:tv",
                    source="themoviedb",
                    media_id="1",
                    media_type="tv",
                    title="课程A",
                    year=2020,
                ),
                score=95,
                reason_codes=("review",),
            ),
            eligible=(
                naming.ScoredCandidate(
                    candidate=naming.MetadataCandidate(
                        key="themoviedb:1:tv",
                        source="themoviedb",
                        media_id="1",
                        media_type="tv",
                        title="课程A",
                        year=2020,
                    ),
                    score=95,
                    reason_codes=("review",),
                ),
            ),
            margin=10,
            reason_codes=("review_needed",),
        )

    resolver = SmartNamingResolver(
        load_data=lambda key, default=None: {},
        save_data=lambda key, value: None,
        provider=SpyProvider(),
        ai_reviewer=LyingReviewer(),
    )

    _patch_resolver_evaluate_candidates(monkeypatch, fake_evaluate_candidates)
    decision = resolver.resolve(
        "课程A",
        naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False),
        NamingConfig(mode="apply", ai_review=True),
    )
    assert decision.status == "local_fallback"
    assert decision.final_root == "课程A"
    assert decision.final_prefix == "课程A"


def test_query_override_blocks_move_until_candidate_or_local(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "课程A"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    class DummyProvider:
        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:1:tv",
                        source="themoviedb",
                        media_id="1",
                        media_type="tv",
                        title="课程A",
                        year=2020,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "apply",
            "naming_manual_overrides": f"{course_name} => query:课程A\n{course_name} => candidate:invalid",
        },
        metadata_provider=DummyProvider(),
    )

    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert not (output / "课程A" / "Season 1").exists()


def test_ignore_override_skips_course(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "课程A"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "apply",
            "naming_manual_overrides": f"{course_name} => ignore",
        }
    )

    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert not any(output.iterdir())
    assert "naming_identity_v1" not in organizer._data
    assert organizer._get_resolver().preview_rows()[-1]["status"] == "ignore"


def test_local_override_applies_exact_title(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "课程A"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "apply",
            "naming_manual_overrides": f"{course_name} => local:课程本地名",
        }
    )

    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is True
    assert (output / "课程本地名" / "Season 1" / "课程本地名 - S01E001.mp4").exists()


def test_provider_normalizes_themoviedb_media_type_and_raw_电视剧():
    provider = MoviePilotMetadataProvider(chain=FakeChain(
        {
            "themoviedb": [
                FakeMediaInfo(
                    "themoviedb",
                    "65047",
                    type("MediaType", (), {"to_agent": lambda self: "电视剧"})(),
                    "Tumble Leaf",
                    "Tumble Leaf",
                    "",
                    (),
                    2013,
                    "65047",
                    "",
                    "",
                )
            ]
        }
    ))
    candidate = provider._from_media_info(
        FakeMediaInfo(
            "themoviedb",
            "65047",
            "电视剧",
            "Tumble Leaf",
            "Tumble Leaf",
            "",
            (),
            2013,
            "65047",
            "",
            "",
        ),
        "themoviedb",
        naming.QueryCandidate("Tumble Leaf", "manual"),
        0,
    )
    assert candidate is not None
    assert candidate.key == "themoviedb:65047:tv"
    assert candidate.media_type == "tv"


def _run_with_ai_loop(monkeypatch: pytest.MonkeyPatch):
    captured = {"llm_calls": 0, "with_structured_output": 0, "with_retry": 0}

    class FakeStructuredChain:
        def __init__(self):
            self.retry_attempts = 0

        def with_retry(self, stop_after_attempt):
            captured["with_retry"] += 1
            self.retry_attempts = stop_after_attempt
            return self

        async def ainvoke(self, payload):
            return json.dumps(
                {
                    "decision": "choose",
                    "candidate_key": "themoviedb:65047:tv",
                    "confidence": 0.95,
                    "reason_codes": ["ai"],
                }
            )

    class FakeLLM:
        def with_structured_output(self, *_args, **_kwargs):
            captured["with_structured_output"] += 1
            return FakeStructuredChain()

    class FakeLLMHelper:
        @classmethod
        async def get_llm(cls, streaming=False):
            captured["llm_calls"] += 1
            assert streaming is False
            return FakeLLM()

    class FakePrompt:
        def __or__(self, other):
            return other

    class FakeChatPromptTemplate:
        @classmethod
        def from_template(cls, _template):
            return FakePrompt()

    module = types.ModuleType("langchain_core")
    prompts = types.ModuleType("langchain_core.prompts")
    prompts.ChatPromptTemplate = FakeChatPromptTemplate
    module.prompts = prompts

    monkeypatch.setitem(sys.modules, "langchain_core", module)
    monkeypatch.setitem(sys.modules, "langchain_core.prompts", prompts)
    monkeypatch.setattr(providers, "LLMHelper", FakeLLMHelper)
    monkeypatch.setattr(providers, "settings", type("Settings", (), {"AI_AGENT_ENABLE": True})())
    return captured


def test_ai_reviewer_async_classmethod_llm_and_running_loop(monkeypatch: pytest.MonkeyPatch):
    captured = _run_with_ai_loop(monkeypatch)

    async def run_review():
        reviewer = MoviePilotAIReviewer(timeout_seconds=1)
        hints = naming.TitleHints(
            raw_title="飘零叶 Tumble Leaf",
            local_title="飘零叶",
            year=None,
            season_hints=(),
            query_candidates=(naming.QueryCandidate("Tumble Leaf", "manual"),),
            reason_codes=(),
        )
        return reviewer.review(
            "飘零叶 Tumble Leaf",
            hints,
            [
                naming.ScoredCandidate(
                    candidate=naming.MetadataCandidate(
                        key="themoviedb:65047:tv",
                        source="themoviedb",
                        media_id="65047",
                        media_type="tv",
                        title="Tumble Leaf",
                        year=2013,
                    ),
                    score=95,
                    reason_codes=(),
                )
            ],
            {"themoviedb:65047:tv": 95},
        )

    result = asyncio.run(run_review())
    assert result.accepted is True
    assert captured["llm_calls"] == 1
    assert captured["with_structured_output"] == 1
    assert captured["with_retry"] == 1


def test_ai_reviewer_respects_ai_agent_disable(monkeypatch):
    captured = {"llm_calls": 0}

    class FakeLLMHelper:
        @classmethod
        async def get_llm(cls, streaming=False):
            captured["llm_calls"] += 1

    monkeypatch.setattr(providers, "LLMHelper", FakeLLMHelper)
    monkeypatch.setattr(providers, "settings", type("Settings", (), {"AI_AGENT_ENABLE": False})())

    reviewer = MoviePilotAIReviewer(timeout_seconds=1)
    hints = naming.TitleHints(
        raw_title="X",
        local_title="X",
        year=None,
        season_hints=(),
        query_candidates=(naming.QueryCandidate("X", "manual"),),
        reason_codes=(),
    )
    result = reviewer.review(
        "X",
        hints,
        [],
        {},
    )
    assert captured["llm_calls"] == 0
    assert result.accepted is False


def test_identity_reuse_external_after_30_days_and_schema_invalidation(tmp_path):
    class SpyProvider:
        PROVIDER_SCHEMA_VERSION = "1"

        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            self.calls += 1
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:65047:tv",
                        source="themoviedb",
                        media_id="65047",
                        media_type="tv",
                        title="Tumble Leaf",
                        year=2013,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    provider = SpyProvider()
    store = {}
    timepoints = {"now": 0}

    def load_data(key, default=None):
        return store.get(key, default)

    def save_data(key, value):
        store[key] = value

    resolver = SmartNamingResolver(
        load_data=load_data,
        save_data=save_data,
        provider=provider,
        clock=lambda: timepoints["now"],
    )
    config = NamingConfig(mode="apply")
    directory = naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False)

    first = resolver.resolve("飘零叶 Tumble Leaf", directory, config)
    assert first.status == "auto_external"
    assert provider.calls == 1

    timepoints["now"] = 31 * 24 * 60 * 60
    second = resolver.resolve("飘零叶 Tumble Leaf", directory, config)
    assert second.status == "auto_external"
    assert provider.calls == 1

    third = resolver.resolve(
        "飘零叶 Tumble Leaf",
        directory,
        NamingConfig(mode="apply", auto_threshold=60),
    )
    assert third.status == "auto_external"
    assert provider.calls == 1

    provider_schema = SpyProvider.PROVIDER_SCHEMA_VERSION
    SpyProvider.PROVIDER_SCHEMA_VERSION = "2"
    fourth = resolver.resolve("飘零叶 Tumble Leaf", directory, config)
    assert fourth.status == "auto_external"
    assert provider.calls == 2
    SpyProvider.PROVIDER_SCHEMA_VERSION = provider_schema


def test_identity_reuse_local_with_no_candidates_without_new_search(tmp_path):
    class SpyProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            self.calls += 1
            return ProviderSearchResult(
                candidates=(),
                errors=("none",),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    provider = SpyProvider()
    store = {}
    timepoints = {"now": 0}

    def load_data(key, default=None):
        return store.get(key, default)

    def save_data(key, value):
        store[key] = value

    resolver = SmartNamingResolver(
        load_data=load_data,
        save_data=save_data,
        provider=provider,
        clock=lambda: timepoints["now"],
    )
    directory = naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False)

    first = resolver.resolve("课程A", directory, NamingConfig(mode="apply"))
    assert first.status == "local_fallback"
    assert provider.calls == 1

    timepoints["now"] = 31 * 24 * 60 * 60
    second = resolver.resolve("课程A", directory, NamingConfig(mode="apply"))
    assert second.status == "local_fallback"
    assert provider.calls == 1


def test_identity_invalidated_by_query_or_source_change():
    class SpyProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return requested

        def search(self, queries, sources):
            self.calls += 1
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:65047:tv",
                        source="themoviedb",
                        media_id="65047",
                        media_type="tv",
                        title="Tumble Leaf",
                        year=2013,
                    ),
                ),
                errors=(),
                attempted_sources=sources,
                all_failed=False,
            )

    provider = SpyProvider()
    store = {}

    def load_data(key, default=None):
        return store.get(key, default)

    def save_data(key, value):
        store[key] = value

    resolver_obj = SmartNamingResolver(
        load_data=load_data,
        save_data=save_data,
        provider=provider,
    )
    directory = naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False)
    first = resolver_obj.resolve("课程A", directory, NamingConfig(mode="apply", sources=("themoviedb",), auto_threshold=70))
    assert provider.calls == 1

    _ = resolver_obj.resolve("课程A", directory, NamingConfig(mode="apply", sources=("douban",), auto_threshold=70))
    assert provider.calls == 2

    _ = resolver_obj.resolve("课程A", directory, NamingConfig(mode="apply", sources=("douban",), auto_threshold=60))
    assert provider.calls == 2

    prev_parser_schema = naming.PARSER_SCHEMA_VERSION
    try:
        naming.PARSER_SCHEMA_VERSION = "2"
        _ = resolver_obj.resolve(
            "课程A",
            directory,
            NamingConfig(mode="apply", sources=("douban",), auto_threshold=70),
        )
    finally:
        naming.PARSER_SCHEMA_VERSION = prev_parser_schema
    assert provider.calls == 3


def test_all_search_failed_retries_after_one_hour():
    class FailingProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return requested

        def search(self, queries, sources):
            self.calls += 1
            return ProviderSearchResult((), ("boom",), tuple(sources), all_failed=True)

    provider = FailingProvider()
    store = {}
    timepoints = {"now": 0}

    def load_data(key, default=None):
        return store.get(key, default)

    def save_data(key, value):
        store[key] = value

    resolver = SmartNamingResolver(
        load_data=load_data,
        save_data=save_data,
        provider=provider,
        clock=lambda: timepoints["now"],
    )
    directory = naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False)

    first = resolver.resolve("课程A", directory, NamingConfig(mode="apply"))
    assert first.status in {"manual_review", "local_fallback"}
    assert provider.calls == 1

    timepoints["now"] = 30 * 60
    _ = resolver.resolve("课程A", directory, NamingConfig(mode="apply"))
    assert provider.calls == 1

    timepoints["now"] = 61 * 60
    _ = resolver.resolve("课程A", directory, NamingConfig(mode="apply"))
    assert provider.calls == 2


def test_parse_manual_ignore_without_colon_is_valid():
    result = naming.parse_manual_overrides("课程A => ignore")
    assert result.errors == ()
    assert len(result.overrides) == 1
    assert result.overrides[0].action == "ignore"


def test_query_override_integration_records_cache_and_blocks_move(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "课程A"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    class QueryProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            self.calls += 1
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:1:tv",
                        source="themoviedb",
                        media_id="1",
                        media_type="tv",
                        title="课程A",
                        year=2020,
                        aliases=("课程A",),
                    ),
                    naming.MetadataCandidate(
                        key="themoviedb:2:tv",
                        source="themoviedb",
                        media_id="2",
                        media_type="tv",
                        title="课程B",
                        year=2021,
                        aliases=("课程B",),
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    provider = QueryProvider()
    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "apply",
            "naming_manual_overrides": f"{course_name} => query:课程A",
        },
        metadata_provider=provider,
    )

    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert not (output / course_name / "Season 1").exists()
    assert organizer._get_resolver().preview_rows()[0]["status"] == "query_blocked"
    assert organizer._get_resolver().preview_rows()[0]["source"] == "themoviedb"
    assert organizer._get_resolver().preview_rows()[0]["media_id"] == "1"
    assert organizer._get_resolver().preview_rows()[0]["score"] > 0
    assert provider.calls == 1

    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert provider.calls == 1
    assert organizer._get_resolver().preview_rows()[0]["status"] == "query_blocked"


def test_query_override_to_candidate_reuses_previous_query_cache_without_requery(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "课程A"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    class CachedQueryProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            self.calls += 1
            texts = tuple(query.text for query in queries)
            if "课程A 精确查询" in texts:
                return ProviderSearchResult(
                    candidates=(
                        naming.MetadataCandidate(
                            key="themoviedb:1:tv",
                            source="themoviedb",
                            media_id="1",
                            media_type="tv",
                            title="课程A",
                            year=2020,
                        ),
                    ),
                    errors=(),
                    attempted_sources=("themoviedb",),
                    all_failed=False,
                )
            return ProviderSearchResult(
                candidates=(),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    provider = CachedQueryProvider()
    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "apply",
            "naming_manual_overrides": f"{course_name} => query:课程A 精确查询",
        },
        metadata_provider=provider,
    )

    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert provider.calls == 1

    organizer._persist_config(
        {
            **organizer._get_config(),
            "naming_manual_overrides": f"{course_name} => candidate:themoviedb:1:tv",
        }
    )
    assert organizer._process_course(course_name, str(course), str(output)) is True
    assert provider.calls == 1
    assert (output / "课程A (2020)" / "Season 1" / "课程A - S01E001.mp4").exists()


def test_apply_blocks_move_when_review_without_ai_hold(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "课程A"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    class HoldProvider:
        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:1:tv",
                        source="themoviedb",
                        media_id="1",
                        media_type="tv",
                        title="课程A",
                        year=2020,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "apply",
            "naming_uncertain_policy": "hold",
            "naming_ai_review": False,
        },
        metadata_provider=HoldProvider(),
    )

    def fake_evaluate_candidates(hints, directory, candidates, auto_threshold=90, min_margin=12):
        return naming.MatchEvaluation(
            status="review",
            top=naming.ScoredCandidate(
                candidate=naming.MetadataCandidate(
                    key="themoviedb:1:tv",
                    source="themoviedb",
                    media_id="1",
                    media_type="tv",
                    title="课程A",
                    year=2020,
                ),
                score=90,
                reason_codes=("review",),
            ),
            eligible=(),
            margin=0,
            reason_codes=("review",),
        )

    _patch_resolver_evaluate_candidates(monkeypatch, fake_evaluate_candidates)
    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert not (output / course_name / "Season 1").exists()
    row = organizer._get_resolver().preview_rows()[-1]
    assert row["status"] == "manual_review"


def test_apply_moves_local_fallback_without_ai_review_to_local_root(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "课程B"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    class LocalFallbackProvider:
        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:1:tv",
                        source="themoviedb",
                        media_id="1",
                        media_type="tv",
                        title="其他标题",
                        year=2020,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "apply",
            "naming_uncertain_policy": "local",
            "naming_ai_review": False,
        },
        metadata_provider=LocalFallbackProvider(),
    )

    def fake_evaluate_candidates(hints, directory, candidates, auto_threshold=90, min_margin=12):
        return naming.MatchEvaluation(
            status="review",
            top=naming.ScoredCandidate(
                candidate=naming.MetadataCandidate(
                    key="themoviedb:1:tv",
                    source="themoviedb",
                    media_id="1",
                    media_type="tv",
                    title="其他标题",
                    year=2020,
                ),
                score=90,
                reason_codes=("review",),
            ),
            eligible=(),
            margin=0,
            reason_codes=("review",),
        )

    _patch_resolver_evaluate_candidates(monkeypatch, fake_evaluate_candidates)
    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is True
    assert (output / course_name / "Season 1" / "课程B - S01E001.mp4").exists()
    row = organizer._get_resolver().preview_rows()[-1]
    assert row["status"] == "local_fallback"
    assert row["final_title"] == course_name


def test_stale_manual_local_identity_is_not_reused_after_override_removed():
    class SpyProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            self.calls += 1
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:65047:tv",
                        source="themoviedb",
                        media_id="65047",
                        media_type="movie",
                        title="课程A",
                        year=2013,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    class ResolverStore:
        def __init__(self):
            self.value = {}

        def load(self, key, default=None):
            return self.value.get(key, default)

        def save(self, key, value):
            self.value[key] = value

    store = ResolverStore()

    provider = SpyProvider()
    resolver = SmartNamingResolver(
        load_data=store.load,
        save_data=store.save,
        provider=provider,
    )

    resolver.resolve(
        "课程A",
        naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False),
        NamingConfig(mode="apply", manual_overrides="课程A => local:旧覆盖名"),
    )

    retry = resolver.resolve(
        "课程A",
        naming.DirectoryHints(media_count=1, seasons=(1,), episodic=True),
        NamingConfig(mode="apply"),
    )
    assert provider.calls >= 1
    assert retry.final_root != "旧覆盖名"
    identity = store.load("naming_identity_v1", {}).get("课程A")
    assert identity is not None
    assert identity.get("manual_local") is False


def test_append_tmdb_id_applies_to_all_tmdb_external_paths(monkeypatch):
    class CandidateProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            self.calls += 1
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:1:movie",
                        source="themoviedb",
                        media_id="1",
                        media_type="movie",
                        title="课程甲",
                        year=2013,
                    ),
                    naming.MetadataCandidate(
                        key="themoviedb:2:movie",
                        source="themoviedb",
                        media_id="2",
                        media_type="movie",
                        title="课程乙",
                        year=2013,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    directory = naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False)

    def evaluate_auto(hints, directory, candidates, auto_threshold=90, min_margin=12):
        candidate_by_key = {item.key: item for item in candidates}
        return naming.MatchEvaluation(
            status="auto_external",
            top=naming.ScoredCandidate(
                candidate=candidate_by_key["themoviedb:1:movie"],
                score=88,
                reason_codes=("title_match",),
            ),
            eligible=(
                naming.ScoredCandidate(
                    candidate=candidate_by_key["themoviedb:1:movie"],
                    score=88,
                    reason_codes=("title_match",),
                ),
                naming.ScoredCandidate(
                    candidate=candidate_by_key["themoviedb:2:movie"],
                    score=82,
                    reason_codes=("title_match",),
                ),
            ),
            margin=20,
            reason_codes=("title_match",),
        )

    def evaluate_review(hints, directory, candidates, auto_threshold=90, min_margin=12):
        candidate_by_key = {item.key: item for item in candidates}
        return naming.MatchEvaluation(
            status="review",
            top=naming.ScoredCandidate(
                candidate=candidate_by_key["themoviedb:1:movie"],
                score=90,
                reason_codes=("title_match",),
            ),
            eligible=(
                naming.ScoredCandidate(
                    candidate=candidate_by_key["themoviedb:1:movie"],
                    score=90,
                    reason_codes=("title_match",),
                ),
                naming.ScoredCandidate(
                    candidate=candidate_by_key["themoviedb:2:movie"],
                    score=82,
                    reason_codes=("title_match",),
                ),
            ),
            margin=20,
            reason_codes=("title_match",),
        )

    auto_store = {}

    def auto_load_data(key, default=None):
        return auto_store.get(key, default)

    def auto_save_data(key, value):
        auto_store[key] = value

    auto_resolver = SmartNamingResolver(
        load_data=auto_load_data,
        save_data=auto_save_data,
        provider=CandidateProvider(),
    )

    class LowConfidenceReview:
        def review(self, raw_title, hints, candidates, score_lookup):
            return providers.AIReviewResult(
                accepted=True,
                decision="choose",
                candidate_key="themoviedb:2:movie",
                confidence=0.95,
                reason_codes=("ai",),
                error="",
            )

    review_store = {}

    def review_load_data(key, default=None):
        return review_store.get(key, default)

    def review_save_data(key, value):
        review_store[key] = value

    reviewed_resolver = SmartNamingResolver(
        load_data=review_load_data,
        save_data=review_save_data,
        provider=CandidateProvider(),
        ai_reviewer=LowConfidenceReview(),
    )

    _patch_resolver_evaluate_candidates(monkeypatch, evaluate_auto)

    first = auto_resolver.resolve(
        "课程甲",
        directory,
        NamingConfig(
            mode="apply",
            auto_threshold=70,
            min_margin=12,
            append_tmdb_id=True,
        ),
    )
    assert first.status == "auto_external"
    assert first.final_root == "课程甲 (2013) [tmdbid-1]"
    assert first.final_prefix == "课程甲"

    second = auto_resolver.resolve(
        "课程甲",
        directory,
        NamingConfig(mode="apply", auto_threshold=70, min_margin=12, append_tmdb_id=False),
    )
    assert second.status == "auto_external"
    assert second.final_root == "课程甲 (2013)"
    assert second.final_prefix == "课程甲"

    _patch_resolver_evaluate_candidates(monkeypatch, evaluate_review)
    reviewed = reviewed_resolver.resolve(
        "课程甲",
        directory,
        NamingConfig(
            mode="apply",
            ai_review=True,
            auto_threshold=70,
            min_margin=12,
            append_tmdb_id=True,
        ),
    )
    assert reviewed.status == "auto_external"
    assert reviewed.candidate_key == "themoviedb:2:movie"
    assert reviewed.final_root == "课程乙 (2013) [tmdbid-2]"
    assert reviewed.final_prefix == "课程乙"
    assert reviewed.score == 82

    reviewed_calls_before = reviewed_resolver._provider.calls
    _patch_resolver_evaluate_candidates(monkeypatch, evaluate_auto)
    manual = reviewed_resolver.resolve(
        "课程甲",
        directory,
        NamingConfig(
            mode="apply",
            auto_threshold=70,
            min_margin=12,
            manual_overrides="课程甲 => candidate:themoviedb:1:movie",
            append_tmdb_id=True,
        ),
    )
    assert manual.status == "auto_external"
    assert manual.final_root == "课程甲 (2013) [tmdbid-1]"
    assert manual.final_prefix == "课程甲"
    assert reviewed_resolver._provider.calls == reviewed_calls_before


def test_normalize_config_booleans_and_numeric_defaults():
    organizer = Module.CourseOrganizer(
        config={
            "enabled": "garbage",
            "run_once": "no",
            "incoming": "/custom",
            "output": "/out",
            "interval": "100",
            "naming_mode": "apply",
            "naming_auto_threshold": "abc",
            "naming_min_margin": "abc",
            "naming_append_tmdb_id": "yes",
            "naming_ai_review": "off",
            "naming_clear_cache_once": "maybe",
        },
    )
    cfg = organizer._normalize_config(organizer._get_config())
    assert cfg["incoming"] == "/custom"
    assert cfg["tv_output"] == "/volume1/TV"
    assert cfg["movie_output"] == "/volume1/Movies"
    assert cfg["children_output"] == "/out"
    assert "output" not in cfg
    assert cfg["naming_mode"] == "apply"
    assert cfg["enabled"] is False
    assert cfg["run_once"] is False
    assert cfg["naming_append_tmdb_id"] is True
    assert cfg["naming_ai_review"] is False
    assert cfg["naming_clear_cache_once"] is False
    assert cfg["naming_auto_threshold"] == 90
    assert cfg["naming_min_margin"] == 12


@pytest.mark.parametrize("raw", [{}, None, ["enabled"]])
def test_normalize_config_fail_closed_for_empty_or_non_dict_values(raw):
    organizer = Module.CourseOrganizer(config={})

    cfg = organizer._normalize_config(raw)

    assert cfg["enabled"] is False
    assert cfg["naming_mode"] == "preview"
    assert cfg["run_once"] is False


def test_normalize_config_falls_back_when_get_config_raises():
    class BrokenConfigOrganizer(Module.CourseOrganizer):
        def get_config(self):
            raise RuntimeError("config unavailable")

    organizer = BrokenConfigOrganizer(
        config={"enabled": True, "naming_mode": "apply", "run_once": True}
    )

    cfg = organizer._get_config()

    assert cfg["enabled"] is False
    assert cfg["naming_mode"] == "preview"
    assert cfg["run_once"] is False


def test_normalize_config_preserves_explicit_legacy_values_and_rejects_invalid_values():
    organizer = Module.CourseOrganizer(config={})

    explicit = organizer._normalize_config(
        {"enabled": True, "naming_mode": "off", "run_once": True}
    )
    assert explicit["enabled"] is True
    assert explicit["naming_mode"] == "off"
    assert explicit["run_once"] is True

    partial = organizer._normalize_config({"enabled": True})
    assert partial["enabled"] is True
    assert partial["naming_mode"] == "preview"
    assert partial["run_once"] is False

    invalid = organizer._normalize_config(
        {"enabled": "garbage", "naming_mode": "invalid", "run_once": "garbage"}
    )
    assert invalid["enabled"] is False
    assert invalid["naming_mode"] == "preview"
    assert invalid["run_once"] is False


def test_naming_config_sanitize_preserves_paths_and_defaults():
    cfg = NamingConfig.sanitize(
        {
            "naming_mode": "apply",
            "incoming": "/custom",
            "output": "/out",
            "naming_auto_threshold": "abc",
            "naming_min_margin": "abc",
            "naming_append_tmdb_id": "1",
            "naming_ai_review": "0",
            "naming_uncertain_policy": "hold",
        }
    )
    assert cfg.mode == "apply"
    assert cfg.auto_threshold == 90
    assert cfg.min_margin == 12
    assert cfg.append_tmdb_id is True
    assert cfg.ai_review is False
    assert cfg.uncertain_policy == "hold"

    cfg_invalid = NamingConfig.sanitize(
        {
            "naming_mode": "apply",
            "naming_append_tmdb_id": "garbage",
            "naming_ai_review": "garbage",
            "naming_uncertain_policy": "hold",
        }
    )
    assert cfg_invalid.append_tmdb_id is False
    assert cfg_invalid.ai_review is False


def test_duplicate_or_invalid_rules_only_block_affected_folder(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()

    for course_name in ("FolderA", "FolderB"):
        course_dir = incoming / course_name
        course_dir.mkdir()
        (course_dir / "lesson.mp4").write_bytes(b"x")

    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "apply",
            "naming_manual_overrides": (
                "FolderA => local:FolderA固定\n"
                "FolderA => local:重复\n"
                "FolderB => local:FolderB本地名"
            ),
        }
    )

    assert organizer._process_course("FolderA", str(incoming / "FolderA"), str(output)) is False
    assert not (output / "FolderA固定" / "Season 1").exists()
    assert organizer._process_course("FolderA", str(incoming / "FolderA"), str(output)) is False

    assert organizer._process_course("FolderB", str(incoming / "FolderB"), str(output)) is False
    assert organizer._process_course("FolderB", str(incoming / "FolderB"), str(output)) is True
    assert (output / "FolderB本地名" / "Season 1" / "FolderB本地名 - S01E001.mp4").exists()


def test_global_invalid_rule_shows_row_diagnostics_but_does_not_block_valid_folder(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()

    for course_name in ("FolderA", "FolderB"):
        course_dir = incoming / course_name
        course_dir.mkdir()
        (course_dir / "lesson.mp4").write_bytes(b"x")

    class DummyProvider:
        def resolve_sources(self, requested):
            return ("themoviedb",)

        def search(self, queries, sources):
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:2:tv",
                        source="themoviedb",
                        media_id="2",
                        media_type="tv",
                        title="FolderB",
                        year=2020,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "apply",
            "naming_manual_overrides": "bad rule\nFolderA => local:FolderA固定",
        },
        metadata_provider=DummyProvider(),
    )

    def fake_evaluate_candidates(hints, directory, candidates, auto_threshold=90, min_margin=12):
        candidates_tuple = tuple(candidates)
        assert candidates_tuple
        return naming.MatchEvaluation(
            status="auto_external",
            top=naming.ScoredCandidate(
                candidate=candidates_tuple[0],
                score=90,
                reason_codes=("title",),
            ),
            eligible=tuple(
                naming.ScoredCandidate(candidate=item, score=90, reason_codes=("title",))
                for item in candidates_tuple
            ),
            margin=20,
            reason_codes=("title",),
        )

    _patch_resolver_evaluate_candidates(monkeypatch, fake_evaluate_candidates)

    assert organizer._process_course("FolderA", str(incoming / "FolderA"), str(output)) is False
    assert organizer._process_course("FolderA", str(incoming / "FolderA"), str(output)) is True
    assert (output / "FolderA固定" / "Season 1" / "FolderA固定 - S01E001.mp4").exists()
    rows_a = organizer._get_resolver().preview_rows()
    assert any("invalid_rule:bad rule" in row["reason_codes"] for row in rows_a if row["raw_title"] == "FolderA")

    assert organizer._process_course("FolderB", str(incoming / "FolderB"), str(output)) is False
    assert organizer._process_course("FolderB", str(incoming / "FolderB"), str(output)) is True
    assert (output / "FolderB (2020)" / "Season 1" / "FolderB - S01E001.mp4").exists()
    rows_b = organizer._get_resolver().preview_rows()
    row_b = next(row for row in rows_b if row["raw_title"] == "FolderB")
    assert row_b["status"] in {"auto_external", "review", "local_fallback"}
    assert "invalid_rule:bad rule" in row_b["reason_codes"]
def test_naming_mode_off_calls_no_provider_no_preview_no_cache(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "课程A"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    class OffProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            self.calls += 1
            return ()

        def search(self, queries, sources):
            self.calls += 1
            raise AssertionError("provider should not be used in naming_off mode")

    provider = OffProvider()
    organizer = Module.CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "output": str(output),
            "naming_mode": "off",
        },
        metadata_provider=provider,
    )

    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is True
    assert provider.calls == 0
    assert "naming_preview_v1" not in organizer._data
    assert "naming_search_cache_v1" not in organizer._data
    assert "naming_identity_v1" not in organizer._data


def test_ai_review_preserves_original_hints_selected_score_and_english_title(monkeypatch, tmp_path):
    raw_title = "飘零叶 Tumble Leaf"

    class DummyProvider:
        def __init__(self):
            self.calls = 0

        def resolve_sources(self, requested):
            self.calls = 1
            return ("themoviedb",)

        def search(self, queries, sources):
            return ProviderSearchResult(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:1:tv",
                        source="themoviedb",
                        media_id="1",
                        media_type="tv",
                        title="Tumble Leaf",
                        year=2013,
                    ),
                    naming.MetadataCandidate(
                        key="douban:2:tv",
                        source="douban",
                        media_id="2",
                        media_type="tv",
                        title="课程备选",
                        en_title="Tumble Leaf",
                        original_title="Tumble Leaf",
                        year=2013,
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb", "douban"),
                all_failed=False,
            )

    def invoke(payload):
        return json.dumps(
            {
                "decision": "choose",
                "candidate_key": "douban:2:tv",
                "confidence": 0.95,
                "reason_codes": ["manual"],
            }
        )

    class SpyReviewer(MoviePilotAIReviewer):
        pass

    top = naming.ScoredCandidate(
        candidate=naming.MetadataCandidate(
            key="themoviedb:1:tv",
            source="themoviedb",
            media_id="1",
            media_type="tv",
            title="Tumble Leaf",
            year=2013,
        ),
        score=94,
        reason_codes=("top",),
    )
    selected = naming.ScoredCandidate(
        candidate=naming.MetadataCandidate(
            key="douban:2:tv",
            source="douban",
            media_id="2",
            media_type="tv",
            title="课程备选",
            en_title="Tumble Leaf",
            original_title="Tumble Leaf",
            year=2013,
        ),
        score=82,
        reason_codes=("selected",),
    )

    def fake_evaluate_candidates(hints, directory, candidates, auto_threshold=90, min_margin=12):
        return naming.MatchEvaluation(
            status="review",
            top=top,
            eligible=(top, selected),
            margin=12,
            reason_codes=("review_needed",),
        )

    monkeypatch.setattr(naming, "evaluate_candidates", fake_evaluate_candidates)

    resolver = SmartNamingResolver(
        load_data=lambda key, default=None: {},
        save_data=lambda key, value: None,
        provider=DummyProvider(),
        ai_reviewer=SpyReviewer(invoke_fn=invoke),
    )

    decision = resolver.resolve(raw_title, naming.DirectoryHints(media_count=1, seasons=(1,), episodic=False), NamingConfig(mode="apply", ai_review=True))
    assert decision.status == "auto_external"
    assert decision.candidate_key == "douban:2:tv"
    assert decision.source == "douban"
    assert decision.media_id == "2"
    assert decision.score == 82
    assert decision.final_root == "Tumble Leaf (2013)"


def test_media_info_regional_aliases_from_custom_fields_are_included():
    class RegionalInfo:
        source = "themoviedb"
        media_id = "100"
        type = "movie"
        title = "Main"
        en_title = "Main"
        original_title = ""
        names = ["Alias"]
        year = 2020
        tmdb_id = "100"
        douban_id = ""
        detail_link = ""
        hk_title = "香港名"
        tw_title = "台灣名"
        sg_title = "Singapore名"

    provider = MoviePilotMetadataProvider(chain=FakeChain({}))
    candidate = provider._from_media_info(RegionalInfo(), "themoviedb", naming.QueryCandidate("Main", "manual"), 0)
    assert candidate is not None
    assert "香港名" in candidate.aliases
    assert "台灣名" in candidate.aliases
    assert "Singapore名" in candidate.aliases


def test_preview_and_apply_detect_legacy_output_conflict(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "《飘零叶 Tumble Leaf》儿童早教动画"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    legacy = output / course_name
    legacy.mkdir()

    for mode in ("preview", "apply"):
        organizer = _fixture_course_organizer(tmp_path, mode=mode)
        assert organizer._process_course(course_name, str(course), str(output)) is False
        assert organizer._process_course(course_name, str(course), str(output)) is False
        rows = organizer._get_resolver().preview_rows()
        assert rows[-1]["status"] == "legacy_output_conflict"
        assert rows[-1]["legacy_output_root"] == str(legacy)
        if mode == "apply":
            assert not (output / "Tumble Leaf (2013)" / "Season 1").exists()


def test_get_page_headers_match_preview_row_schema(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course_name = "《飘零叶 Tumble Leaf》儿童早教动画"
    course = incoming / course_name
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"x")

    organizer = _fixture_course_organizer(tmp_path, mode="preview")
    assert organizer._process_course(course_name, str(course), str(output)) is False
    assert organizer._process_course(course_name, str(course), str(output)) is False

    page = organizer.get_page()
    assert page and page[0]["component"] == "VCol"
    assert page[0]["props"]["cols"] == 12
    preview_card = page[0]["content"][0]
    assert preview_card["component"] == "VCard"
    def walk(value):
        if isinstance(value, dict):
            if "component" in value:
                yield value
            yield from walk(value.get("content"))
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(item)

    table = next(component for component in walk(preview_card) if component["component"] == "VDataTableVirtual")
    assert table["component"] == "VDataTableVirtual"
    assert table["props"]["height"] == "min(52vh, 30rem)"
    assert table["props"]["fixed-header"] is True
    assert table["props"]["items"]
    display_keys = {"raw_title", "final_title", "target_position", "status"}
    headers = {item["key"] for item in table["props"]["headers"]}
    rows = organizer._get_resolver().preview_rows()
    assert rows
    assert headers == display_keys
    assert {"target_library", "target_output_root"}.issubset(rows[0])
    assert set(rows[0]) - display_keys
    assert all(set(item) == display_keys for item in table["props"]["items"])
    assert table["props"]["items"] != rows
