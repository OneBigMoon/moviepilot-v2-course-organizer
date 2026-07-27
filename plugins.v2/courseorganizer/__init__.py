import logging
import os
import re
import shutil
import threading
from typing import Any, Dict, List, Optional, Tuple

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


_NATURAL_SPLIT_RE = re.compile(r"(\d+)")
_INVALID_NAME_RE = re.compile(r"[\\/:*?\"<>|]+")
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
_EN_SEASON_RE = re.compile(r"(?i)\bseason\s*([0-9]{1,3})\b|\bs\s*([0-9]{1,3})\b")
_CN_SEASON_RE = re.compile(r"第\s*([0-9零一二三四五六七八九十]+)\s*季")


class CourseOrganizer(_PluginBase):
    plugin_name = "课程自动整理"
    plugin_config_prefix = "courseorganizer_"
    auth_level = 1
    plugin_order = 90
    plugin_version = "1.1.0"
    plugin_desc = "整理课程目录：两次快照稳定后按目录季节标识移动文件"
    plugin_author = "OpenAI"
    plugin_icon = "icons/courseorganizer.svg"

    MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".m4a"}
    SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
    INCOMPLETE_SUFFIXES = (".partial", ".part", ".tmp", ".crdownload", ".incomplete", ".!qb")
    DEFAULT_INTERVAL = 300
    DEFAULT_INCOMING = "/volume1/未整理"
    DEFAULT_OUTPUT = "/volume1/儿童"

    _thread_lock = threading.Lock()
    _run_once_timer: Optional[threading.Timer] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._config_snapshot: Dict[str, Any] = self._normalize_config()
        self._logger = _logger

    def init_plugin(self, config: Optional[Dict[str, Any]] = None):
        self.stop_service()
        self._config_snapshot = self._normalize_config(config)

        if self._config_snapshot.get("run_once"):
            self._logger.info("CourseOrganizer: scheduling one-time run asynchronously")
            reset_config = dict(self._config_snapshot)
            reset_config["run_once"] = False
            self._persist_config(reset_config)
            self._config_snapshot = reset_config

            self._run_once_timer = threading.Timer(0.2, self._run_once_and_reset)
            self._run_once_timer.daemon = True
            self._run_once_timer.start()

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[Dict[str, Any]]:
        return []

    def get_state(self) -> bool:
        return bool(self._get_config().get("enabled"))

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        defaults = self._normalize_config()
        return [
            {
                "component": "VForm",
                "props": {
                    "label-position": "left",
                    "hide-required-asterisk": True,
                },
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "text": "该插件仅扫描 incoming 下的一级目录作为课程，不处理根目录中的松散文件。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "text": "课程目录在连续两次扫描内容完全一致后才会进入整理，并在立刻再次快照确认后执行，避免下载未写入完成的文件被误搬移。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "text": "本插件不会进行 TMDB / 刮削 / 任何网络请求。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "text": "部署本地仓库前请确认：/path/to/outputs 在 PLUGIN_LOCAL_REPO_PATHS 中，并确保 incoming/output 为 RW 挂载。",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用插件",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "run_once",
                            "label": "一次性运行",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "incoming",
                            "label": "incoming 目录",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "output",
                            "label": "output 目录",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "interval",
                            "label": "扫描间隔（秒）",
                            "type": "number",
                            "min": 30,
                        },
                    },
                ],
            }
        ], defaults

    def get_service(self) -> List[Dict[str, Any]]:
        config = self._get_config()
        if not bool(config.get("enabled")):
            return []
        interval = int(config.get("interval", self.DEFAULT_INTERVAL))
        return [{
            "id": self.__class__.__name__,
            "name": "CourseOrganizer 课程整理服务",
            "trigger": "interval",
            "func": self.run,
            "kwargs": {
                "seconds": interval,
            },
        }]

    def stop_service(self) -> None:
        timer = self._run_once_timer
        if timer is not None and timer.is_alive():
            timer.cancel()
        self._run_once_timer = None

    def run(self) -> None:
        with self._thread_lock:
            self._run()

    def _run(self, force: bool = False) -> None:
        config = self._get_config()
        if not force and not config.get("enabled"):
            self._logger.debug("CourseOrganizer: plugin disabled")
            return

        incoming = config.get("incoming")
        output_root = config.get("output")

        if not incoming or not os.path.isdir(incoming):
            self._logger.error("incoming path invalid: %s", incoming)
            return

        if not output_root:
            self._logger.error("output path missing")
            return

        processed = 0
        moved = 0
        for entry in sorted(os.listdir(incoming), key=self._natural_key):
            course_dir = os.path.join(incoming, entry)
            if not os.path.isdir(course_dir):
                continue

            processed += 1
            try:
                if self._process_course(entry, course_dir, output_root):
                    moved += 1
            except Exception as exc:
                self._logger.error("CourseOrganizer: failed to process %s, %s", entry, exc)

        self._logger.info("CourseOrganizer: scanned %d courses, moved %d", processed, moved)

    def _run_once_and_reset(self) -> None:
        with self._thread_lock:
            try:
                self._run(force=True)
            finally:
                latest_config = self._normalize_config(self._get_config())
                latest_config["run_once"] = False
                self._persist_config(latest_config)
                self._config_snapshot = latest_config

    def _process_course(self, course_name: str, course_path: str, output_root: str) -> bool:
        state_key = self._state_key(course_name)
        signature = self._coerce_signature(self._snapshot_signature(course_path))
        state = self.get_data(state_key, {}) or {}
        persisted_signature = self._coerce_signature(state.get("signature"))

        if self._has_incomplete_file(course_path):
            self.save_data(state_key, {"signature": signature, "stable_count": 0, "blocked": True})
            self._logger.debug("CourseOrganizer: %s blocked by incomplete marker", course_name)
            return False

        if not state:
            self.save_data(state_key, {"signature": signature, "stable_count": 1})
            self._logger.debug("CourseOrganizer: %s first snapshot", course_name)
            return False

        if signature != persisted_signature:
            self.save_data(state_key, {"signature": signature, "stable_count": 1})
            self._logger.debug("CourseOrganizer: %s changed before stabilization", course_name)
            return False

        stable_count = int(state.get("stable_count", 0))
        if stable_count < 1:
            self.save_data(state_key, {"signature": signature, "stable_count": stable_count + 1})
            return False

        latest_signature = self._coerce_signature(self._snapshot_signature(course_path))
        if latest_signature != signature:
            self.save_data(state_key, {"signature": latest_signature, "stable_count": 1})
            self._logger.debug("CourseOrganizer: %s changed before final confirmation", course_name)
            return False

        if self._has_incomplete_file(course_path):
            self.save_data(state_key, {"signature": latest_signature, "stable_count": 0, "blocked": True})
            return False

        media_by_season, subtitle_by_season = self._collect_course_files(course_path)
        if not media_by_season:
            self.save_data(state_key, None)
            return False

        output_course_root = os.path.join(output_root, self._safe_name(course_name))
        os.makedirs(output_course_root, exist_ok=True)

        moved_files = 0
        moved_subtitles = 0
        for season in sorted(media_by_season.keys()):
            season_files = sorted(media_by_season[season], key=lambda file_path: self._course_sort_key(file_path, course_path))
            season_root = os.path.join(output_course_root, f"Season {season}")
            os.makedirs(season_root, exist_ok=True)

            for index, media_file in enumerate(season_files, start=1):
                ext = self._lower_extension(media_file)
                episode_name = f"{self._safe_name(course_name)} - S{season:02d}E{index:02d}{ext}"
                target_media = self._reserve_path(os.path.join(season_root, episode_name))
                self._move_file(media_file, target_media)
                moved_files += 1

                media_key = os.path.splitext(os.path.basename(media_file))[0].lower()
                for subtitle_file in subtitle_by_season.get(season, {}).get(media_key, []):
                    subtitle_ext = self._lower_extension(subtitle_file)
                    subtitle_name = f"{self._safe_name(course_name)} - S{season:02d}E{index:02d}{subtitle_ext}"
                    target_subtitle = self._reserve_path(os.path.join(season_root, subtitle_name))
                    self._move_file(subtitle_file, target_subtitle)
                    moved_subtitles += 1

        if moved_files:
            self._delete_if_empty_recursive(course_path)
            self._logger.info(
                "CourseOrganizer: moved %d media and %d subtitles for course %s",
                moved_files,
                moved_subtitles,
                course_name,
            )

        self.save_data(state_key, None)
        return moved_files > 0

    def _collect_course_files(self, course_path: str) -> Tuple[Dict[int, List[str]], Dict[int, Dict[str, List[str]]]]:
        media_files: Dict[int, List[str]] = {}
        subtitle_map: Dict[int, Dict[str, List[str]]] = {}

        for root, _, filenames in os.walk(course_path):
            for filename in filenames:
                if self._is_incomplete(filename):
                    continue

                extension = self._lower_extension(filename)
                season = self._detect_season_from_path(os.path.join(root, filename), course_path)
                if extension in self.MEDIA_EXTENSIONS:
                    media_files.setdefault(season, []).append(os.path.join(root, filename))
                    continue
                if extension in self.SUBTITLE_EXTENSIONS:
                    base = os.path.splitext(filename)[0].lower()
                    subtitle_map.setdefault(season, {}).setdefault(base, []).append(os.path.join(root, filename))

        for season, subtitles in subtitle_map.items():
            for key, subtitle_files in subtitles.items():
                subtitle_map[season][key] = sorted(subtitle_files, key=self._natural_path_key)

        return media_files, subtitle_map

    @classmethod
    def _detect_season_from_path(cls, file_path: str, course_path: str) -> int:
        relative = os.path.relpath(file_path, course_path)
        directories = os.path.dirname(relative).split(os.sep)
        season = 1
        for directory in directories:
            if not directory:
                continue
            parsed = cls._parse_season_from_component(directory)
            if parsed is not None:
                season = parsed
        return season

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
        normalized = _INVALID_NAME_RE.sub("_", value).strip() or "Course"
        return normalized[:160]

    @staticmethod
    def _lower_extension(filename: str) -> str:
        return os.path.splitext(filename)[1].lower()

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
    def _move_file(source: str, target: str) -> None:
        if not os.path.exists(source):
            return
        if os.path.abspath(source) == os.path.abspath(target):
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(source, target)

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
        raw = config or {}
        return {
            "enabled": bool(raw.get("enabled", True)),
            "run_once": bool(raw.get("run_once", False)),
            "incoming": str(raw.get("incoming", self.DEFAULT_INCOMING)),
            "output": str(raw.get("output", self.DEFAULT_OUTPUT)),
            "interval": self._normalize_interval(raw.get("interval", self.DEFAULT_INTERVAL)),
        }

    @classmethod
    def _normalize_interval(cls, interval: Any) -> int:
        try:
            value = int(interval)
            return value if value > 0 else cls.DEFAULT_INTERVAL
        except (TypeError, ValueError):
            return cls.DEFAULT_INTERVAL

    def _get_config(self) -> Dict[str, Any]:
        try:
            raw = self.get_config()
            if not isinstance(raw, dict):
                return self._normalize_config()
            return self._normalize_config(raw)
        except Exception:
            return self._normalize_config(self._config_snapshot)

    def _persist_config(self, config: Dict[str, Any]) -> None:
        if not hasattr(self, "update_config"):
            return
        try:
            self.update_config(config)
            return
        except TypeError:
            pass
        try:
            self.update_config(config=config)
        except Exception:
            pass
