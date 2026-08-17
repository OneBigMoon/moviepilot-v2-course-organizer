from pathlib import Path

import pytest

from tests.courseorganizer_testkit import load_courseorganizer


MODULE = load_courseorganizer()
CourseOrganizer = MODULE.CourseOrganizer
NamingDecision = MODULE.resolver.NamingDecision
LibraryRouteResult = MODULE.providers.LibraryRouteResult
MoviePilotLibraryClassifier = MODULE.providers.MoviePilotLibraryClassifier
SmartNamingResolver = MODULE.resolver.SmartNamingResolver


class StaticClassifier:
    def __init__(self, result: LibraryRouteResult) -> None:
        self.result = result
        self.calls = []

    def classify(self, **payload):
        self.calls.append(payload)
        return self.result


def _organizer(tmp_path: Path, classifier: StaticClassifier, mode: str = "apply"):
    roots = {
        "tv": tmp_path / "TV",
        "movie": tmp_path / "Movies",
        "children": tmp_path / "儿童",
    }
    organizer = CourseOrganizer(
        config={
            "enabled": False,
            "incoming": str(tmp_path / "未整理"),
            "tv_output": str(roots["tv"]),
            "movie_output": str(roots["movie"]),
            "children_output": str(roots["children"]),
            "naming_mode": mode,
            "naming_ai_review": True,
        },
        metadata_provider=object(),
        ai_reviewer=object(),
        library_classifier=classifier,
    )
    return organizer, roots


def _stable_course(tmp_path: Path, name: str = "示例媒体") -> Path:
    course = tmp_path / "未整理" / name
    course.mkdir(parents=True)
    (course / "01.mp4").write_bytes(b"media")
    return course


@pytest.mark.parametrize(
    ("library", "media_type"),
    [("tv", "tv"), ("movie", "movie"), ("children", "tv")],
)
def test_apply_routes_to_exact_selected_library(tmp_path, library, media_type):
    classifier = StaticClassifier(
        LibraryRouteResult(True, library, 0.97, ("ai_classified",), "")
    )
    organizer, roots = _organizer(tmp_path, classifier)
    course = _stable_course(tmp_path)
    organizer._resolve_naming = lambda *args, **kwargs: NamingDecision(
        status="auto_external",
        raw_title=course.name,
        local_title=course.name,
        final_root="规范名称",
        final_prefix="规范名称",
        source="themoviedb",
        media_id="1",
        media_type=media_type,
        score=98,
        margin=20,
        candidate_key=f"themoviedb:1:{media_type}",
    )

    assert organizer._process_course(course.name, str(course)) is False
    assert organizer._process_course(course.name, str(course)) is True

    expected = roots[library] / "规范名称" / "Season 1" / "规范名称 - S01E001.mp4"
    assert expected.read_bytes() == b"media"
    assert len(classifier.calls) == 1
    for other, root in roots.items():
        if other != library:
            assert not root.exists()


def test_preview_records_three_library_target_without_moving(tmp_path):
    classifier = StaticClassifier(
        LibraryRouteResult(True, "movie", 0.96, ("movie_audience",), "")
    )
    organizer, roots = _organizer(tmp_path, classifier, mode="preview")
    course = _stable_course(tmp_path)
    organizer._resolve_naming = lambda *args, **kwargs: NamingDecision(
        status="auto_external",
        raw_title=course.name,
        local_title=course.name,
        final_root="规范电影",
        final_prefix="规范电影",
        media_type="movie",
    )

    assert organizer._process_course(course.name, str(course)) is False
    assert organizer._process_course(course.name, str(course)) is False
    assert course.exists()
    assert all(not root.exists() for root in roots.values())
    row = organizer._data["naming_preview_v1"][-1]
    assert row["target_library"] == "movie"
    assert row["target_output_root"] == str(roots["movie"] / "规范电影")


@pytest.mark.parametrize(
    "result",
    [
        LibraryRouteResult(False, "hold", 0.70, ("low_confidence",), ""),
        LibraryRouteResult(False, "hold", 0.0, ("exception",), "llm_not_ready"),
    ],
)
def test_untrusted_or_unavailable_classification_holds_without_creating_targets(tmp_path, result):
    classifier = StaticClassifier(result)
    organizer, roots = _organizer(tmp_path, classifier)
    course = _stable_course(tmp_path)
    organizer._resolve_naming = lambda *args, **kwargs: NamingDecision(
        status="auto_external",
        raw_title=course.name,
        local_title=course.name,
        final_root="不可移动",
        final_prefix="不可移动",
        media_type="tv",
    )

    assert organizer._process_course(course.name, str(course)) is False
    assert organizer._process_course(course.name, str(course)) is False
    assert course.exists()
    assert all(not root.exists() for root in roots.values())
    row = organizer._data["naming_preview_v1"][-1]
    assert row["status"] == "library_hold"
    assert row["target_library"] == "hold"
    assert row["library_confidence"] == pytest.approx(result.confidence)
    assert row["library_reason_codes"] == list(result.reason_codes)


def test_naming_off_never_calls_ai_or_moves(tmp_path, monkeypatch):
    classifier = StaticClassifier(
        LibraryRouteResult(True, "children", 0.99, ("should_not_run",), "")
    )
    organizer, roots = _organizer(tmp_path, classifier, mode="off")
    course = _stable_course(tmp_path)

    monkeypatch.setattr(
        organizer,
        "_get_resolver",
        lambda: (_ for _ in ()).throw(AssertionError("resolver must stay unused in off mode")),
    )

    assert organizer._process_course(course.name, str(course)) is False
    assert organizer._process_course(course.name, str(course)) is False
    assert classifier.calls == []
    assert course.exists()
    assert all(not root.exists() for root in roots.values())
    assert "naming_preview_v1" not in organizer._data


@pytest.mark.parametrize("status", ["ignore", "query_blocked"])
def test_non_movable_naming_statuses_skip_library_classification(tmp_path, status):
    classifier = StaticClassifier(
        LibraryRouteResult(True, "movie", 0.99, ("should_not_run",), "")
    )
    organizer, roots = _organizer(tmp_path, classifier)
    course = _stable_course(tmp_path)
    organizer._resolve_naming = lambda *args, **kwargs: NamingDecision(
        status=status,
        raw_title=course.name,
        local_title=course.name,
        final_root="不可移动",
        final_prefix="不可移动",
    )

    assert organizer._process_course(course.name, str(course)) is False
    assert organizer._process_course(course.name, str(course)) is False

    assert classifier.calls == []
    assert course.exists()
    assert all(not root.exists() for root in roots.values())
    assert organizer._data["naming_preview_v1"][-1]["status"] == status
    assert organizer._data["naming_preview_v1"][-1]["library_confidence"] == 0.0
    assert organizer._data["naming_preview_v1"][-1]["library_reason_codes"] == ["naming_blocked"]
    assert organizer._data["naming_preview_v1"][-1]["target_library"] == "hold"


def test_classifier_rejects_low_confidence_and_metadata_conflict():
    low = MoviePilotLibraryClassifier(
        invoke_fn=lambda payload: {"library": "children", "confidence": 0.84}
    ).classify("标题", "标题", "tv", True)
    conflict = MoviePilotLibraryClassifier(
        invoke_fn=lambda payload: {"library": "tv", "confidence": 0.99}
    ).classify("标题", "标题", "movie", False)

    assert not low.accepted and low.library == "hold"
    assert low.reason_codes == ("low_confidence",)
    assert not conflict.accepted and conflict.library == "hold"
    assert conflict.reason_codes == ("metadata_type_conflict",)


def test_classifier_accepts_children_but_requires_metadata_for_tv_movie():
    children = MoviePilotLibraryClassifier(
        invoke_fn=lambda payload: {"library": "children", "confidence": 0.95}
    ).classify("儿童课程", "儿童课程", "unknown", True)
    unknown_tv = MoviePilotLibraryClassifier(
        invoke_fn=lambda payload: {"library": "tv", "confidence": 0.99}
    ).classify("未知标题", "未知标题", "unknown", True)

    assert children.accepted and children.library == "children"
    assert not unknown_tv.accepted and unknown_tv.library == "hold"
    assert unknown_tv.reason_codes == ("metadata_type_conflict",)


def test_classifier_failure_is_a_hold_not_a_metadata_fallback():
    def unavailable(payload):
        raise RuntimeError("llm_not_ready")

    result = MoviePilotLibraryClassifier(invoke_fn=unavailable, max_attempts=1).classify(
        "电视剧标题", "电视剧标题", "tv", True
    )

    assert not result.accepted
    assert result.library == "hold"
    assert result.reason_codes == ("exception",)
    assert result.error == "llm_not_ready"


def test_naming_decision_and_preview_preserve_metadata_type():
    data = {}
    resolver = SmartNamingResolver(
        load_data=lambda key, default: data.get(key, default),
        save_data=lambda key, value: data.__setitem__(key, value),
        provider=object(),
    )
    hints = MODULE.naming.parse_title("示例剧集")
    candidate = MODULE.naming.MetadataCandidate(
        key="themoviedb:1:tv",
        source="themoviedb",
        media_id="1",
        media_type="tv",
        title="示例剧集",
    )

    decision = resolver._decision(
        status="auto_external",
        raw_title="示例剧集",
        hints=hints,
        candidate=candidate,
        score=98,
    )
    resolver.record_decision(decision, target_library="tv")

    assert decision.media_type == "tv"
    assert data["naming_preview_v1"][-1]["media_type"] == "tv"
def test_classifier_accepts_minimum_confidence_boundary():
    children = MoviePilotLibraryClassifier(
        invoke_fn=lambda payload: {"library": "children", "confidence": 0.85}
    ).classify("儿童课程", "儿童课程", "unknown", True)
    tv = MoviePilotLibraryClassifier(
        invoke_fn=lambda payload: {"library": "tv", "confidence": 0.85}
    ).classify("示例剧", "示例剧", "tv", True)

    assert children.accepted and children.library == "children"
    assert children.reason_codes == ()
    assert tv.accepted and tv.library == "tv"
