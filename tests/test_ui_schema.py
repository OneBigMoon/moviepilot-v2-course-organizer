import copy
import json
from pathlib import Path

import pytest

from tests.courseorganizer_testkit import load_courseorganizer


MODULE = load_courseorganizer()
CourseOrganizer = MODULE.CourseOrganizer


def _walk_components(value):
    if isinstance(value, dict):
        if "component" in value:
            yield value
        yield from _walk_components(value.get("content"))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_components(item)


def _preview_table(page):
    return next(
        component
        for component in _walk_components(page)
        if component["component"] == "VDataTableVirtual"
    )


def _config(**overrides):
    config = {
        "enabled": False,
        "incoming": "/srv/incoming",
        "tv_output": "/srv/tv",
        "movie_output": "/srv/movies",
        "children_output": "/srv/children",
        "interval": 420,
        "naming_mode": "preview",
        "naming_sources": "themoviedb,douban",
        "naming_auto_threshold": 92,
        "naming_min_margin": 14,
        "naming_uncertain_policy": "hold",
        "naming_append_tmdb_id": True,
        "naming_ai_review": True,
        "naming_manual_overrides": "课程=Course",
        "naming_clear_cache_once": True,
    }
    config.update(overrides)
    return config


def _set_rows_resolver(organizer, rows):
    class _RowsResolver:
        def preview_rows(self):
            return rows

    normalized = organizer._get_config()
    organizer._resolver = _RowsResolver()
    organizer._resolver_signature = tuple(
        normalized[key]
        for key in (
            "naming_mode",
            "naming_ai_review",
            "naming_sources",
            "naming_auto_threshold",
            "naming_min_margin",
            "naming_uncertain_policy",
            "naming_append_tmdb_id",
            "naming_manual_overrides",
        )
    )


def test_form_is_lightweight_and_keeps_moviepilot_directory_settings_authoritative():
    organizer = CourseOrganizer(config=_config())
    before = organizer._get_config()

    form, defaults = organizer.get_form()
    components = list(_walk_components(form))
    rendered = json.dumps(form, ensure_ascii=False)
    models = {
        component.get("props", {}).get("model")
        for component in components
        if component.get("props", {}).get("model")
    }

    assert models == {
        "naming_sources",
        "naming_auto_threshold",
        "naming_min_margin",
        "naming_append_tmdb_id",
        "naming_ai_review",
        "naming_clear_cache_once",
    }
    assert not models.intersection(
        {
            "enabled",
            "run_once",
            "incoming",
            "tv_output",
            "movie_output",
            "children_output",
            "interval",
            "naming_mode",
        }
    )
    assert "目录和整理规则沿用 MoviePilot 系统设置" in rendered
    settings_button = next(
        component
        for component in components
        if component["component"] == "VBtn"
        and component.get("props", {}).get("href") == "#/setting"
    )
    assert settings_button["text"] == "打开 MoviePilot 存储与目录设置"
    assert defaults["incoming"] == before["incoming"]
    assert defaults["naming_mode"] == before["naming_mode"]


def test_plugin_exposes_project_homepage_for_local_install_fallback():
    expected = (
        "https://github.com/OneBigMoon/moviepilot-v2-course-organizer"
    )
    assert CourseOrganizer.author_url == expected
    assert CourseOrganizer.project_url == expected
    assert CourseOrganizer.plugin_repo == expected


def test_review_reads_whitelisted_moviepilot_directory_rules(monkeypatch):
    organizer = CourseOrganizer(config=_config())
    _set_rows_resolver(organizer, [])
    rules = [
        {
            "name": "电视剧",
            "download_path": "/media/incoming",
            "library_path": "/media/tv",
            "media_type": "电视剧",
            "storage": "local",
            "library_storage": "local",
            "monitor_type": "monitor",
            "transfer_type": "move",
            "renaming": True,
            "scraping": True,
            "notify": False,
            "secret": "must-not-leak",
        },
        {
            "name": "电影",
            "download_path": "/media/incoming",
            "library_path": "/media/movies",
            "media_type": "电影",
            "storage": "local",
            "library_storage": "local",
            "transfer_type": "copy",
            "renaming": True,
        },
        {
            "name": "儿童课程",
            "download_path": "/media/incoming",
            "library_path": "/media/courses",
            "media_type": "电视剧",
            "storage": "local",
            "library_storage": "local",
            "transfer_type": "link",
            "renaming": True,
            "media_category": "儿童",
        },
    ]
    monkeypatch.setattr(
        organizer,
        "_load_moviepilot_directory_rules",
        lambda: (rules, ""),
    )

    data = organizer.get_review()["data"]

    assert data["rules_ready"] is True
    assert data["rules_message"] == ""
    assert data["monitoring_enabled"] is True
    assert data["monitoring_rules"] == ["电视剧"]
    assert data["incoming_path"] == "/media/incoming"
    assert data["settings_url"] == "#/setting"
    assert [(item["value"], item["path"]) for item in data["libraries"]] == [
        ("tv", "/media/tv"),
        ("movie", "/media/movies"),
        ("children", "/media/courses"),
    ]
    assert all("secret" not in item for item in data["directory_rules"])
    assert organizer._review_path_config()["incoming"] == "/media/incoming"
    assert organizer._review_path_config()["children_output"] == "/media/courses"


@pytest.mark.parametrize("name", ["儿童", "少儿", "幼儿", "课程"])
def test_directory_rule_children_library_uses_alias_not_target_path(name):
    organizer = CourseOrganizer(config=_config())

    assert organizer._directory_rule_library(
        {
            "name": name,
            "library_path": "/media/ordinary-library",
            "media_type": "电视剧",
        }
    ) == "children"


def test_directory_rule_path_courses_does_not_make_tv_rule_children():
    organizer = CourseOrganizer(config=_config())

    assert organizer._directory_rule_library(
        {
            "name": "电视剧",
            "library_path": "/media/courses",
            "media_type": "电视剧",
        }
    ) == "tv"


def test_directory_rule_prefers_moviepilot_media_category_over_alias_fallback():
    organizer = CourseOrganizer(config=_config())

    assert organizer._directory_rule_library(
        {
            "name": "学习资源",
            "media_type": "电视剧",
            "media_category": "儿童",
        }
    ) == "children"
    assert organizer._directory_rule_library(
        {
            "name": "亲子电影",
            "media_type": "电影",
            "media_category": "儿童",
        }
    ) == "movie"


def test_review_requires_moviepilot_smart_renaming(monkeypatch):
    organizer = CourseOrganizer(config=_config())
    _set_rows_resolver(organizer, [])
    rules = [
        {
            "name": "电影",
            "download_path": "/media/incoming",
            "library_path": "/media/movies",
            "media_type": "电影",
            "storage": "local",
            "library_storage": "local",
            "renaming": False,
        }
    ]
    monkeypatch.setattr(organizer, "_load_moviepilot_directory_rules", lambda: (rules, ""))

    data = organizer.get_review()["data"]

    assert data["rules_ready"] is False
    assert "电影规则未开启智能重命名" in data["rules_message"]


def test_review_hides_preview_row_when_source_directory_is_missing(monkeypatch):
    organizer = CourseOrganizer(config=_config())
    _set_rows_resolver(
        organizer,
        [
            {
                "raw_title": "已移动目录",
                "final_title": "已移动目录",
                "target_library": "tv",
                "target_output_root": "/srv/tv/已移动目录",
                "status": "review",
                "reason_codes": ["review_needed"],
                "source": "themoviedb",
                "media_id": "123",
                "media_type": "tv",
            }
        ],
    )
    monkeypatch.setattr(organizer, "_current_source_binding", lambda _raw_title: None)

    assert organizer.get_review()["data"]["items"] == []


def test_refresh_review_runs_preview_scan_even_when_saved_mode_is_apply(monkeypatch):
    organizer = CourseOrganizer(config=_config(naming_mode="apply"))
    calls = []

    def fake_run(*, force=False):
        calls.append((force, organizer._get_config()["naming_mode"]))

    monkeypatch.setattr(organizer, "_run", fake_run)
    monkeypatch.setattr(organizer, "get_review", lambda: {"refreshed": True})

    assert organizer.refresh_review() == {"refreshed": True}
    assert calls == [(True, "preview")]
    assert organizer._get_config()["naming_mode"] == "apply"


def test_review_fails_closed_for_missing_or_ambiguous_moviepilot_rules(monkeypatch):
    organizer = CourseOrganizer(config=_config())
    _set_rows_resolver(organizer, [])
    rules = [
        {
            "name": "电视剧 A",
            "download_path": "/media/incoming",
            "library_path": "/media/tv-a",
            "media_type": "电视剧",
            "storage": "local",
            "library_storage": "local",
        },
        {
            "name": "电视剧 B",
            "download_path": "/media/incoming",
            "library_path": "/media/tv-b",
            "media_type": "电视剧",
            "storage": "local",
            "library_storage": "local",
        },
    ]
    monkeypatch.setattr(
        organizer,
        "_load_moviepilot_directory_rules",
        lambda: (rules, ""),
    )

    data = organizer.get_review()["data"]

    assert data["rules_ready"] is False
    assert data["libraries"] == []
    assert "电视剧存在多条匹配规则" in data["rules_message"]
    assert "缺少电影目录规则" in data["rules_message"]
    assert "缺少儿童课程目录规则" in data["rules_message"]


def test_vue_render_mode_and_review_api_are_host_contracts():
    organizer = CourseOrganizer(config=_config())
    assert organizer.get_render_mode() == ("vue", "dist/assets")
    api = organizer.get_api()
    assert {(item["path"], tuple(item["methods"]), item["auth"]) for item in api} == {
        ("/review", ("GET",), "bear"),
        ("/review", ("POST",), "bear"),
        ("/review/refresh", ("POST",), "bear"),
        ("/review/tmdb/search", ("POST",), "bear"),
        ("/review/tmdb/associate", ("POST",), "bear"),
    }


def test_vue_build_filter_preserves_course_component_overrides():
    plugin_root = Path(__file__).parents[1] / "plugins.v2" / "courseorganizer"
    vite_config = (plugin_root / "vite.config.js").read_text(encoding="utf-8")
    page_source = (plugin_root / "src" / "components" / "Page.vue").read_text(
        encoding="utf-8"
    )
    config_source = (plugin_root / "src" / "components" / "Config.vue").read_text(
        encoding="utf-8"
    )
    assert "emit('close')\n  await nextTick()\n  window.location.assign(target)" in page_source
    assert '@click.stop="openMoviePilotSettings"' in page_source
    assert "emit('close')\n  await nextTick()\n  window.location.assign('#/setting')" in config_source

    assert "!rule.selector.includes('.course-')" in vite_config
    assert ".course-review-page :deep(.v-btn)" in page_source
    assert "min-height: 44px" in page_source
    assert "min-width: 44px" in page_source
    assert "review/tmdb/search" in page_source
    assert "review/tmdb/associate" in page_source
    assert "review/refresh" in page_source
    assert "async function refreshReview" in page_source
    assert "重新搜索 TMDB" in page_source
    assert "未找到匹配，可修改名称后重试" in page_source
    assert "raw_title: row.raw_title" in page_source
    assert "const savingKeys = ref([])" in page_source
    assert "const tmdbLoadingKeys = ref([])" in page_source
    assert "const savingKey =" not in page_source
    assert "new EventSource('/api/v1/system/progress/filetransfer'" in page_source
    assert "fileTransferSeenActive" in page_source
    assert "sanitizeProgressText" in page_source
    assert "organizingStatusText" in page_source
    assert 'aria-live="polite"' in page_source
    assert page_source.count('aria-live="polite"') >= 2
    assert "已关联 TMDB：${data.final_title}" in page_source
    assert "[row.raw_title]: []" in page_source
    assert page_source.count("确认并整理") >= 2
    assert "saveReview(row, 'restore')" in page_source
    assert "当前一次只能整理一个项目，完成后可继续下一项" in page_source
    assert "整理中" in page_source
    assert "const organizingKey = ref('')" in page_source
    assert "const confirmingKey = ref('')" not in page_source
    assert "function isConfirming(row)" not in page_source
    assert "function isOrganizing(row)" in page_source
    associate_source = page_source.split("async function associateTmdb", 1)[1].split(
        "async function saveReview", 1
    )[0]
    save_source = page_source.split("async function saveReview", 1)[1].split(
        "function isSaving", 1
    )[0]
    assert "organizingKey" not in associate_source
    sanitize_source = page_source.split("function sanitizeProgressText", 1)[1].split(
        "function stopFileTransferProgress", 1
    )[0]
    assert ".split('/')" not in sanitize_source
    assert "if (action === 'confirm' && organizingKey.value) return" in save_source
    assert (
        "if (action === 'confirm' && organizingKey.value && organizingKey.value !== row.raw_title) return"
        not in save_source
    )
    assert "startFileTransferProgress()" in save_source
    assert "await loadReview()" not in associate_source
    assert "await loadReview()" not in save_source
    assert page_source.count("<VProgressLinear") >= 2
    assert page_source.count('v-if="isOrganizing(row)"') >= 4
    assert page_source.count(':loading="isOrganizing(row)"') >= 2
    assert "hasOrganizingValue()" in page_source
    assert page_source.count(
        ':disabled="batchRunning || Boolean(organizingKey) || !canConfirm(row) || isTmdbLoading(row)"'
    ) >= 2
    assert page_source.count(
        ':disabled="batchRunning || isSourcePending(row) || isSaving(row) || isOrganizing(row)"'
    ) >= 2
    assert "async function organizeSelected" in page_source
    assert "await saveReview(row, 'confirm', { queued: true })" in page_source
    assert "失败项目已保留" in page_source
    assert "批量整理" in page_source
    assert "items.value = items.value.filter(item => item.raw_title !== row.raw_title)" in page_source
    assert "notice.value = '整理完成'" in page_source
    assert "文件移动完成，正在写入整理记录…" in page_source
    assert "设置 → 存储 &amp; 目录" in page_source
    assert "directoryRules" in page_source
    assert "monitoringEnabled" in page_source
    assert 'v-if="monitoringEnabled && hasItems"' in page_source
    assert "沿用 MoviePilot 系统设置" in config_source
    for duplicate_model in (
        "incoming",
        "tv_output",
        "movie_output",
        "children_output",
        "interval",
        "naming_mode",
        "enabled",
        "run_once",
    ):
        assert f'localConfig.{duplicate_model}' not in config_source


def test_page_uses_four_compact_headers_and_localized_status_counts():
    organizer = CourseOrganizer(config=_config(enabled=True))
    rows = [
        {
            "raw_title": "课程 A",
            "local_title": "课程 A",
            "final_title": "课程 A (2024)",
            "target_library": "tv",
            "target_output_root": "/srv/tv/课程 A",
            "status": "auto_external",
        },
        {
            "raw_title": "课程 B",
            "local_title": "课程 B",
            "final_title": "",
            "target_library": "movie",
            "target_output_root": "/srv/movies/课程 B",
            "status": "local_fallback",
        },
        {
            "raw_title": "课程 C",
            "local_title": "课程 C",
            "final_title": "课程 C",
            "target_library": "children",
            "status": "ignore",
        },
        {
            "raw_title": "课程 D",
            "local_title": "课程 D",
            "final_title": "课程 D",
            "target_library": "hold",
            "status": "auto_external",
        },
        {
            "raw_title": "课程 E",
            "local_title": "课程 E",
            "final_title": "课程 E",
            "target_library": "tv",
            "status": "review",
        },
    ]
    original_rows = copy.deepcopy(rows)
    _set_rows_resolver(organizer, rows)

    page = organizer.get_page()
    table = _preview_table(page)
    headers = table["props"]["headers"]
    items = table["props"]["items"]

    assert [header["title"] for header in headers] == [
        "原始名称",
        "建议名称",
        "目标位置",
        "状态",
    ]
    assert [header["key"] for header in headers] == [
        "raw_title",
        "final_title",
        "target_position",
        "status",
    ]
    assert len(headers) == 4
    assert all(set(item) == {"raw_title", "final_title", "target_position", "status"} for item in items)
    assert all(
        technical_key not in item
        for item in items
        for technical_key in ("target_library", "target_output_root", "source", "score")
    )
    assert [item["status"] for item in items] == [
        "可以整理",
        "可以整理",
        "已跳过",
        "需要确认",
        "需要确认",
    ]
    assert items[0]["target_position"] == "/srv/tv/课程 A"
    assert items[1]["final_title"] == "课程 B"
    assert items[3]["target_position"] == "待确认"
    assert rows == original_rows

    chips = [
        component
        for component in _walk_components(page)
        if component["component"] == "VChip"
    ]
    chip_text = {component["text"]: component["props"]["color"] for component in chips}
    assert chip_text["可以整理 2"] == "success"
    assert chip_text["需要确认 2"] == "warning"
    assert chip_text["已跳过 1"] == "default"
    component_order = [component["component"] for component in _walk_components(page)]
    assert max(index for index, kind in enumerate(component_order) if kind == "VChip") < component_order.index(
        "VDataTableVirtual"
    )
    table_wrap = next(
        component
        for component in _walk_components(page)
        if component["component"] == "VSheet"
        and "course-preview-table-wrap" in component["props"].get("class", "")
    )
    assert "overflow-x-auto" in table_wrap["props"]["class"]
    rendered = json.dumps(page, ensure_ascii=False)
    assert "只记录建议，不移动文件" in rendered


def test_page_off_does_not_touch_resolver_and_exposes_empty_localized_summary(monkeypatch):
    organizer = CourseOrganizer(config=_config(enabled=True, naming_mode="off"))
    monkeypatch.setattr(
        organizer,
        "_get_resolver",
        lambda: (_ for _ in ()).throw(AssertionError("resolver should not be used")),
    )
    monkeypatch.setattr(
        organizer,
        "_build_resolver",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider should not be touched")),
    )

    page = organizer.get_page()
    table = _preview_table(page)

    assert page[0]["component"] == "VCol"
    assert table["props"]["items"] == []
    assert [header["title"] for header in table["props"]["headers"]] == [
        "原始名称",
        "建议名称",
        "目标位置",
        "状态",
    ]
    rendered = json.dumps(page, ensure_ascii=False)
    assert "命名已关闭，仍会整理" in rendered
    assert "暂无可预览记录" in rendered
    assert all(
        component["text"].endswith(" 0")
        for component in _walk_components(page)
        if component["component"] == "VChip"
        and any(label in component["text"] for label in ("可以整理", "需要确认", "已跳过"))
    )


@pytest.mark.parametrize("naming_mode", ["preview", "apply"])
def test_page_keeps_multiple_rows_visible_without_provider_access(naming_mode):
    organizer = CourseOrganizer(config=_config(enabled=True, naming_mode=naming_mode))
    rows = [
        {
            "raw_title": "课程目录A",
            "final_title": "课程目录A",
            "target_library": "tv",
            "target_output_root": "/configured/tv/课程目录A",
            "status": "auto_external",
        },
        {
            "raw_title": "课程目录B",
            "final_title": "课程目录B",
            "target_library": "movie",
            "target_output_root": "/configured/movies/课程目录B",
            "status": "local_fallback",
        },
    ]
    _set_rows_resolver(organizer, rows)

    table = _preview_table(organizer.get_page())
    assert table["props"]["items"][0]["raw_title"] == "课程目录A"
    assert table["props"]["items"][1]["raw_title"] == "课程目录B"
    assert table["props"]["items"][0]["status"] == "可以整理"
    assert table["props"]["items"][1]["status"] == "可以整理"
    assert table["props"]["height"] == "min(52vh, 30rem)"
    assert table["props"]["fixed-header"] is True


def test_page_does_not_interpolate_untrusted_titles_or_paths_into_component_labels():
    unsafe_title = '<img src=x onerror="alert(1)">'
    unsafe_path = "/srv/<script>alert(1)</script>"
    organizer = CourseOrganizer(
        config=_config(
            incoming=unsafe_path,
            tv_output=unsafe_path,
            movie_output=unsafe_path,
            children_output=unsafe_path,
        )
    )
    _set_rows_resolver(
        organizer,
        [
            {
                "raw_title": unsafe_title,
                "final_title": unsafe_title,
                "target_library": "tv",
                "target_output_root": unsafe_path,
                "status": "auto_external",
            }
        ],
    )

    form, _ = organizer.get_form()
    page = organizer.get_page()
    for component in [*_walk_components(form), *_walk_components(page)]:
        props = component.get("props", {})
        assert unsafe_title not in str(props.get("title", ""))
        assert unsafe_title not in str(props.get("label", ""))
        assert unsafe_path not in str(props.get("title", ""))
        assert unsafe_path not in str(props.get("label", ""))
