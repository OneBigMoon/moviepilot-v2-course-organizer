import logging
import errno
import os
import stat
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_ORIGINAL_LINK = os.link

try:
    from app.log import logger as _logger
except Exception:
    _logger = logging.getLogger("CourseOrganizer")

try:
    from app.plugins import _PluginBase
except Exception:

    class _PluginBase:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._config: Dict[str, Any] = kwargs.get("config", {})
            self._data: Dict[str, Any] = {}

        def get_config(self) -> Dict[str, Any]:
            return self._config

        def get_data(self, key: str, default: Any = None) -> Any:
            return self._data.get(key, default)

        def save_data(self, key: str, value: Any) -> None:
            if value is None:
                self._data.pop(key, None)
            else:
                self._data[key] = value

        def update_config(self, config: Dict[str, Any]) -> None:
            self._config.update(config)

from . import naming
from .providers import (
    LibraryRouteResult,
    MoviePilotAIReviewer,
    MoviePilotLibraryClassifier,
    MoviePilotMetadataProvider,
)
from .resolver import NamingConfig, NamingDecision, SmartNamingResolver


_NATURAL_SPLIT_RE = re.compile(r"(\d+)")
_INVALID_NAME_RE = re.compile(r"[\\/:*?\"<>|]+")
_LEADING_EPISODE_RE = re.compile(r"^(0*\d+)(?=[\s._\-、，—–·．・]|$)")
_RANGE_BOUNDARY = r"(?=$|[\s._\-、，—–·．・])"
_LEADING_BARE_RANGE_RE = re.compile(
    rf"^(?P<start>0*\d+)\s*-\s*(?P<end>0*\d+){_RANGE_BOUNDARY}"
)
_LEADING_EN_RANGE_RE = re.compile(
    rf"^(?:EP?|ep?)\s*(?P<start>0*\d+)\s*-\s*(?:EP?|ep?)?\s*(?P<end>0*\d+){_RANGE_BOUNDARY}"
)
_LEADING_CN_RANGE_RE = re.compile(
    r"^第\s*(?P<start>0*\d+)\s*-\s*(?P<end>0*\d+)\s*集"
)
_LEADING_RANGE_CANDIDATE_RE = re.compile(
    r"^(?:(?:EP?|ep?)\s*)?(?P<start>0*\d+)\s*-\s*(?:(?:EP?|ep?)\s*)?(?P<end>0*\d+)"
)
_CHINESE_NUMERAL_MAP = {
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_CHILD_AUDIENCE_TERMS = ("儿童", "少儿", "幼儿", "早教", "宝宝", "亲子")
_CHILD_AUDIENCE_RE = re.compile("|".join(re.escape(term) for term in _CHILD_AUDIENCE_TERMS))


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in {"false", "0", "no", "off", ""}:
            return False
        if lower in {"true", "1", "yes", "on", "y", "t"}:
            return True
        return default
    if value is None:
        return default
    return bool(value)
_EN_SEASON_RE = re.compile(r"(?i)\bseason\s*([0-9]{1,3})\b|\bs\s*([0-9]{1,3})\b")
_CN_SEASON_RE = re.compile(r"第\s*([0-9零一二三四五六七八九十]+)\s*季")


class CourseOrganizer(_PluginBase):
    plugin_name = "课程自动整理"
    plugin_config_prefix = "courseorganizer_"
    auth_level = 1
    plugin_order = 90
    plugin_version = "1.5.4"
    plugin_desc = "稳定后识别、分类并整理到电视剧、电影或儿童媒体库"
    plugin_author = "OpenAI"
    plugin_icon = "icons/courseorganizer.svg"

    MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".m4a"}
    SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
    INCOMPLETE_SUFFIXES = (".partial", ".part", ".tmp", ".crdownload", ".incomplete", ".!qb")
    DEFAULT_INTERVAL = 300
    DEFAULT_INCOMING = "/volume1/未整理"
    DEFAULT_TV_OUTPUT = "/volume1/TV"
    DEFAULT_MOVIE_OUTPUT = "/volume1/Movies"
    DEFAULT_CHILDREN_OUTPUT = "/volume1/儿童"

    _thread_lock = threading.Lock()
    # MoviePilot update_config persists only; this lock linearizes persistence and cancellation.
    _run_once_lock = threading.Lock()
    _run_once_timer: Optional[threading.Timer] = None
    _run_once_owner: Optional["CourseOrganizer"] = None
    _run_once_claimed = False
    _run_once_generation = 0
    _run_once_token: Optional[int] = None

    def __init__(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._metadata_provider_override = kwargs.pop("metadata_provider", None)
        self._ai_reviewer_override = kwargs.pop("ai_reviewer", None)
        self._library_classifier_override = kwargs.pop("library_classifier", None)
        self._clock = kwargs.pop("clock", time.time)
        super().__init__(*args, **kwargs)
        self._config_snapshot: Dict[str, Any] = self._normalize_config(kwargs.get("config", {}))
        self._resolver: Optional[SmartNamingResolver] = None
        self._resolver_signature: Tuple[Any, ...] = ()
        self._library_classifier: Optional[MoviePilotLibraryClassifier] = None
        self._logger = _logger
        self._run_config_local = threading.local()

    def _load_plugin_data(self, key: str, default: Any) -> Any:
        try:
            value = self.get_data(key)
        except Exception:
            return default
        return default if value is None else value

    def _build_resolver(self, config: Dict[str, Any]) -> SmartNamingResolver:
        provider = self._metadata_provider_override or MoviePilotMetadataProvider()
        reviewer = None
        if config.get("naming_ai_review"):
            reviewer = self._ai_reviewer_override or MoviePilotAIReviewer()

        return SmartNamingResolver(
            load_data=self._load_plugin_data,
            save_data=self.save_data,
            provider=provider,
            ai_reviewer=reviewer,
            clock=self._clock,
        )

    def init_plugin(self, config: Optional[Dict[str, Any]] = None):
        self._config_snapshot = self._normalize_config(config)
        plugin_cls = type(self)

        if bool(self._config_snapshot.get("naming_clear_cache_once")):
            self._build_resolver(self._config_snapshot).clear()
            self.save_data("library_routing_cache_v1", {})
            reset = dict(self._config_snapshot)
            reset["naming_clear_cache_once"] = False
            self._persist_config(reset)
            self._config_snapshot = reset

        if self._config_snapshot.get("run_once"):
            with plugin_cls._run_once_lock:
                timer = plugin_cls._run_once_timer
                pending_config = dict(self._config_snapshot)
                pending_config["run_once"] = True
                if timer is not None and timer.is_alive():
                    if not self._persist_config(pending_config):
                        self._logger.error(
                            "CourseOrganizer[event=run_once_persist_failed] phase=pending"
                        )
                        return
                    plugin_cls._run_once_owner = self
                    self._config_snapshot = pending_config
                    self._logger.info(
                        "CourseOrganizer[event=run_once_coalesced] pending=true"
                    )
                    return

                if not self._persist_config(pending_config):
                    plugin_cls._invalidate_run_once_locked()
                    self._logger.error(
                        "CourseOrganizer[event=run_once_persist_failed] phase=pending"
                    )
                    return
                self._config_snapshot = pending_config
                plugin_cls._run_once_owner = self
                plugin_cls._run_once_claimed = False
                plugin_cls._run_once_generation += 1
                token = plugin_cls._run_once_generation
                plugin_cls._run_once_token = token
                self._logger.info(
                    "CourseOrganizer[event=run_once_scheduled] delay=0.2"
                )
                timer = None
                try:
                    timer = threading.Timer(
                        0.2,
                        lambda token=token: plugin_cls._run_once_dispatch(token),
                    )
                    timer.daemon = True
                    plugin_cls._run_once_timer = timer
                    timer.start()
                except Exception as exc:
                    plugin_cls._run_once_timer = None
                    plugin_cls._run_once_owner = None
                    plugin_cls._run_once_claimed = False
                    plugin_cls._run_once_token = None
                    if timer is not None:
                        try:
                            timer.cancel()
                        except Exception:
                            pass
                    self._logger.error(
                        "CourseOrganizer[event=run_once_schedule_failed] phase=%s error=%s",
                        "start" if timer is not None else "construct",
                        exc.__class__.__name__,
                    )
            return

        with plugin_cls._run_once_lock:
            timer = plugin_cls._run_once_timer
            if timer is not None and timer.is_alive():
                pending_config = dict(self._config_snapshot)
                pending_config["run_once"] = True
                if not self._persist_config(pending_config):
                    self._logger.error(
                        "CourseOrganizer[event=run_once_persist_failed] phase=pending"
                    )
                    return
                plugin_cls._run_once_owner = self
                self._config_snapshot = pending_config

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[Dict[str, Any]]:
        config = self._get_config()
        naming_mode = str(config["naming_mode"])
        rows: List[Dict[str, Any]] = []
        if naming_mode != "off":
            rows = self._get_resolver().preview_rows()

        target_labels = {"tv": "电视剧", "movie": "电影", "children": "儿童"}
        display_rows: List[Dict[str, Any]] = []
        status_counts = {"可以整理": 0, "需要确认": 0, "已跳过": 0}
        for source_row in rows:
            if not isinstance(source_row, dict):
                continue
            stored_status = str(source_row.get("status", "")).strip().lower()
            target_library = str(source_row.get("target_library", "")).lower()
            if stored_status == "ignore":
                display_status = "已跳过"
            elif stored_status in {"auto_external", "local_fallback"} and target_library in target_labels:
                display_status = "可以整理"
            else:
                display_status = "需要确认"

            display_rows.append(
                {
                    "raw_title": str(source_row.get("raw_title", "")),
                    "final_title": str(
                        source_row.get("final_title")
                        or source_row.get("local_title")
                        or source_row.get("raw_title", "")
                    ),
                    "target_position": str(
                        source_row.get("target_output_root")
                        or target_labels.get(target_library, "待确认")
                    ),
                    "status": display_status,
                }
            )
            status_counts[display_status] += 1

        headers = [
            {"title": title, "text": title, "key": key, "value": key}
            for key, title in (
                ("raw_title", "原始名称"),
                ("final_title", "建议名称"),
                ("target_position", "目标位置"),
                ("status", "状态"),
            )
        ]
        preview_subtitle = (
            "命名已关闭，仍会整理；此页未读取预览记录"
            if naming_mode == "off"
            else f"{len(display_rows)} 条记录 · 只记录建议，不移动文件"
        )
        preview_table = {
            "component": "VDataTableVirtual",
            "props": {
                "class": "course-preview-table text-sm",
                "headers": headers,
                "items": display_rows,
                "height": "min(52vh, 30rem)",
                "density": "compact",
                "fixed-header": True,
                "hide-no-data": True,
                "hover": True,
            },
        }
        status_chips = [
            {
                "component": "VChip",
                "props": {
                    "color": color,
                    "size": "small",
                    "variant": "tonal",
                    "aria-label": f"{label} {status_counts[label]} 条",
                },
                "text": f"{label} {status_counts[label]}",
            }
            for label, color in (
                ("可以整理", "success"),
                ("需要确认", "warning"),
                ("已跳过", "default"),
            )
        ]
        preview_content: List[Dict[str, Any]] = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "density": "comfortable",
                    "class": "ma-3 mb-2",
                },
                "text": "只记录建议，不移动文件。先核对名称和目标位置，再决定是否开启自动整理。",
            },
            {
                "component": "VSheet",
                "props": {
                    "class": "course-status-summary d-flex flex-wrap align-center ga-2 px-4 py-2 border-b",
                    "color": "transparent",
                    "aria-label": "预览状态统计",
                },
                "content": status_chips,
            },
        ]
        if not display_rows:
            preview_content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "class": "mx-3 mb-3",
                        "role": "status",
                    },
                    "text": "暂无可预览记录。选择目录并开启安全预览后，这里会显示建议。",
                }
            )
        preview_content.append(
            {
                "component": "VSheet",
                "props": {
                    "class": "course-preview-table-wrap overflow-x-auto",
                    "color": "transparent",
                },
                "content": [preview_table],
            }
        )
        preview_card = {
            "component": "VCard",
            "props": {
                "title": "安全预览",
                "subtitle": preview_subtitle,
                "variant": "outlined",
                "class": "course-preview-card",
            },
            "content": preview_content,
        }

        enabled_text = "已启用" if config["enabled"] else "已停用"
        run_once_text = "已请求一次性运行" if config["run_once"] else "未请求一次性运行"
        policy_text = (
            "低置信度时保留本地名"
            if config["naming_uncertain_policy"] == "local"
            else "低置信度时暂停整理"
        )
        return [
            {
                "component": "VCol",
                "props": {"cols": 12, "class": "pt-0"},
                "content": [preview_card],
            },
            {
                "component": "VRow",
                "props": {"align": "stretch", "class": "mt-2 ga-2"},
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4, "class": "pa-0"},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {
                                    "title": "运行状态",
                                    "text": (
                                        f"{enabled_text} · 每 {config['interval']} 秒扫描 · {run_once_text} · "
                                        f"{policy_text}"
                                    ),
                                    "variant": "tonal",
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 8, "class": "pa-0"},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {
                                    "title": "目录流向",
                                    "text": (
                                        f"{config['incoming']} → 电视剧 {config['tv_output']} · "
                                        f"电影 {config['movie_output']} · 儿童 {config['children_output']}"
                                    ),
                                    "variant": "tonal",
                                    "class": "text-break",
                                },
                            }
                        ],
                    },
                ],
            },
        ]

    def get_state(self) -> bool:
        return bool(self._get_config().get("enabled"))

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        config = self._get_config()
        defaults = dict(config)
        # FormRender-only state; configuration normalization ignores it.
        defaults["_active_config_tab"] = "directories"

        field_props = {
            "density": "comfortable",
            "variant": "outlined",
            "hide-details": True,
        }

        def text_field(model: str, label: str, **extra: Any) -> Dict[str, Any]:
            props = dict(field_props)
            props.update({"model": model, "label": label, "aria-label": label})
            props.update(extra)
            return {"component": "VTextField", "props": props}

        def switch(model: str, label: str, **extra: Any) -> Dict[str, Any]:
            props = {
                "model": model,
                "label": label,
                "aria-label": label,
                "color": "primary",
                "density": "comfortable",
                "hide-details": True,
                "inset": True,
            }
            props.update(extra)
            return {"component": "VSwitch", "props": props}

        def select(model: str, label: str, items: List[Dict[str, str]]) -> Dict[str, Any]:
            props = dict(field_props)
            props.update({"model": model, "label": label, "aria-label": label, "items": items})
            return {"component": "VSelect", "props": props}

        def step_item(
            title: str,
            subtitle: str,
            tab: str,
            content: List[Dict[str, Any]],
        ) -> Dict[str, Any]:
            return {
                "component": "VWindowItem",
                "props": {"value": tab, "class": "pt-1"},
                "content": [
                    {
                        "component": "VCard",
                        "props": {
                            "title": title,
                            "subtitle": subtitle,
                            "variant": "outlined",
                            "class": "pa-3",
                        },
                        "content": content,
                    }
                ],
            }

        directory_content = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "density": "comfortable",
                    "class": "mb-3",
                },
                "text": "先确认四个目录均已正确挂载；下一步安全预览只记录建议，不移动文件。",
            },
            text_field("incoming", "待整理目录"),
            text_field("tv_output", "电视剧目录"),
            text_field("movie_output", "电影目录"),
            text_field("children_output", "儿童目录"),
        ]

        preview_content = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "density": "comfortable",
                    "class": "mb-3",
                },
                "text": "安全预览只记录建议，不移动文件。关闭命名仍会整理；先核对原始名称、建议名称和目标位置。",
            },
            select(
                "naming_mode",
                "命名模式",
                [
                    {"title": "关闭命名（仍会整理）", "value": "off"},
                    {"title": "预览模式", "value": "preview"},
                    {"title": "应用模式", "value": "apply"},
                ],
            ),
        ]

        exception_content = [
            {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "density": "comfortable",
                    "class": "mb-3",
                },
                "text": "下载未完成、分类低置信度或需要人工判断时，会暂停整理并保留原目录。",
            }
        ]

        advanced_content = [
            text_field("interval", "扫描间隔（秒）", type="number", min=30),
            text_field("naming_sources", "命名数据源（逗号分隔）"),
            text_field("naming_auto_threshold", "自动采用阈值（80~100）", type="number", min=80, max=100),
            text_field("naming_min_margin", "领先幅度（5~30）", type="number", min=5, max=30),
            select(
                "naming_uncertain_policy",
                "低置信度策略",
                [
                    {"title": "仅预览，不自动采用", "value": "local"},
                    {"title": "阻止整理并等待确认", "value": "hold"},
                ],
            ),
            switch("naming_append_tmdb_id", "追加 TMDB ID 到目录"),
            switch(
                "naming_ai_review",
                "启用 AI 复核（受 MoviePilot 全局 AI 配置影响）",
            ),
            {
                "component": "VTextarea",
                "props": {
                    "model": "naming_manual_overrides",
                    "label": "人工覆盖",
                    "aria-label": "人工覆盖",
                    "rows": 4,
                    **field_props,
                },
            },
            switch(
                "naming_clear_cache_once",
                "一次性清空命名缓存",
                color="error",
            ),
        ]

        automatic_content = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "density": "comfortable",
                    "class": "mb-3",
                },
                "text": "确认安全预览结果后，再开启自动整理；保存语义保持不变。",
            },
            switch("enabled", "开启自动整理"),
            switch("run_once", "保存后扫描一次"),
        ]

        advanced_panels = {
            "component": "VExpansionPanels",
            "props": {
                "variant": "accordion",
                "class": "course-advanced-panels",
                "aria-label": "高级设置",
            },
            "content": [
                {
                    "component": "VExpansionPanel",
                    "props": {
                        "title": "高级设置",
                        "value": "advanced",
                        "aria-label": "高级设置",
                    },
                    "content": [
                        {
                            "component": "VExpansionPanelText",
                            "content": advanced_content,
                        }
                    ],
                }
            ],
        }
        exception_content.append(advanced_panels)

        tab_items = [
            ("directories", "选择目录"),
            ("preview", "安全预览"),
            ("exceptions", "处理异常"),
            ("automatic", "开启自动整理"),
        ]

        def tabs(class_name: str, **props: Any) -> Dict[str, Any]:
            tab_props = {
                "model": "_active_config_tab",
                "color": "primary",
                "density": "comfortable",
                "class": class_name,
                "aria-label": "课程整理步骤",
            }
            tab_props.update(props)
            return {
                "component": "VTabs",
                "props": tab_props,
                "content": [
                    {
                        "component": "VTab",
                        "props": {
                            "value": value,
                            "aria-label": label,
                            "min-height": 48,
                            "height": 48,
                        },
                        "text": label,
                    }
                    for value, label in tab_items
                ],
            }

        desktop_tabs = tabs(
            "course-step-tabs d-none d-md-flex",
            **{"direction": "vertical", "align-tabs": "start", "grow": False},
        )
        mobile_tabs = tabs(
            "course-step-tabs-mobile d-flex d-md-none overflow-x-auto mb-2",
            **{"show-arrows": True, "grow": False},
        )
        step_window = {
            "component": "VWindow",
            "props": {
                "model": "_active_config_tab",
                "class": "course-step-window",
                "aria-label": "课程整理步骤内容",
            },
            "content": [
                step_item(
                    "选择目录",
                    "先确认 incoming、电视剧、电影和儿童目录",
                    "directories",
                    directory_content,
                ),
                step_item(
                    "安全预览",
                    "只记录建议，不移动文件",
                    "preview",
                    preview_content,
                ),
                step_item(
                    "处理异常",
                    "低置信度和未完成下载会暂停整理",
                    "exceptions",
                    exception_content,
                ),
                step_item(
                    "开启自动整理",
                    "确认预览结果后再保存并开启",
                    "automatic",
                    automatic_content,
                ),
            ],
        }

        return [
            {
                "component": "VForm",
                "props": {
                    "label-position": "left",
                    "hide-required-asterisk": True,
                    "class": "courseorganizer-form overflow-x-hidden",
                    "aria-label": "课程整理设置",
                },
                "content": [
                    {
                        "component": "VSheet",
                        "props": {
                            "class": "d-flex flex-wrap align-center ga-2 px-3 py-2 mb-2 border",
                            "color": "transparent",
                            "rounded": "lg",
                        },
                        "content": [
                            {
                                "component": "VChip",
                                "props": {
                                    "color": "success" if config["enabled"] else "default",
                                    "size": "small",
                                    "variant": "tonal",
                                    "aria-label": "自动整理已开启" if config["enabled"] else "自动整理已关闭",
                                },
                                "text": "自动整理已开启" if config["enabled"] else "自动整理已关闭",
                            },
                            {
                                "component": "VCardText",
                                "props": {"class": "pa-0 text-caption text-medium-emphasis"},
                                "text": "先选目录，再安全预览，最后决定是否自动整理。",
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "props": {"class": "course-step-layout", "align": "start", "no-gutters": True},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3, "class": "pe-md-4"},
                                "content": [desktop_tabs],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9, "class": "min-w-0"},
                                "content": [mobile_tabs, step_window],
                            },
                        ],
                    },
                ],
            }
        ], defaults

    def get_service(self) -> List[Dict[str, Any]]:
        config = self._get_config()
        if not bool(config.get("enabled")):
            return []
        interval = int(config.get("interval", self.DEFAULT_INTERVAL))
        return [
            {
                "id": self.__class__.__name__,
                "name": "CourseOrganizer 课程整理服务",
                "trigger": "interval",
                "func": self.run,
                "kwargs": {
                    "seconds": interval,
                },
            }
        ]

    def stop_service(self) -> None:
        plugin_cls = type(self)
        with plugin_cls._run_once_lock:
            owner = plugin_cls._run_once_owner
            plugin_cls._invalidate_run_once_locked()
            reset_target = owner or self
            reset_config = reset_target._normalize_config(reset_target._get_config())
            reset_config["run_once"] = False
            if not reset_target._persist_config(reset_config):
                reset_target._logger.error(
                    "CourseOrganizer[event=run_once_persist_failed] phase=stop"
                )
            reset_target._config_snapshot = reset_config
            if reset_target is not self:
                reset_config = self._normalize_config(self._get_config())
                reset_config["run_once"] = False
                if not self._persist_config(reset_config):
                    self._logger.error(
                        "CourseOrganizer[event=run_once_persist_failed] phase=stop"
                    )
                self._config_snapshot = reset_config

    def run(self) -> None:
        with self._thread_lock:
            self._run()

    def _run(self, force: bool = False) -> None:
        config = self._get_config()
        if not force and not config.get("enabled"):
            self._logger.debug("CourseOrganizer[event=wait] plugin disabled")
            return

        incoming = config.get("incoming")
        output_roots = {
            "tv": config.get("tv_output"),
            "movie": config.get("movie_output"),
            "children": config.get("children_output"),
        }

        if not incoming or not os.path.isdir(incoming):
            self._logger.error("incoming path invalid")
            return

        missing_outputs = [name for name, path in output_roots.items() if not path]
        if missing_outputs:
            self._logger.error("output paths missing: %s", ",".join(missing_outputs))
            return

        def canonical(path: Any) -> str:
            return os.path.realpath(os.path.abspath(str(path)))

        incoming_path = canonical(incoming)
        normalized_outputs = {
            name: canonical(path) for name, path in output_roots.items()
        }

        for name, path in normalized_outputs.items():
            if not os.path.isdir(path):
                self._logger.error("output path not exist for library: %s", name)
                return

        if len(set(normalized_outputs.values())) != len(normalized_outputs):
            self._logger.error("output paths must be three distinct directories")
            return

        if any(path == os.path.realpath(os.path.abspath(os.sep)) for path in normalized_outputs.values()):
            self._logger.error("filesystem root cannot be an output path")
            return

        try:
            overlaps_source = any(
                os.path.commonpath((incoming_path, path)) in {incoming_path, path}
                for path in normalized_outputs.values()
            )
            overlaps_outputs = any(
                os.path.commonpath((first, second)) in {first, second}
                for first in normalized_outputs.values()
                for second in normalized_outputs.values()
                if first != second
            )
        except ValueError:
            overlaps_source = True
            overlaps_outputs = False

        if overlaps_source:
            self._logger.error("incoming output paths must not contain one another")
            return

        if overlaps_outputs:
            self._logger.error("output paths must not contain one another")
            return

        trigger = "manual" if force else "scheduled"
        mode = str(config.get("naming_mode") or "off")
        self._logger.info(
            "CourseOrganizer[event=scan_started] trigger=%s mode=%s",
            trigger,
            mode,
        )
        processed = 0
        moved = 0
        for entry in sorted(os.listdir(incoming), key=self._natural_key):
            course_dir = os.path.join(incoming, entry)
            if not os.path.isdir(course_dir):
                continue
            processed += 1
            try:
                if self._process_course(entry, course_dir, source_root=incoming_path):
                    moved += 1
            except Exception as exc:
                item_reason = exc.__class__.__name__
                item_error = "exception"
                self._logger.info(
                    "CourseOrganizer[event=item_error] item_course=%s item_reason=%s item_error=%s",
                    entry,
                    item_reason,
                    item_error,
                )
                self._logger.error(
                    "CourseOrganizer[event=item_error] item_course=%s item_reason=%s item_error=%s",
                    entry,
                    item_reason,
                    item_error,
                )
        self._logger.info(
            "CourseOrganizer[event=scan_completed] trigger=%s scanned=%d moved=%d",
            trigger,
            processed,
            moved,
        )

    @classmethod
    def _run_once_dispatch(cls, token: int) -> None:
        cls._run_once_and_reset(token)

    @classmethod
    def _invalidate_run_once_locked(cls) -> None:
        timer = cls._run_once_timer
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        cls._run_once_timer = None
        cls._run_once_owner = None
        cls._run_once_claimed = False
        cls._run_once_generation += 1
        cls._run_once_token = None

    @classmethod
    def _run_once_and_reset(cls, token: Optional[int] = None) -> None:
        with cls._thread_lock:
            with cls._run_once_lock:
                if token is None:
                    token = cls._run_once_token
                if token is None or cls._run_once_token != token:
                    return
                owner = cls._run_once_owner
                if owner is None or cls._run_once_claimed:
                    return
                cls._run_once_claimed = True
                latest_config = owner._normalize_config(owner._get_config())
                latest_config["run_once"] = False
                if not owner._persist_config(latest_config):
                    cls._invalidate_run_once_locked()
                    owner._logger.error(
                        "CourseOrganizer[event=run_once_persist_failed] phase=claim"
                    )
                    return
                owner._config_snapshot = latest_config
                owner._run_config_local.config = dict(latest_config)
                cls._run_once_timer = None
                cls._run_once_owner = None
                cls._run_once_claimed = False
                cls._run_once_generation += 1
                cls._run_once_token = None
            try:
                owner._run(force=True)
            finally:
                try:
                    del owner._run_config_local.config
                except AttributeError:
                    pass

    def _resolve_naming(
        self,
        course_name: str,
        directory_hints: naming.DirectoryHints,
        legacy_output_root: str,
        target_output_root: str,
    ) -> NamingDecision:
        config = NamingConfig.sanitize(self._get_config())
        if config.mode == "off":
            return self._decision_from_config_off(course_name)

        resolver = self._get_resolver()
        decision = resolver.resolve(
            course_name,
            directory_hints,
            config,
            legacy_output_root=legacy_output_root,
            target_output_root=target_output_root,
        )
        return decision

    def _decision_from_config_off(self, course_name: str) -> NamingDecision:
        return NamingDecision(
            status="local_fallback",
            raw_title=course_name,
            local_title=course_name,
            final_root=course_name,
            final_prefix=course_name,
        )

    @staticmethod
    def _decision_from_resolver(
        source: NamingDecision,
        status: str,
        blocked_reason: str,
    ) -> NamingDecision:
        return NamingDecision(
            status=status,
            raw_title=source.raw_title,
            local_title=source.local_title,
            final_root=source.final_root,
            final_prefix=source.final_prefix,
            source=source.source,
            media_id=source.media_id,
            media_type=source.media_type,
            score=source.score,
            margin=source.margin,
            candidate_key=source.candidate_key,
            reason_codes=tuple(source.reason_codes),
            source_errors=tuple(source.source_errors),
            blocked_reason=blocked_reason,
            legacy_output_root=source.legacy_output_root,
            target_output_root=source.target_output_root,
        )

    def _get_resolver(self) -> SmartNamingResolver:
        config = self._get_config()
        signature = (
            config.get("naming_mode"),
            config.get("naming_ai_review"),
            config.get("naming_sources"),
            config.get("naming_auto_threshold"),
            config.get("naming_min_margin"),
            config.get("naming_uncertain_policy"),
            config.get("naming_append_tmdb_id"),
            config.get("naming_manual_overrides"),
        )
        if self._resolver is None or self._resolver_signature != signature:
            self._resolver = self._build_resolver(config)
            self._resolver_signature = signature
        return self._resolver

    def _get_library_classifier(self) -> MoviePilotLibraryClassifier:
        if self._library_classifier_override is not None:
            return self._library_classifier_override
        if self._library_classifier is None:
            self._library_classifier = MoviePilotLibraryClassifier()
        return self._library_classifier

    def _resolve_library_route(
        self,
        course_name: str,
        decision: NamingDecision,
        directory_hints: naming.DirectoryHints,
    ) -> LibraryRouteResult:
        if self._get_config().get("naming_mode") == "off":
            return LibraryRouteResult(
                accepted=False,
                library="hold",
                confidence=0.0,
                reason_codes=("naming_off",),
                error="",
            )
        if not self._get_config().get("naming_ai_review"):
            return LibraryRouteResult(
                accepted=False,
                library="hold",
                confidence=0.0,
                reason_codes=("ai_routing_disabled",),
                error="",
            )

        cache_key = "\x1f".join(
            (
                "library-routing-v1",
                course_name,
                decision.final_root,
                decision.media_type,
                "1" if directory_hints.episodic else "0",
            )
        )
        cache = self._load_plugin_data("library_routing_cache_v1", {})
        if not isinstance(cache, dict):
            cache = {}
        now = int(self._clock())
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            ttl = 30 * 24 * 60 * 60 if cached.get("accepted") else 60 * 60
            try:
                fresh = now - int(cached.get("updated", 0)) < ttl
            except (TypeError, ValueError):
                fresh = False
            if fresh:
                return LibraryRouteResult(
                    accepted=bool(cached.get("accepted")),
                    library=str(cached.get("library", "hold")),
                    confidence=float(cached.get("confidence", 0.0)),
                    reason_codes=tuple(cached.get("reason_codes", ())),
                    error=str(cached.get("error", "")),
                )

        result = self._get_library_classifier().classify(
            raw_title=course_name,
            final_title=decision.final_root,
            media_type=decision.media_type,
            episodic=directory_hints.episodic,
        )
        cache[cache_key] = {
            "updated": now,
            "accepted": result.accepted,
            "library": result.library,
            "confidence": result.confidence,
            "reason_codes": list(result.reason_codes),
            "error": result.error,
        }
        if len(cache) > 500:
            oldest = sorted(
                cache,
                key=lambda key: int(cache[key].get("updated", 0))
                if isinstance(cache.get(key), dict)
                else 0,
            )
            for key in oldest[: len(cache) - 500]:
                cache.pop(key, None)
        self.save_data("library_routing_cache_v1", cache)
        return result

    def _process_course(
        self,
        course_name: str,
        course_path: str,
        output_root: Optional[str] = None,
        source_root: Optional[str] = None,
    ) -> bool:
        state_key = self._state_key(course_name)
        if source_root is not None and not self._is_within_realpath(source_root, course_path):
            self.save_data(state_key, None)
            self._logger.error(
                "CourseOrganizer[event=source_path_escape] item_course=%s",
                course_name,
            )
            return False
        signature = self._coerce_signature(self._snapshot_signature(course_path))
        state = self._load_plugin_data(state_key, {})
        persisted_signature = self._coerce_signature(state.get("signature")) if isinstance(state, dict) else ()

        if self._has_incomplete_file(course_path):
            self.save_data(
                state_key,
                {
                    "signature": signature,
                    "stable_count": 0,
                    "blocked": True,
                },
            )
            self._logger.info(
                "CourseOrganizer[event=item_deferred] item_course=%s item_reason=initial_incomplete",
                course_name,
            )
            self._logger.debug(
                "CourseOrganizer[event=incomplete_blocked] item_phase=initial item_course=%s",
                course_name,
            )
            return False

        if not state:
            self.save_data(state_key, {"signature": signature, "stable_count": 1})
            self._logger.debug(
                "CourseOrganizer[event=item_deferred] item_course=%s "
                "item_reason=first_snapshot item_stable_count=1",
                course_name,
            )
            return False

        if signature != persisted_signature:
            self.save_data(state_key, {"signature": signature, "stable_count": 1})
            self._logger.debug(
                "CourseOrganizer[event=item_deferred] item_course=%s "
                "item_reason=changed_before_stabilization item_stable_count=1",
                course_name,
            )
            return False

        stable_count = int(state.get("stable_count", 0)) if isinstance(state, dict) else 0
        if stable_count < 1:
            next_stable_count = stable_count + 1
            self.save_data(state_key, {"signature": signature, "stable_count": next_stable_count})
            self._logger.debug(
                "CourseOrganizer[event=item_deferred] item_course=%s "
                "item_reason=stable_count_pending item_stable_count=%d",
                course_name,
                next_stable_count,
            )
            return False

        latest_signature = self._coerce_signature(self._snapshot_signature(course_path))
        if latest_signature != signature:
            self.save_data(state_key, {"signature": latest_signature, "stable_count": 1})
            self._logger.debug(
                "CourseOrganizer[event=item_deferred] item_course=%s "
                "item_reason=changed_before_final_confirmation item_stable_count=1",
                course_name,
            )
            return False

        if self._has_incomplete_file(course_path):
            self._logger.debug(
                "CourseOrganizer[event=incomplete_blocked] item_phase=final item_course=%s",
                course_name,
            )
            self.save_data(state_key, {"signature": latest_signature, "stable_count": 0, "blocked": True})
            return False

        media_by_season, subtitle_by_season, has_explicit_season = self._collect_course_files(course_path)
        if not media_by_season:
            self._logger.debug("CourseOrganizer[event=no_media] item_course=%s", course_name)
            self.save_data(state_key, None)
            return False

        parse_hints = naming.parse_title(course_name)
        media_count = sum(len(items) for items in media_by_season.values())
        directory_hints = naming.DirectoryHints(
            media_count=media_count,
            seasons=tuple(sorted(media_by_season)) if has_explicit_season else (),
            episodic=(media_count > 1 or len(media_by_season) > 1 or bool(parse_hints.season_hints)),
        )

        mode = self._get_config().get("naming_mode")
        if mode == "off":
            decision = self._decision_from_config_off(course_name)
        else:
            decision = self._resolve_naming(
                course_name,
                directory_hints,
                "",
                "",
            )

        route_result: Optional[LibraryRouteResult] = None
        target_library = "legacy"
        if output_root is None:
            if not decision.allowed_to_move:
                if mode != "off":
                    decision = self._get_resolver().record_decision(
                        decision,
                        target_library="hold",
                        library_confidence=0.0,
                        library_reason_codes=("naming_blocked",),
                    )
                if mode == "preview":
                    self._logger.info(
                        "CourseOrganizer[event=preview] item_course=%s item_final=%s item_library=%s",
                        course_name,
                        decision.final_root,
                        "hold",
                    )
                    return False
                self._logger.debug(
                    "CourseOrganizer[event=naming_blocked] item_course=%s item_reason=%s",
                    course_name,
                    decision.status,
                )
                return False

            route_result = self._resolve_library_route(course_name, decision, directory_hints)
            target_library = route_result.library
            if not route_result.accepted:
                self._logger.debug(
                    "CourseOrganizer[event=library_hold] item_course=%s item_confidence=%.3f item_reasons=%s",
                    course_name,
                    route_result.confidence,
                    ",".join(route_result.reason_codes),
                )
                held = self._decision_from_resolver(
                    source=decision,
                    status="library_hold",
                    blocked_reason="library_hold",
                )
                if mode != "off":
                    self._get_resolver().record_decision(
                        held,
                        target_library="hold",
                        library_confidence=route_result.confidence,
                        library_reason_codes=route_result.reason_codes,
                    )
                self._logger.debug(
                    "CourseOrganizer: %s held by library classification %s",
                    course_name,
                    ",".join(route_result.reason_codes),
                )
                return False
            output_key = {
                "tv": "tv_output",
                "movie": "movie_output",
                "children": "children_output",
            }.get(target_library)
            output_root = self._get_config().get(output_key) if output_key else None
        if not output_root:
            self._logger.error("output path missing for library: %s", target_library)
            return False

        output_root = os.path.realpath(os.path.abspath(str(output_root)))
        if not os.path.isdir(output_root):
            if mode != "preview":
                if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
                    self._logger.error(
                        "CourseOrganizer[event=secure_move_unsupported] item_course=%s",
                        course_name,
                    )
                    return False
                try:
                    os.makedirs(output_root, exist_ok=True)
                except OSError:
                    self._logger.error(
                        "CourseOrganizer[event=destination_root_create_failed] item_course=%s",
                        course_name,
                    )
                    return False

        legacy_output_root = os.path.join(output_root, self._safe_name(course_name))
        target_output_root = os.path.join(output_root, self._safe_name(decision.final_root))
        if mode != "off":
            decision = self._get_resolver().record_decision(
                decision,
                legacy_output_root=legacy_output_root,
                target_output_root=target_output_root,
                target_library=target_library,
                library_confidence=route_result.confidence if route_result else 1.0,
                library_reason_codes=route_result.reason_codes if route_result else ("legacy_override",),
            )

        if self._safe_name(decision.final_root) != self._safe_name(course_name) and os.path.isdir(legacy_output_root):
            self._logger.debug(
                "CourseOrganizer[event=legacy_conflict] item_course=%s item_final=%s item_library=%s",
                course_name,
                decision.final_root,
                target_library,
            )
            decision = self._decision_from_resolver(
                source=decision,
                status="legacy_output_conflict",
                blocked_reason="legacy_output_conflict",
            )
            if mode != "off":
                decision = self._get_resolver().record_output_conflict(
                    decision,
                    legacy_output_root=legacy_output_root,
                    target_output_root=os.path.join(output_root, self._safe_name(decision.final_root)),
                    target_library=target_library,
                    library_confidence=route_result.confidence if route_result else 1.0,
                    library_reason_codes=route_result.reason_codes if route_result else ("legacy_override",),
                )
            return False

        if mode == "preview":
            self._logger.info(
                "CourseOrganizer[event=preview] item_course=%s item_final=%s item_library=%s",
                course_name,
                decision.final_root,
                target_library,
            )
            return False

        if not decision.allowed_to_move:
            self._logger.debug(
                "CourseOrganizer[event=naming_blocked] item_course=%s item_reason=%s",
                course_name,
                decision.status,
            )
            return False

        output_course_root = os.path.join(output_root, self._safe_name(decision.final_root))
        if not self._is_within_realpath(output_root, output_course_root):
            self._logger.error(
                "CourseOrganizer[event=destination_path_escape] item_course=%s",
                course_name,
            )
            return False

        self._logger.info(
            "CourseOrganizer[event=move_started] item_course=%s item_final=%s item_library=%s item_media_count=%d",
            course_name,
            decision.final_root,
            target_library,
            media_count,
        )
        if not self._is_within_realpath(output_root, output_course_root):
            self._logger.error(
                "CourseOrganizer[event=destination_path_escape] item_course=%s",
                course_name,
            )
            return False

        move_context = self._create_move_context(course_path, output_root)
        if move_context is None:
            self.save_data(state_key, None)
            return False

        move_plan: List[Tuple[str, str, List[Tuple[str, str]]]] = []
        planned_targets: set[str] = set()
        season_roots: List[str] = []
        safe_course_name = self._safe_name(decision.final_prefix)

        def _reserve_plan_path(path: str) -> str:
            base, ext = os.path.splitext(path)
            candidate = path
            index = 1
            while os.path.exists(candidate) or candidate in planned_targets:
                candidate = f"{base}_{index}{ext}"
                index += 1
            return candidate

        for season in sorted(media_by_season.keys()):
            season_files = sorted(media_by_season[season], key=lambda file_path: self._course_sort_key(file_path, course_path))
            season_root = os.path.join(output_course_root, f"Season {season}")
            season_roots.append(season_root)
            leading_spans = [self._extract_leading_episode_span(media_file) for media_file in season_files]

            used_episodes = set(self._collect_existing_episodes(season_root, safe_course_name, season))
            next_episode = max(
                self._next_episode_number(season_root, safe_course_name, season) - 1,
                max((span[1] for span in leading_spans if span is not None), default=0),
            )
            next_episode += 1

            for media_file, leading_span in zip(season_files, leading_spans):
                if leading_span is not None:
                    requested_start, requested_end = leading_span
                    requested_width = requested_end - requested_start + 1
                    if all(
                        requested_index not in used_episodes
                        for requested_index in range(requested_start, requested_end + 1)
                    ):
                        assigned_start = requested_start
                        assigned_end = requested_end
                    else:
                        assigned_start = next_episode
                        while True:
                            requested_window = range(
                                assigned_start,
                                assigned_start + requested_width,
                            )
                            if all(index not in used_episodes for index in requested_window):
                                assigned_end = assigned_start + requested_width - 1
                                break
                            assigned_start += 1
                else:
                    assigned_start = next_episode
                    assigned_end = next_episode

                next_episode = max(next_episode, assigned_end + 1)
                used_episodes.update(range(assigned_start, assigned_end + 1))

                ext = self._lower_extension(media_file)
                episode_name = (
                    f"{safe_course_name} - "
                    f"{self._format_episode_token(season, assigned_start, assigned_end)}{ext}"
                )
                target_media = _reserve_plan_path(os.path.join(season_root, episode_name))
                planned_targets.add(target_media)

                media_key = os.path.splitext(os.path.basename(media_file))[0].lower()
                target_subtitles: List[Tuple[str, str]] = []
                for subtitle_file in subtitle_by_season.get(season, {}).get(media_key, []):
                    subtitle_ext = self._lower_extension(subtitle_file)
                    subtitle_name = (
                        f"{safe_course_name} - "
                        f"{self._format_episode_token(season, assigned_start, assigned_end)}{subtitle_ext}"
                    )
                    target_subtitle = _reserve_plan_path(os.path.join(season_root, subtitle_name))
                    planned_targets.add(target_subtitle)
                    target_subtitles.append((subtitle_file, target_subtitle))

                move_plan.append((media_file, target_media, target_subtitles))

        for season_root in season_roots:
            if not self._is_within_realpath(output_root, season_root):
                self._logger.error(
                    "CourseOrganizer[event=destination_path_escape] item_course=%s",
                    course_name,
                )
                self._rollback_move_context(move_context)
                self._close_move_context(move_context)
                return False

        for media_file, target_media, subtitle_pairs in move_plan:
            if not self._is_within_realpath(course_path, media_file):
                self._logger.error(
                    "CourseOrganizer[event=source_path_escape] item_course=%s",
                    course_name,
                )
                self._rollback_move_context(move_context)
                self._close_move_context(move_context)
                return False
            if not self._is_within_realpath(output_root, target_media):
                self._logger.error(
                    "CourseOrganizer[event=destination_path_escape] item_course=%s",
                    course_name,
                )
                self._rollback_move_context(move_context)
                self._close_move_context(move_context)
                return False

            for subtitle_file, target_subtitle in subtitle_pairs:
                if not self._is_within_realpath(course_path, subtitle_file):
                    self._logger.error(
                        "CourseOrganizer[event=source_path_escape] item_course=%s",
                        course_name,
                    )
                    self._rollback_move_context(move_context)
                    self._close_move_context(move_context)
                    return False
                if not self._is_within_realpath(output_root, target_subtitle):
                    self._logger.error(
                        "CourseOrganizer[event=destination_path_escape] item_course=%s",
                        course_name,
                    )
                    self._rollback_move_context(move_context)
                    self._close_move_context(move_context)
                    return False

        try:
            move_counts = self._execute_move_plan(
                move_plan,
                course_path,
                output_root,
                move_context=move_context,
            )
        finally:
            self._close_move_context(move_context)
        if move_counts is None:
            self.save_data(state_key, None)
            return False
        moved_files, moved_subtitles = move_counts

        if moved_files:
            self._delete_if_empty_recursive(course_path)
            self._logger.info(
                "CourseOrganizer[event=move_completed] item_course=%s item_final=%s item_library=%s item_moved=%d item_subtitles=%d",
                course_name,
                decision.final_root,
                target_library,
                moved_files,
                moved_subtitles,
            )
            self.save_data(state_key, None)
            return True

        self.save_data(state_key, None)
        return False

    def _collect_course_files(self, course_path: str) -> Tuple[Dict[int, List[str]], Dict[int, Dict[str, List[str]]], bool]:
        media_files: Dict[int, List[str]] = {}
        subtitle_map: Dict[int, Dict[str, List[str]]] = {}
        has_explicit_season = False

        for root, _, filenames in os.walk(course_path):
            for filename in filenames:
                if self._is_incomplete(filename):
                    continue

                extension = self._lower_extension(filename)
                season, explicit = self._detect_season_from_path(os.path.join(root, filename), course_path)
                has_explicit_season = has_explicit_season or explicit
                if extension in self.MEDIA_EXTENSIONS:
                    media_files.setdefault(season, []).append(os.path.join(root, filename))
                    continue
                if extension in self.SUBTITLE_EXTENSIONS:
                    base = os.path.splitext(filename)[0].lower()
                    subtitle_map.setdefault(season, {}).setdefault(base, []).append(os.path.join(root, filename))

        for season, subtitles in subtitle_map.items():
            for key, subtitle_files in subtitles.items():
                subtitle_map[season][key] = sorted(subtitle_files, key=self._natural_path_key)

        return media_files, subtitle_map, has_explicit_season

    @classmethod
    def _detect_season_from_path(cls, file_path: str, course_path: str) -> Tuple[int, bool]:
        relative = os.path.relpath(file_path, course_path)
        directories = os.path.dirname(relative).split(os.sep)
        season = 1
        found = False
        for directory in directories:
            if not directory:
                continue
            parsed = cls._parse_season_from_component(directory)
            if parsed is not None:
                season = parsed
                found = True
        return season, found

    @staticmethod
    def _is_within_realpath(root: str, path: str) -> bool:
        root_real = os.path.realpath(os.path.abspath(root))
        path_real = os.path.realpath(os.path.abspath(path))
        try:
            return os.path.commonpath([path_real, root_real]) == root_real
        except ValueError:
            return False

    @classmethod
    def _parse_season_from_component(cls, component: str) -> Optional[int]:
        match = _EN_SEASON_RE.search(component)
        if match:
            raw = match.group(1) or match.group(2)
            try:
                season = int(raw)
            except (TypeError, ValueError):
                season = None
            else:
                if season > 0:
                    return season

        match = _CN_SEASON_RE.search(component)
        if match:
            season = cls._chinese_numeral_to_int(match.group(1))
            if season and season > 0:
                return season

        return None

    @staticmethod
    def _chinese_numeral_to_int(value: str) -> Optional[int]:
        if not value:
            return None
        if value.isdigit():
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        chars = list(value)
        if not chars:
            return None
        if "十" in value:
            if value == "十":
                return 10
            before, after = value.split("十", 1)
            tens = 1 if not before else _CHINESE_NUMERAL_MAP.get(before, 0)
            ones = _CHINESE_NUMERAL_MAP.get(after, 0) if after else 0
            return tens * 10 + ones

        total = 0
        for char in chars:
            if char not in _CHINESE_NUMERAL_MAP:
                return None
            total += _CHINESE_NUMERAL_MAP[char]

        return total if total > 0 else None

    def _snapshot_signature(self, course_path: str) -> List[Tuple[str, int, int]]:
        snapshot: List[Tuple[str, int, int]] = []
        for root, _, filenames in os.walk(course_path):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                try:
                    stats = os.stat(file_path)
                except FileNotFoundError:
                    continue
                relative_path = os.path.relpath(file_path, course_path).replace(os.sep, "/")
                snapshot.append((relative_path, int(stats.st_size), int(stats.st_mtime_ns)))
        snapshot.sort(key=lambda item: self._natural_key(item[0]))
        return snapshot

    @staticmethod
    def _coerce_signature(signature: Any) -> Tuple[Tuple[str, int, int], ...]:
        if not isinstance(signature, (list, tuple)):
            return ()

        normalized = []
        for item in signature:
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                return ()
            path, size, mtime = item
            try:
                normalized.append((str(path), int(size), int(mtime)))
            except (TypeError, ValueError):
                return ()
        return tuple(normalized)

    def _delete_if_empty_recursive(self, course_path: str) -> None:
        for root, _, files in os.walk(course_path, topdown=False):
            if files:
                return
            if os.path.isdir(root):
                try:
                    os.rmdir(root)
                except OSError:
                    return

    @staticmethod
    def _is_incomplete(filename: str) -> bool:
        lower = filename.lower()
        return any(lower.endswith(suffix) for suffix in CourseOrganizer.INCOMPLETE_SUFFIXES)

    @classmethod
    def _has_incomplete_file(cls, course_path: str) -> bool:
        for root, _, filenames in os.walk(course_path):
            for filename in filenames:
                if cls._is_incomplete(filename):
                    return True
        return False

    @classmethod
    def _safe_name(cls, value: str) -> str:
        normalized = _INVALID_NAME_RE.sub("_", value).strip()
        if normalized in {"", ".", ".."}:
            normalized = "Course"
        return normalized[:160]

    @staticmethod
    def _lower_extension(filename: str) -> str:
        return os.path.splitext(filename)[1].lower()

    @classmethod
    def _extract_leading_episode(cls, file_path: str) -> Optional[int]:
        span = cls._extract_leading_episode_span(file_path)
        if span is None:
            return None
        return span[0]

    @classmethod
    def _extract_leading_episode_span(cls, file_path: str) -> Optional[Tuple[int, int]]:
        stem = os.path.splitext(os.path.basename(file_path))[0].strip()

        def _parse_range(start_text: str, end_text: str) -> Optional[Tuple[int, int]]:
            try:
                start = int(start_text)
                end = int(end_text)
            except (TypeError, ValueError):
                return None
            if start <= 0:
                return None
            if end <= start:
                return None
            if end - start + 1 > 1000:
                return None
            return start, end

        for pattern in (_LEADING_CN_RANGE_RE, _LEADING_EN_RANGE_RE, _LEADING_BARE_RANGE_RE):
            match = pattern.match(stem)
            if match:
                start_text = match.group("start")
                end_text = match.group("end")
                if (
                    pattern is _LEADING_BARE_RANGE_RE
                    and len(start_text) == 4
                    and len(end_text) == 4
                ):
                    return None
                parsed = _parse_range(start_text, end_text)
                if parsed is not None:
                    return parsed
                return None

        if _LEADING_RANGE_CANDIDATE_RE.match(stem):
            return None

        match = _LEADING_EPISODE_RE.match(stem)
        if not match:
            return None

        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None

        return value, value

    @staticmethod
    def _format_episode_token(season: int, start: int, end: int) -> str:
        token = f"S{season:02d}E{start:03d}"
        if end > start:
            token += f"-E{end:03d}"
        return token

    @classmethod
    def _collect_existing_episodes(
        cls, season_root: str, safe_course_name: str, season: int
    ) -> List[int]:
        if not os.path.isdir(season_root):
            return []

        episode_pattern = re.compile(
            rf"^{re.escape(safe_course_name)} - S{season:02d}E([0-9]+)(?:-E([0-9]+))?(?:_[0-9]+)?$"
        )
        episodes: List[int] = []
        for filename in os.listdir(season_root):
            episode_path = os.path.join(season_root, filename)
            if not os.path.isfile(episode_path):
                continue
            stem, extension = os.path.splitext(filename)
            if extension.lower() not in cls.MEDIA_EXTENSIONS:
                continue
            match = episode_pattern.fullmatch(stem)
            if match:
                try:
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) is not None else start
                    if start <= 0 or end < start or (end - start + 1) > 1000:
                        continue
                    episodes.extend(range(start, end + 1))
                except (TypeError, ValueError):
                    continue

        return episodes

    @classmethod
    def _next_episode_number(cls, season_root: str, safe_course_name: str, season: int) -> int:
        if not os.path.isdir(season_root):
            return 1

        return max(cls._collect_existing_episodes(season_root, safe_course_name, season), default=0) + 1

    @classmethod
    def _state_key(cls, course_name: str) -> str:
        return f"courseorganizer_state_{course_name}"

    @staticmethod
    def _reserve_path(path: str) -> str:
        if not os.path.exists(path):
            return path

        base, ext = os.path.splitext(path)
        index = 1
        while True:
            candidate = f"{base}_{index}{ext}"
            if not os.path.exists(candidate):
                return candidate
            index += 1

    @staticmethod
    def _safe_relative_path(root: str, path: str) -> Optional[str]:
        root_abs = os.path.abspath(root)
        candidate_abs = os.path.abspath(path)
        relative = os.path.relpath(candidate_abs, root_abs)
        normalized = os.path.normpath(relative)
        if normalized in {"", "."}:
            return None
        if normalized == ".." or normalized.startswith(f"..{os.sep}"):
            return None
        return normalized

    @classmethod
    def _open_nofollow_dir_chain(
        cls, base_dir_fd: int, relative_path: str, create: bool = False
    ) -> int:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        dir_chain_flags = no_follow | os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_chain_flags |= os.O_DIRECTORY
        flags = dir_chain_flags
        current_fd = os.dup(base_dir_fd)
        try:
            for component in (part for part in relative_path.split(os.sep) if part and part != "."):
                if component == "..":
                    raise ValueError("directory path escapes bound root")
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, 0o755, dir_fd=current_fd)
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                if current_fd != base_dir_fd:
                    os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            if current_fd is not None:
                os.close(current_fd)
            raise

    @staticmethod
    def _entry_lstat(parent_fd: Optional[int], name: str) -> Optional[os.stat_result]:
        if parent_fd is None:
            return None
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except (OSError, TypeError, NotImplementedError, ValueError):
            return None

    @classmethod
    def _regular_entry_stat(cls, parent_fd: Optional[int], name: str) -> Optional[os.stat_result]:
        current = cls._entry_lstat(parent_fd, name)
        if current is None or not stat.S_ISREG(current.st_mode):
            return None
        return current

    @classmethod
    def _unlink_if_identity(
        cls,
        parent_fd: Optional[int],
        name: str,
        expected: Optional[Tuple[int, int]],
    ) -> bool:
        current = cls._entry_lstat(parent_fd, name)
        if current is None or expected is None or (current.st_dev, current.st_ino) != expected:
            return False
        try:
            os.unlink(name, dir_fd=parent_fd)
        except (OSError, TypeError, NotImplementedError, ValueError):
            return False
        return True

    @staticmethod
    def _fsync_dir(directory_fd: Optional[int]) -> None:
        if directory_fd is None:
            return
        try:
            os.fsync(directory_fd)
        except (OSError, TypeError, NotImplementedError, ValueError):
            pass

    def _open_bound_root(self, root: str) -> Optional[Dict[str, Any]]:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if not no_follow or not directory_flag or not root:
            return None
        flags = no_follow | directory_flag | os.O_RDONLY
        root_abs = os.path.abspath(str(root))
        base_fd = root_fd = parent_fd = None
        result = None
        try:
            if root_abs == os.path.abspath(os.sep):
                root_fd = os.open(os.sep, flags)
                root_name = ""
            else:
                base_fd = os.open(os.sep, flags)
                relative_root = root_abs.lstrip(os.sep)
                parent_relative = os.path.dirname(relative_root)
                root_name = os.path.basename(relative_root)
                parent_fd = self._open_nofollow_dir_chain(base_fd, parent_relative)
                root_fd = os.open(root_name, flags, dir_fd=parent_fd)
            root_stat = os.fstat(root_fd)
            if not stat.S_ISDIR(root_stat.st_mode):
                return None
            if root_name:
                parent_stat = self._entry_lstat(parent_fd, root_name)
                if (
                    parent_stat is None
                    or not stat.S_ISDIR(parent_stat.st_mode)
                    or (parent_stat.st_dev, parent_stat.st_ino)
                    != (root_stat.st_dev, root_stat.st_ino)
                ):
                    return None
            result = {
                "path": root_abs,
                "fd": root_fd,
                "identity": (root_stat.st_dev, root_stat.st_ino),
                "parent_fd": parent_fd,
                "name": root_name,
            }
            return result
        except (OSError, TypeError, NotImplementedError, ValueError):
            return None
        finally:
            if result is None:
                for fd in (parent_fd, root_fd, base_fd):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
            elif base_fd is not None:
                try:
                    os.close(base_fd)
                except OSError:
                    pass

    def _create_move_context(self, source_root: str, target_root: str) -> Optional[Dict[str, Any]]:
        source = self._open_bound_root(source_root)
        target = self._open_bound_root(target_root)
        if source is None or target is None:
            for bound in (source, target):
                if bound is not None:
                    for fd in (bound["parent_fd"], bound["fd"]):
                        if fd is not None:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
            return None
        stage_fd = stage_name = None
        context = None
        try:
            base = f".courseorganizer-stage.{os.getpid()}.{time.time_ns()}"
            for suffix in range(1000):
                candidate = base if suffix == 0 else f"{base}.{suffix}"
                try:
                    os.mkdir(candidate, 0o700, dir_fd=source["fd"])
                    stage_name = candidate
                    stage_fd = os.open(
                        candidate,
                        getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY,
                        dir_fd=source["fd"],
                    )
                    break
                except FileExistsError:
                    continue
            if stage_fd is None or stage_name is None:
                return None
            stage_stat = os.fstat(stage_fd)
            context = {
                "source_root": source,
                "target_root": target,
                "stage_fd": stage_fd,
                "stage_name": stage_name,
                "stage_identity": (stage_stat.st_dev, stage_stat.st_ino),
                "counter": 0,
                "staged": [],
                "published": [],
                "open_fds": [],
            }
            return context
        except (OSError, TypeError, NotImplementedError, ValueError):
            return None
        finally:
            if context is None:
                if stage_fd is not None:
                    try:
                        os.close(stage_fd)
                    except OSError:
                        pass
                if stage_name is not None:
                    self._unlink_if_identity(source["fd"], stage_name, None)
                    try:
                        os.rmdir(stage_name, dir_fd=source["fd"])
                    except OSError:
                        pass
                for bound in (source, target):
                    for fd in (bound["parent_fd"], bound["fd"]):
                        if fd is not None:
                            try:
                                os.close(fd)
                            except OSError:
                                pass

    @classmethod
    def _bound_root_is_current(cls, bound: Dict[str, Any]) -> bool:
        try:
            current = os.fstat(bound["fd"])
        except OSError:
            return False
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != bound["identity"]
        ):
            return False
        if bound["parent_fd"] is None:
            return True
        entry = cls._entry_lstat(bound["parent_fd"], bound["name"])
        return bool(
            entry is not None
            and stat.S_ISDIR(entry.st_mode)
            and (entry.st_dev, entry.st_ino) == bound["identity"]
        )

    @classmethod
    def _move_context_is_current(cls, context: Dict[str, Any]) -> bool:
        return cls._bound_root_is_current(context["source_root"]) and cls._bound_root_is_current(
            context["target_root"]
        )

    def _capture_move_identity(
        self,
        context: Dict[str, Any],
        source: str,
    ) -> Optional[Dict[str, Any]]:
        if not self._move_context_is_current(context):
            return None
        root = context["source_root"]
        relative = self._safe_relative_path(root["path"], source)
        if relative is None:
            return None
        parent_name, source_name = os.path.split(relative)
        parent_fd = None
        source_fd = None
        try:
            parent_fd = self._open_nofollow_dir_chain(root["fd"], parent_name)
            source_fd = os.open(
                source_name,
                getattr(os, "O_NOFOLLOW", 0)
                | os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
            source_stat = os.fstat(source_fd)
            entry = self._regular_entry_stat(parent_fd, source_name)
            identity = (source_stat.st_dev, source_stat.st_ino)
            if entry is None or (entry.st_dev, entry.st_ino) != identity:
                return None
            return {
                "relative": relative,
                "parent": parent_name,
                "name": source_name,
                "identity": identity,
                "stat": source_stat,
            }
        except (OSError, TypeError, NotImplementedError, ValueError):
            return None
        finally:
            for fd in (source_fd, parent_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _restore_published_item(self, item: Dict[str, Any]) -> bool:
        publication = item.get("published")
        source_parent_fd = item.get("source_parent_fd")
        published_fd = publication.get("fd") if isinstance(publication, dict) else None
        if source_parent_fd is None or not isinstance(published_fd, int):
            return False

        current_source = self._entry_lstat(source_parent_fd, item["source_name"])
        if current_source is not None:
            return (current_source.st_dev, current_source.st_ino) == item["identity"]

        try:
            published_stat = os.fstat(published_fd)
            if (
                not stat.S_ISREG(published_stat.st_mode)
                or (published_stat.st_dev, published_stat.st_ino) != publication["identity"]
            ):
                return False
            source_stat = item.get("stat")
            mode = stat.S_IMODE(getattr(source_stat, "st_mode", 0o600))
            source_fd = os.open(
                item["source_name"],
                getattr(os, "O_NOFOLLOW", 0)
                | os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_BINARY", 0),
                mode,
                dir_fd=source_parent_fd,
            )
        except (OSError, TypeError, NotImplementedError, ValueError):
            return False

        source_identity = None
        restored = False
        try:
            source_stat = os.fstat(source_fd)
            source_identity = (source_stat.st_dev, source_stat.st_ino)
            if not stat.S_ISREG(source_stat.st_mode):
                return False
            os.lseek(published_fd, 0, os.SEEK_SET)
            remaining = published_stat.st_size
            while remaining:
                chunk = os.read(published_fd, min(1024 * 1024, remaining))
                if not chunk:
                    return False
                remaining -= len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(source_fd, view)
                    if written <= 0:
                        return False
                    view = view[written:]
            if os.read(published_fd, 1):
                return False
            os.fchmod(source_fd, stat.S_IMODE(getattr(item.get("stat"), "st_mode", 0o600)))
            source_times = item.get("stat")
            if source_times is not None:
                os.utime(
                    source_fd,
                    ns=(source_times.st_atime_ns, source_times.st_mtime_ns),
                )
            os.fsync(source_fd)
            current_source = self._entry_lstat(source_parent_fd, item["source_name"])
            if (
                current_source is None
                or (current_source.st_dev, current_source.st_ino) != source_identity
            ):
                return False
            self._fsync_dir(source_parent_fd)
            restored = True
            return True
        except (OSError, TypeError, NotImplementedError, ValueError):
            return False
        finally:
            try:
                os.close(source_fd)
            except OSError:
                pass
            if not restored:
                self._unlink_if_identity(source_parent_fd, item["source_name"], source_identity)

    def _restore_staged_item(self, context: Dict[str, Any], item: Dict[str, Any]) -> bool:
        source_parent_fd = item.get("source_parent_fd")
        stage_fd = context["stage_fd"]
        stage_entry = self._entry_lstat(stage_fd, item["stage_name"])
        if stage_entry is None:
            current_source = self._entry_lstat(source_parent_fd, item["source_name"])
            if current_source is not None:
                return (current_source.st_dev, current_source.st_ino) == item["identity"]
            return self._restore_published_item(item)
        if (stage_entry.st_dev, stage_entry.st_ino) != item.get("stage_identity"):
            return False
        current_source = self._entry_lstat(source_parent_fd, item["source_name"])
        if current_source is not None:
            if (current_source.st_dev, current_source.st_ino) == item["identity"]:
                restored = self._unlink_if_identity(
                    stage_fd, item["stage_name"], item["stage_identity"]
                )
                if restored:
                    self._fsync_dir(source_parent_fd)
                    self._fsync_dir(stage_fd)
                return restored
            return False
        try:
            _ORIGINAL_LINK(
                item["stage_name"],
                item["source_name"],
                src_dir_fd=stage_fd,
                dst_dir_fd=source_parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        except (OSError, TypeError, NotImplementedError, ValueError):
            return False
        restored = self._unlink_if_identity(stage_fd, item["stage_name"], item["stage_identity"])
        if restored:
            self._fsync_dir(source_parent_fd)
            self._fsync_dir(stage_fd)
        return restored

    def _stage_move_source(
        self,
        context: Dict[str, Any],
        source: str,
        captured: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not self._move_context_is_current(context):
            return None
        root = context["source_root"]
        parent_fd = None
        try:
            parent_fd = self._open_nofollow_dir_chain(root["fd"], captured["parent"])
            context["open_fds"].append(parent_fd)
            current = self._regular_entry_stat(parent_fd, captured["name"])
            if current is None or (current.st_dev, current.st_ino) != captured["identity"]:
                return None
            context["counter"] += 1
            stage_name = f"{context['counter']:08x}-{captured['name']}"
            os.rename(
                captured["name"],
                stage_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=context["stage_fd"],
            )
            self._fsync_dir(parent_fd)
            self._fsync_dir(context["stage_fd"])
            staged_entry = self._entry_lstat(context["stage_fd"], stage_name)
            staged = (
                staged_entry
                if staged_entry is not None
                and stat.S_ISREG(staged_entry.st_mode)
                and (staged_entry.st_dev, staged_entry.st_ino) == captured["identity"]
                else None
            )
            if staged is None:
                item = {
                    "source_parent_fd": parent_fd,
                    "source_name": captured["name"],
                    "stage_name": stage_name,
                    "identity": captured["identity"],
                    "stage_identity": (
                        (staged_entry.st_dev, staged_entry.st_ino)
                        if staged_entry is not None
                        else None
                    ),
                }
                self._restore_staged_item(context, item)
                return None
            item = {
                "source_parent_fd": parent_fd,
                "source_name": captured["name"],
                "stage_name": stage_name,
                "identity": captured["identity"],
                "stage_identity": (staged.st_dev, staged.st_ino),
                "stat": captured["stat"],
                "source": source,
            }
            context["staged"].append(item)
            return item
        except (OSError, TypeError, NotImplementedError, ValueError):
            if parent_fd is not None:
                current = self._entry_lstat(context["stage_fd"], locals().get("stage_name", ""))
                if current is not None:
                    self._restore_staged_item(
                        context,
                        {
                            "source_parent_fd": parent_fd,
                            "source_name": captured["name"],
                            "stage_name": locals().get("stage_name", ""),
                            "identity": captured["identity"],
                            "stage_identity": (current.st_dev, current.st_ino),
                        },
                    )
            return None

    def _publish_staged_move(
        self,
        context: Dict[str, Any],
        item: Dict[str, Any],
        target: str,
    ) -> bool:
        if not self._move_context_is_current(context):
            return False
        root = context["target_root"]
        relative = self._safe_relative_path(root["path"], target)
        if relative is None:
            return False
        target_parent, target_name = os.path.split(relative)
        target_parent_fd = temp_fd = None
        temp_name = None
        published_identity = None
        published_fd = None
        source_fd = None
        try:
            target_parent_fd = self._open_nofollow_dir_chain(root["fd"], target_parent, create=True)
            context["open_fds"].append(target_parent_fd)
            source_stat = self._regular_entry_stat(context["stage_fd"], item["stage_name"])
            if source_stat is None or (source_stat.st_dev, source_stat.st_ino) != item["identity"]:
                return False
            source_fd = os.open(
                item["stage_name"],
                getattr(os, "O_NOFOLLOW", 0)
                | os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=context["stage_fd"],
            )
            source_stat = os.fstat(source_fd)
            if (source_stat.st_dev, source_stat.st_ino) != item["identity"]:
                return False

            same_filesystem = os.fstat(target_parent_fd).st_dev == source_stat.st_dev
            if same_filesystem:
                try:
                    os.link(
                        item["stage_name"],
                        target_name,
                        src_dir_fd=context["stage_fd"],
                        dst_dir_fd=target_parent_fd,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        return False
                else:
                    published_identity = item["identity"]
                    target_stat = self._regular_entry_stat(target_parent_fd, target_name)
                    if target_stat is None or (target_stat.st_dev, target_stat.st_ino) != published_identity:
                        self._unlink_if_identity(target_parent_fd, target_name, published_identity)
                        return False
                    published_fd = os.open(
                        target_name,
                        getattr(os, "O_NOFOLLOW", 0)
                        | os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NONBLOCK", 0),
                        dir_fd=target_parent_fd,
                    )
                    published_stat = os.fstat(published_fd)
                    if (
                        not stat.S_ISREG(published_stat.st_mode)
                        or (published_stat.st_dev, published_stat.st_ino) != published_identity
                    ):
                        self._unlink_if_identity(target_parent_fd, target_name, published_identity)
                        return False
                    self._fsync_dir(target_parent_fd)
                    publication = {
                        "parent_fd": target_parent_fd,
                        "name": target_name,
                        "identity": published_identity,
                        "fd": published_fd,
                    }
                    context["published"].append(publication)
                    item["published"] = publication
                    published_fd = None
                    return True

            mode = stat.S_IMODE(source_stat.st_mode) & ~(stat.S_ISUID | stat.S_ISGID)
            for suffix in range(1000):
                candidate = f".{target_name}.tmp.{context['counter']}.{suffix}"
                try:
                    temp_fd = os.open(
                        candidate,
                        getattr(os, "O_NOFOLLOW", 0)
                        | os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_BINARY", 0),
                        mode,
                        dir_fd=target_parent_fd,
                    )
                    temp_name = candidate
                    break
                except FileExistsError:
                    continue
            if temp_fd is None or temp_name is None:
                return False
            temp_stat = os.fstat(temp_fd)
            temp_identity = (temp_stat.st_dev, temp_stat.st_ino)
            remaining = source_stat.st_size
            while remaining:
                chunk = os.read(source_fd, min(1024 * 1024, remaining))
                if not chunk:
                    return False
                remaining -= len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(temp_fd, view)
                    if written <= 0:
                        return False
                    view = view[written:]
            if os.read(source_fd, 1):
                return False
            os.fchmod(temp_fd, mode)
            os.utime(
                temp_name,
                ns=(item["stat"].st_atime_ns, item["stat"].st_mtime_ns),
                dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
            os.fsync(temp_fd)
            if (
                (current := self._regular_entry_stat(target_parent_fd, temp_name)) is None
                or (current.st_dev, current.st_ino) != temp_identity
            ):
                return False
            os.link(
                temp_name,
                target_name,
                src_dir_fd=target_parent_fd,
                dst_dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
            published_identity = temp_identity
            target_stat = self._regular_entry_stat(target_parent_fd, target_name)
            if target_stat is None or (target_stat.st_dev, target_stat.st_ino) != published_identity:
                self._unlink_if_identity(target_parent_fd, target_name, published_identity)
                return False
            if not self._unlink_if_identity(target_parent_fd, temp_name, temp_identity):
                self._unlink_if_identity(target_parent_fd, target_name, published_identity)
                return False
            temp_name = None
            published_fd = os.open(
                target_name,
                getattr(os, "O_NOFOLLOW", 0)
                | os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=target_parent_fd,
            )
            published_stat = os.fstat(published_fd)
            if (
                not stat.S_ISREG(published_stat.st_mode)
                or (published_stat.st_dev, published_stat.st_ino) != published_identity
            ):
                self._unlink_if_identity(target_parent_fd, target_name, published_identity)
                return False
            self._fsync_dir(target_parent_fd)
            publication = {
                "parent_fd": target_parent_fd,
                "name": target_name,
                "identity": published_identity,
                "fd": published_fd,
            }
            context["published"].append(publication)
            item["published"] = publication
            published_fd = None
            return True
        except (OSError, TypeError, NotImplementedError, ValueError):
            if published_identity is not None:
                self._unlink_if_identity(target_parent_fd, target_name, published_identity)
            return False
        finally:
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temp_name is not None:
                self._unlink_if_identity(target_parent_fd, temp_name, locals().get("temp_identity"))
            for fd in (source_fd, published_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _rollback_move_context(self, context: Dict[str, Any]) -> None:
        for item in reversed(context.get("staged", [])):
            self._restore_staged_item(context, item)
        for published in reversed(context.get("published", [])):
            self._unlink_if_identity(
                published["parent_fd"], published["name"], published["identity"]
            )
        context["rolled_back"] = True
        stage_entry = self._entry_lstat(context["source_root"]["fd"], context["stage_name"])
        if (
            stage_entry is not None
            and (stage_entry.st_dev, stage_entry.st_ino) == context["stage_identity"]
        ):
            try:
                os.rmdir(context["stage_name"], dir_fd=context["source_root"]["fd"])
            except OSError:
                pass

    def _commit_move_context(self, context: Dict[str, Any]) -> bool:
        if not self._move_context_is_current(context):
            return False
        for item in context.get("staged", []):
            if not self._unlink_if_identity(
                context["stage_fd"], item["stage_name"], item["stage_identity"]
            ):
                return False
        self._fsync_dir(context["stage_fd"])
        stage_entry = self._entry_lstat(context["source_root"]["fd"], context["stage_name"])
        if (
            stage_entry is None
            or (stage_entry.st_dev, stage_entry.st_ino) != context["stage_identity"]
        ):
            return False
        try:
            os.rmdir(context["stage_name"], dir_fd=context["source_root"]["fd"])
        except OSError:
            return False
        self._fsync_dir(context["source_root"]["fd"])
        context["committed"] = True
        return True

    @staticmethod
    def _close_move_context(context: Optional[Dict[str, Any]]) -> None:
        if context is None:
            return
        fds: List[Optional[int]] = [context.get("stage_fd")]
        fds.extend(context.get("open_fds", []))
        for bound in (context.get("source_root"), context.get("target_root")):
            if bound:
                fds.extend((bound.get("parent_fd"), bound.get("fd")))
        for item in context.get("staged", []):
            fds.append(item.get("source_parent_fd"))
        for item in context.get("published", []):
            fds.append(item.get("parent_fd"))
            fds.append(item.get("fd"))
        seen = set()
        for fd in fds:
            if fd is None or fd in seen:
                continue
            seen.add(fd)
            try:
                os.close(fd)
            except OSError:
                pass

    def _move_file(
        self,
        source: str,
        target: str,
        source_root: Optional[str] = None,
        target_root: Optional[str] = None,
        *,
        move_context: Optional[Dict[str, Any]] = None,
        source_identity: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not source_root or not target_root:
            return False
        own_context = move_context is None
        context = move_context or self._create_move_context(source_root, target_root)
        if context is None:
            return False
        try:
            captured = source_identity or self._capture_move_identity(context, source)
            if captured is None:
                if own_context:
                    self._rollback_move_context(context)
                return False
            staged = self._stage_move_source(context, source, captured)
            if staged is None or not self._publish_staged_move(context, staged, target):
                if own_context:
                    self._rollback_move_context(context)
                return False
            if own_context:
                if not self._commit_move_context(context):
                    self._rollback_move_context(context)
                    return False
            return True
        finally:
            if own_context:
                self._close_move_context(context)

    def _execute_move_plan(
        self,
        move_plan: List[Tuple[str, str, List[Tuple[str, str]]]],
        source_root: str,
        target_root: str,
        move_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Tuple[int, int]]:
        own_context = move_context is None
        context = move_context or self._create_move_context(source_root, target_root)
        if context is None:
            return None
        try:
            identities: Dict[str, Dict[str, Any]] = {}
            for media_file, _, subtitle_pairs in move_plan:
                for candidate in [media_file, *(subtitle_file for subtitle_file, _ in subtitle_pairs)]:
                    if candidate not in identities:
                        captured = self._capture_move_identity(context, candidate)
                        if captured is None:
                            self._rollback_move_context(context)
                            return None
                        identities[candidate] = captured

            moved_files = moved_subtitles = 0
            for media_file, target_media, subtitle_pairs in move_plan:
                if not self._move_file(
                    media_file,
                    target_media,
                    source_root=source_root,
                    target_root=target_root,
                    move_context=context,
                    source_identity=identities[media_file],
                ):
                    self._rollback_move_context(context)
                    return None
                moved_files += 1
                for subtitle_file, target_subtitle in subtitle_pairs:
                    if not self._move_file(
                        subtitle_file,
                        target_subtitle,
                        source_root=source_root,
                        target_root=target_root,
                        move_context=context,
                        source_identity=identities[subtitle_file],
                    ):
                        self._rollback_move_context(context)
                        return None
                    moved_subtitles += 1

            if not self._commit_move_context(context):
                self._rollback_move_context(context)
                return None
            return moved_files, moved_subtitles
        except (OSError, TypeError, NotImplementedError, ValueError):
            self._rollback_move_context(context)
            return None
        finally:
            if own_context:
                self._close_move_context(context)

    @classmethod
    def _course_sort_key(cls, file_path: str, course_path: str) -> List[Any]:
        return cls._natural_key(os.path.relpath(file_path, course_path).replace("\\", "/"))

    @classmethod
    def _natural_path_key(cls, path: str) -> List[Any]:
        return cls._natural_key(path)

    @staticmethod
    def _natural_key(value: str) -> List[Any]:
        parts = _NATURAL_SPLIT_RE.split(value)
        output: List[Any] = []
        for part in parts:
            if part.isdigit():
                output.append(int(part))
            else:
                output.append(part.lower())
        return output

    def _normalize_config(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        def _coerce_threshold(raw: Any, default: int) -> int:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return default
            return value if 80 <= value <= 100 else default

        def _coerce_margin(raw: Any, default: int) -> int:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return default
            return value if 5 <= value <= 30 else default

        raw = config if isinstance(config, dict) else {}
        naming_mode = str(raw.get("naming_mode", "preview") or "preview").strip().lower()
        if naming_mode not in {"off", "preview", "apply"}:
            naming_mode = "preview"

        naming_uncertain_policy = str(raw.get("naming_uncertain_policy", "local")).lower()
        if naming_uncertain_policy not in {"local", "hold"}:
            naming_uncertain_policy = "local"

        return {
            "enabled": _coerce_bool(raw.get("enabled", False), False),
            "run_once": _coerce_bool(raw.get("run_once", False), False),
            "incoming": str(raw.get("incoming", self.DEFAULT_INCOMING)),
            "tv_output": str(raw.get("tv_output", self.DEFAULT_TV_OUTPUT)),
            "movie_output": str(raw.get("movie_output", self.DEFAULT_MOVIE_OUTPUT)),
            "children_output": str(
                raw.get("children_output", raw.get("output", self.DEFAULT_CHILDREN_OUTPUT))
            ),
            "interval": self._normalize_interval(raw.get("interval", self.DEFAULT_INTERVAL)),
            "naming_mode": naming_mode,
            "naming_sources": str(raw.get("naming_sources", "themoviedb,douban")),
            "naming_auto_threshold": _coerce_threshold(raw.get("naming_auto_threshold", 90), 90),
            "naming_min_margin": _coerce_margin(raw.get("naming_min_margin", 12), 12),
            "naming_uncertain_policy": naming_uncertain_policy,
            "naming_append_tmdb_id": _coerce_bool(
                raw.get("naming_append_tmdb_id", False),
                False,
            ),
            "naming_ai_review": _coerce_bool(
                raw.get("naming_ai_review", False),
                False,
            ),
            "naming_manual_overrides": str(raw.get("naming_manual_overrides", "")),
            "naming_clear_cache_once": _coerce_bool(
                raw.get("naming_clear_cache_once", False),
                False,
            ),
        }

    @classmethod
    def _normalize_interval(cls, interval: Any) -> int:
        try:
            value = int(interval)
            return value if value > 0 else cls.DEFAULT_INTERVAL
        except (TypeError, ValueError):
            return cls.DEFAULT_INTERVAL

    def _get_config(self) -> Dict[str, Any]:
        run_config = getattr(self._run_config_local, "config", None)
        if isinstance(run_config, dict):
            return dict(run_config)
        try:
            raw = self.get_config()
            if not isinstance(raw, dict):
                return self._normalize_config()
            return self._normalize_config(raw)
        except Exception:
            return self._normalize_config()

    def _persist_config(self, config: Dict[str, Any]) -> bool:
        if not hasattr(self, "update_config"):
            return False
        try:
            result = self.update_config(config)
            return result is not False
        except TypeError:
            pass
        except Exception:
            return False
        try:
            result = self.update_config(config=config)
            return result is not False
        except Exception:
            return False
