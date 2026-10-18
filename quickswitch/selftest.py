"""自检脚本：``python QuickSwitch.pyw --selftest``。

不打开主界面，逐项验证快捷键解析、窗口枚举、热键注册、钩子接管、
前台切换和启动命令。运行过程中会短暂弹出记事本窗口用于验证切换。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time

import win32api
import win32con
import win32gui
import win32process

from . import config as config_mod
from . import hotkeys as hotkeys_mod
from . import keys as keys_mod
from . import matcher as matcher_mod
from . import winapi
from .config import Binding, Config


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.items.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)

    def section(self, title: str) -> None:
        print(f"\n== {title} ==")

    @property
    def failed(self) -> int:
        return sum(1 for _name, ok, _detail in self.items if not ok)


def _wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _ComboOccupier(threading.Thread):
    """在独立线程里占住一个全局热键，用来验证冲突接管逻辑。"""

    daemon = True

    def __init__(self, mods: int, vk: int) -> None:
        super().__init__(name="QuickSwitchSelftestOccupier")
        self.mods = mods
        self.vk = vk
        self.ready = threading.Event()
        self.hwnd = 0
        self.registered = False
        self.class_name = "QuickSwitchSelftestOccupierWindow"

    def run(self) -> None:  # pragma: no cover - Win32 消息循环
        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = self.class_name
            wc.lpfnWndProc = self._proc
            try:
                win32gui.RegisterClass(wc)
            except win32gui.error:
                pass
            self.hwnd = win32gui.CreateWindow(
                self.class_name, "occupier", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None
            )
            try:
                win32gui.RegisterHotKey(self.hwnd, 1, self.mods | 0x4000, self.vk)
                self.registered = True
            except win32gui.error:
                self.registered = False
            self.ready.set()
            win32gui.PumpMessages()
        finally:
            self.ready.set()

    def _proc(self, hwnd, message, wparam, lparam):
        if message == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        if message == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    def stop(self) -> None:
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass


def _find_free_combo(candidates: list[tuple[int, int]]):
    """找出一个当前没有被注册的测试组合键。"""
    for mods, vk in candidates:
        hwnd = 0
        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = "QuickSwitchSelftestProbe"
            wc.lpfnWndProc = win32gui.DefWindowProc
            try:
                win32gui.RegisterClass(wc)
            except win32gui.error:
                pass
            hwnd = win32gui.CreateWindow(
                "QuickSwitchSelftestProbe", "probe", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None
            )
            win32gui.RegisterHotKey(hwnd, 1, mods | 0x4000, vk)
            win32gui.UnregisterHotKey(hwnd, 1)
            win32gui.DestroyWindow(hwnd)
            return mods, vk
        except win32gui.error:
            try:
                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass
    return None


def _close_window(hwnd: int) -> None:
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass


def _kill_pid(pid: int) -> None:
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
        win32api.TerminateProcess(handle, 0)
        win32api.CloseHandle(handle)
    except Exception:
        pass


def _notepad_pids() -> set[int]:
    pids: set[int] = set()
    try:
        for pid in win32process.EnumProcesses():
            if winapi.process_name(pid).lower() == "notepad.exe":
                pids.add(pid)
    except Exception:
        pass
    return pids


def run_selftest() -> int:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    report = Report()
    print("QuickSwitch 自检")
    print(f"Python {sys.version.split()[0]} / {sys.platform}")
    print(f"程序目录：{config_mod.app_dir()}")

    # --- 快捷键解析 -------------------------------------------------------
    report.section("快捷键解析")
    try:
        mods, vk = keys_mod.parse_hotkey("Ctrl+Alt+Shift+1")
        report.add(
            "解析 Ctrl+Alt+Shift+1",
            (mods, vk) == (keys_mod.MOD_CONTROL | keys_mod.MOD_ALT | keys_mod.MOD_SHIFT, 0x31),
        )
        report.add(
            "规范格式化",
            keys_mod.normalize_hotkey("alt+ctrl+f12") == "Ctrl+Alt+F12",
            keys_mod.normalize_hotkey("alt+ctrl+f12"),
        )
        report.add("递增数字键", keys_mod.increment_hotkey("Ctrl+1", 1) == "Ctrl+2")
        report.add("递增 F 键", keys_mod.increment_hotkey("Ctrl+F9", 1) == "Ctrl+F10")
        try:
            keys_mod.parse_hotkey("Ctrl+NoSuchKey")
            report.add("非法按键被拒绝", False)
        except ValueError:
            report.add("非法按键被拒绝", True)
    except Exception as exc:
        report.add("快捷键解析", False, repr(exc))

    # --- 配置读写 ---------------------------------------------------------
    report.section("配置文件")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "config.json")
            cfg = Config(
                bindings=[
                    Binding(
                        id="demo",
                        name="终端",
                        hotkey="Ctrl+1",
                        process="WindowsTerminal.exe",
                        index=1,
                        when=config_mod.WHEN_ACTIVATE,
                    )
                ]
            )
            config_mod.save(cfg, path)
            loaded = config_mod.load(path)
            report.add("配置写入 / 读取", loaded.bindings[0].name == "终端" and loaded.bindings[0].index == 1)
            problems = config_mod.validate_bindings(loaded.bindings)
            report.add("校验配置", not problems, str(problems))
    except Exception as exc:
        report.add("配置文件", False, repr(exc))

    # --- 窗口枚举 ---------------------------------------------------------
    report.section("窗口枚举")
    windows: list[winapi.WindowInfo] = []
    try:
        windows = winapi.list_windows()
        report.add("枚举到窗口", len(windows) > 0, f"{len(windows)} 个")
        report.add(
            "都能拿到进程名",
            all(window.process for window in windows),
            "样例：" + ", ".join(sorted({w.process for w in windows})[:6]),
        )
    except Exception as exc:
        report.add("窗口枚举", False, repr(exc))

    # --- 匹配规则 ---------------------------------------------------------
    report.section("匹配规则")
    try:
        fake = [
            winapi.WindowInfo(11, "agent-alpha - 终端", "CASCADIA_HOSTING_WINDOW_CLASS", 1, "WindowsTerminal.exe"),
            winapi.WindowInfo(22, "agent-beta - 终端", "CASCADIA_HOSTING_WINDOW_CLASS", 1, "WindowsTerminal.exe"),
            winapi.WindowInfo(33, "Code.exe", "Chrome_WidgetWin_1", 2, "Code.exe"),
        ]
        rule = matcher_mod.compile_rule(
            Binding(
                id="m1",
                hotkey="Ctrl+9",
                process="WindowsTerminal.exe",
                title="agent-",
                when=config_mod.WHEN_ACTIVATE,
            )
        )
        matches = matcher_mod.find_matches(rule, fake)
        report.add("进程 + 标题正则", [w.hwnd for w in matches] == [11, 22])
        report.add(
            "循环：未在前台时取最上层",
            matcher_mod.choose_target(rule, matches, 0).hwnd == 11,
        )
        report.add(
            "循环：前台时取下一个",
            matcher_mod.choose_target(rule, matches, 11).hwnd == 22,
        )
        fixed_rule = matcher_mod.compile_rule(
            Binding(
                id="m2",
                hotkey="Ctrl+8",
                process="WindowsTerminal.exe",
                index=2,
                when=config_mod.WHEN_ACTIVATE,
            )
        )
        report.add(
            "固定第 2 个（按句柄排序）",
            matcher_mod.choose_target(fixed_rule, matches, 0).hwnd == 22,
        )
        exclude_rule = matcher_mod.compile_rule(
            Binding(
                id="m3",
                hotkey="Ctrl+7",
                process="*Terminal.exe",
                exclude_title="beta",
                when=config_mod.WHEN_ACTIVATE,
            )
        )
        report.add(
            "排除标题",
            [w.hwnd for w in matcher_mod.find_matches(exclude_rule, fake)] == [11],
        )
    except Exception as exc:
        report.add("匹配规则", False, repr(exc))

    # --- 快捷键注册与分发 -------------------------------------------------
    report.section("全局热键与消息泵")
    free = _find_free_combo(
        [
            (keys_mod.MOD_CONTROL | keys_mod.MOD_ALT | keys_mod.MOD_SHIFT, 0x7B),  # F12
            (keys_mod.MOD_CONTROL | keys_mod.MOD_ALT | keys_mod.MOD_SHIFT, 0x7A),  # F11
            (keys_mod.MOD_CONTROL | keys_mod.MOD_ALT, 0x7B),
        ]
    )
    fired: list[str] = []
    pump = None
    if free is None:
        report.add("找到可用的测试快捷键", False, "F12/F11 都被占用，跳过注册测试")
    else:
        mods, vk = free
        combo_text = keys_mod.format_hotkey(mods, vk)
        report.add("找到可用的测试快捷键", True, combo_text)
        try:
            pump = hotkeys_mod.MessagePump(
                on_fire=fired.append,
                class_name="QuickSwitchSelftestPump",
            )
            pump.start()
            pump._ready.wait(5.0)
            report.add("消息泵启动", bool(pump.hwnd), f"hwnd={pump.hwnd}")

            statuses = pump.apply(
                [
                    Binding(
                        id="hot-1",
                        name="测试热键",
                        hotkey=combo_text,
                        process="notepad.exe",
                        when=config_mod.WHEN_ACTIVATE,
                    )
                ]
            )
            status = statuses.get("hot-1")
            report.add(
                "注册全局热键",
                status is not None and status.state == "registered",
                status.detail if status else "无状态",
            )
            win32gui.PostMessage(pump.hwnd, win32con.WM_HOTKEY, 1, 0)
            report.add("分发 WM_HOTKEY", _wait_until(lambda: bool(fired), 1.0), str(fired))

            pump.begin_capture()
            report.add("录制模式安装钩子", pump.hook is not None and pump.hook.installed)
            pump.cancel_capture()
            report.add("退出录制后卸载钩子", pump.hook is None)

            # 冲突接管：让另一个线程先占住组合键
            conflict_free = _find_free_combo(
                [
                    (keys_mod.MOD_CONTROL | keys_mod.MOD_ALT | keys_mod.MOD_SHIFT, 0x79),  # F10
                    (keys_mod.MOD_CONTROL | keys_mod.MOD_ALT | keys_mod.MOD_SHIFT, 0x78),  # F9
                ]
            )
            if conflict_free is None:
                report.add("冲突接管测试", False, "没有可用组合键")
            else:
                c_mods, c_vk = conflict_free
                occupier = _ComboOccupier(c_mods, c_vk)
                occupier.start()
                occupier.ready.wait(3.0)
                if not occupier.registered:
                    report.add("冲突接管测试", False, "占位线程未能注册快捷键")
                else:
                    conflict_text = keys_mod.format_hotkey(c_mods, c_vk)
                    hook_statuses = pump.apply(
                        [
                            Binding(
                                id="hot-2",
                                name="冲突热键",
                                hotkey=conflict_text,
                                process="notepad.exe",
                                override=True,
                                when=config_mod.WHEN_ACTIVATE,
                            )
                        ]
                    )
                    hook_status = hook_statuses.get("hot-2")
                    report.add(
                        "冲突时自动用钩子接管",
                        hook_status is not None and hook_status.state == "hook"
                        and pump.hook is not None,
                        hook_status.detail if hook_status else "无状态",
                    )
                    strict_statuses = pump.apply(
                        [
                            Binding(
                                id="hot-3",
                                name="冲突热键（不覆盖）",
                                hotkey=conflict_text,
                                process="notepad.exe",
                                override=False,
                                when=config_mod.WHEN_ACTIVATE,
                            )
                        ]
                    )
                    strict_status = strict_statuses.get("hot-3")
                    report.add(
                        "关闭覆盖时报告冲突",
                        strict_status is not None and strict_status.state == "conflict",
                        strict_status.detail if strict_status else "无状态",
                    )
                    pump.apply([])
                occupier.stop()
        except Exception as exc:
            report.add("全局热键与消息泵", False, repr(exc))
        finally:
            if pump is not None:
                try:
                    pump.request_stop()
                except Exception:
                    pass

    # --- 前台切换 ---------------------------------------------------------
    report.section("窗口切换")
    notepad_pids_before = _notepad_pids()
    try:
        subprocess.Popen(["notepad.exe"])
        target = winapi.wait_for_window(
            lambda w: w.process.lower() == "notepad.exe", timeout=8.0
        )
        report.add("启动记事本并找到窗口", target is not None)
        if target is not None:
            ok = winapi.activate_window(target.hwnd)
            report.add("切换到前台", ok and winapi.foreground_window() == target.hwnd)
            _close_window(target.hwnd)
            time.sleep(0.4)
    except Exception as exc:
        report.add("窗口切换", False, repr(exc))
    finally:
        for pid in _notepad_pids() - notepad_pids_before:
            _kill_pid(pid)

    # --- 启动命令 ---------------------------------------------------------
    report.section("启动命令")
    notepad_pids_before = _notepad_pids()
    try:
        winapi.launch_command("notepad.exe")
        window = winapi.wait_for_window(
            lambda w: w.process.lower() == "notepad.exe", timeout=8.0
        )
        report.add("launch_command 启动并找到窗口", window is not None)
        if window is not None:
            _close_window(window.hwnd)
    except Exception as exc:
        report.add("启动命令", False, repr(exc))
    finally:
        for pid in _notepad_pids() - notepad_pids_before:
            _kill_pid(pid)

    print("\n" + "=" * 60)
    if report.failed:
        print(f"自检失败：{report.failed} 项未通过（共 {len(report.items)} 项）")
        return 1
    print(f"自检通过：{len(report.items)} 项全部正常")
    return 0
