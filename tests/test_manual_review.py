import copy
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.courseorganizer_testkit import load_courseorganizer


MODULE = load_courseorganizer()
CourseOrganizer = MODULE.CourseOrganizer
naming = MODULE.naming


class _TestNativeAdapter:
    def __init__(self, config):
        self.config = dict(config)
        self.calls = []
        self._title = ""

    def get_directory_rules(self):
        return [
            {
                "name": "电视剧",
                "download_path": self.config["incoming"],
                "library_path": self.config["tv_output"],
                "media_type": "电视剧",
                "storage": "local",
                "library_storage": "local",
                "transfer_type": "move",
                "renaming": True,
                "naming_format": "{{title}} {{year}}/Season {{season}}/{{title}} - {{season}}x{{episode}} - 第{{episode}}集",
            },
            {
                "name": "电影",
                "download_path": self.config["incoming"],
                "library_path": self.config["movie_output"],
                "media_type": "电影",
                "storage": "local",
                "library_storage": "local",
                "transfer_type": "move",
                "renaming": True,
                "movie_naming_format": "{{title}} ({{year}})",
            },
            {
                "name": "儿童课程",
                "download_path": self.config["incoming"],
                "library_path": self.config["children_output"],
                "media_type": "电视剧",
                "storage": "local",
                "library_storage": "local",
                "transfer_type": "move",
                "renaming": True,
            },
        ]

    def get_file_item(self, source_path, source_storage="local"):
        return Path(source_path) if Path(source_path).exists() else None

    @contextmanager
    def rename_context(self, source_tree, final_title):
        previous = self._title
        self._title = final_title
        try:
            yield
        finally:
            self._title = previous

    def manual_transfer(self, **kwargs):
        self.calls.append(dict(kwargs))
        source = Path(kwargs["fileitem"])
        media_files = [
            item
            for item in source.rglob("*")
            if item.is_file() and item.suffix.lower() in CourseOrganizer.MEDIA_EXTENSIONS
        ]
        if not media_files:
            return False, "没有媒体文件"
        target = Path(kwargs["target_path"]) / self._title / "Season 1"
        if target.parent.exists():
            return False, "目标已存在"
        target.mkdir(parents=True, exist_ok=False)
        for index, item in enumerate(sorted(media_files), start=1):
            shutil.move(
                str(item),
                str(target / f"{self._title} - S01E{index:03d}{item.suffix}"),
            )
        shutil.rmtree(source)
        return True, ""


class _NoMediaNativeAdapter(_TestNativeAdapter):
    def manual_transfer(self, **kwargs):
        self.calls.append(dict(kwargs))
        return False, "没有找到可整理的媒体文件"


def _response_data(response):
    return response.data if hasattr(response, "data") else response["data"]


def _success(response):
    return bool(response.success) if hasattr(response, "success") else bool(response["success"])


def _message(response):
    return response.message if hasattr(response, "message") else response["message"]


def _organizer(rows, **config_overrides):
    classifier = config_overrides.pop("library_classifier", None)
    metadata_provider = config_overrides.pop("metadata_provider", None)
    incoming = config_overrides.get("incoming")
    test_tempdir = None
    if incoming is None:
        test_tempdir = tempfile.TemporaryDirectory(prefix="courseorganizer-review-")
        incoming = test_tempdir.name
        config_overrides["incoming"] = incoming
    config = {
        "naming_mode": "preview",
        "incoming": incoming,
        "tv_output": "/tv",
        "movie_output": "/movies",
        "children_output": "/children",
        "naming_manual_overrides": "legacy => local:旧名称",
    }
    config.update(config_overrides)
    native_adapter = config_overrides.pop("native_adapter", None) or _TestNativeAdapter(config)
    organizer = CourseOrganizer(
        config=config,
        library_classifier=classifier,
        metadata_provider=metadata_provider,
        native_adapter=native_adapter,
    )
    if test_tempdir is not None:
        organizer._test_tempdir = test_tempdir
    for row in rows:
        raw_title = row.get("raw_title") if isinstance(row, dict) else None
        valid, _ = naming.validate_manual_raw_title(raw_title)
        if not valid:
            continue
        source_path = Path(incoming) / raw_title
        try:
            if os.path.commonpath((os.path.realpath(incoming), os.path.realpath(source_path))) != os.path.realpath(incoming):
                continue
            source_path.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError):
            continue
    organizer.save_data("naming_preview_v1", rows)
    return organizer


def _row(raw_title="课程", timestamp=1, **extra):
    return {
        "raw_title": raw_title,
        "final_title": "候选名称",
        "target_library": "hold",
        "target_output_root": "",
        "status": "review",
        "reason_codes": ["review_needed"],
        "source": "themoviedb",
        "media_id": "12345",
        "media_type": "tv",
        "timestamp": timestamp,
        **extra,
    }


def test_review_filters_nonempty_residual_manifest_without_media(tmp_path, caplog):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "Season 1").mkdir()
    (source / "Season 1" / "episode.mp4.fg.ed").write_bytes(b"residual")
    (source / "Season 1" / "episode.mp4.fg.op").write_bytes(b"residual")
    (source / "Season 1" / "episode.nfo").write_text("metadata")
    caplog.set_level("DEBUG")

    organizer = _organizer([_row()], incoming=str(incoming))

    assert _response_data(organizer.get_review())["items"] == []
    assert "reason=no_media" in caplog.text


def test_review_keeps_nonempty_manifest_with_real_media(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")

    organizer = _organizer([_row()], incoming=str(incoming))

    assert _data_item(organizer.get_review(), "课程")["raw_title"] == "课程"


def _persist_confirm_for_scan(organizer, raw_title="课程", final_title="人工名称", target_library="tv"):
    row = _data_item(organizer.get_review(), raw_title)
    binding = organizer._current_source_binding(raw_title)
    assert binding is not None
    assert organizer._save_manual_decision(
        raw_title,
        "confirm",
        final_title,
        target_library,
        str(row.get("source_revision", "")),
        binding,
    )


def test_confirm_applies_structured_decision_without_changing_legacy_config(tmp_path, caplog):
    caplog.set_level("INFO")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for root in roots.values():
        root.mkdir()
    organizer = _organizer(
        [_row()],
        naming_mode="preview",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    legacy = organizer._get_config()["naming_manual_overrides"]
    row = _response_data(organizer.get_review())["items"][0]

    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "movie",
        }
    )

    assert _success(response)
    assert organizer._get_config()["naming_manual_overrides"] == legacy
    payload = organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)
    assert payload["schema"] == 1
    assert payload["items"] == {}
    assert not source.exists()
    assert (roots["movie"] / "人工名称" / "Season 1" / "人工名称 - S01E001.mp4").is_file()
    assert _response_data(organizer.get_review())["items"] == []
    assert organizer.get_data("naming_preview_v1")[0]["completed_at"]
    assert "event=manual_review_saved" in caplog.text
    assert "event=manual_review_applied" in caplog.text
    assert "event=move_completed" not in caplog.text


def _data_item(response, raw_title):
    return next(item for item in _response_data(response)["items"] if item["raw_title"] == raw_title)


def test_stale_revision_is_rejected_without_writing_data():
    organizer = _organizer([_row()])
    row = _data_item(organizer.get_review(), "课程")
    source = organizer.get_data("naming_preview_v1")
    source[0]["final_title"] = "变化后的建议名称"
    organizer.save_data("naming_preview_v1", source)

    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "ignore",
        }
    )

    assert not _success(response)
    assert _message(response) == "预览已更新，请刷新后再确认"
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY) is None


def test_timestamp_only_refresh_does_not_block_save_review():
    organizer = _organizer([_row()])
    row = _data_item(organizer.get_review(), "课程")
    source = organizer.get_data("naming_preview_v1")
    source[0]["timestamp"] = 2
    organizer.save_data("naming_preview_v1", source)

    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "ignore",
        }
    )

    assert _success(response)
    latest = _data_item(organizer.get_review(), "课程")
    assert latest["status"] == "ignore"
    assert latest["status_label"] == "已跳过"


@pytest.mark.parametrize(
    "raw_title",
    ["黑冰（2001）高清修复版 未删减", "全角ＡＢＣ课程", "课程  名称", "  首尾空格  "],
)
def test_confirm_preserves_exact_raw_preview_key(tmp_path, raw_title):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / raw_title
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for root in roots.values():
        root.mkdir()
    organizer = _organizer(
        [_row(raw_title)],
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    row = _data_item(organizer.get_review(), raw_title)
    response = organizer.save_review(
        {
            "raw_title": raw_title,
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "tv",
        }
    )

    assert _success(response)
    assert not source.exists()
    history = organizer.get_data("naming_preview_v1")
    assert history[0]["raw_title"] == raw_title
    assert history[0]["completed_at"]
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"] == {}


@pytest.mark.parametrize(
    "raw_title",
    [
        "课程\n名称",
        "课程\u0085名称",
        "课程\u2028名称",
        "课程\u2029名称",
        "\x01课程",
        "课程\u061c名称",
        "课程\u200e名称",
        "课程\u200f名称",
        "课程\u202e名称",
        "课程\u2066名称",
    ],
)
def test_raw_preview_key_rejects_controls_and_line_separators(raw_title):
    organizer = _organizer([_row(raw_title)])
    assert _response_data(organizer.get_review())["items"] == []
    response = organizer.save_review(
        {
            "raw_title": raw_title,
            "revision": "not-used",
            "action": "ignore",
        }
    )
    assert not _success(response)
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY) is None


def test_bad_surrogate_preview_row_is_rejected_without_breaking_review(caplog):
    caplog.set_level("WARNING")
    organizer = _organizer([_row("坏\ud800名称"), _row("正常")])
    items = _response_data(organizer.get_review())["items"]
    assert [item["raw_title"] for item in items] == ["正常"]
    assert "review_row_rejected" in caplog.text
    assert "\\ud800" in caplog.text
    assert "坏\ud800名称" not in caplog.text


def test_saved_ignore_decision_survives_later_preview_timestamp_refresh():
    organizer = _organizer([_row()])
    row = _data_item(organizer.get_review(), "课程")
    assert _success(
        organizer.save_review(
            {
                "raw_title": "课程",
                "revision": row["revision"],
                "action": "ignore",
            }
        )
    )
    source = organizer.get_data("naming_preview_v1")
    source[0]["timestamp"] = 2
    organizer.save_data("naming_preview_v1", source)
    latest = _data_item(organizer.get_review(), "课程")
    assert latest["status"] == "ignore"
    assert latest["status_label"] == "已跳过"
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]["action"] == "ignore"


def test_get_post_rejects_same_name_directory_replacement(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"old")
    organizer = _organizer(
        [_row()],
        incoming=str(incoming),
        tv_output=str(tmp_path / "tv"),
        movie_output=str(tmp_path / "movies"),
        children_output=str(tmp_path / "children"),
    )
    row = _data_item(organizer.get_review(), "课程")
    shutil.rmtree(source)
    source.mkdir()
    (source / "1.mp4").write_bytes(b"replacement")

    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "tv",
        }
    )

    assert not _success(response)
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY) is None


def test_post_rechecks_binding_before_save_after_revision_race(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"old")
    organizer = _organizer(
        [_row()],
        incoming=str(incoming),
        tv_output=str(tmp_path / "tv"),
        movie_output=str(tmp_path / "movies"),
        children_output=str(tmp_path / "children"),
    )
    row = _data_item(organizer.get_review(), "课程")
    original_binding = organizer._current_source_binding
    calls = {"count": 0}

    def race(raw_title):
        calls["count"] += 1
        binding = original_binding(raw_title)
        if calls["count"] == 2:
            shutil.rmtree(source)
            source.mkdir()
            (source / "1.mp4").write_bytes(b"replacement")
        return binding

    monkeypatch.setattr(organizer, "_current_source_binding", race)
    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "tv",
        }
    )

    assert calls["count"] >= 3
    assert not _success(response)
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY) is None
    assert (source / "1.mp4").read_bytes() == b"replacement"


def test_equal_length_overwrite_with_restored_mtime_invalidates_apply_binding(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    media = source / "1.mp4"
    media.write_bytes(b"old")
    original_mtime = media.stat().st_mtime_ns
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    organizer = _organizer(
        [_row()],
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    _persist_confirm_for_scan(organizer)
    assert organizer._process_course("课程", str(source)) is False
    media.write_bytes(b"new")
    os.utime(media, ns=(original_mtime, original_mtime))

    assert organizer._process_course("课程", str(source)) is False
    assert media.read_bytes() == b"new"
    assert not (roots["tv"] / "人工名称").exists()
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]


@pytest.mark.parametrize("mutation", ["add_media", "add_nonmedia", "remove_media"])
def test_apply_manifest_change_before_capture_keeps_source_and_decision(
    tmp_path, monkeypatch, mutation
):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    media = source / "1.mp4"
    media.write_bytes(b"old")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    organizer = _organizer(
        [_row()],
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
        library_classifier=_ClassifierSpy(),
    )
    _persist_confirm_for_scan(organizer)
    assert organizer._process_course("课程", str(source)) is False
    original_matches = organizer._source_binding_matches
    replaced = {"value": False}

    def replace_after_binding(raw_title, expected, current=None):
        result = original_matches(raw_title, expected, current)
        if result and not replaced["value"]:
            if mutation == "add_media":
                (source / "2.mp4").write_bytes(b"new-media")
            elif mutation == "add_nonmedia":
                (source / "notes.txt").write_bytes(b"new-note")
            else:
                (source / "1.mp4").unlink()
            replaced["value"] = True
        return result

    monkeypatch.setattr(organizer, "_source_binding_matches", replace_after_binding)
    assert organizer._process_course("课程", str(source)) is False
    assert replaced["value"]
    if mutation == "add_media":
        assert (source / "1.mp4").read_bytes() == b"old"
        assert (source / "2.mp4").read_bytes() == b"new-media"
    elif mutation == "add_nonmedia":
        assert (source / "1.mp4").read_bytes() == b"old"
        assert (source / "notes.txt").read_bytes() == b"new-note"
    else:
        assert not (source / "1.mp4").exists()
    assert not (roots["tv"] / "人工名称").exists()
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]
    assert organizer._library_classifier_override.calls == 0


@pytest.mark.parametrize("mutation", ["add_media", "add_nonmedia"])
def test_apply_stage_boundary_rechecks_full_tree(tmp_path, monkeypatch, mutation):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    media = source / "1.mp4"
    media.write_bytes(b"old")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    classifier = _ClassifierSpy()
    organizer = _organizer(
        [_row()],
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
        library_classifier=classifier,
    )
    _persist_confirm_for_scan(organizer)
    assert organizer._process_course("课程", str(source)) is False
    original_stage = organizer._stage_move_source
    injected = {"value": False}

    def stage_then_add(context, source_path, captured):
        if not injected["value"]:
            if mutation == "add_media":
                (source / "2.mp4").write_bytes(b"late-media")
            else:
                (source / "notes.txt").write_bytes(b"late-note")
            injected["value"] = True
        return original_stage(context, source_path, captured)

    monkeypatch.setattr(organizer, "_stage_move_source", stage_then_add)
    assert organizer._process_course("课程", str(source)) is False
    assert injected["value"]
    assert media.read_bytes() == b"old"
    if mutation == "add_media":
        assert (source / "2.mp4").read_bytes() == b"late-media"
    else:
        assert (source / "notes.txt").read_bytes() == b"late-note"
    assert not (roots["tv"] / "人工名称").exists()
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]
    assert classifier.calls == 0


@pytest.mark.parametrize(
    "mutation",
    ["add_media", "rewrite_notes", "remove_notes", "add_nested_dir", "add_decoy"],
)
def test_apply_publish_boundary_rechecks_files_and_directories(
    tmp_path, monkeypatch, mutation
):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    media = source / "1.mp4"
    media.write_bytes(b"old")
    notes = source / "notes.txt"
    notes.write_bytes(b"old-note")
    nested = source / "nested"
    nested.mkdir()
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    classifier = _ClassifierSpy()
    organizer = _organizer(
        [_row()],
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
        library_classifier=classifier,
    )
    _persist_confirm_for_scan(organizer)
    assert organizer._process_course("课程", str(source)) is False
    original_publish = organizer._publish_staged_move
    injected = {"value": False}

    def publish_then_change(context, item, target):
        if not injected["value"]:
            if mutation == "add_media":
                (source / "2.mp4").write_bytes(b"late-media")
            elif mutation == "rewrite_notes":
                notes.write_bytes(b"rewritten-note")
            elif mutation == "remove_notes":
                notes.unlink()
            elif mutation == "add_nested_dir":
                (nested / "late-empty").mkdir()
            else:
                decoy = source / ".courseorganizer-stage.decoy"
                decoy.mkdir()
                (decoy / "late.txt").write_bytes(b"decoy")
            injected["value"] = True
        return original_publish(context, item, target)

    monkeypatch.setattr(organizer, "_publish_staged_move", publish_then_change)
    assert organizer._process_course("课程", str(source)) is False
    assert injected["value"]
    assert media.read_bytes() == b"old"
    assert not (roots["tv"] / "人工名称").exists()
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]
    assert classifier.calls == 0


@pytest.mark.parametrize("mutation", ["add_media", "add_nonmedia", "add_empty_dir"])
def test_apply_commit_boundary_rechecks_remaining_tree(tmp_path, monkeypatch, mutation):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    media = source / "1.mp4"
    media.write_bytes(b"old")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    classifier = _ClassifierSpy()
    organizer = _organizer(
        [_row()],
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
        library_classifier=classifier,
    )
    _persist_confirm_for_scan(organizer)
    assert organizer._process_course("课程", str(source)) is False
    original_commit = organizer._commit_move_context
    injected = {"value": False}

    def commit_then_change(context):
        if not injected["value"]:
            if mutation == "add_media":
                (source / "2.mp4").write_bytes(b"late-media")
            elif mutation == "add_nonmedia":
                (source / "notes.txt").write_bytes(b"late-note")
            else:
                (source / "late-empty").mkdir()
            injected["value"] = True
        return original_commit(context)

    monkeypatch.setattr(organizer, "_commit_move_context", commit_then_change)
    assert organizer._process_course("课程", str(source)) is False
    assert injected["value"]
    assert media.read_bytes() == b"old"
    assert not (roots["tv"] / "人工名称").exists()
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]
    assert classifier.calls == 0


@pytest.mark.parametrize("mutation", ["replace_inode", "rewrite_same_inode"])
def test_apply_commit_target_stat_race_rolls_back_safely(tmp_path, monkeypatch, mutation):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    media = source / "1.mp4"
    media.write_bytes(b"old")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    classifier = _ClassifierSpy()
    organizer = _organizer(
        [_row()],
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
        library_classifier=classifier,
    )
    _persist_confirm_for_scan(organizer)
    assert organizer._process_course("课程", str(source)) is False
    original_publish = organizer._publish_staged_move
    injected = {"value": False}
    target_path = roots["tv"] / "人工名称" / "Season 1" / "人工名称 - S01E001.mp4"

    def publish_then_change(context, item, target):
        result = original_publish(context, item, target)
        if not injected["value"]:
            if mutation == "replace_inode":
                target_path.unlink()
                target_path.write_bytes(b"replacement")
            else:
                target_path.write_bytes(b"raced-content")
            injected["value"] = True
        return result

    monkeypatch.setattr(organizer, "_publish_staged_move", publish_then_change)
    assert organizer._process_course("课程", str(source)) is False
    assert injected["value"]
    assert media.read_bytes() == b"old"
    if mutation == "replace_inode":
        assert target_path.read_bytes() == b"replacement"
    else:
        assert target_path.read_bytes() == b"raced-content"
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]
    assert classifier.calls == 0


def test_apply_two_file_cleanup_race_restores_originals_and_keeps_drifted_target(
    tmp_path, monkeypatch
):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    first = source / "a.mp4"
    second = source / "b.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    organizer = _organizer(
        [_row()],
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    _persist_confirm_for_scan(organizer)
    assert organizer._process_course("课程", str(source)) is False
    first_target = roots["tv"] / "人工名称" / "Season 1" / "人工名称 - S01E001.mp4"
    second_target = roots["tv"] / "人工名称" / "Season 1" / "人工名称 - S01E002.mp4"
    original_unlink = organizer._unlink_if_identity
    stage_cleanups = []
    injected = {"value": False}

    def fail_second_stage_cleanup(parent_fd, name, expected):
        prefix, separator, _ = name.partition("-")
        is_stage_name = separator and len(prefix) == 8 and all(
            character in "0123456789abcdef" for character in prefix
        )
        if is_stage_name:
            stage_cleanups.append(name)
            if len(stage_cleanups) == 2 and not injected["value"]:
                first_target.write_bytes(b"raced-target")
                injected["value"] = True
                return False
        return original_unlink(parent_fd, name, expected)

    monkeypatch.setattr(organizer, "_unlink_if_identity", fail_second_stage_cleanup)
    assert organizer._process_course("课程", str(source)) is False
    assert injected["value"] is True
    assert stage_cleanups[:2] == ["00000001-a.mp4", "00000002-b.mp4"]
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert first_target.read_bytes() == b"raced-target"
    assert not second_target.exists()
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]


def test_apply_rejects_same_name_replacement_without_inheriting_decision(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"old")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    organizer = _organizer(
        [_row()],
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    _persist_confirm_for_scan(organizer)
    assert organizer._process_course("课程", str(source)) is False
    shutil.rmtree(source)
    source.mkdir()
    (source / "1.mp4").write_bytes(b"replacement")
    assert organizer._process_course("课程", str(source)) is False
    assert organizer._process_course("课程", str(source)) is False
    assert source.exists()
    assert not (roots["tv"] / "人工名称").exists()


def test_manual_decision_remains_valid_after_restart_when_source_is_unchanged(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    config = {
        "naming_mode": "apply",
        "incoming": str(incoming),
        "tv_output": str(roots["tv"]),
        "movie_output": str(roots["movie"]),
        "children_output": str(roots["children"]),
    }
    first = _organizer([_row()], **config)
    _persist_confirm_for_scan(first)
    saved_decisions = copy.deepcopy(first.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY))
    saved_preview = copy.deepcopy(first.get_data("naming_preview_v1"))

    second = CourseOrganizer(config=config)
    second.save_data(CourseOrganizer.MANUAL_DECISIONS_KEY, saved_decisions)
    second.save_data("naming_preview_v1", saved_preview)
    assert second._manual_decision_for("课程").action == "confirm"
    assert second._process_course("课程", str(source)) is False
    assert second._process_course("课程", str(source)) is True
    assert not second.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]


def test_manual_name_uses_strict_utf8_byte_limit_and_rejects_surrogates():
    assert naming.validate_manual_name("A" * 160)[0]
    assert not naming.validate_manual_name("A" * 161)[0]
    assert naming.validate_manual_name("课" * 53)[0]  # 159 UTF-8 bytes
    assert not naming.validate_manual_name("课" * 54)[0]  # 162 UTF-8 bytes
    assert not naming.validate_manual_name("坏\ud800名称")[0]


def test_multibyte_manual_name_applies_and_oversize_save_is_rejected(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    config = {
        "naming_mode": "apply",
        "incoming": str(incoming),
        "tv_output": str(roots["tv"]),
        "movie_output": str(roots["movie"]),
        "children_output": str(roots["children"]),
    }
    organizer = _organizer([_row()], **config)
    row = _data_item(organizer.get_review(), "课程")
    valid_name = "课" * 50
    assert _success(
        organizer.save_review(
            {
                "raw_title": "课程",
                "revision": row["revision"],
                "action": "confirm",
                "final_title": valid_name,
                "target_library": "tv",
            }
        )
    )
    assert not source.exists()
    assert (roots["tv"] / valid_name / "Season 1").is_dir()
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"] == {}

    second = _organizer([_row()], **config)
    second_row = _data_item(second.get_review(), "课程")
    assert not _success(
        second.save_review(
            {
                "raw_title": "课程",
                "revision": second_row["revision"],
                "action": "confirm",
                "final_title": "课" * 54,
                "target_library": "tv",
            }
        )
    )


def test_transient_consumption_failure_retries_and_closes_record(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"old")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    organizer = _organizer(
        [_row()],
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    row = _data_item(organizer.get_review(), "课程")
    original_save_data = organizer.save_data
    failed = {"initial": True, "consume": True}

    def fail_consumption(key, value):
        if key == CourseOrganizer.MANUAL_DECISIONS_KEY and failed["initial"]:
            failed["initial"] = False
            return original_save_data(key, value)
        if key == CourseOrganizer.MANUAL_DECISIONS_KEY and failed["consume"]:
            failed["consume"] = False
            return False
        return original_save_data(key, value)

    organizer.save_data = fail_consumption
    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "tv",
        }
    )
    assert _success(response)
    assert not source.exists()
    assert (roots["tv"] / "人工名称" / "Season 1" / "人工名称 - S01E001.mp4").is_file()
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"] == {}
    assert organizer.get_data("naming_preview_v1")[0]["completed_at"]

    replacement = incoming / "课程"
    replacement.mkdir()
    (replacement / "1.mp4").write_bytes(b"replacement")
    assert organizer._process_course("课程", str(replacement)) is False
    assert organizer._process_course("课程", str(replacement)) is False
    assert (roots["tv"] / "人工名称").is_dir()
    assert (replacement / "1.mp4").is_file()


def test_permanent_consumption_failure_returns_partial_without_retry(tmp_path, caplog):
    caplog.set_level("INFO")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    organizer = _organizer(
        [_row()],
        naming_mode="preview",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    row = _data_item(organizer.get_review(), "课程")
    original_save_data = organizer.save_data
    initial = {"value": True}

    def fail_consumption_forever(key, value):
        if key == CourseOrganizer.MANUAL_DECISIONS_KEY:
            if initial["value"]:
                initial["value"] = False
                return original_save_data(key, value)
            return False
        return original_save_data(key, value)

    organizer.save_data = fail_consumption_forever
    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "tv",
        }
    )

    assert not _success(response)
    assert _message(response) == "文件已移动，但人工复核记录未完整保存，请勿重复确认；请检查记录后处理"
    assert _response_data(response) == {"moved": True, "record_incomplete": True}
    assert not source.exists()
    assert (roots["tv"] / "人工名称" / "Season 1" / "人工名称 - S01E001.mp4").is_file()
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]["action"] == "confirm"
    assert "completed_at" not in organizer.get_data("naming_preview_v1")[0]
    assert "event=moved_but_record_incomplete" in caplog.text


def test_permanent_completed_marker_failure_returns_partial(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for path in roots.values():
        path.mkdir()
    organizer = _organizer(
        [_row()],
        naming_mode="preview",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    row = _data_item(organizer.get_review(), "课程")
    monkeypatch.setattr(MODULE.SmartNamingResolver, "mark_completed", lambda self, raw_title: False)

    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "tv",
        }
    )

    assert not _success(response)
    assert "文件已移动" in _message(response)
    assert "请勿重复确认" in _message(response)
    assert _response_data(response) == {"moved": True, "record_incomplete": True}
    assert not source.exists()
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"] == {}
    assert "completed_at" not in organizer.get_data("naming_preview_v1")[0]


def test_mark_completed_rejects_permanent_false_save_without_mutating_shared_rows():
    organizer = _organizer([_row()])
    shared_rows = organizer.get_data("naming_preview_v1")
    assert organizer.get_data("naming_preview_v1") is shared_rows
    original_save_data = organizer.save_data
    attempts = []

    def reject_preview_save(key, value):
        if key == "naming_preview_v1":
            attempts.append(value)
            return False
        return original_save_data(key, value)

    organizer.save_data = reject_preview_save
    resolver = organizer._build_resolver(organizer._get_config())

    assert resolver.mark_completed("课程") is False
    assert len(attempts) == 2
    assert "completed_at" not in shared_rows[0]


def test_adjacent_rows_are_upserted_without_losing_each_other():
    organizer = _organizer([_row("课程 A"), _row("课程 B")])
    rows = _response_data(organizer.get_review())["items"]
    for row in rows:
        assert _success(
            organizer.save_review(
                {
                    "raw_title": row["raw_title"],
                    "revision": row["revision"],
                    "action": "ignore",
                }
            )
        )
    assert set(organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]) == {
        "课程 A",
        "课程 B",
    }


def test_review_loads_manual_store_once_for_multiple_rows():
    organizer = _organizer([_row(f"课程 {index}") for index in range(20)])
    original_get_data = organizer.get_data
    original_manifest = organizer._source_tree_manifest
    original_snapshot = organizer._snapshot_signature
    calls = {"count": 0}
    manifest_calls = {"count": 0}
    snapshot_calls = {"count": 0}

    def counted_get_data(key, *args, **kwargs):
        if key == CourseOrganizer.MANUAL_DECISIONS_KEY:
            calls["count"] += 1
        return original_get_data(key, *args, **kwargs)

    organizer.get_data = counted_get_data
    def counted_manifest(path):
        manifest_calls["count"] += 1
        return original_manifest(path)

    def counted_snapshot(path):
        snapshot_calls["count"] += 1
        return original_snapshot(path)

    organizer._source_tree_manifest = counted_manifest
    organizer._snapshot_signature = counted_snapshot
    assert len(_response_data(organizer.get_review())["items"]) == 20
    assert calls["count"] == 1
    assert manifest_calls["count"] == 20
    assert snapshot_calls["count"] == 0


def test_post_review_scans_only_requested_row(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    for index in range(20):
        (incoming / f"课程 {index}").mkdir()
    (incoming / "课程 7" / "1.mp4").write_bytes(b"media")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for root in roots.values():
        root.mkdir()
    organizer = _organizer(
        [_row(f"课程 {index}") for index in range(20)],
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    row = _data_item(organizer.get_review(), "课程 7")
    original_manifest = organizer._source_tree_manifest
    manifest_calls = {"count": 0}

    def counted_manifest(path):
        manifest_calls["count"] += 1
        return original_manifest(path)

    monkeypatch.setattr(organizer, "_source_tree_manifest", counted_manifest)
    response = organizer.save_review(
        {
            "raw_title": "课程 7",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "tv",
        }
    )

    assert _success(response)
    assert 0 < manifest_calls["count"] <= 5
    assert not (incoming / "课程 7").exists()
    assert (incoming / "课程 6").exists()
    assert (roots["tv"] / "人工名称" / "Season 1" / "人工名称 - S01E001.mp4").is_file()


def test_concurrent_rows_are_serialized_without_lost_updates():
    organizer = _organizer([_row("课程 A"), _row("课程 B")])
    rows = {item["raw_title"]: item for item in _response_data(organizer.get_review())["items"]}
    results = []
    barrier = threading.Barrier(3)

    def save(raw_title):
        barrier.wait()
        results.append(
            organizer.save_review(
                {
                    "raw_title": raw_title,
                    "revision": rows[raw_title]["revision"],
                    "action": "ignore",
                }
            )
        )

    threads = [threading.Thread(target=save, args=(raw_title,)) for raw_title in rows]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert all(_success(response) for response in results)
    assert set(organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]) == set(rows)


@pytest.mark.parametrize(
    "bad_name",
    [" Course", "Course ", "A/B", "A\\B", "A\u0085B", "A\u2028B", "A\u2029B", ".", ".."],
)
def test_confirm_name_validation_is_fail_closed(bad_name):
    organizer = _organizer([_row()])
    row = _data_item(organizer.get_review(), "课程")
    before = copy.deepcopy(organizer.get_config())
    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": bad_name,
            "target_library": "tv",
        }
    )
    assert not _success(response)
    assert organizer.get_config() == before
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY) is None


def test_bad_structured_entry_and_capacity_fail_without_throwing():
    organizer = _organizer([_row()])
    organizer.save_data(
        CourseOrganizer.MANUAL_DECISIONS_KEY,
        {
            "schema": 1,
            "items": {
                "课程": {
                    "action": "confirm",
                    "final_title": "坏目标",
                    "target_library": "/etc",
                    "updated_at": 1,
                    "source_revision": "old",
                }
            },
        },
    )
    assert _data_item(organizer.get_review(), "课程")["status"] == "invalid_manual_decision"

    full = {
        str(index): {
            "action": "ignore",
            "final_title": "",
            "target_library": "",
            "updated_at": "bad",
            "source_revision": str(index),
        }
        for index in range(500)
    }
    organizer = _organizer([_row("新课程")])
    organizer.save_data(CourseOrganizer.MANUAL_DECISIONS_KEY, {"schema": 1, "items": full})
    before = copy.deepcopy(organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY))
    row = _data_item(organizer.get_review(), "新课程")
    response = organizer.save_review(
        {"raw_title": "新课程", "revision": row["revision"], "action": "ignore"}
    )
    assert not _success(response)
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY) == before


def test_invalid_target_and_unknown_row_leave_data_unchanged():
    organizer = _organizer([_row()])
    row = _data_item(organizer.get_review(), "课程")
    before = copy.deepcopy(organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY))
    invalid_target = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "/tmp",
        }
    )
    unknown = organizer.save_review(
        {
            "raw_title": "不存在",
            "revision": row["revision"],
            "action": "ignore",
        }
    )
    assert not _success(invalid_target)
    assert not _success(unknown)
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY) == before


def test_structured_confirm_has_priority_over_legacy_override():
    organizer = _organizer([_row()], naming_mode="apply")
    binding = organizer._current_source_binding("课程")
    assert binding is not None
    organizer.save_data(
        CourseOrganizer.MANUAL_DECISIONS_KEY,
        {
            "schema": 1,
            "items": {
                "课程": {
                    "action": "confirm",
                    "final_title": "结构化名称",
                        "target_library": "children",
                        "updated_at": 1,
                        "source_revision": "any",
                        **binding,
                    }
                },
        },
    )
    decision = organizer._resolve_naming(
        "课程",
        naming.DirectoryHints(1, (), False),
        "",
        "",
        manual_decision=organizer._manual_decision_for("课程"),
    )
    assert decision.final_root == "结构化名称"
    assert decision.target_library == "children"


class _TmdbProvider:
    def __init__(self):
        self.calls = []

    def resolve_sources(self, requested):
        return ("themoviedb",) if "themoviedb" in requested else ()

    def search(self, queries, sources):
        self.calls.append((tuple(query.text for query in queries), tuple(sources)))
        candidates = tuple(
            naming.MetadataCandidate(
                key=f"themoviedb:{query.text}-{index}:movie",
                source="themoviedb",
                media_id=f"{query.text}-{index}",
                media_type="movie",
                title=f"{query.text} 正式名 {index}",
                year=2024,
                matched_query=query.text,
                query_origin=query.origin,
            )
            for query in queries[:1]
            for index in range(12)
        )
        return SimpleNamespace(
            candidates=candidates,
            errors=(),
            attempted_sources=("themoviedb",),
            all_failed=False,
        )


class _RecoveringTmdbProvider:
    def __init__(self, empty=False, first_empty=False):
        self.calls = 0
        self.empty = empty
        self.first_empty = first_empty
        self.success_provider = _TmdbProvider()

    def resolve_sources(self, requested):
        return ("themoviedb",) if "themoviedb" in requested else ()

    def search(self, queries, sources):
        self.calls += 1
        if self.empty or (self.first_empty and self.calls == 1):
            return SimpleNamespace(
                candidates=(),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )
        if self.calls == 1:
            return SimpleNamespace(
                candidates=(),
                errors=("themoviedb:connection_refused",),
                attempted_sources=("themoviedb",),
                all_failed=True,
            )
        return self.success_provider.search(queries, sources)


def test_review_source_labels_are_public_and_technical_fields_stay_hidden():
    organizer = _organizer(
        [
            _row("TMDB", status="auto_external", source="themoviedb"),
            _row("豆瓣", status="auto_external", source="douban"),
            _row("AI", status="auto_external", source="themoviedb", reason_codes=["ai_review"]),
            _row("本地", status="local_fallback", source=""),
        ]
    )
    items = _response_data(organizer.get_review())["items"]
    labels = {item["raw_title"]: item["recognition_source_label"] for item in items}
    assert labels == {"TMDB": "TMDB", "豆瓣": "豆瓣", "AI": "DeepSeek", "本地": "本地"}
    assert all("source" not in item and "media_id" not in item for item in items)


def test_ignored_row_keeps_recognition_source_and_preview_search_works_when_disabled():
    provider = _TmdbProvider()
    organizer = _organizer(
        [_row("课程", status="auto_external", source="themoviedb")],
        metadata_provider=provider,
        enabled=False,
    )
    row = _data_item(organizer.get_review(), "课程")
    assert row["recognition_source_label"] == "TMDB"
    ignored = organizer.save_review(
        {"raw_title": "课程", "revision": row["revision"], "action": "ignore"}
    )
    assert _success(ignored)
    assert _data_item(organizer.get_review(), "课程")["recognition_source_label"] == "TMDB"
    search = organizer.search_tmdb({"raw_title": "课程", "revision": _data_item(organizer.get_review(), "课程")["revision"]})
    assert _success(search)


def test_tmdb_search_uses_raw_title_and_returns_only_server_whitelisted_candidates():
    provider = _TmdbProvider()
    organizer = _organizer([_row("课程（2024）")], metadata_provider=provider, enabled=True)
    row = _data_item(organizer.get_review(), "课程（2024）")

    response = organizer.search_tmdb(
        {
            "raw_title": "课程（2024）",
            "revision": row["revision"],
            "query": "攻击者自定义词",
        }
    )

    assert _success(response)
    data = _response_data(response)
    assert len(data["items"]) == 10
    assert all(set(item) == {"candidate_key", "title", "year", "media_type", "label"} for item in data["items"])
    assert provider.calls == [(("课程",), ("themoviedb",))]
    cached = organizer.search_tmdb(
        {"raw_title": "课程（2024）", "revision": row["revision"]}
    )
    assert _success(cached)
    assert provider.calls == [(("课程",), ("themoviedb",))]


def test_tmdb_manual_retry_bypasses_fresh_failed_cache_after_network_recovers():
    provider = _RecoveringTmdbProvider()
    organizer = _organizer([_row("课程")], metadata_provider=provider, enabled=True)
    row = _data_item(organizer.get_review(), "课程")

    failed = organizer.search_tmdb({"raw_title": "课程", "revision": row["revision"]})
    assert not _success(failed)
    assert _message(failed) == "TMDB 连接失败，请检查 MoviePilot 网络或 TMDB API 服务地址"
    assert provider.calls == 1

    recovered = organizer.search_tmdb({"raw_title": "课程", "revision": row["revision"]})
    assert _success(recovered)
    assert provider.calls == 2
    assert len(_response_data(recovered)["items"]) == 10


def test_tmdb_empty_result_is_not_reported_as_connection_failure():
    provider = _RecoveringTmdbProvider(empty=True)
    organizer = _organizer([_row("不存在的课程")], metadata_provider=provider, enabled=True)
    row = _data_item(organizer.get_review(), "不存在的课程")

    response = organizer.search_tmdb(
        {"raw_title": "不存在的课程", "revision": row["revision"]}
    )
    assert not _success(response)
    assert _message(response) == "未找到 TMDB 候选，请检查名称或 TMDB 数据源配置"
    assert "连接失败" not in _message(response)


def test_tmdb_manual_retry_bypasses_successful_empty_cache_after_candidates_appear():
    provider = _RecoveringTmdbProvider(first_empty=True)
    organizer = _organizer([_row("课程")], metadata_provider=provider, enabled=True)
    row = _data_item(organizer.get_review(), "课程")

    empty = organizer.search_tmdb({"raw_title": "课程", "revision": row["revision"]})
    assert not _success(empty)
    assert _message(empty) == "未找到 TMDB 候选，请检查名称或 TMDB 数据源配置"
    recovered = organizer.search_tmdb({"raw_title": "课程", "revision": row["revision"]})
    assert _success(recovered)
    assert provider.calls == 2


def test_tmdb_manual_retries_each_time_for_continuous_empty_results():
    provider = _RecoveringTmdbProvider(empty=True)
    organizer = _organizer([_row("不存在的课程")], metadata_provider=provider, enabled=True)
    row = _data_item(organizer.get_review(), "不存在的课程")
    payload = {"raw_title": "不存在的课程", "revision": row["revision"]}

    assert not _success(organizer.search_tmdb(payload))
    assert not _success(organizer.search_tmdb(payload))
    assert provider.calls == 2


def test_normal_preview_keeps_successful_empty_cache():
    provider = _RecoveringTmdbProvider(empty=True)
    organizer = _organizer([_row("不存在的课程")], metadata_provider=provider, enabled=True)
    hints = naming.DirectoryHints(media_count=1, seasons=(), episodic=False)

    organizer._resolve_naming("不存在的课程", hints, "", "")
    organizer._resolve_naming("不存在的课程", hints, "", "")
    assert provider.calls == 1


def test_tmdb_association_rejects_stale_cross_row_and_forged_candidates():
    provider = _TmdbProvider()
    organizer = _organizer(
        [_row("课程甲"), _row("课程乙")], metadata_provider=provider, enabled=True
    )
    rows = {item["raw_title"]: item for item in _response_data(organizer.get_review())["items"]}
    first = _response_data(
        organizer.search_tmdb({"raw_title": "课程甲", "revision": rows["课程甲"]["revision"]})
    )["items"][0]
    forged = organizer.associate_tmdb(
        {
            "raw_title": "课程乙",
            "revision": rows["课程乙"]["revision"],
            "candidate_key": "themoviedb:forged:movie",
        }
    )
    cross_row = organizer.associate_tmdb(
        {
            "raw_title": "课程乙",
            "revision": rows["课程乙"]["revision"],
            "candidate_key": first["candidate_key"],
        }
    )
    assert not _success(forged)
    assert not _success(cross_row)
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY) is None

    stale = organizer.associate_tmdb(
        {
            "raw_title": "课程甲",
            "revision": "stale-revision",
            "candidate_key": first["candidate_key"],
        }
    )
    assert not _success(stale)
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY) is None


def test_tmdb_association_persists_and_resolver_reuses_selected_candidate_after_restart():
    provider = _TmdbProvider()
    organizer = _organizer([_row("课程")], metadata_provider=provider, enabled=True)
    row = _data_item(organizer.get_review(), "课程")
    candidate = _response_data(
        organizer.search_tmdb({"raw_title": "课程", "revision": row["revision"]})
    )["items"][0]
    response = organizer.associate_tmdb(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "candidate_key": candidate["candidate_key"],
        }
    )
    assert _success(response)
    latest = _data_item(organizer.get_review(), "课程")
    assert latest["recognition_source_label"] == "TMDB"
    assert latest["final_title"].startswith("课程 正式名")
    stored = copy.deepcopy(organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY))
    assert stored["items"]["课程"]["action"] == "candidate"
    assert stored["items"]["课程"]["candidate_key"] == candidate["candidate_key"]

    calls_before_restart = len(provider.calls)
    restarted = _organizer([_row("课程")], metadata_provider=provider, enabled=True)
    restarted.save_data(CourseOrganizer.MANUAL_DECISIONS_KEY, stored)
    decision = restarted._resolve_naming(
        "课程",
        naming.DirectoryHints(1, (), False),
        "",
        "",
        manual_decision=restarted._manual_decision_for("课程"),
    )
    assert decision.candidate_key == candidate["candidate_key"]
    assert decision.source == "themoviedb"
    assert decision.status == "auto_external"
    assert len(provider.calls) == calls_before_restart


def test_tmdb_association_uses_exact_selected_tmdb_title_not_raw_english_alias():
    class _LocalizedTmdbProvider(_TmdbProvider):
        def search(self, queries, sources):
            self.calls.append((tuple(query.text for query in queries), tuple(sources)))
            return SimpleNamespace(
                candidates=(
                    naming.MetadataCandidate(
                        key="themoviedb:55000:tv",
                        source="themoviedb",
                        media_id="55000",
                        media_type="tv",
                        title="飘飘叶",
                        en_title="Tumble Leaf",
                        original_title="Tumble Leaf",
                        year=2013,
                        matched_query="Tumble Leaf",
                        query_origin="clean_title",
                    ),
                ),
                errors=(),
                attempted_sources=("themoviedb",),
                all_failed=False,
            )

    raw_title = "《飘零叶 Tumble Leaf》儿童早教动画"
    organizer = _organizer(
        [_row(raw_title, target_library="children")],
        metadata_provider=_LocalizedTmdbProvider(),
        enabled=True,
    )
    row = _data_item(organizer.get_review(), raw_title)
    candidate = _response_data(
        organizer.search_tmdb({"raw_title": raw_title, "revision": row["revision"]})
    )["items"][0]

    response = organizer.associate_tmdb(
        {
            "raw_title": raw_title,
            "revision": row["revision"],
            "candidate_key": candidate["candidate_key"],
        }
    )

    assert _success(response)
    latest = _data_item(organizer.get_review(), raw_title)
    assert latest["final_title"] == "飘飘叶 (2013)"
    assert latest["target_path"].endswith("/飘飘叶 (2013)")


def test_confirm_allows_no_media_identity_direct_transfer():
    # 用户要求：无媒体 ID（课程等不在 TMDB 上）也能"保存并整理"。
    # 无媒体身份时确认应放行，并走"直接按标题搬移"：源目录移到目标媒体库/最终名称下，源消失。
    native = _TestNativeAdapter(
        {
            "incoming": tempfile.mkdtemp(),
            "tv_output": tempfile.mkdtemp(),
            "movie_output": tempfile.mkdtemp(),
            "children_output": tempfile.mkdtemp(),
        }
    )
    organizer = _organizer(
        [_row("课程", target_library="tv", source="", media_id="")],
        native_adapter=native,
        incoming=native.config["incoming"],
        tv_output=native.config["tv_output"],
        movie_output=native.config["movie_output"],
        children_output=native.config["children_output"],
    )
    (Path(native.config["incoming"]) / "课程").mkdir(parents=True, exist_ok=True)
    (Path(native.config["incoming"]) / "课程" / "S01E01.mp4").write_bytes(b"media")
    (Path(native.config["incoming"]) / "课程" / "10.友好的汽车世界.mp4").write_bytes(b"media")
    row = _data_item(organizer.get_review(), "课程")

    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "课程 (2024)",
            "target_library": "tv",
        }
    )

    assert _success(response)
    # 直接搬移 + 按 MoviePilot 配置的电视剧重命名格式重组：
    # 目标媒体库/课程 (2024)/Season 1/课程 - 1x1 - 第1集.mp4，源目录消失
    dest_season1 = Path(native.config["tv_output"]) / "课程 (2024)" / "Season 1"
    assert (dest_season1 / "课程 - 1x1 - 第1集.mp4").exists()
    # 前置序号(10.标题)使用配置格式命名
    assert (dest_season1 / "课程 - 1x10 - 第10集.mp4").exists()
    assert not (Path(native.config["incoming"]) / "课程").exists()
    # 未走 MoviePilot native 识别路径
    assert native.calls == []


def test_confirm_no_media_identity_uses_movie_naming_format():
    # 目标媒体库=电影时，直接用 MoviePilot 配置的电影重命名格式命名
    native = _TestNativeAdapter(
        {
            "incoming": tempfile.mkdtemp(),
            "tv_output": tempfile.mkdtemp(),
            "movie_output": tempfile.mkdtemp(),
            "children_output": tempfile.mkdtemp(),
        }
    )
    organizer = _organizer(
        [_row("影集", target_library="movie", source="", media_id="")],
        native_adapter=native,
        incoming=native.config["incoming"],
        tv_output=native.config["tv_output"],
        movie_output=native.config["movie_output"],
        children_output=native.config["children_output"],
    )
    (Path(native.config["incoming"]) / "影集").mkdir(parents=True, exist_ok=True)
    (Path(native.config["incoming"]) / "影集" / "1.mp4").write_bytes(b"media")
    row = _data_item(organizer.get_review(), "影集")
    response = organizer.save_review(
        {
            "raw_title": "影集",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "影集 (2001)",
            "target_library": "movie",
        }
    )
    assert _success(response)
    # 电影格式 {{title}} ({{year}}) => 影集 (2001).mp4（无 Season 子目录）
    assert (Path(native.config["movie_output"]) / "影集 (2001)" / "影集 (2001).mp4").exists()
    assert native.calls == []
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        config = {
            "incoming": str(root_path / "incoming"),
            "tv_output": str(root_path / "tv"),
            "movie_output": str(root_path / "movies"),
            "children_output": str(root_path / "courses"),
        }
        for path in config.values():
            Path(path).mkdir()
        native = _TestNativeAdapter(config)
        organizer = _organizer(
            [_row("课程", target_library="movie", media_type="movie")],
            native_adapter=native,
            **config,
        )
        (Path(config["incoming"]) / "课程" / "1.mkv").write_bytes(b"media")
        row = _data_item(organizer.get_review(), "课程")

        response = organizer.save_review(
            {
                "raw_title": "课程",
                "revision": row["revision"],
                "action": "confirm",
                "final_title": "课程电影 (2024)",
                "target_library": "movie",
            }
        )

        assert _success(response)
        assert len(native.calls) == 1
        call = native.calls[0]
        assert call["target_path"] == Path(config["movie_output"])
        assert call["tmdbid"] == 12345
        assert call["doubanid"] is None
        assert call["mtype"] == "movie"
        assert call["transfer_type"] == "move"
        assert call["background"] is False
        assert call["preview"] is False
        assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"] == {}


def test_native_rename_context_only_changes_current_thread_and_source_tree(tmp_path):
    class _Events:
        def __init__(self):
            self.handler = None

        def add_event_listener(self, _event_type, handler):
            self.handler = handler

        def remove_event_listener(self, _event_type, handler):
            assert handler is self.handler
            self.handler = None

    class _Types:
        TransferRenameBuild = "rename-build"

    events = _Events()
    adapter = MODULE._MoviePilotNativeAdapter(
        directory_helper=object,
        storage_chain=object,
        transfer_chain=object,
        chain_event_type=_Types,
        event_manager=events,
        media_type=None,
    )
    source = tmp_path / "source"
    source.mkdir()
    inside = source / "episode.mkv"
    outside = tmp_path / "other.mkv"

    with adapter.rename_context(str(source), "飘飘叶 (2013)"):
        inside_data = SimpleNamespace(
            source_path=inside, rename_dict={"title": "old", "year": 2013}
        )
        events.handler(SimpleNamespace(event_data=inside_data))
        assert inside_data.rename_dict == {"title": "飘飘叶 (2013)"}

        outside_data = SimpleNamespace(
            source_path=str(outside), rename_dict={"title": "other", "year": 2024}
        )
        events.handler(SimpleNamespace(event_data=outside_data))
        assert outside_data.rename_dict == {"title": "other", "year": 2024}

        threaded_data = SimpleNamespace(
            source_path=str(inside), rename_dict={"title": "thread", "year": 2013}
        )
        thread = threading.Thread(
            target=lambda: events.handler(SimpleNamespace(event_data=threaded_data))
        )
        thread.start()
        thread.join()
        assert threaded_data.rename_dict == {"title": "thread", "year": 2013}

    assert events.handler is None


def test_tmdb_association_does_not_process_or_move_files(monkeypatch):
    provider = _TmdbProvider()
    organizer = _organizer([_row("课程")], metadata_provider=provider, enabled=True)
    row = _data_item(organizer.get_review(), "课程")
    candidate = _response_data(
        organizer.search_tmdb({"raw_title": "课程", "revision": row["revision"]})
    )["items"][0]
    moved = []
    monkeypatch.setattr(organizer, "_process_course", lambda *args, **kwargs: moved.append(True))
    response = organizer.associate_tmdb(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "candidate_key": candidate["candidate_key"],
        }
    )
    assert _success(response)
    assert moved == []


def test_candidate_association_is_fail_closed_when_source_changes_before_apply(tmp_path):
    provider = _TmdbProvider()
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    media = source / "1.mp4"
    media.write_bytes(b"old")
    tv_output = tmp_path / "tv"
    movie_output = tmp_path / "movies"
    children_output = tmp_path / "children"
    for output in (tv_output, movie_output, children_output):
        output.mkdir()
    organizer = _organizer(
        [_row("课程")],
        metadata_provider=provider,
        enabled=True,
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(tv_output),
        movie_output=str(movie_output),
        children_output=str(children_output),
    )
    row = _data_item(organizer.get_review(), "课程")
    candidate = _response_data(
        organizer.search_tmdb({"raw_title": "课程", "revision": row["revision"]})
    )["items"][0]
    assert _success(
        organizer.associate_tmdb(
            {
                "raw_title": "课程",
                "revision": row["revision"],
                "candidate_key": candidate["candidate_key"],
            }
        )
    )
    media.write_bytes(b"new")
    assert organizer._process_course("课程", str(source)) is False
    assert organizer._process_course("课程", str(source)) is False
    assert media.exists()
    assert not list(tv_output.rglob("*.mp4"))
    assert not list(movie_output.rglob("*.mp4"))
    assert not list(children_output.rglob("*.mp4"))


@pytest.mark.parametrize(
    "candidate_key,media_id,media_type",
    [
        ("themoviedb:2:movie", "1", "movie"),
        ("themoviedb:1:tv", "1", "movie"),
        ("themoviedb:1:movie", "2", "movie"),
    ],
)
def test_malformed_structured_candidate_key_id_type_fails_closed_without_network_or_move(
    tmp_path, candidate_key, media_id, media_type
):
    provider = _TmdbProvider()
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    media = source / "1.mp4"
    media.write_bytes(b"media")
    roots = {name: tmp_path / name for name in ("tv", "movies", "children")}
    for root in roots.values():
        root.mkdir()
    organizer = _organizer(
        [_row("课程")],
        metadata_provider=provider,
        enabled=True,
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movies"]),
        children_output=str(roots["children"]),
    )
    binding = organizer._current_source_binding("课程")
    assert binding is not None
    candidate = naming.MetadataCandidate(
        key=candidate_key,
        source="themoviedb",
        media_id=media_id,
        media_type=media_type,
        title="候选",
        year=2024,
    )
    organizer.save_data(
        CourseOrganizer.MANUAL_DECISIONS_KEY,
        {
            "schema": 1,
            "items": {
                "课程": {
                    "action": "candidate",
                    "final_title": "候选",
                    "target_library": "movie",
                    "updated_at": 1,
                    "source_revision": "revision",
                    **binding,
                    "candidate_key": candidate.key,
                    "candidate": candidate.to_dict(),
                }
            },
        },
    )
    assert organizer._manual_decision_for("课程").action == "invalid"
    assert organizer._process_course("课程", str(source)) is False
    assert organizer._process_course("课程", str(source)) is False
    assert provider.calls == []
    assert media.exists()
    assert not list(roots["tv"].rglob("*.mp4"))
    assert not list(roots["movies"].rglob("*.mp4"))
    assert not list(roots["children"].rglob("*.mp4"))


class _ClassifierSpy:
    def __init__(self):
        self.calls = 0

    def classify(self, **_kwargs):
        self.calls += 1
        raise AssertionError("manual decision must bypass classifier")


@pytest.mark.parametrize("library", ["tv", "movie", "children"])
def test_confirm_apply_uses_exact_library_without_classifier(tmp_path, library):
    root = Path(os.path.realpath(tmp_path))
    incoming = root / "incoming"
    roots = {name: root / name for name in ("tv", "movie", "children")}
    incoming.mkdir()
    for path in roots.values():
        path.mkdir()
    course = incoming / "课程"
    course.mkdir()
    (course / "1.mp4").write_bytes(b"media")
    spy = _ClassifierSpy()
    organizer = _organizer(
        [_row()],
        enabled=False,
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
        library_classifier=spy,
    )
    row = _data_item(organizer.get_review(), "课程")
    assert _success(
        organizer.save_review(
            {
                "raw_title": "课程",
                "revision": row["revision"],
                "action": "confirm",
                "final_title": "人工名称",
                "target_library": library,
            }
        )
    )
    assert not course.exists()
    assert (roots[library] / "人工名称" / "Season 1" / "人工名称 - S01E001.mp4").is_file()
    assert _response_data(organizer.get_review())["items"] == []
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"] == {}
    assert spy.calls == 0


def test_ignore_never_moves_or_calls_classifier(tmp_path):
    root = Path(os.path.realpath(tmp_path))
    incoming = root / "incoming"
    roots = {name: root / name for name in ("tv", "movie", "children")}
    incoming.mkdir()
    for path in roots.values():
        path.mkdir()
    course = incoming / "课程"
    course.mkdir()
    (course / "1.mp4").write_bytes(b"media")
    spy = _ClassifierSpy()
    organizer = _organizer(
        [_row()],
        naming_mode="apply",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
        library_classifier=spy,
    )
    row = _data_item(organizer.get_review(), "课程")
    assert _success(
        organizer.save_review(
            {"raw_title": "课程", "revision": row["revision"], "action": "ignore"}
        )
    )
    assert organizer._process_course("课程", str(course)) is False
    assert organizer._process_course("课程", str(course)) is False
    assert course.exists()
    assert spy.calls == 0


def test_preview_scan_keeps_all_files_and_directories_unchanged(tmp_path):
    root = Path(os.path.realpath(tmp_path))
    incoming = root / "incoming"
    roots = {name: root / name for name in ("tv", "movie", "children")}
    incoming.mkdir()
    for path in roots.values():
        path.mkdir()
    course = incoming / "课程"
    course.mkdir()
    (course / "1.mp4").write_bytes(b"media")

    organizer = _organizer(
        [_row()],
        naming_mode="preview",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    def fingerprint():
        return sorted(
            (
                str(path.relative_to(root)),
                "file" if path.is_file() else "dir",
                path.stat().st_size if path.is_file() else 0,
                path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
        )

    before = fingerprint()
    assert organizer._process_course("课程", str(course)) is False
    assert organizer._process_course("课程", str(course)) is False
    assert fingerprint() == before
    assert course.exists()
    assert not list(roots["tv"].rglob("*.mp4"))
    assert not list(roots["movie"].rglob("*.mp4"))
    assert not list(roots["children"].rglob("*.mp4"))


def test_confirm_immediately_applies_one_item_and_keeps_completed_history(tmp_path, caplog):
    caplog.set_level("INFO")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for root in roots.values():
        root.mkdir()
    organizer = _organizer(
        [_row()],
        naming_mode="preview",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
    )
    organizer._run = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("manual confirmation must not trigger a full scan")
    )
    row = _data_item(organizer.get_review(), "课程")

    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "tv",
        }
    )

    assert _success(response)
    assert not source.exists()
    assert (roots["tv"] / "人工名称" / "Season 1" / "人工名称 - S01E001.mp4").is_file()
    assert _response_data(organizer.get_review())["items"] == []
    history = organizer.get_data("naming_preview_v1")
    assert history[0]["raw_title"] == "课程"
    assert history[0]["completed_at"]
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"] == {}
    assert "event=manual_review_applied" in caplog.text
    assert "event=move_started" not in caplog.text
    assert "event=move_completed" not in caplog.text


def test_confirm_failure_keeps_review_row_and_decision_for_retry(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")
    roots = {name: tmp_path / name for name in ("tv", "movie", "children")}
    for root in roots.values():
        root.mkdir()
    native_adapter = _NoMediaNativeAdapter(
        {
            "incoming": str(incoming),
            "tv_output": str(roots["tv"]),
            "movie_output": str(roots["movie"]),
            "children_output": str(roots["children"]),
        }
    )
    organizer = _organizer(
        [_row()],
        naming_mode="preview",
        incoming=str(incoming),
        tv_output=str(roots["tv"]),
        movie_output=str(roots["movie"]),
        children_output=str(roots["children"]),
        native_adapter=native_adapter,
    )
    row = _data_item(organizer.get_review(), "课程")

    response = organizer.save_review(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "action": "confirm",
            "final_title": "人工名称",
            "target_library": "tv",
        }
    )

    assert not _success(response)
    assert source.exists()
    assert _data_item(organizer.get_review(), "课程")["final_title"] == "人工名称"
    assert organizer.get_data(CourseOrganizer.MANUAL_DECISIONS_KEY)["items"]["课程"]["action"] == "confirm"
    assert _message(response) == "源目录中没有可整理的视频文件，可能已经整理过，请刷新列表"
def test_review_surfaces_source_pending_when_binding_unavailable_but_dir_exists(
    tmp_path, monkeypatch
):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")

    organizer = _organizer([_row()], incoming=str(incoming))
    monkeypatch.setattr(organizer, "_current_source_binding", lambda raw_title: None)

    items = _response_data(organizer.get_review())["items"]
    assert len(items) == 1
    assert items[0]["raw_title"] == "课程"
    assert items[0]["status_label"] == "源目录待稳定"


def test_review_hides_row_when_source_directory_missing(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    organizer = _organizer([_row()], incoming=str(incoming))
    source_dir = incoming / "课程"
    assert source_dir.exists()
    source_dir.rmdir()

    monkeypatch.setattr(organizer, "_current_source_binding", lambda raw_title: None)

    assert _response_data(organizer.get_review())["items"] == []
def test_search_tmdb_not_blocked_by_running_confirm_move(tmp_path):
    import threading
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")

    organizer = _organizer(
        [_row()],
        incoming=str(incoming),
        metadata_provider=_TmdbProvider(),
    )
    row = _data_item(organizer.get_review(), "课程")

    lock_held = threading.Event()
    release = threading.Event()

    def hold_move_lock():
        with organizer._thread_lock:
            lock_held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_move_lock, daemon=True)
    holder.start()
    assert lock_held.wait(3), "move lock not acquired"

    search = organizer.search_tmdb({"raw_title": "课程", "revision": row["revision"]})
    release.set()
    holder.join(timeout=5)

    assert _success(search)


def test_associate_tmdb_not_blocked_by_running_confirm_move(tmp_path):
    import threading
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "课程"
    source.mkdir()
    (source / "1.mp4").write_bytes(b"media")

    organizer = _organizer(
        [_row()],
        incoming=str(incoming),
        metadata_provider=_TmdbProvider(),
    )
    row = _data_item(organizer.get_review(), "课程")

    lock_held = threading.Event()
    release = threading.Event()

    def hold_move_lock():
        with organizer._thread_lock:
            lock_held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_move_lock, daemon=True)
    holder.start()
    assert lock_held.wait(3), "move lock not acquired"

    associate = organizer.associate_tmdb(
        {
            "raw_title": "课程",
            "revision": row["revision"],
            "candidate_key": "themoviedb:课程-0:movie",
        }
    )
    release.set()
    holder.join(timeout=5)

    assert _success(associate)
