import errno
import json
import os
import pytest
import stat
import threading
from pathlib import Path

from tests.courseorganizer_testkit import load_courseorganizer


MODULE = load_courseorganizer()
CourseOrganizer = MODULE.CourseOrganizer


def test_plugin_version_matches_package_metadata():
    package_path = Path(__file__).parents[1] / "package.v2.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))

    assert CourseOrganizer.plugin_version == "1.7.10"
    assert package["CourseOrganizer"]["version"] == CourseOrganizer.plugin_version


def test_normalize_config_defaults_invalid_uncertain_policy_to_local_and_reflects_in_ui_text():
    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "naming_mode": "apply",
            "naming_uncertain_policy": "invalid-policy",
        }
    )
    config = organizer._get_config()

    assert config["naming_uncertain_policy"] == "local"
    page = str(organizer.get_page())
    assert "低置信度时保留本地名" in page
    assert "低置信度时暂停整理" not in page


def test_empty_config_disables_service_and_scheduled_run_without_side_effects(monkeypatch):
    organizer = CourseOrganizer(config={})
    processed = []

    monkeypatch.setattr(organizer, "_process_course", lambda *args, **kwargs: processed.append(args))

    assert organizer.get_service() == []
    organizer._run(force=False)

    assert processed == []


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("1-2.mp4", (1, 2)),
        ("01-02 标题.mp4", (1, 2)),
        ("第1-2集.mp4", (1, 2)),
        ("E01-E02.mp4", (1, 2)),
        ("EP01-02.mp4", (1, 2)),
        ("001.单集.mp4", (1, 1)),
    ],
)
def test_extracts_supported_leading_episode_spans(filename, expected):
    assert CourseOrganizer._extract_leading_episode_span(filename) == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("0-1.mp4", None),
        ("2-1.mp4", None),
        ("1-1001.mp4", None),
        ("2020-2024 课程.mp4", None),
        ("1-2课程.mp4", None),
    ],
)
def test_rejects_invalid_leading_episode_spans(filename, expected):
    assert CourseOrganizer._extract_leading_episode_span(filename) == expected


@pytest.mark.parametrize(
    ("course_name", "expected"),
    [
        (".", "Course"),
        ("..", "Course"),
        (" . ", "Course"),
        ("\t..\t", "Course"),
    ],
)
def test_safe_name_canonicalizes_dot_names(course_name, expected):
    assert CourseOrganizer._safe_name(course_name) == expected


def test_safe_name_prevents_output_root_escape_for_dot_names(tmp_path):
    output_root = tmp_path / "output"
    output_root.mkdir()

    for course_name in (".", "..", " . ", "\t..\t"):
        assert output_root / CourseOrganizer._safe_name(course_name) == output_root / "Course"


def test_safe_name_preserves_regular_dot_titles():
    assert CourseOrganizer._safe_name("Season 1.2 - Test") == "Season 1.2 - Test"


def test_detect_season_from_path_prefers_deepest_matching_component(tmp_path):
    course_path = tmp_path / "Course"
    nested = course_path / "Season 1" / "S02"
    nested.mkdir(parents=True)
    candidate = nested / "episode.mp4"

    assert CourseOrganizer._detect_season_from_path(
        str(candidate),
        str(course_path),
    ) == (2, True)


def _organizer():
    return CourseOrganizer(config={"enabled": True, "naming_mode": "off"})

EXPECTED_INCOMPLETE_SUFFIXES = (".partial", ".part", ".tmp", ".crdownload", ".incomplete", ".!qb")


@pytest.mark.parametrize("suffix", EXPECTED_INCOMPLETE_SUFFIXES)
def test_incomplete_suffixes_block_processing(tmp_path, suffix):
    assert CourseOrganizer.INCOMPLETE_SUFFIXES == EXPECTED_INCOMPLETE_SUFFIXES

    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"

    incoming.mkdir()
    output.mkdir()
    course.mkdir(parents=True)
    (course / "lesson.mp4").write_bytes(b"video")
    (course / f"lesson{suffix}").write_bytes(b"marker")

    organizer = _organizer()
    assert organizer._process_course("Course", str(course), str(output)) is False
    assert organizer._process_course("Course", str(course), str(output)) is False

    assert (course / "lesson.mp4").exists()
    assert (course / f"lesson{suffix}").exists()
    assert not any(output.iterdir())


def test_nested_uppercase_incomplete_marker_blocks_processing(tmp_path):
    incoming = tmp_path / "incoming"
    output_root = tmp_path / "output"
    course = incoming / "Course"
    nested_marker = course / "Downloads" / "partial" / "lesson.PART"

    incoming.mkdir()
    output_root.mkdir()
    course.mkdir(parents=True)
    nested_marker.parent.mkdir(parents=True)

    (course / "lesson.mp4").write_bytes(b"video")
    nested_marker.write_bytes(b"marker")

    organizer = _organizer()
    assert organizer._process_course("Course", str(course), str(output_root)) is False
    assert organizer._process_course("Course", str(course), str(output_root)) is False

    assert (course / "lesson.mp4").exists()
    assert nested_marker.exists()
    assert not any(output_root.iterdir())


def test_incomplete_marker_created_after_second_snapshot_blocks_processing(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output_root = tmp_path / "output"
    course = incoming / "Course"
    marker = course / "Downloads" / "Partial" / "late.PART"

    incoming.mkdir()
    output_root.mkdir()
    course.mkdir(parents=True)
    marker.parent.mkdir(parents=True)

    (course / "lesson.mp4").write_bytes(b"video")
    organizer = _organizer()
    assert organizer._process_course("Course", str(course), str(output_root)) is False

    snapshot_calls = {"count": 0}
    original_snapshot = organizer._snapshot_signature

    def delayed_incomplete_marker_snapshot(course_path):
        snapshot = original_snapshot(course_path)
        snapshot_calls["count"] += 1
        if snapshot_calls["count"] == 2:
            marker.write_bytes(b"late")
        return snapshot

    monkeypatch.setattr(organizer, "_snapshot_signature", delayed_incomplete_marker_snapshot)
    assert organizer._process_course("Course", str(course), str(output_root)) is False
    assert snapshot_calls["count"] == 2
    assert (course / "lesson.mp4").exists()
    assert marker.exists()
    assert not any(output_root.iterdir())


def test_threaded_late_uppercase_incomplete_marker_blocks_processing(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output_root = tmp_path / "output"
    course = incoming / "Course"
    marker = course / "Downloads" / "Partial" / "late.PART"

    incoming.mkdir()
    output_root.mkdir()
    course.mkdir(parents=True)
    marker.parent.mkdir(parents=True)

    (course / "lesson.mp4").write_bytes(b"video")
    organizer = _organizer()
    assert organizer._process_course("Course", str(course), str(output_root)) is False

    snapshot_calls = {"count": 0}
    second_snapshot_ready = threading.Event()
    writer_done = threading.Event()
    writer_error: list[BaseException] = []
    original_snapshot = organizer._snapshot_signature

    def create_late_marker():
        try:
            assert second_snapshot_ready.wait(timeout=5), "writer did not observe second snapshot"
            marker.write_bytes(b"late")
        except Exception as error:
            writer_error.append(error)
        finally:
            writer_done.set()

    writer = threading.Thread(target=create_late_marker)

    def delayed_snapshot(course_path):
        snapshot = original_snapshot(course_path)
        snapshot_calls["count"] += 1
        if snapshot_calls["count"] == 2:
            second_snapshot_ready.set()
            assert writer_done.wait(timeout=5), "writer should finish before final guard"
        return snapshot

    monkeypatch.setattr(organizer, "_snapshot_signature", delayed_snapshot)
    writer.start()
    try:
        assert organizer._process_course("Course", str(course), str(output_root)) is False
    finally:
        writer.join(timeout=5)
    assert not writer.is_alive()
    assert not writer_error
    assert writer_done.is_set()
    assert snapshot_calls["count"] == 2
    assert (course / "lesson.mp4").exists()
    assert marker.exists()
    assert not any(output_root.iterdir())


def test_incomplete_marker_removal_allows_recovery(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    marker = course / "lesson.partial"

    incoming.mkdir()
    output.mkdir()
    course.mkdir(parents=True)

    (course / "lesson.mp4").write_bytes(b"video")
    marker.write_bytes(b"incomplete")
    organizer = _organizer()

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert not any(output.iterdir())
    assert marker.exists()

    marker.unlink()

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert (course / "lesson.mp4").exists()
    assert not any(output.iterdir())
    assert organizer._process_course("Course", str(course), str(output)) is True

    renamed = output / "Course" / "Season 1" / "Course - S01E001.mp4"
    assert renamed.read_bytes() == b"video"
    assert not marker.exists()
    assert not (course / "lesson.mp4").exists()


def _process_stable_course(organizer, incoming, output, course_name):
    course_path = incoming / course_name
    assert organizer._process_course(course_name, str(course_path), str(output)) is False
    assert organizer._process_course(course_name, str(course_path), str(output)) is True


def test_off_mode_stable_scan_skips_resolver_and_naming_preview_even_with_ai_review(
    tmp_path, monkeypatch
):
    incoming = tmp_path / "incoming"
    tv_output = tmp_path / "tv"
    movie_output = tmp_path / "movie"
    children_output = tmp_path / "children"
    incoming.mkdir()
    tv_output.mkdir()
    movie_output.mkdir()
    children_output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "tv_output": str(tv_output),
            "movie_output": str(movie_output),
            "children_output": str(children_output),
            "naming_mode": "off",
            "naming_ai_review": True,
        }
    )

    monkeypatch.setattr(
        organizer,
        "_get_resolver",
        lambda: (_ for _ in ()).throw(AssertionError("resolver must stay unused in off mode")),
    )

    organizer._run()
    organizer._run()

    assert "naming_preview_v1" not in organizer._data
    assert course.exists()
    assert all(not any(root.iterdir()) for root in (tv_output, movie_output, children_output))


def test_continues_after_highest_existing_matching_episode(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Python"
    course.mkdir(parents=True)
    (course / "lesson.mp4").write_bytes(b"new")

    season = output / "Python" / "Season 1"
    season.mkdir(parents=True)
    (season / "Python - S01E099.mp4").write_bytes(b"old")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Python")

    assert (season / "Python - S01E100.mp4").read_bytes() == b"new"


def test_fresh_batch_of_101_reaches_episode_101(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Math"
    course.mkdir(parents=True)
    for episode in range(1, 102):
        (course / f"lesson-{episode:03d}.mp4").write_bytes(str(episode).encode())

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Math")

    season = output / "Math" / "Season 1"
    assert len(list(season.glob("*.mp4"))) == 101
    assert (season / "Math - S01E001.mp4").read_bytes() == b"1"
    assert (season / "Math - S01E101.mp4").read_bytes() == b"101"


def test_multi_episode_file_stays_single_file_and_assigns_range_episode(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    course.mkdir(parents=True)
    (course / "1-2.mp4").write_bytes(b"one-two")
    (course / "1-2.srt").write_text("sub")
    (course / "3.mp4").write_bytes(b"three")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Course")

    season = output / "Course" / "Season 1"
    assert (season / "Course - S01E001-E002.mp4").read_bytes() == b"one-two"
    assert (season / "Course - S01E001-E002.srt").read_text() == "sub"
    assert (season / "Course - S01E003.mp4").read_bytes() == b"three"
    assert len(list(season.glob("*.mp4"))) == 2


def test_explicit_low_range_preserves_next_episode_cursor_with_existing_high_number(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    course.mkdir(parents=True)
    (course / "1-2.mp4").write_bytes(b"range")
    (course / "z.mp4").write_bytes(b"next")

    season = output / "Course" / "Season 1"
    season.mkdir(parents=True)
    (season / "Course - S01E100.mp4").write_bytes(b"existing")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Course")

    assert (season / "Course - S01E001-E002.mp4").read_bytes() == b"range"
    assert (season / "Course - S01E101.mp4").read_bytes() == b"next"


def test_existing_multi_episode_range_marks_following_content(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    course.mkdir(parents=True)
    (course / "new.mp4").write_bytes(b"new")

    season = output / "Course" / "Season 1"
    season.mkdir(parents=True)
    (season / "Course - S01E001-E002.mp4").write_bytes(b"existing")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Course")

    assert (season / "Course - S01E003.mp4").read_bytes() == b"new"


def test_existing_single_episode_keeps_multi_episode_request_from_first_conflict(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    course.mkdir(parents=True)
    (course / "1-2.mp4").write_bytes(b"range")

    season = output / "Course" / "Season 1"
    season.mkdir(parents=True)
    (season / "Course - S01E002.mp4").write_bytes(b"existing")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Course")

    assert (season / "Course - S01E003-E004.mp4").read_bytes() == b"range"


def test_duplicate_multi_episode_spans_advance_consecutively(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    course.mkdir(parents=True)
    (course / "1-2.mp4").write_bytes(b"first")
    (course / "1-2-copy.mp4").write_bytes(b"second")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Course")

    season = output / "Course" / "Season 1"
    outputs = sorted(season.glob("*.mp4"))
    assert len(outputs) == 2
    assert outputs[0].name == "Course - S01E001-E002.mp4"
    assert outputs[1].name == "Course - S01E003-E004.mp4"
    assert sorted({outputs[0].read_bytes(), outputs[1].read_bytes()}) == [b"first", b"second"]


def test_seasons_continue_independently(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Science"
    (course / "Season 1").mkdir(parents=True)
    (course / "Season 2").mkdir(parents=True)
    (course / "Season 1" / "new.mp4").write_bytes(b"season-one")
    (course / "Season 2" / "new.mp4").write_bytes(b"season-two")

    season_one = output / "Science" / "Season 1"
    season_two = output / "Science" / "Season 2"
    season_one.mkdir(parents=True)
    season_two.mkdir(parents=True)
    (season_one / "Science - S01E09.mp4").write_bytes(b"old-one")
    (season_two / "Science - S02E21.mkv").write_bytes(b"old-two")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Science")

    assert (season_one / "Science - S01E010.mp4").read_bytes() == b"season-one"
    assert (season_two / "Science - S02E022.mp4").read_bytes() == b"season-two"


def test_unrelated_files_do_not_influence_next_episode(tmp_path):
    season = tmp_path / "Course" / "Season 1"
    season.mkdir(parents=True)
    unrelated = [
        "Other Course - S01E900.mp4",
        "Course - S02E800.mp4",
        "Course - S01E700.srt",
        "Course S01E600.mp4",
        "Course - S01E500.txt",
        "Course - S01E4.mp4",
        "notes-E999.mp4",
    ]
    for filename in unrelated:
        (season / filename).write_bytes(b"unrelated")
    (season / "Course - S01E999.mp4").mkdir()
    (season / "Course - S01E07.mp4").write_bytes(b"matching")

    assert CourseOrganizer._next_episode_number(str(season), "Course", 1) == 8


def test_chinese_leading_prefixes_use_zero_padded_three_digit_episodes(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Poetry"
    course.mkdir(parents=True)
    (course / "001.一代诗仙--李白.mp4").write_bytes(b"001")
    (course / "002.《早发白帝城》--李白.mp4").write_bytes(b"002")
    (course / "003 汤郁诗史--杜甫.mp4").write_bytes(b"003")
    (course / "001.一代诗仙--李白.srt").write_text("subtitle")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Poetry")

    season = output / "Poetry" / "Season 1"
    assert (season / "Poetry - S01E001.mp4").read_bytes() == b"001"
    assert (season / "Poetry - S01E002.mp4").read_bytes() == b"002"
    assert (season / "Poetry - S01E003.mp4").read_bytes() == b"003"
    assert (season / "Poetry - S01E001.srt").read_text() == "subtitle"


def test_numeric_stem_only_file_uses_zero_padded_episode(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Poetry"
    course.mkdir(parents=True)
    (course / "001.mp4").write_bytes(b"pure")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Poetry")

    season = output / "Poetry" / "Season 1"
    assert (season / "Poetry - S01E001.mp4").read_bytes() == b"pure"


def test_leading_episode_collision_uses_next_available_without_suffixing(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Poetry"
    course.mkdir(parents=True)
    (course / "001-序章.mp4").write_bytes(b"new")

    season = output / "Poetry" / "Season 1"
    season.mkdir(parents=True)
    (season / "Poetry - S01E001.mp4").write_bytes(b"old")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Poetry")

    assert (season / "Poetry - S01E002.mp4").read_bytes() == b"new"
    assert not (season / "Poetry - S01E001_1.mp4").exists()


def test_zero_episode_prefix_is_not_used(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Poetry"
    course.mkdir(parents=True)
    (course / "000-测试.mp4").write_bytes(b"zero")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Poetry")

    season = output / "Poetry" / "Season 1"
    assert (season / "Poetry - S01E001.mp4").read_bytes() == b"zero"


def test_non_delimited_numeric_leading_title_is_not_treated_as_episode(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    course.mkdir(parents=True)
    (course / "2024课程介绍.mp4").write_bytes(b"intro")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Course")

    season = output / "Course" / "Season 1"
    assert (season / "Course - S01E001.mp4").read_bytes() == b"intro"


def test_mixed_explicit_and_unnumbered_files_use_explicit_then_continuation(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Poetry"
    course.mkdir(parents=True)
    (course / "010-开场.mp4").write_bytes(b"first")
    (course / "011-正文.mp4").write_bytes(b"second")
    (course / "序章.mp4").write_bytes(b"third")
    (course / "补充.mp4").write_bytes(b"fourth")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Poetry")

    season = output / "Poetry" / "Season 1"
    assert (season / "Poetry - S01E010.mp4").read_bytes() == b"first"
    assert (season / "Poetry - S01E011.mp4").read_bytes() == b"second"
    assert (season / "Poetry - S01E012.mp4").read_bytes() == b"third"
    assert (season / "Poetry - S01E013.mp4").read_bytes() == b"fourth"


def test_leading_batch_starts_at_010_keeps_episode_prefix(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Poetry"
    course.mkdir(parents=True)
    (course / "010-序章.mp4").write_bytes(b"first")
    (course / "011-其二.mp4").write_bytes(b"second")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Poetry")

    season = output / "Poetry" / "Season 1"
    assert (season / "Poetry - S01E010.mp4").read_bytes() == b"first"
    assert (season / "Poetry - S01E011.mp4").read_bytes() == b"second"


def test_old_e01_style_files_still_increase_next_episode(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    course.mkdir(parents=True)
    (course / "episode.mp4").write_bytes(b"new")

    season = output / "Course" / "Season 1"
    season.mkdir(parents=True)
    (season / "Course - S01E01.mp4").write_bytes(b"old")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Course")

    assert (season / "Course - S01E002.mp4").read_bytes() == b"new"


def test_no_prefix_files_follow_next_available_episode(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    course.mkdir(parents=True)
    (course / "intro.mp4").write_bytes(b"intro")
    (course / "main.mp4").write_bytes(b"main")

    season = output / "Course" / "Season 1"
    season.mkdir(parents=True)
    (season / "Course - S01E07.mp4").write_bytes(b"old")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Course")

    assert (season / "Course - S01E008.mp4").read_bytes() == b"intro"
    assert (season / "Course - S01E009.mp4").read_bytes() == b"main"


def test_high_numbered_episodes_continue_naturally(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    course.mkdir(parents=True)
    (course / "next.mp4").write_bytes(b"next")

    season = output / "Course" / "Season 1"
    season.mkdir(parents=True)
    (season / "Course - S01E0999.mp4").write_bytes(b"old")

    organizer = _organizer()
    _process_stable_course(organizer, incoming, output, "Course")

    assert (season / "Course - S01E1000.mp4").read_bytes() == b"next"


def test_stability_state_reads_data_without_passing_a_fake_default(tmp_path):
    class HostShapedOrganizer(CourseOrganizer):
        def get_data(self, key=None, plugin_id=None):
            assert plugin_id is None
            return self._data.get(key)

    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    course = incoming / "Course"
    incoming.mkdir()
    output.mkdir()
    course.mkdir(parents=True)
    (course / "lesson.mp4").write_bytes(b"x")

    organizer = HostShapedOrganizer(config={"enabled": True})
    assert organizer._process_course("Course", str(course), str(output)) is False


class _RunLogger:
    def __init__(self):
        self.errors = []
        self.infos = []
        self.debugs = []

    def _format_messages(self, message: str, *args: object, **kwargs: object) -> str:
        # noqa: ARG001
        if args:
            try:
                return message % args
            except TypeError:
                return str(message)
        return str(message)

    def error(self, message: str, *args: object, **kwargs: object) -> None:  # noqa: ARG001
        self.errors.append(self._format_messages(message, *args, **kwargs))

    def info(self, *args: object, **kwargs: object) -> None:  # noqa: ARG001
        message = str(args[0]) if args else ""
        self.infos.append(self._format_messages(message, *args[1:], **kwargs))

    def debug(self, *args: object, **kwargs: object) -> None:  # noqa: ARG001
        message = str(args[0]) if args else ""
        self.debugs.append(self._format_messages(message, *args[1:], **kwargs))


class _RunOnceFakeTimer:
    created = []

    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.daemon = False
        self.started = False
        self.finished = False
        self.cancelled = False
        self.callback_started = threading.Event()
        self.__class__.created.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started and not self.finished and not self.cancelled

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.is_alive():
            return False
        self.callback_started.set()
        try:
            self.callback()
            return True
        finally:
            self.finished = True


def _filesystem_fingerprint(root: Path):
    paths = [root, *root.rglob("*")]
    return [
        (
            path.relative_to(root).as_posix(),
            path.is_dir(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in sorted(paths)
    ]


def _preview_run_once_case(tmp_path):
    incoming = tmp_path / "incoming"
    tv_output = tmp_path / "tv"
    movie_output = tmp_path / "movie"
    children_output = tmp_path / "children"
    for directory in (incoming, tv_output, movie_output, children_output):
        directory.mkdir()

    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    config = {
        "enabled": False,
        "run_once": True,
        "incoming": str(incoming),
        "tv_output": str(tv_output),
        "movie_output": str(movie_output),
        "children_output": str(children_output),
        "naming_mode": "preview",
    }
    roots = (incoming, tv_output, movie_output, children_output)
    return config, roots


def _seed_stable_course(organizer, incoming):
    course = incoming / "Course"
    organizer.save_data(
        organizer._state_key("Course"),
        {"signature": organizer._snapshot_signature(str(course)), "stable_count": 1},
    )


def _event_counts_from_log(message: str) -> dict[str, int]:
    values = {}
    for token in str(message).split():
        if "=" not in token:
            continue
        key, raw = token.split("=", 1)
        try:
            values[key] = int(raw)
        except ValueError:
            continue
    return values


def test_run_once_preview_lifecycle_is_fail_closed_and_resets(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    tv_output = tmp_path / "tv"
    movie_output = tmp_path / "movie"
    children_output = tmp_path / "children"
    for directory in (incoming, tv_output, movie_output, children_output):
        directory.mkdir()

    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    config = {
        "enabled": False,
        "run_once": True,
        "incoming": str(incoming),
        "tv_output": str(tv_output),
        "movie_output": str(movie_output),
        "children_output": str(children_output),
        "interval": 611,
        "naming_mode": "preview",
        "naming_sources": "douban,themoviedb",
        "naming_auto_threshold": 93,
        "naming_min_margin": 15,
        "naming_uncertain_policy": "hold",
        "naming_append_tmdb_id": True,
        "naming_ai_review": False,
        "naming_manual_overrides": "",
        "naming_clear_cache_once": False,
    }

    roots = (incoming, tv_output, movie_output, children_output)
    before = {root.name: _filesystem_fingerprint(root) for root in roots}
    organizer = CourseOrganizer(config=dict(config))
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)
    organizer.save_data(
        organizer._state_key("Course"),
        {"signature": organizer._snapshot_signature(str(course)), "stable_count": 1},
    )
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)

    organizer.init_plugin(dict(config))

    assert len(_RunOnceFakeTimer.created) == 1
    timer = _RunOnceFakeTimer.created[0]
    assert timer.interval == pytest.approx(0.2)
    assert timer.started is True
    assert any(
        "CourseOrganizer[event=run_once_scheduled] delay=0.2" in message
        for message in logger.infos
    )
    pending_config = {**config, "run_once": True}
    assert organizer._get_config() == pending_config

    organizer.init_plugin({**config, "run_once": False})
    assert len(_RunOnceFakeTimer.created) == 1
    assert timer.cancelled is False
    assert organizer._get_config() == pending_config
    assert timer.fire() is True

    scan_started = [
        message
        for message in logger.infos
        if "CourseOrganizer[event=scan_started]" in message
    ]
    assert scan_started == [
        "CourseOrganizer[event=scan_started] trigger=manual mode=preview"
    ]
    scan_completed = [
        message
        for message in logger.infos
        if "CourseOrganizer[event=scan_completed]" in message
    ]
    assert len(scan_completed) == 1
    assert "trigger=manual" in scan_completed[0]
    assert _event_counts_from_log(scan_completed[0]) == {"scanned": 1, "moved": 0}
    all_messages = logger.errors + logger.infos + logger.debugs
    assert not any("CourseOrganizer[event=move_started]" in message for message in all_messages)
    assert not any("CourseOrganizer[event=move_completed]" in message for message in all_messages)
    assert any("CourseOrganizer[event=preview]" in message for message in all_messages)
    assert {root.name: _filesystem_fingerprint(root) for root in roots} == before
    assert organizer._get_config() == {**config, "run_once": False}


def test_run_once_repeated_true_reuses_live_timer(tmp_path, monkeypatch):
    config, roots = _preview_run_once_case(tmp_path)
    before = {root.name: _filesystem_fingerprint(root) for root in roots}
    organizer = CourseOrganizer(config=dict(config))
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)

    organizer.init_plugin(dict(config))
    organizer.init_plugin(dict(config))

    assert len(_RunOnceFakeTimer.created) == 1
    timer = _RunOnceFakeTimer.created[0]
    assert timer.cancelled is False
    assert organizer._get_config()["run_once"] is True
    assert any(
        "CourseOrganizer[event=run_once_coalesced] pending=true" in message
        for message in logger.infos
    )

    assert timer.fire() is True
    assert sum("CourseOrganizer[event=scan_completed]" in message for message in logger.infos) == 1
    assert organizer._get_config()["run_once"] is False
    assert {root.name: _filesystem_fingerprint(root) for root in roots} == before


def test_stop_service_cancels_pending_run_once_without_firing(tmp_path, monkeypatch):
    config, roots = _preview_run_once_case(tmp_path)
    before = {root.name: _filesystem_fingerprint(root) for root in roots}
    organizer = CourseOrganizer(config=dict(config))
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)

    organizer.init_plugin(dict(config))
    timer = _RunOnceFakeTimer.created[0]
    assert organizer._get_config()["run_once"] is True
    organizer.stop_service()

    assert timer.cancelled is True
    assert timer.is_alive() is False
    assert timer.fire() is False
    assert organizer._get_config()["run_once"] is False
    assert not any("CourseOrganizer[event=scan_completed]" in message for message in logger.infos)
    assert {root.name: _filesystem_fingerprint(root) for root in roots} == before


def test_run_once_cross_instance_uses_latest_owner_and_one_timer(tmp_path, monkeypatch):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_config, first_roots = _preview_run_once_case(first_root)
    second_config, second_roots = _preview_run_once_case(second_root)
    first_before = {root.name: _filesystem_fingerprint(root) for root in first_roots}
    second_before = {root.name: _filesystem_fingerprint(root) for root in second_roots}

    first = CourseOrganizer(config=dict(first_config))
    second = CourseOrganizer(config=dict(second_config))
    first_logger = _RunLogger()
    second_logger = _RunLogger()
    monkeypatch.setattr(first, "_logger", first_logger)
    monkeypatch.setattr(second, "_logger", second_logger)
    _seed_stable_course(second, second_root / "incoming")
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)

    first.init_plugin(dict(first_config))
    second.init_plugin(dict(second_config))

    assert len(_RunOnceFakeTimer.created) == 1
    assert type(second)._run_once_owner is second
    assert second._get_config()["run_once"] is True
    assert _RunOnceFakeTimer.created[0].fire() is True

    assert not any("CourseOrganizer[event=scan_completed]" in message for message in first_logger.infos)
    assert sum("CourseOrganizer[event=scan_completed]" in message for message in second_logger.infos) == 1
    assert {root.name: _filesystem_fingerprint(root) for root in first_roots} == first_before
    assert {root.name: _filesystem_fingerprint(root) for root in second_roots} == second_before
    assert second._get_config()["run_once"] is False


def test_run_once_cross_instance_stop_cancels_previous_timer(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    config, roots = _preview_run_once_case(root)
    before = {path.name: _filesystem_fingerprint(path) for path in roots}
    first = CourseOrganizer(config=dict(config))
    second = CourseOrganizer(config={**config, "run_once": False})
    first_logger = _RunLogger()
    second_logger = _RunLogger()
    monkeypatch.setattr(first, "_logger", first_logger)
    monkeypatch.setattr(second, "_logger", second_logger)
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)

    first.init_plugin(dict(config))
    timer = _RunOnceFakeTimer.created[0]
    second.stop_service()

    assert timer.cancelled is True
    assert timer.fire() is False
    assert first._get_config()["run_once"] is False
    assert second._get_config()["run_once"] is False
    assert not any("CourseOrganizer[event=scan_completed]" in message for message in first_logger.infos)
    assert not any("CourseOrganizer[event=scan_completed]" in message for message in second_logger.infos)
    assert {path.name: _filesystem_fingerprint(path) for path in roots} == before


def test_run_once_concurrent_true_requests_create_one_timer_and_one_scan(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    config, roots = _preview_run_once_case(root)
    before = {path.name: _filesystem_fingerprint(path) for path in roots}
    first = CourseOrganizer(config=dict(config))
    second = CourseOrganizer(config=dict(config))
    first_logger = _RunLogger()
    second_logger = _RunLogger()
    monkeypatch.setattr(first, "_logger", first_logger)
    monkeypatch.setattr(second, "_logger", second_logger)
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)
    persist_errors = []
    start_barrier = threading.Barrier(2)

    def init(organizer):
        try:
            start_barrier.wait(timeout=2)
            organizer.init_plugin(dict(config))
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            persist_errors.append(exc)

    threads = [threading.Thread(target=init, args=(organizer,)) for organizer in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert persist_errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert len(_RunOnceFakeTimer.created) == 1
    assert _RunOnceFakeTimer.created[0].fire() is True
    assert sum(
        "CourseOrganizer[event=scan_completed]" in message
        for logger in (first_logger, second_logger)
        for message in logger.infos
    ) == 1
    assert {path.name: _filesystem_fingerprint(path) for path in roots} == before


def test_run_once_stop_invalidates_callback_blocked_on_scan_lock(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    config, roots = _preview_run_once_case(root)
    before = {path.name: _filesystem_fingerprint(path) for path in roots}
    organizer = CourseOrganizer(config=dict(config))
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)

    organizer.init_plugin(dict(config))
    timer = _RunOnceFakeTimer.created[0]
    assert CourseOrganizer._thread_lock.acquire(timeout=1)
    fire_errors = []

    def fire():
        try:
            timer.fire()
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            fire_errors.append(exc)

    fire_thread = threading.Thread(target=fire)
    fire_thread.start()
    try:
        assert timer.callback_started.wait(timeout=1)
        organizer.stop_service()
    finally:
        CourseOrganizer._thread_lock.release()
    fire_thread.join(timeout=2)

    assert fire_errors == []
    assert not fire_thread.is_alive()
    assert not any("CourseOrganizer[event=scan_completed]" in message for message in logger.infos)
    assert {path.name: _filesystem_fingerprint(path) for path in roots} == before


def test_run_once_stale_callback_token_cannot_run_new_generation(tmp_path, monkeypatch):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_config, first_roots = _preview_run_once_case(first_root)
    second_config, second_roots = _preview_run_once_case(second_root)
    second_before = {path.name: _filesystem_fingerprint(path) for path in second_roots}
    first = CourseOrganizer(config=dict(first_config))
    second = CourseOrganizer(config=dict(second_config))
    first_logger = _RunLogger()
    second_logger = _RunLogger()
    monkeypatch.setattr(first, "_logger", first_logger)
    monkeypatch.setattr(second, "_logger", second_logger)
    _seed_stable_course(second, second_root / "incoming")
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)

    first.init_plugin(dict(first_config))
    timer_a = _RunOnceFakeTimer.created[0]
    assert CourseOrganizer._thread_lock.acquire(timeout=1)
    fire_thread = threading.Thread(target=timer_a.fire)
    fire_thread.start()
    try:
        assert timer_a.callback_started.wait(timeout=1)
        first.stop_service()
        second.init_plugin(dict(second_config))
        assert len(_RunOnceFakeTimer.created) == 2
        timer_b = _RunOnceFakeTimer.created[1]
    finally:
        CourseOrganizer._thread_lock.release()
    fire_thread.join(timeout=2)

    assert not fire_thread.is_alive()
    assert not any("CourseOrganizer[event=scan_completed]" in message for message in first_logger.infos)
    assert timer_b.fire() is True
    assert sum("CourseOrganizer[event=scan_completed]" in message for message in second_logger.infos) == 1
    assert {path.name: _filesystem_fingerprint(path) for path in second_roots} == second_before


def test_run_once_timer_constructor_failure_keeps_retry_flag(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    config, _ = _preview_run_once_case(root)
    organizer = CourseOrganizer(config=dict(config))
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    class FailingTimer:
        def __init__(self, interval, callback):  # noqa: ARG002
            raise RuntimeError("constructor failed")

    monkeypatch.setattr(MODULE.threading, "Timer", FailingTimer)
    organizer.init_plugin(dict(config))

    assert type(organizer)._run_once_timer is None
    assert type(organizer)._run_once_token is None
    assert organizer._get_config()["run_once"] is True
    assert any(
        "CourseOrganizer[event=run_once_schedule_failed] phase=construct error=RuntimeError" in message
        for message in logger.errors
    )


def test_run_once_timer_start_failure_keeps_retry_flag(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    config, _ = _preview_run_once_case(root)
    organizer = CourseOrganizer(config=dict(config))
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    class FailingTimer(_RunOnceFakeTimer):
        def start(self):
            raise RuntimeError("start failed")

    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", FailingTimer)
    organizer.init_plugin(dict(config))

    timer = FailingTimer.created[0]
    assert timer.cancelled is True
    assert type(organizer)._run_once_timer is None
    assert type(organizer)._run_once_token is None
    assert organizer._get_config()["run_once"] is True
    assert any(
        "CourseOrganizer[event=run_once_schedule_failed] phase=start error=RuntimeError" in message
        for message in logger.errors
    )


def test_run_once_persist_and_stop_are_linearized(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    config, _ = _preview_run_once_case(root)
    organizer = CourseOrganizer(config=dict(config))
    entered = threading.Event()
    release = threading.Event()
    original_persist = organizer._persist_config
    calls = {"count": 0}

    def blocking_persist(config_value):
        calls["count"] += 1
        if calls["count"] == 1:
            entered.set()
            assert release.wait(timeout=2)
        return original_persist(config_value)

    monkeypatch.setattr(organizer, "_persist_config", blocking_persist)
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)
    init_thread = threading.Thread(target=organizer.init_plugin, args=(dict(config),))
    init_thread.start()
    assert entered.wait(timeout=1)
    stop_thread = threading.Thread(target=organizer.stop_service)
    stop_thread.start()
    release.set()
    init_thread.join(timeout=2)
    stop_thread.join(timeout=2)

    assert not init_thread.is_alive()
    assert not stop_thread.is_alive()
    assert len(_RunOnceFakeTimer.created) == 1
    assert _RunOnceFakeTimer.created[0].cancelled is True
    assert type(organizer)._run_once_token is None
    assert organizer._get_config()["run_once"] is False


def test_run_once_claim_uses_stable_latest_preview_config_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    config, _ = _preview_run_once_case(root)
    organizer = CourseOrganizer(config=dict(config))
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)
    organizer.init_plugin(dict(config))
    timer = _RunOnceFakeTimer.created[0]
    observed = []
    entered = threading.Event()
    release = threading.Event()

    def run(force=False):  # noqa: ARG001
        observed.append(organizer._get_config()["naming_mode"])
        entered.set()
        assert release.wait(timeout=2)
        observed.append(organizer._get_config()["naming_mode"])

    monkeypatch.setattr(organizer, "_run", run)
    fire_thread = threading.Thread(target=timer.fire)
    fire_thread.start()
    assert entered.wait(timeout=1)
    organizer.update_config({**config, "run_once": False, "naming_mode": "apply"})
    release.set()
    fire_thread.join(timeout=2)

    assert not fire_thread.is_alive()
    assert observed == ["preview", "preview"]
    assert organizer._get_config()["naming_mode"] == "apply"


def test_run_once_new_request_during_claimed_run_gets_second_generation_after_failure(
    tmp_path, monkeypatch
):
    root = tmp_path / "case"
    root.mkdir()
    config, _ = _preview_run_once_case(root)
    organizer = CourseOrganizer(config=dict(config))
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)
    organizer.init_plugin(dict(config))
    timer_a = _RunOnceFakeTimer.created[0]
    first_entered = threading.Event()
    release_first = threading.Event()
    run_calls = []

    def run(force=False):  # noqa: ARG001
        run_calls.append(force)
        if len(run_calls) == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
            raise RuntimeError("first run failed")

    monkeypatch.setattr(organizer, "_run", run)
    fire_errors = []

    def fire_first():
        try:
            timer_a.fire()
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            fire_errors.append(exc)

    fire_thread = threading.Thread(target=fire_first)
    fire_thread.start()
    assert first_entered.wait(timeout=1)

    organizer.init_plugin(dict(config))
    assert len(_RunOnceFakeTimer.created) == 2
    timer_b = _RunOnceFakeTimer.created[1]
    assert type(organizer)._run_once_token is not None
    release_first.set()
    fire_thread.join(timeout=2)

    assert not fire_thread.is_alive()
    assert len(fire_errors) == 1
    assert isinstance(fire_errors[0], RuntimeError)
    assert timer_b.fire() is True
    assert run_calls == [True, True]
    assert type(organizer)._run_once_token is None
    assert organizer._get_config()["run_once"] is False


def test_run_once_claim_persist_failure_does_not_run(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    config, roots = _preview_run_once_case(root)
    before = {path.name: _filesystem_fingerprint(path) for path in roots}
    organizer = CourseOrganizer(config=dict(config))
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)
    original_persist = organizer._persist_config
    calls = {"count": 0}

    def fail_claim(config_value):
        calls["count"] += 1
        if calls["count"] == 1:
            return original_persist(config_value)
        return False

    monkeypatch.setattr(organizer, "_persist_config", fail_claim)
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)
    organizer.init_plugin(dict(config))
    timer = _RunOnceFakeTimer.created[0]

    assert timer.fire() is True
    assert not any("CourseOrganizer[event=scan_completed]" in message for message in logger.infos)
    assert any(
        "CourseOrganizer[event=run_once_persist_failed] phase=claim" in message
        for message in logger.errors
    )
    assert type(organizer)._run_once_token is None
    assert organizer._get_config()["run_once"] is True
    assert {path.name: _filesystem_fingerprint(path) for path in roots} == before


def test_run_once_coalesce_persist_failure_preserves_original_pending_request(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    config, roots = _preview_run_once_case(root)
    before = {path.name: _filesystem_fingerprint(path) for path in roots}
    first = CourseOrganizer(config=dict(config))
    second = CourseOrganizer(config=dict(config))
    first_logger = _RunLogger()
    second_logger = _RunLogger()
    monkeypatch.setattr(first, "_logger", first_logger)
    monkeypatch.setattr(second, "_logger", second_logger)
    monkeypatch.setattr(second, "_persist_config", lambda config_value: False)
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)

    first.init_plugin(dict(config))
    timer = _RunOnceFakeTimer.created[0]
    original_owner = type(first)._run_once_owner
    second.init_plugin(dict(config))

    assert timer.cancelled is False
    assert type(first)._run_once_owner is original_owner is first
    assert any(
        "CourseOrganizer[event=run_once_persist_failed] phase=pending" in message
        for message in second_logger.errors
    )
    assert timer.fire() is True
    assert sum("CourseOrganizer[event=scan_completed]" in message for message in first_logger.infos) == 1
    assert {path.name: _filesystem_fingerprint(path) for path in roots} == before


def test_run_once_false_reload_persist_failure_preserves_original_pending_request(
    tmp_path, monkeypatch
):
    root = tmp_path / "case"
    root.mkdir()
    config, roots = _preview_run_once_case(root)
    before = {path.name: _filesystem_fingerprint(path) for path in roots}
    first = CourseOrganizer(config=dict(config))
    second = CourseOrganizer(config={**config, "run_once": False})
    first_logger = _RunLogger()
    second_logger = _RunLogger()
    monkeypatch.setattr(first, "_logger", first_logger)
    monkeypatch.setattr(second, "_logger", second_logger)
    monkeypatch.setattr(second, "_persist_config", lambda config_value: False)
    _RunOnceFakeTimer.created.clear()
    monkeypatch.setattr(MODULE.threading, "Timer", _RunOnceFakeTimer)

    first.init_plugin(dict(config))
    timer = _RunOnceFakeTimer.created[0]
    original_token = type(first)._run_once_token
    original_owner = type(first)._run_once_owner
    second.init_plugin({**config, "run_once": False})

    assert timer.cancelled is False
    assert type(first)._run_once_token == original_token
    assert type(first)._run_once_owner is original_owner is first
    assert any(
        "CourseOrganizer[event=run_once_persist_failed] phase=pending" in message
        for message in second_logger.errors
    )
    assert timer.fire() is True
    assert sum("CourseOrganizer[event=scan_completed]" in message for message in first_logger.infos) == 1
    assert {path.name: _filesystem_fingerprint(path) for path in roots} == before


def test_stability_defer_branches_emit_structured_item_events_without_paths(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()

    organizer = CourseOrganizer(config={"enabled": True, "naming_mode": "off"})
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    def make_course(name: str) -> Path:
        course = incoming / name
        course.mkdir()
        (course / "lesson.mp4").write_bytes(b"video")
        return course

    first = make_course("First")
    assert organizer._process_course("First", str(first), str(output)) is False

    changed = make_course("Changed")
    assert organizer._process_course("Changed", str(changed), str(output)) is False
    (changed / "lesson.mp4").write_bytes(b"changed")
    assert organizer._process_course("Changed", str(changed), str(output)) is False

    pending = make_course("Pending")
    assert organizer._process_course("Pending", str(pending), str(output)) is False
    pending_key = organizer._state_key("Pending")
    pending_signature = organizer._snapshot_signature(str(pending))
    organizer.save_data(pending_key, {"signature": pending_signature, "stable_count": 0})
    assert organizer._process_course("Pending", str(pending), str(output)) is False

    final = make_course("Final")
    assert organizer._process_course("Final", str(final), str(output)) is False
    final_key = organizer._state_key("Final")
    final_signature = organizer._snapshot_signature(str(final))
    organizer.save_data(final_key, {"signature": final_signature, "stable_count": 1})
    snapshot_calls = {"count": 0}

    def changed_final_snapshot(course_path: str):
        snapshot_calls["count"] += 1
        current = CourseOrganizer._snapshot_signature(organizer, course_path)
        if snapshot_calls["count"] == 1:
            return current
        return current + [("late-marker", 0, 0)]

    monkeypatch.setattr(organizer, "_snapshot_signature", changed_final_snapshot)
    assert organizer._process_course("Final", str(final), str(output)) is False

    assert any(
        "CourseOrganizer[event=item_deferred] item_course=First "
        "item_reason=first_snapshot item_stable_count=1" in message
        for message in logger.debugs
    )
    assert any(
        "CourseOrganizer[event=item_deferred] item_course=Changed "
        "item_reason=changed_before_stabilization item_stable_count=1" in message
        for message in logger.debugs
    )
    assert any(
        "CourseOrganizer[event=item_deferred] item_course=Pending "
        "item_reason=stable_count_pending item_stable_count=1" in message
        for message in logger.debugs
    )
    assert any(
        "CourseOrganizer[event=item_deferred] item_course=Final "
        "item_reason=changed_before_final_confirmation item_stable_count=1" in message
        for message in logger.debugs
    )
    assert all(str(tmp_path) not in message for message in logger.debugs)


def test_run_skips_when_target_output_root_missing_without_leaking_full_paths(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming_secret_token_alpha"
    tv_output = tmp_path / "tv_secret_token_alpha"
    movie_output = tmp_path / "movie_secret_token_alpha"
    missing_children_output = tmp_path / "children_secret_token_alpha"

    incoming.mkdir()
    tv_output.mkdir()
    movie_output.mkdir()

    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "tv_output": str(tv_output),
            "movie_output": str(movie_output),
            "children_output": str(missing_children_output),
        }
    )

    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)
    called = []

    def blocked_course(*args: object, **kwargs: object) -> bool:
        called.append(True)
        return True

    monkeypatch.setattr(organizer, "_process_course", blocked_course)

    organizer._run()

    assert called == []
    all_logged = logger.errors + logger.debugs + logger.infos
    assert any("output path not exist for library: children" in err for err in logger.errors)
    assert not missing_children_output.exists()
    for sensitive in (
        str(incoming),
        str(tv_output),
        str(movie_output),
        str(missing_children_output),
        "secret_token_alpha",
    ):
        assert all(sensitive not in message for message in all_logged)


def test_run_rejects_invalid_incoming_without_leaking_full_paths(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming_secret_token_beta"
    tv_output = tmp_path / "tv_secret_token_beta"
    movie_output = tmp_path / "movie_secret_token_beta"
    children_output = tmp_path / "children_secret_token_beta"

    tv_output.mkdir()
    movie_output.mkdir()
    children_output.mkdir()

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "tv_output": str(tv_output),
            "movie_output": str(movie_output),
            "children_output": str(children_output),
        }
    )

    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    organizer._run()

    all_logged = logger.errors + logger.debugs + logger.infos
    assert any("incoming path invalid" in err for err in logger.errors)
    for sensitive in (
        str(incoming),
        str(tv_output),
        str(movie_output),
        str(children_output),
        "secret_token_beta",
    ):
        assert all(sensitive not in message for message in all_logged)


def test_process_course_blocks_source_root_escape_via_symlink(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    external_course_root = tmp_path / "external_course"
    external_course = external_course_root / "Course"

    incoming.mkdir()
    output.mkdir()
    external_course.mkdir(parents=True)
    (external_course / "lesson.mp4").write_bytes(b"video")

    symlink_course = incoming / "Course"
    symlink_course.symlink_to(external_course)

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "naming_mode": "off",
        }
    )

    assert organizer._process_course(
        "Course",
        str(symlink_course),
        str(output),
        str(incoming),
    ) is False

    assert (external_course / "lesson.mp4").exists()
    assert not any(output.iterdir())


def test_process_course_blocks_destination_escape_via_output_symlinked_descendant(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    escaped_target = tmp_path / "escaped"
    escaped_payload = escaped_target / "payload"

    incoming.mkdir()
    output.mkdir()
    escaped_payload.mkdir(parents=True)
    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")
    (output / "Course").symlink_to(escaped_payload)

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "naming_mode": "off",
        }
    )

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert (course / "lesson.mp4").exists()
    assert not any(escaped_payload.glob("*.mp4"))


def test_process_course_source_escape_in_later_file_aborts_without_partial_move(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    external_course_root = tmp_path / "external"
    external_course_root.mkdir()
    external_video = external_course_root / "z.mp4"
    external_video.write_bytes(b"external")

    incoming.mkdir()
    output.mkdir()

    course = incoming / "Course"
    course.mkdir()
    (course / "a.mp4").write_bytes(b"safe")
    (course / "z.mp4").symlink_to(external_video)

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "naming_mode": "off",
        }
    )

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert organizer._process_course("Course", str(course), str(output)) is False

    assert (course / "a.mp4").exists()
    assert (course / "z.mp4").exists()
    assert not any(output.iterdir())


def test_process_course_destination_escape_in_later_season_aborts_without_partial_move(tmp_path):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    escaped_target = tmp_path / "escaped"
    escaped_payload = escaped_target / "payload"
    escaped_payload.mkdir(parents=True)

    incoming.mkdir()
    output.mkdir()

    course = incoming / "Course"
    course.mkdir()
    (course / "Season 1").mkdir()
    (course / "Season 2").mkdir()
    (course / "Season 1" / "a.mp4").write_bytes(b"first")
    (course / "Season 2" / "b.mp4").write_bytes(b"second")
    (output / "Course").mkdir()
    (output / "Course" / "Season 2").symlink_to(escaped_payload)

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "naming_mode": "off",
        }
    )

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert organizer._process_course("Course", str(course), str(output)) is False

    assert not any((output / "Course" / "Season 1").glob("*.mp4"))

    assert not any(escaped_payload.glob("*.mp4"))


def test_process_course_source_symlink_replaced_in_single_call_after_boundary_check_is_rejected(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    external_root = tmp_path / "external"
    external_video = external_root / "outside.mp4"

    incoming.mkdir()
    output.mkdir()
    external_root.mkdir()
    external_video.write_bytes(b"outside")

    course = incoming / "Course"
    course.mkdir()
    source_video = course / "a.mp4"
    source_video.write_bytes(b"safe")

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "naming_mode": "off",
        }
    )

    original_check = organizer._is_within_realpath
    check_calls = {"count": 0, "replaced": False}

    def raced_check(root: str, path: str) -> bool:
        result = original_check(root, path)
        check_calls["count"] += 1
        if (
            not check_calls["replaced"]
            and os.path.abspath(root) == os.path.abspath(str(course))
            and source_video.name in os.path.basename(path)
        ):
            source_video.unlink()
            source_video.symlink_to(external_video)
            check_calls["replaced"] = True
            return True
        return result

    monkeypatch.setattr(organizer, "_is_within_realpath", raced_check)

    organizer.save_data(
        organizer._state_key("Course"),
        {
            "signature": organizer._coerce_signature(organizer._snapshot_signature(str(course))),
            "stable_count": 1,
        },
    )

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert check_calls["count"] >= 1
    assert check_calls["replaced"] is True
    assert not any(output.iterdir())
    assert external_video.exists()
    assert source_video.is_symlink()
    assert not any(path.suffix == ".mp4" and path.name.startswith(".tmp") for path in output.rglob("*"))


def test_process_course_destination_parent_symlink_replaced_in_single_call_after_boundary_check_is_rejected(
    tmp_path, monkeypatch
):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    escaped = tmp_path / "escaped"
    escaped_payload = escaped / "payload"

    incoming.mkdir()
    output.mkdir()
    escaped_payload.mkdir(parents=True)

    course = incoming / "Course"
    course.mkdir()
    (course / "a.mp4").write_bytes(b"safe")

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "naming_mode": "off",
        }
    )

    original_check = organizer._is_within_realpath
    check_calls = {"count": 0}

    def raced_check(root: str, path: str) -> bool:
        result = original_check(root, path)
        check_calls["count"] += 1
        if (
            not (check_calls["count"] == 0)
            and os.path.abspath(root) == os.path.abspath(str(output))
            and str(os.path.abspath(path)).startswith(str((output / "Course").resolve()))
        ):
            (output / "Course").symlink_to(escaped_payload)
            return True
        return result

    monkeypatch.setattr(organizer, "_is_within_realpath", raced_check)

    organizer.save_data(
        organizer._state_key("Course"),
        {
            "signature": organizer._coerce_signature(organizer._snapshot_signature(str(course))),
            "stable_count": 1,
        },
    )

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert check_calls["count"] >= 1
    assert check_calls["count"] >= 2
    assert not any(output.glob("*.mp4"))
    assert not any(escaped_payload.glob("*.mp4"))
    assert not any(
        path.suffix == ".mp4" and path.name.startswith(".tmp") for path in output.rglob("*")
    )
    assert not any(
        path.suffix == ".mp4" and path.name.startswith(".tmp") for path in escaped_payload.rglob("*")
    )


def test_process_course_source_swap_before_move_keeps_backup_and_external_bytes(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    external = tmp_path / "external"
    incoming.mkdir()
    output.mkdir()
    external.mkdir()

    course = incoming / "Course"
    course.mkdir()
    source_video = course / "lesson.mp4"
    source_video.write_bytes(b"safe-source")
    external_video = external / "outside.mp4"
    external_video.write_bytes(b"external-bytes")
    backup_video = course / "lesson.safe-backup.mp4"

    organizer = _organizer()
    organizer.save_data(
        organizer._state_key("Course"),
        {
            "signature": organizer._coerce_signature(organizer._snapshot_signature(str(course))),
            "stable_count": 1,
        },
    )

    original_move = organizer._move_file
    swapped = {"value": False}

    def swap_then_move(*args: object, **kwargs: object) -> bool:
        if not swapped["value"]:
            source_video.replace(backup_video)
            source_video.symlink_to(external_video)
            swapped["value"] = True
        return original_move(*args, **kwargs)

    monkeypatch.setattr(organizer, "_move_file", swap_then_move)

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert swapped["value"] is True
    assert backup_video.read_bytes() == b"safe-source"
    assert source_video.is_symlink()
    assert external_video.read_bytes() == b"external-bytes"
    assert not any(path.is_file() for path in output.rglob("*"))
    assert not any(path.name.startswith(".") and ".tmp." in path.name for path in output.rglob("*"))


def test_process_course_destination_parent_swap_before_move_stays_bound(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    escaped = tmp_path / "escaped"
    incoming.mkdir()
    output.mkdir()
    escaped.mkdir()

    course = incoming / "Course"
    course.mkdir()
    source_video = course / "lesson.mp4"
    source_video.write_bytes(b"safe-source")
    marker = escaped / "marker.txt"
    marker.write_bytes(b"marker")
    escaped_target = escaped / "payload"
    escaped_target.mkdir()
    target_parent = output / "Course" / "Season 1"

    organizer = _organizer()
    organizer.save_data(
        organizer._state_key("Course"),
        {
            "signature": organizer._coerce_signature(organizer._snapshot_signature(str(course))),
            "stable_count": 1,
        },
    )

    original_move = organizer._move_file
    swapped = {"value": False}

    def swap_then_move(*args: object, **kwargs: object) -> bool:
        if not swapped["value"]:
            target_parent.parent.mkdir(parents=True, exist_ok=True)
            target_parent.symlink_to(escaped_target)
            swapped["value"] = True
        return original_move(*args, **kwargs)

    monkeypatch.setattr(organizer, "_move_file", swap_then_move)

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert swapped["value"] is True
    assert source_video.read_bytes() == b"safe-source"
    assert marker.read_bytes() == b"marker"
    assert not any(path.is_file() for path in escaped_target.rglob("*"))
    assert not any(path.is_file() for path in output.rglob("*"))
    assert not any(path.name.startswith(".") and ".tmp." in path.name for path in tmp_path.rglob("*"))


def test_move_file_destination_race_is_no_clobber(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source = source_root / "lesson.mp4"
    target = target_root / "Season 1" / "lesson.mp4"
    source.write_bytes(b"source-bytes")
    target.parent.mkdir()
    target.write_bytes(b"raced-bytes")

    organizer = _organizer()
    assert organizer._move_file(
        str(source),
        str(target),
        source_root=str(source_root),
        target_root=str(target_root),
    ) is False
    assert source.read_bytes() == b"source-bytes"
    assert target.read_bytes() == b"raced-bytes"
    assert not any(path.name.startswith(".") and ".tmp." in path.name for path in target_root.rglob("*"))


def test_move_file_without_bound_roots_fails_closed(tmp_path):
    source = tmp_path / "source.mp4"
    target = tmp_path / "target" / "lesson.mp4"
    source.write_bytes(b"source-bytes")

    organizer = _organizer()
    assert organizer._move_file(str(source), str(target), source_root=None, target_root=None) is False
    assert source.read_bytes() == b"source-bytes"
    assert not target.exists()
    assert not target.parent.exists()


def test_rollback_quarantine_preserves_replacement_after_identity_check(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    created_fd = organizer._open_nofollow_dir_chain(
        context["target_root"]["fd"],
        "created",
        create=True,
        created=context["created_target_dirs"],
    )
    os.close(created_fd)
    original_noreplace = organizer._rename_noreplace
    raced = {"value": False}

    def noreplace_then_replace(parent_fd, source_name, target_parent_fd, target_name):
        result = original_noreplace(
            parent_fd, source_name, target_parent_fd, target_name
        )
        if result and source_name == "created" and target_name.startswith(
            ".courseorganizer-rollback-"
        ):
            os.mkdir("created", dir_fd=parent_fd)
            raced["value"] = True
        return result

    monkeypatch.setattr(organizer, "_rename_noreplace", noreplace_then_replace)
    try:
        organizer._rollback_move_context(context)
    finally:
        organizer._close_move_context(context)

    assert raced["value"] is True
    assert (target_root / "created").is_dir()
    quarantine_dirs = list(target_root.glob(".courseorganizer-rollback-*"))
    assert len(quarantine_dirs) == 1
    assert not list(quarantine_dirs[0].iterdir())


def test_rollback_quarantine_restores_late_content_to_original_name(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    created_fd = organizer._open_nofollow_dir_chain(
        context["target_root"]["fd"],
        "created",
        create=True,
        created=context["created_target_dirs"],
    )
    os.close(created_fd)
    original_noreplace = organizer._rename_noreplace
    injected = {"value": False}

    def noreplace_then_write(parent_fd, source_name, target_parent_fd, target_name):
        result = original_noreplace(
            parent_fd, source_name, target_parent_fd, target_name
        )
        if result and source_name == "created" and target_name.startswith(
            ".courseorganizer-rollback-"
        ):
            (target_root / target_name / "late.txt").write_bytes(b"late")
            injected["value"] = True
        return result

    monkeypatch.setattr(organizer, "_rename_noreplace", noreplace_then_write)
    try:
        organizer._rollback_move_context(context)
    finally:
        organizer._close_move_context(context)

    assert injected["value"] is True
    assert (target_root / "created" / "late.txt").read_bytes() == b"late"
    assert not list(target_root.glob(".courseorganizer-rollback-*"))


def test_rollback_quarantine_restores_write_after_initial_empty_check(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    created_fd = organizer._open_nofollow_dir_chain(
        context["target_root"]["fd"],
        "created",
        create=True,
        created=context["created_target_dirs"],
    )
    os.close(created_fd)
    original_noreplace = organizer._rename_noreplace
    original_fsync = organizer._fsync_dir
    state = {"candidate": None, "written": False}

    def capture_quarantine(parent_fd, source_name, target_parent_fd, target_name):
        result = original_noreplace(
            parent_fd, source_name, target_parent_fd, target_name
        )
        if result and source_name == "created":
            state["candidate"] = target_name
        return result

    def write_after_first_check(directory_fd):
        original_fsync(directory_fd)
        candidate = state["candidate"]
        if candidate is not None and not state["written"]:
            (target_root / candidate / "late.txt").write_bytes(b"late")
            state["written"] = True

    monkeypatch.setattr(organizer, "_rename_noreplace", capture_quarantine)
    monkeypatch.setattr(organizer, "_fsync_dir", write_after_first_check)
    try:
        organizer._rollback_move_context(context)
    finally:
        organizer._close_move_context(context)

    assert state["written"] is True
    assert (target_root / "created" / "late.txt").read_bytes() == b"late"
    assert not list(target_root.glob(".courseorganizer-rollback-*"))


def test_rollback_quarantine_keeps_late_content_when_name_is_replaced(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    created_fd = organizer._open_nofollow_dir_chain(
        context["target_root"]["fd"],
        "created",
        create=True,
        created=context["created_target_dirs"],
    )
    os.close(created_fd)
    original_noreplace = organizer._rename_noreplace
    injected = {"value": False}

    def noreplace_then_replace_and_write(
        parent_fd, source_name, target_parent_fd, target_name
    ):
        result = original_noreplace(
            parent_fd, source_name, target_parent_fd, target_name
        )
        if result and source_name == "created" and target_name.startswith(
            ".courseorganizer-rollback-"
        ):
            (target_root / "created").mkdir()
            (target_root / "created" / "replacement.txt").write_bytes(b"replacement")
            (target_root / target_name / "late.txt").write_bytes(b"late")
            injected["value"] = True
        return result

    monkeypatch.setattr(
        organizer, "_rename_noreplace", noreplace_then_replace_and_write
    )
    try:
        organizer._rollback_move_context(context)
    finally:
        organizer._close_move_context(context)

    assert injected["value"] is True
    assert (target_root / "created" / "replacement.txt").read_bytes() == b"replacement"
    quarantine_dirs = list(target_root.glob(".courseorganizer-rollback-*"))
    assert len(quarantine_dirs) == 1
    assert (quarantine_dirs[0] / "late.txt").read_bytes() == b"late"


def test_rollback_quarantine_candidate_replacement_is_not_deleted_or_overwritten(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    created_fd = organizer._open_nofollow_dir_chain(
        context["target_root"]["fd"],
        "created",
        create=True,
        created=context["created_target_dirs"],
    )
    os.close(created_fd)
    original_noreplace = organizer._rename_noreplace
    injected = {"value": False}

    def noreplace_then_replace_candidate(
        parent_fd, source_name, target_parent_fd, target_name
    ):
        result = original_noreplace(
            parent_fd, source_name, target_parent_fd, target_name
        )
        if result and source_name == "created" and target_name.startswith(
            ".courseorganizer-rollback-"
        ):
            original_candidate = f"{target_name}.original"
            os.rename(
                target_name,
                original_candidate,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.mkdir(target_name, dir_fd=parent_fd)
            (target_root / target_name / "replacement.txt").write_bytes(
                b"replacement"
            )
            injected["value"] = True
        return result

    monkeypatch.setattr(
        organizer, "_rename_noreplace", noreplace_then_replace_candidate
    )
    try:
        organizer._rollback_move_context(context)
    finally:
        organizer._close_move_context(context)

    assert injected["value"] is True
    candidate_dirs = list(target_root.glob(".courseorganizer-rollback-*"))
    assert len(candidate_dirs) == 2
    replacement = next(
        path for path in candidate_dirs if (path / "replacement.txt").exists()
    )
    assert (replacement / "replacement.txt").read_bytes() == b"replacement"


def test_rollback_quarantine_does_not_use_name_based_final_rmdir(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    created_fd = organizer._open_nofollow_dir_chain(
        context["target_root"]["fd"],
        "created",
        create=True,
        created=context["created_target_dirs"],
    )
    os.close(created_fd)

    def forbidden_rmdir(*args: object, **kwargs: object) -> None:
        raise AssertionError("created target cleanup must not use name-based rmdir")

    monkeypatch.setattr(os, "rmdir", forbidden_rmdir)
    try:
        organizer._remove_created_target_dirs(context)
    finally:
        organizer._close_move_context(context)

    assert not (target_root / "created").exists()
    assert len(list(target_root.glob(".courseorganizer-rollback-*"))) == 1


def test_rollback_quarantine_nested_created_tree_uses_one_outer_root(
    tmp_path,
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    nested_fd = organizer._open_nofollow_dir_chain(
        context["target_root"]["fd"],
        "created/child",
        create=True,
        created=context["created_target_dirs"],
    )
    os.close(nested_fd)
    leaf_fd = organizer._open_nofollow_dir_chain(
        context["target_root"]["fd"],
        "created/child/leaf",
        create=True,
        created=context["created_target_dirs"],
    )
    os.close(leaf_fd)
    try:
        organizer._rollback_move_context(context)
    finally:
        organizer._close_move_context(context)

    assert not (target_root / "created").exists()
    quarantine_dirs = list(target_root.glob(".courseorganizer-rollback-*"))
    assert len(quarantine_dirs) == 1
    assert (quarantine_dirs[0] / "child" / "leaf").is_dir()
    assert not list(quarantine_dirs[0].rglob(".courseorganizer-rollback-*"))


@pytest.mark.parametrize(
    "invalid_entry",
    [
        ("/a", (1, 2)),
        ("a/../a", (1, 2)),
        ("a/./b", (1, 2)),
        ("a//b", (1, 2)),
        ("a/", (1, 2)),
        ("../a", (1, 2)),
        ("a\x00b", (1, 2)),
        ("a\\b", (1, 2)),
        (None, (1, 2)),
        (123, (1, 2)),
        ("other", (True, 2)),
        ("other", (1,)),
        ("other", (1, "2")),
        ("other", [1, 2]),
        ("other", (-1, 2)),
        ("other", (1, -1)),
        (("other", (1, 2), "extra"),),
        ("valid", (1, 2)),
    ],
)
def test_remove_created_target_dirs_rejects_invalid_ledger_before_io(
    tmp_path, monkeypatch, invalid_entry
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    tracked = target_root / "valid"
    tracked.mkdir()
    (tracked / "keep.txt").write_bytes(b"keep")
    tracked_stat = tracked.stat()
    valid_entry = ("valid", (int(tracked_stat.st_dev), int(tracked_stat.st_ino)))
    ledger = [valid_entry, invalid_entry]
    original_ledger = ledger
    original_ledger_content = list(ledger)
    context["created_target_dirs"] = ledger
    io_calls = {"open": 0, "lstat": 0, "rename": 0}

    def snapshot(path):
        entries = [path, *sorted(path.rglob("*"))]
        result = []
        for entry in entries:
            info = entry.lstat()
            content = entry.read_bytes() if stat.S_ISREG(info.st_mode) else None
            result.append(
                (
                    str(entry.relative_to(target_root)),
                    int(info.st_dev),
                    int(info.st_ino),
                    int(info.st_mode),
                    int(info.st_size),
                    content,
                )
            )
        return tuple(result)

    before = snapshot(target_root)

    original_open = os.open
    original_stat = os.stat

    def count_open(*args, **kwargs):
        io_calls["open"] += 1
        return original_open(*args, **kwargs)

    def count_lstat(*args, **kwargs):
        io_calls["lstat"] += 1
        return original_stat(*args, **kwargs)

    def count_rename(*args, **kwargs):
        io_calls["rename"] += 1
        return False

    monkeypatch.setattr(os, "open", count_open)
    monkeypatch.setattr(os, "stat", count_lstat)
    monkeypatch.setattr(organizer, "_rename_noreplace", count_rename)
    try:
        organizer._remove_created_target_dirs(context)
    finally:
        organizer._close_move_context(context)

    assert io_calls == {"open": 0, "lstat": 0, "rename": 0}
    assert context["created_target_dirs"] is original_ledger
    assert context["created_target_dirs"] == original_ledger_content
    assert snapshot(target_root) == before
    assert not list(target_root.glob(".courseorganizer-rollback-*"))


def test_move_file_exdev_copy_preserves_metadata_and_cleans_publish_error(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source = source_root / "lesson.mp4"
    target = target_root / "Season 1" / "lesson.mp4"
    source.write_bytes(b"cross-filesystem-bytes")
    source.chmod(0o640)
    source_atime_ns = 1_650_000_000_123_456_789
    source_mtime_ns = 1_650_000_100_123_456_789
    os.utime(source, ns=(source_atime_ns, source_mtime_ns))

    organizer = _organizer()
    original_link = os.link
    link_calls = {"count": 0}

    def exdev_once(src: str, dst: str, **kwargs: object) -> None:
        link_calls["count"] += 1
        if link_calls["count"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        original_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", exdev_once)
    assert organizer._move_file(
        str(source),
        str(target),
        source_root=str(source_root),
        target_root=str(target_root),
    ) is True
    assert not source.exists()
    target_stat = target.stat()
    assert target.read_bytes() == b"cross-filesystem-bytes"
    assert stat.S_IMODE(target_stat.st_mode) == 0o640
    assert target_stat.st_atime_ns == source_atime_ns
    assert target_stat.st_mtime_ns == source_mtime_ns
    assert not any(path.name.startswith(".") and ".tmp." in path.name for path in target_root.rglob("*"))

    error_source = source_root / "error.mp4"
    error_target = target_root / "Season 1" / "error.mp4"
    error_source.write_bytes(b"must-remain")
    link_calls["count"] = 0

    def exdev_publish_error(src: str, dst: str, **kwargs: object) -> None:
        link_calls["count"] += 1
        if link_calls["count"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        raise OSError(errno.EIO, "publish failure")

    monkeypatch.setattr(os, "link", exdev_publish_error)
    assert organizer._move_file(
        str(error_source),
        str(error_target),
        source_root=str(source_root),
        target_root=str(target_root),
    ) is False
    assert error_source.read_bytes() == b"must-remain"
    assert not error_target.exists()
    assert not any(path.name.startswith(".") and ".tmp." in path.name for path in target_root.rglob("*"))


def test_move_file_source_replacement_at_stage_boundary_is_restored_without_success(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    external_root = tmp_path / "external"
    source_root.mkdir()
    target_root.mkdir()
    external_root.mkdir()
    source = source_root / "lesson.mp4"
    backup = source_root / "lesson.safe-backup.mp4"
    external = external_root / "outside.mp4"
    target = target_root / "lesson.mp4"
    source.write_bytes(b"safe-source")
    external.write_bytes(b"external-bytes")

    original_rename = os.rename
    swapped = {"value": False}

    def replace_before_stage(src: str, dst: str, **kwargs: object) -> None:
        if not swapped["value"] and kwargs.get("src_dir_fd") is not None:
            source.replace(backup)
            source.symlink_to(external)
            swapped["value"] = True
        original_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", replace_before_stage)
    organizer = _organizer()

    assert organizer._move_file(
        str(source), str(target), source_root=str(source_root), target_root=str(target_root)
    ) is False
    assert swapped["value"] is True
    assert backup.read_bytes() == b"safe-source"
    assert source.read_bytes() == b"safe-source"
    stage_dirs = list(source_root.glob(".courseorganizer-stage*"))
    assert len(stage_dirs) == 1
    staged_replacement = stage_dirs[0] / "00000001-lesson.mp4"
    assert staged_replacement.is_symlink()
    assert staged_replacement.resolve() == external
    assert external.read_bytes() == b"external-bytes"
    assert not os.path.lexists(target)
    assert not list(target_root.rglob("*.tmp.*"))


def test_stage_source_open_emfile_fails_closed_before_rename(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source = source_root / "lesson.mp4"
    source.write_bytes(b"safe-source")
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    captured = organizer._capture_move_identity(context, str(source))
    assert captured is not None
    original_open = os.open
    failed = {"value": False}

    def fail_source_open(path, *args, **kwargs):
        if path == "lesson.mp4" and kwargs.get("dir_fd") is not None:
            failed["value"] = True
            raise OSError(errno.EMFILE, "source fd limit")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_source_open)
    try:
        assert organizer._stage_move_source(context, str(source), captured) is None
        assert failed["value"] is True
        assert source.read_bytes() == b"safe-source"
        stage_dirs = list(source_root.glob(".courseorganizer-stage*"))
        assert len(stage_dirs) == 1
        assert list(stage_dirs[0].iterdir()) == []
    finally:
        organizer._rollback_move_context(context)
        organizer._close_move_context(context)


def test_stage_source_fstat_emfile_closes_held_fd_before_rename(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source = source_root / "lesson.mp4"
    source.write_bytes(b"safe-source")
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    captured = organizer._capture_move_identity(context, str(source))
    assert captured is not None
    original_open = os.open
    original_fstat = os.fstat
    original_close = os.close
    state = {"fd": None, "failed": False, "closed": False}

    def track_source_open(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if path == "lesson.mp4" and kwargs.get("dir_fd") is not None:
            state["fd"] = fd
        return fd

    def fail_source_fstat(fd):
        if fd == state["fd"] and not state["failed"]:
            state["failed"] = True
            raise OSError(errno.EMFILE, "source stat limit")
        return original_fstat(fd)

    def track_close(fd):
        if fd == state["fd"]:
            state["closed"] = True
        return original_close(fd)

    monkeypatch.setattr(os, "open", track_source_open)
    monkeypatch.setattr(os, "fstat", fail_source_fstat)
    monkeypatch.setattr(os, "close", track_close)
    try:
        assert organizer._stage_move_source(context, str(source), captured) is None
        assert state == {"fd": state["fd"], "failed": True, "closed": True}
        assert source.read_bytes() == b"safe-source"
        stage_dirs = list(source_root.glob(".courseorganizer-stage*"))
        assert len(stage_dirs) == 1
        assert list(stage_dirs[0].iterdir()) == []
    finally:
        organizer._rollback_move_context(context)
        organizer._close_move_context(context)


def test_stage_original_fd_is_held_until_context_close(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source = source_root / "lesson.mp4"
    source.write_bytes(b"safe-source")
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    captured = organizer._capture_move_identity(context, str(source))
    assert captured is not None
    item = organizer._stage_move_source(context, str(source), captured)
    assert item is not None
    original_fd = item["original_fd"]
    assert os.fstat(original_fd).st_ino == item["identity"][1]
    organizer._rollback_move_context(context)
    organizer._close_move_context(context)
    with pytest.raises(OSError):
        os.fstat(original_fd)


def test_stage_path_replacement_after_rename_restores_original_and_keeps_replacement(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    external_root = tmp_path / "external"
    source_root.mkdir()
    target_root.mkdir()
    external_root.mkdir()
    source = source_root / "lesson.mp4"
    external = external_root / "outside.mp4"
    target = target_root / "lesson.mp4"
    source.write_bytes(b"safe-source")
    external.write_bytes(b"replacement")
    original_rename = os.rename
    swapped = {"value": False}

    def replace_stage_after_rename(src: str, dst: str, **kwargs: object) -> None:
        original_rename(src, dst, **kwargs)
        if not swapped["value"] and kwargs.get("dst_dir_fd") is not None:
            os.unlink(dst, dir_fd=kwargs["dst_dir_fd"])
            os.symlink(str(external), dst, dir_fd=kwargs["dst_dir_fd"])
            swapped["value"] = True

    monkeypatch.setattr(os, "rename", replace_stage_after_rename)
    organizer = _organizer()
    assert organizer._move_file(
        str(source), str(target), source_root=str(source_root), target_root=str(target_root)
    ) is False
    assert swapped["value"] is True
    assert source.read_bytes() == b"safe-source"
    stage_dirs = list(source_root.glob(".courseorganizer-stage*"))
    assert len(stage_dirs) == 1
    staged_replacement = stage_dirs[0] / "00000001-lesson.mp4"
    assert staged_replacement.is_symlink()
    assert staged_replacement.resolve() == external
    assert not os.path.lexists(target)


def test_stage_path_inode_replacement_prefers_held_original_link(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    external_root = tmp_path / "external"
    source_root.mkdir()
    target_root.mkdir()
    external_root.mkdir()
    source = source_root / "lesson.mp4"
    external = external_root / "outside.mp4"
    target = target_root / "lesson.mp4"
    source.write_bytes(b"safe-source")
    external.write_bytes(b"replacement")
    original_inode = source.stat().st_ino
    original_rename = os.rename
    swapped = {"value": False}

    def replace_stage_with_held_original(
        src: str, dst: str, **kwargs: object
    ) -> None:
        original_rename(src, dst, **kwargs)
        if not swapped["value"] and kwargs.get("dst_dir_fd") is not None:
            stage_fd = kwargs["dst_dir_fd"]
            held_name = "held-original"
            original_rename(
                dst,
                held_name,
                src_dir_fd=stage_fd,
                dst_dir_fd=stage_fd,
            )
            os.symlink(str(external), dst, dir_fd=stage_fd)
            swapped["value"] = True

    monkeypatch.setattr(os, "rename", replace_stage_with_held_original)
    organizer = _organizer()

    assert organizer._move_file(
        str(source), str(target), source_root=str(source_root), target_root=str(target_root)
    ) is False
    assert swapped["value"] is True
    assert source.read_bytes() == b"safe-source"
    assert source.stat().st_ino == original_inode
    assert not os.path.lexists(target)
    stage_dirs = list(source_root.glob(".courseorganizer-stage*"))
    assert len(stage_dirs) == 1
    stage_entries = list(stage_dirs[0].iterdir())
    assert [entry.name for entry in stage_entries] == ["00000001-lesson.mp4"]
    assert stage_entries[0].is_symlink()
    assert stage_entries[0].resolve() == external
    assert not any(
        not entry.is_symlink() and entry.stat().st_ino == original_inode
        for entry in stage_entries
    )


def test_stage_path_inode_replacement_closes_bound_fds_after_restore(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    external_root = tmp_path / "external"
    source_root.mkdir()
    target_root.mkdir()
    external_root.mkdir()
    source = source_root / "lesson.mp4"
    external = external_root / "outside.mp4"
    source.write_bytes(b"safe-source")
    external.write_bytes(b"replacement")
    original_rename = os.rename
    swapped = {"value": False}

    def replace_stage_with_held_original(
        src: str, dst: str, **kwargs: object
    ) -> None:
        original_rename(src, dst, **kwargs)
        if not swapped["value"] and kwargs.get("dst_dir_fd") is not None:
            stage_fd = kwargs["dst_dir_fd"]
            original_rename(
                dst,
                "held-original",
                src_dir_fd=stage_fd,
                dst_dir_fd=stage_fd,
            )
            os.symlink(str(external), dst, dir_fd=stage_fd)
            swapped["value"] = True

    monkeypatch.setattr(os, "rename", replace_stage_with_held_original)
    organizer = _organizer()
    context = organizer._create_move_context(str(source_root), str(target_root))
    assert context is not None
    captured = organizer._capture_move_identity(context, str(source))
    assert captured is not None
    bound_fds = [
        context["stage_fd"],
        context["source_root"]["fd"],
        context["target_root"]["fd"],
    ]
    item = organizer._stage_move_source(context, str(source), captured)
    assert item is None
    assert swapped["value"] is True
    assert source.read_bytes() == b"safe-source"
    assert not list(target_root.iterdir())
    organizer._rollback_move_context(context)
    organizer._close_move_context(context)
    for fd in bound_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_process_course_rejects_bound_real_directory_root_replacement(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    source = course / "lesson.mp4"
    source.write_bytes(b"safe-source")

    organizer = _organizer()
    organizer.save_data(
        organizer._state_key("Course"),
        {
            "signature": organizer._coerce_signature(organizer._snapshot_signature(str(course))),
            "stable_count": 1,
        },
    )
    original_move = organizer._move_file
    replacement = tmp_path / "course-replacement"
    swapped = {"value": False}

    def replace_root_then_move(*args: object, **kwargs: object) -> bool:
        if not swapped["value"]:
            course.rename(replacement)
            course.mkdir()
            swapped["value"] = True
        return original_move(*args, **kwargs)

    monkeypatch.setattr(organizer, "_move_file", replace_root_then_move)
    assert organizer._process_course("Course", str(course), str(output)) is False
    assert swapped["value"] is True
    assert (replacement / "lesson.mp4").read_bytes() == b"safe-source"
    assert not any(path.is_file() for path in output.rglob("*"))
    assert not list(replacement.glob(".courseorganizer-stage*"))


def test_move_file_exdev_clears_special_bits_on_destination(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source = source_root / "lesson.mp4"
    target = target_root / "Season 1" / "lesson.mp4"
    source.write_bytes(b"special-mode")
    source.chmod(0o6750)
    os.utime(source, ns=(1_650_000_000_123_456_789, 1_650_000_100_123_456_789))

    original_link = os.link
    calls = {"count": 0}

    def exdev_once(src: str, dst: str, **kwargs: object) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        original_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", exdev_once)
    organizer = _organizer()
    assert organizer._move_file(
        str(source), str(target), source_root=str(source_root), target_root=str(target_root)
    ) is True
    target_stat = target.stat()
    assert stat.S_IMODE(target_stat.st_mode) == 0o750
    assert target_stat.st_mtime_ns == 1_650_000_100_123_456_789
    assert not source.exists()
    assert not list(target_root.rglob("*.tmp.*"))


def test_move_file_failed_same_fs_validation_preserves_raced_target(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source = source_root / "lesson.mp4"
    target = target_root / "lesson.mp4"
    source.write_bytes(b"safe-source")

    original_link = os.link
    raced = {"value": False}

    def publish_then_race(src: str, dst: str, **kwargs: object) -> None:
        original_link(src, dst, **kwargs)
        if not raced["value"] and dst == target.name:
            raced["value"] = True
            os.unlink(target)
            target.write_bytes(b"raced-target")

    monkeypatch.setattr(os, "link", publish_then_race)
    organizer = _organizer()
    assert organizer._move_file(
        str(source), str(target), source_root=str(source_root), target_root=str(target_root)
    ) is False
    assert raced["value"] is True
    assert source.read_bytes() == b"safe-source"
    assert target.read_bytes() == b"raced-target"
    assert not list(source_root.glob(".courseorganizer-stage*"))
    assert not list(target_root.rglob("*.tmp.*"))


def test_process_course_commit_cleanup_failure_restores_from_published_descriptor(
    tmp_path, monkeypatch
):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    first = course / "a.mp4"
    second = course / "b.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    organizer = _organizer()
    organizer.save_data(
        organizer._state_key("Course"),
        {
            "signature": organizer._coerce_signature(organizer._snapshot_signature(str(course))),
            "stable_count": 1,
        },
    )
    first_target = output / "Course" / "Season 1" / "Course - S01E001.mp4"
    second_target = output / "Course" / "Season 1" / "Course - S01E002.mp4"
    original_unlink = organizer._unlink_if_identity
    stage_cleanups = []
    failure_injected = {"value": False}

    def fail_second_staged_cleanup(parent_fd, name, expected):
        prefix, separator, _ = name.partition("-")
        is_stage_name = separator and len(prefix) == 8 and all(
            character in "0123456789abcdef" for character in prefix
        )
        if is_stage_name:
            stage_cleanups.append(name)
            if len(stage_cleanups) == 2 and not failure_injected["value"]:
                first_target.unlink()
                first_target.write_bytes(b"raced-target")
                failure_injected["value"] = True
                return False
        return original_unlink(parent_fd, name, expected)

    monkeypatch.setattr(organizer, "_unlink_if_identity", fail_second_staged_cleanup)

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert failure_injected["value"] is True
    assert stage_cleanups[:2] == ["00000001-a.mp4", "00000002-b.mp4"]
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert first_target.read_bytes() == b"raced-target"
    assert not second_target.exists()
    assert not list(course.glob(".courseorganizer-stage*"))
    assert not list(output.rglob("*.tmp.*"))


def test_process_course_later_plan_failure_restores_earlier_sources_and_targets(
    tmp_path, monkeypatch
):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    first = course / "a.mp4"
    second = course / "b.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    organizer = _organizer()
    organizer.save_data(
        organizer._state_key("Course"),
        {
            "signature": organizer._coerce_signature(organizer._snapshot_signature(str(course))),
            "stable_count": 1,
        },
    )
    original_move = organizer._move_file
    calls = {"count": 0}

    def fail_second(*args: object, **kwargs: object) -> bool:
        calls["count"] += 1
        if calls["count"] == 2:
            return False
        return original_move(*args, **kwargs)

    monkeypatch.setattr(organizer, "_move_file", fail_second)
    assert organizer._process_course("Course", str(course), str(output)) is False
    assert calls["count"] == 2
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert not any(path.is_file() for path in output.rglob("*"))
    assert not list(course.glob(".courseorganizer-stage*"))
    assert not list(output.rglob("*.tmp.*"))


def test_run_skips_when_output_roots_are_nested(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    tv_output = tmp_path / "tv"
    movie_output = tv_output / "movie"
    children_output = tmp_path / "children"

    incoming.mkdir()
    movie_output.mkdir(parents=True)
    children_output.mkdir()

    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "tv_output": str(tv_output),
            "movie_output": str(movie_output),
            "children_output": str(children_output),
        }
    )

    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)
    called = []

    def blocked_course(*args: object, **kwargs: object) -> bool:
        called.append(True)
        return True

    monkeypatch.setattr(organizer, "_process_course", blocked_course)

    organizer._run()

    assert called == []
    assert not list(movie_output.iterdir())
    assert not list(children_output.iterdir())
    assert any("output paths must not contain one another" in err for err in logger.errors)


def test_run_allows_distinct_existing_output_roots_for_non_nested_case(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    tv_output = tmp_path / "tv"
    movie_output = tmp_path / "movie"
    children_output = tmp_path / "children"

    incoming.mkdir()
    tv_output.mkdir()
    movie_output.mkdir()
    children_output.mkdir()

    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "tv_output": str(tv_output),
            "movie_output": str(movie_output),
            "children_output": str(children_output),
        }
    )

    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)
    called = []

    def blocked_course(*args: object, **kwargs: object) -> bool:
        called.append(True)
        return True

    monkeypatch.setattr(organizer, "_process_course", blocked_course)

    organizer._run()

    assert called == [True]
    assert not logger.errors


def test_run_logs_scan_started_and_scan_completed_with_counts_and_no_path_leakage(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    tv_output = tmp_path / "tv"
    movie_output = tmp_path / "movie"
    children_output = tmp_path / "children"
    output_roots = [tv_output, movie_output, children_output]

    incoming.mkdir()
    for output_root in output_roots:
        output_root.mkdir()

    for course_name in ("CourseA", "CourseB"):
        course = incoming / course_name
        course.mkdir()
        (course / "lesson.mp4").write_bytes(f"{course_name}".encode())

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "tv_output": str(tv_output),
            "movie_output": str(movie_output),
            "children_output": str(children_output),
            "naming_mode": "apply",
        }
    )

    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    def simulated_process_course(course_name: str, *args: object, **kwargs: object) -> bool:
        return course_name == "CourseA"

    monkeypatch.setattr(organizer, "_process_course", simulated_process_course)

    organizer._run()
    organizer._run(force=True)

    scan_started = [
        msg for msg in logger.infos if "CourseOrganizer[event=scan_started]" in msg
    ]
    assert len(scan_started) == 2
    assert any("trigger=scheduled" in msg and "mode=apply" in msg for msg in scan_started)
    assert any("trigger=manual" in msg and "mode=apply" in msg for msg in scan_started)

    scan_completed = [
        msg for msg in logger.infos if "CourseOrganizer[event=scan_completed]" in msg
    ]
    assert len(scan_completed) == 2
    assert any("trigger=scheduled" in msg for msg in scan_completed)
    assert any("trigger=manual" in msg for msg in scan_completed)
    for message in scan_completed:
        counts = _event_counts_from_log(message)
        assert counts["scanned"] == 2
        assert counts["moved"] == 1
    all_logged = logger.infos + logger.debugs
    assert not any(str(root) in msg for root in output_roots for msg in all_logged)


def test_run_logs_item_error_with_item_keys(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    tv_output = tmp_path / "tv"
    movie_output = tmp_path / "movie"
    children_output = tmp_path / "children"

    incoming.mkdir()
    tv_output.mkdir()
    movie_output.mkdir()
    children_output.mkdir()

    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "incoming": str(incoming),
            "tv_output": str(tv_output),
            "movie_output": str(movie_output),
            "children_output": str(children_output),
            "naming_mode": "off",
        }
    )
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    exception_payload = f"boom:{course.resolve()}/private-token"

    def failed_course(*args: object, **kwargs: object) -> bool:
        raise RuntimeError(exception_payload)

    monkeypatch.setattr(organizer, "_process_course", failed_course)

    organizer._run()

    structured_errors = [msg for msg in logger.infos if "CourseOrganizer[event=item_error]" in msg]
    assert structured_errors
    assert any(
        "CourseOrganizer[event=item_error] item_course=Course" in msg
        and "item_reason=RuntimeError" in msg
        and "item_error=exception" in msg
        for msg in structured_errors
    )

    all_logged = logger.infos + logger.errors + logger.debugs
    item_lines = [msg for msg in all_logged if "item_" in msg]
    assert item_lines
    assert all("CourseOrganizer[event=" in msg for msg in item_lines)
    assert all(exception_payload not in msg for msg in all_logged)
    assert all(str(course.resolve()) not in msg for msg in all_logged)


def test_process_course_logs_second_incomplete_check_event(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    organizer = CourseOrganizer(config={"enabled": True, "naming_mode": "off"})
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    calls = {"count": 0}

    def delayed_incomplete(_course_path: str) -> bool:
        calls["count"] += 1
        return calls["count"] in {1, 4}

    monkeypatch.setattr(organizer, "_has_incomplete_file", delayed_incomplete)

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert organizer._process_course("Course", str(course), str(output)) is False
    assert organizer._process_course("Course", str(course), str(output)) is False

    assert any(
        "CourseOrganizer[event=incomplete_blocked] item_phase=initial item_course=Course" in msg
        for msg in logger.debugs
    )
    assert any(
        "CourseOrganizer[event=incomplete_blocked] item_phase=final item_course=Course" in msg
        for msg in logger.debugs
    )


def test_process_course_logs_no_media_event(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    (course / "notes.txt").write_text("notes")

    organizer = CourseOrganizer(config={"enabled": True, "naming_mode": "off"})
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert organizer._process_course("Course", str(course), str(output)) is False

    assert any("CourseOrganizer[event=no_media]" in msg for msg in logger.debugs)


def test_process_course_logs_preview_event_without_moving(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    organizer = CourseOrganizer(config={"enabled": True, "naming_mode": "preview"})
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert organizer._process_course("Course", str(course), str(output)) is False
    preview_message = next(msg for msg in logger.infos if "CourseOrganizer[event=preview]" in msg)
    assert "CourseOrganizer[event=preview] item_course=Course" in preview_message
    assert "item_final=" in preview_message
    assert "item_library=" in preview_message
    assert not list(output.iterdir())


def test_process_course_logs_naming_blocked_event(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    organizer = CourseOrganizer(config={"enabled": True, "naming_mode": "apply"})
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    def blocked_naming(*args: object, **kwargs: object) -> MODULE.NamingDecision:
        return MODULE.NamingDecision(
            status="uncertain",
            raw_title="Course",
            local_title="Course",
            final_root="Course",
            final_prefix="Course",
        )

    monkeypatch.setattr(organizer, "_resolve_naming", blocked_naming)

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert organizer._process_course("Course", str(course), str(output)) is False
    assert any(
        "CourseOrganizer[event=naming_blocked] item_course=Course item_reason=uncertain" in msg
        for msg in logger.debugs
    )


def test_process_course_logs_library_hold_event(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    tv_output = tmp_path / "tv"
    movie_output = tmp_path / "movie"
    children_output = tmp_path / "children"
    incoming.mkdir()
    tv_output.mkdir()
    movie_output.mkdir()
    children_output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    organizer = CourseOrganizer(
        config={
            "enabled": True,
            "naming_mode": "apply",
            "incoming": str(incoming),
            "tv_output": str(tv_output),
            "movie_output": str(movie_output),
            "children_output": str(children_output),
        },
    )
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    def hold_route(*args: object, **kwargs: object) -> MODULE.LibraryRouteResult:
        return MODULE.LibraryRouteResult(
            accepted=False,
            library="hold",
            confidence=0.0,
            reason_codes=("hold",),
            error="",
        )

    class _ResolverStub:
        def resolve(self, *args: object, **kwargs: object) -> MODULE.NamingDecision:
            course_name = str(args[0]) if args else "Course"
            return MODULE.NamingDecision(
                status="auto_external",
                raw_title=course_name,
                local_title=course_name,
                final_root=course_name,
                final_prefix=course_name,
            )

        def record_decision(self, *args: object, **kwargs: object) -> MODULE.NamingDecision:
            return args[0]

    monkeypatch.setattr(organizer, "_resolve_library_route", hold_route)
    monkeypatch.setattr(organizer, "_get_resolver", lambda: _ResolverStub())

    assert organizer._process_course("Course", str(course)) is False
    assert organizer._process_course("Course", str(course)) is False
    library_hold = next(
        msg for msg in logger.debugs if "CourseOrganizer[event=library_hold]" in msg
    )
    assert "CourseOrganizer[event=library_hold] item_course=Course" in library_hold
    assert "item_confidence=0.000" in library_hold
    assert "item_reasons=hold" in library_hold


def test_process_course_logs_legacy_conflict_event(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")
    (output / "Course").mkdir()

    organizer = CourseOrganizer(
        config={"enabled": True, "naming_mode": "apply"},
    )
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    def renamed_target(*args: object, **kwargs: object) -> MODULE.NamingDecision:
        return MODULE.NamingDecision(
            status="auto_external",
            raw_title="Course",
            local_title="Course",
            final_root="Renamed Course",
            final_prefix="Renamed Course",
        )

    class _ResolverStub:
        def resolve(self, *args: object, **kwargs: object) -> MODULE.NamingDecision:
            raise AssertionError("resolve should not be called when naming is overridden")

        def record_decision(self, *args: object, **kwargs: object) -> MODULE.NamingDecision:
            return args[0]

        def record_output_conflict(self, *args: object, **kwargs: object) -> MODULE.NamingDecision:
            return args[0]

    monkeypatch.setattr(organizer, "_resolve_naming", renamed_target)
    monkeypatch.setattr(organizer, "_get_resolver", lambda: _ResolverStub())

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert organizer._process_course("Course", str(course), str(output)) is False
    legacy_conflict = next(
        msg for msg in logger.debugs if "CourseOrganizer[event=legacy_conflict]" in msg
    )
    assert "CourseOrganizer[event=legacy_conflict] item_course=Course" in legacy_conflict
    assert "item_final=Renamed Course" in legacy_conflict
    assert "item_library=" in legacy_conflict


def test_process_course_logs_move_started_and_completed_events(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    output = tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    course = incoming / "Course"
    course.mkdir()
    (course / "lesson.mp4").write_bytes(b"video")

    organizer = CourseOrganizer(config={"enabled": True, "naming_mode": "off"})
    logger = _RunLogger()
    monkeypatch.setattr(organizer, "_logger", logger)

    assert organizer._process_course("Course", str(course), str(output)) is False
    assert organizer._process_course("Course", str(course), str(output)) is True
    move_started = next(msg for msg in logger.infos if "CourseOrganizer[event=move_started]" in msg)
    assert "CourseOrganizer[event=move_started] item_course=Course" in move_started
    assert "item_final=Course" in move_started
    assert "item_library=legacy" in move_started
    assert "item_media_count=1" in move_started
    assert any(
        "CourseOrganizer[event=move_completed] item_course=Course" in msg
        and "item_final=Course" in msg
        and "item_library=legacy" in msg
        and "item_moved=1" in msg
        and "item_subtitles=0" in msg
        for msg in logger.infos
    )
