import importlib.util
import json
from pathlib import Path


PLUGIN_PATH = (
    Path(__file__).parents[1] / "plugins.v2" / "courseorganizer" / "__init__.py"
)
SPEC = importlib.util.spec_from_file_location("courseorganizer", PLUGIN_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
CourseOrganizer = MODULE.CourseOrganizer


def test_plugin_version_matches_package_metadata():
    package_path = Path(__file__).parents[1] / "package.v2.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))

    assert CourseOrganizer.plugin_version == "1.2.0"
    assert package["CourseOrganizer"]["version"] == CourseOrganizer.plugin_version


def _organizer():
    return CourseOrganizer(config={"enabled": True})


def _process_stable_course(organizer, incoming, output, course_name):
    course_path = incoming / course_name
    assert organizer._process_course(course_name, str(course_path), str(output)) is False
    assert organizer._process_course(course_name, str(course_path), str(output)) is True


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
