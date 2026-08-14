import copy
import json

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


def test_form_has_four_steps_and_preserves_every_config_control():
    organizer = CourseOrganizer(config=_config())
    before = organizer._get_config()

    form, defaults = organizer.get_form()
    components = list(_walk_components(form))
    tabs = [component for component in components if component["component"] == "VTabs"]
    assert len(tabs) == 2
    tab_labels = [item["text"] for item in tabs[0]["content"]]

    assert tab_labels == ["选择目录", "安全预览", "处理异常", "开启自动整理"]
    assert all(tab["props"]["model"] == "_active_config_tab" for tab in tabs)
    assert "d-none d-md-flex" in tabs[0]["props"]["class"]
    assert tabs[0]["props"]["direction"] == "vertical"
    assert "d-flex d-md-none" in tabs[1]["props"]["class"]
    assert "overflow-x-auto" in tabs[1]["props"]["class"]
    assert all(
        int(item["props"]["min-height"]) >= 44
        for tab in tabs
        for item in tab["content"]
    )
    assert defaults["_active_config_tab"] == "directories"
    assert defaults["enabled"] is before["enabled"]
    assert defaults["naming_mode"] == before["naming_mode"]
    assert organizer._get_config()["enabled"] is before["enabled"]
    assert organizer._get_config()["naming_mode"] == before["naming_mode"]

    expected_models = {
        "_active_config_tab",
        "enabled",
        "run_once",
        "incoming",
        "tv_output",
        "movie_output",
        "children_output",
        "interval",
        "naming_mode",
        "naming_sources",
        "naming_auto_threshold",
        "naming_min_margin",
        "naming_uncertain_policy",
        "naming_append_tmdb_id",
        "naming_ai_review",
        "naming_manual_overrides",
        "naming_clear_cache_once",
    }
    models = {
        component.get("props", {}).get("model")
        for component in components
        if component.get("props", {}).get("model")
    }
    assert models == expected_models
    assert all(component["component"] != "VStepper" for component in components)
    windows = [component for component in components if component["component"] == "VWindow"]
    assert len(windows) == 1
    assert windows[0]["props"]["model"] == "_active_config_tab"
    assert [item["props"]["value"] for item in windows[0]["content"]] == [
        "directories",
        "preview",
        "exceptions",
        "automatic",
    ]
    assert all(
        "show" not in component.get("props", {})
        for component in components
        if component["component"] == "VCard"
    )

    directory_card = next(
        component
        for component in components
        if component["component"] == "VCard"
        and component["props"].get("title") == "选择目录"
    )
    directory_models = {
        component["props"].get("model")
        for component in _walk_components(directory_card)
        if component["props"].get("model")
    }
    assert directory_models == {"incoming", "tv_output", "movie_output", "children_output"}
    assert "只记录建议，不移动文件" in json.dumps(directory_card, ensure_ascii=False)

    advanced_panels = next(
        component for component in components if component["component"] == "VExpansionPanels"
    )
    assert "model" not in advanced_panels["props"]
    assert "modelValue" not in advanced_panels["props"]
    assert "mandatory" not in advanced_panels["props"]
    advanced_panel = next(
        component
        for component in components
        if component["component"] == "VExpansionPanel"
        and component["props"].get("title") == "高级设置"
    )
    assert "model" not in advanced_panel["props"]

    advanced_panel_content = advanced_panel.get("content", [])
    assert len(advanced_panel_content) == 1
    advanced_panel_text = advanced_panel_content[0]
    assert advanced_panel_text["component"] == "VExpansionPanelText"

    assert all(
        "model" not in component.get("props", {})
        for component in advanced_panel_content
        if component is not advanced_panel_text
    )

    advanced_content = advanced_panel_text.get("content", [])

    advanced_models = {
        component["props"].get("model")
        for component in _walk_components(advanced_content)
        if component["props"].get("model")
    }
    assert advanced_models == {
        "interval",
        "naming_sources",
        "naming_auto_threshold",
        "naming_min_margin",
        "naming_uncertain_policy",
        "naming_append_tmdb_id",
        "naming_ai_review",
        "naming_manual_overrides",
        "naming_clear_cache_once",
    }

    editable_types = {"VSwitch", "VTextField", "VSelect", "VTextarea"}
    editable = [
        component for component in components if component["component"] in editable_types
    ]
    assert editable
    assert all(component["props"].get("density") != "compact" for component in editable)
    assert all("aria-label" in component["props"] for component in editable)
    assert "仍会整理" in json.dumps(form, ensure_ascii=False)


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
