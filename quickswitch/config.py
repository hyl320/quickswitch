"""配置文件读写、绑定模型与校验。"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field

from . import keys

APP_NAME = "QuickSwitch"
CONFIG_FILENAME = "config.json"


def app_dir() -> str:
    """程序根目录：源码运行时是 quickswitch 包的上一层，打包后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_dir() -> str:
    """静态资源目录（PyInstaller 单文件模式下是解包目录）。"""
    bundle = getattr(sys, "_MEIPASS", None)
    return os.path.abspath(bundle) if bundle else app_dir()


def config_path() -> str:
    """配置文件路径，可用环境变量 QUICKSWITCH_CONFIG 覆盖（测试用）。"""
    override = os.environ.get("QUICKSWITCH_CONFIG")
    if override:
        return os.path.abspath(override)
    return os.path.join(app_dir(), CONFIG_FILENAME)


# 「匹配到窗口后做什么」的三种模式
WHEN_ACTIVATE = "activate"          # 只切换现有窗口
WHEN_ACTIVATE_OR_LAUNCH = "activateOrLaunch"  # 先找窗口，找不到再启动
WHEN_LAUNCH = "launch"              # 总是启动新实例

WHEN_LABELS = {
    WHEN_ACTIVATE: "只切换现有窗口",
    WHEN_ACTIVATE_OR_LAUNCH: "切换，找不到就启动",
    WHEN_LAUNCH: "总是启动新窗口",
}


def new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Binding:
    """一条快捷键绑定。"""

    id: str = field(default_factory=new_id)
    name: str = ""
    hotkey: str = ""
    enabled: bool = True

    # 窗口匹配条件
    process: str = ""        # 进程名，支持 * 通配，如 Code.exe / *Terminal*.exe
    title: str = ""          # 标题正则（不区分大小写，搜索匹配）
    window_class: str = ""   # 窗口类名子串
    exclude_title: str = ""  # 排除的标题正则
    index: int = 0           # 0 = 循环切换，N = 固定第 N 个匹配窗口
    override: bool = True    # 与系统/其他程序快捷键冲突时，用键盘钩子接管

    # 找不到窗口时的行为
    when: str = WHEN_ACTIVATE_OR_LAUNCH
    launch: str = ""
    launch_cwd: str = ""
    wait_seconds: float = 8.0

    def display_name(self) -> str:
        if self.name.strip():
            return self.name.strip()
        summary = target_summary(self)
        return summary or "未命名"

    def match_summary(self) -> str:
        return target_summary(self)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "hotkey": self.hotkey,
            "enabled": self.enabled,
            "process": self.process,
            "title": self.title,
            "window_class": self.window_class,
            "exclude_title": self.exclude_title,
            "index": self.index,
            "override": self.override,
            "when": self.when,
            "launch": self.launch,
            "launch_cwd": self.launch_cwd,
            "wait_seconds": self.wait_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Binding":
        def _str(key: str, default: str = "") -> str:
            value = data.get(key, default)
            return default if value is None else str(value)

        def _bool(key: str, default: bool) -> bool:
            value = data.get(key, default)
            if isinstance(value, str):
                return value.strip().lower() not in ("0", "false", "no", "off", "")
            return bool(value)

        try:
            index = int(data.get("index", 0) or 0)
        except (TypeError, ValueError):
            index = 0
        try:
            wait = float(data.get("wait_seconds", 8.0) or 0)
        except (TypeError, ValueError):
            wait = 8.0

        when = _str("when", WHEN_ACTIVATE_OR_LAUNCH)
        if when not in WHEN_LABELS:
            when = WHEN_ACTIVATE_OR_LAUNCH

        return cls(
            id=_str("id") or new_id(),
            name=_str("name"),
            hotkey=_str("hotkey"),
            enabled=_bool("enabled", True),
            process=_str("process"),
            title=_str("title"),
            window_class=_str("window_class"),
            exclude_title=_str("exclude_title"),
            index=max(0, index),
            override=_bool("override", True),
            when=when,
            launch=_str("launch"),
            launch_cwd=_str("launch_cwd"),
            wait_seconds=max(0.0, min(120.0, wait)),
        )


@dataclass
class Config:
    version: int = 1
    bindings: list[Binding] = field(default_factory=list)
    start_with_windows: bool = False
    start_minimized: bool = False
    close_to_tray: bool = True
    notifications: bool = True

    def binding_by_id(self, binding_id: str) -> Binding | None:
        for binding in self.bindings:
            if binding.id == binding_id:
                return binding
        return None

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "start_with_windows": self.start_with_windows,
            "start_minimized": self.start_minimized,
            "close_to_tray": self.close_to_tray,
            "notifications": self.notifications,
            "bindings": [b.to_dict() for b in self.bindings],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        raw_bindings = data.get("bindings")
        bindings: list[Binding] = []
        if isinstance(raw_bindings, list):
            for item in raw_bindings:
                if isinstance(item, dict):
                    bindings.append(Binding.from_dict(item))
        return cls(
            version=int(data.get("version", 1) or 1),
            bindings=bindings,
            start_with_windows=bool(data.get("start_with_windows", False)),
            start_minimized=bool(data.get("start_minimized", False)),
            close_to_tray=bool(data.get("close_to_tray", True)),
            notifications=bool(data.get("notifications", True)),
        )


def load(path: str | None = None) -> Config:
    """读取配置；文件不存在时返回默认配置。"""
    path = path or config_path()
    if not os.path.exists(path):
        return Config()
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        # 配置损坏时不要静默覆盖，留给界面提示，同时返回空配置继续运行
        raise
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    return Config.from_dict(data)


def save(config: Config, path: str | None = None) -> str:
    """原子写入配置，并保留一份 .bak 备份。"""
    path = path or config_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    if os.path.exists(path):
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass

    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return path


def target_summary(binding: Binding) -> str:
    """用一句话描述匹配目标，用于列表展示。"""
    parts: list[str] = []
    if binding.process:
        parts.append(binding.process)
    if binding.title:
        parts.append(f"标题≈{binding.title}")
    if binding.window_class:
        parts.append(f"类={binding.window_class}")
    if binding.exclude_title:
        parts.append(f"排除≈{binding.exclude_title}")
    text = " ".join(parts)
    if binding.index > 0:
        text += f"  #第{binding.index}个"
    elif parts:
        text += "  ↻循环"
    if not parts:
        text = "（未设置匹配条件）"
    return text


def validate_bindings(bindings: list[Binding]) -> list[tuple[str, str]]:
    """返回 ``(binding_id, 错误信息)`` 列表，空列表表示全部合法。"""
    problems: list[tuple[str, str]] = []
    seen: dict[tuple[int, int], str] = {}
    for binding in bindings:
        if not binding.enabled:
            continue
        if not binding.hotkey.strip():
            problems.append((binding.id, "未设置快捷键"))
        else:
            try:
                combo = keys.parse_hotkey(binding.hotkey)
            except ValueError as exc:
                problems.append((binding.id, str(exc)))
            else:
                if combo in seen:
                    problems.append((binding.id, f"快捷键 {binding.hotkey} 与「{seen[combo]}」重复"))
                else:
                    seen[combo] = binding.display_name()

        for field_name, label in (
            ("title", "标题正则"),
            ("exclude_title", "排除标题正则"),
        ):
            pattern = getattr(binding, field_name)
            if pattern:
                try:
                    re.compile(pattern, re.IGNORECASE)
                except re.error as exc:
                    problems.append((binding.id, f"{label}无效：{exc}"))

        if binding.when in (WHEN_ACTIVATE, WHEN_ACTIVATE_OR_LAUNCH):
            if not (binding.process or binding.title or binding.window_class):
                problems.append((binding.id, "切换窗口需要至少填写进程、标题或窗口类之一"))
        if binding.when in (WHEN_ACTIVATE_OR_LAUNCH, WHEN_LAUNCH):
            if not binding.launch.strip():
                problems.append((binding.id, "未填写启动命令"))
    return problems
