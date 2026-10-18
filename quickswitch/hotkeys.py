"""全局快捷键：注册、冲突接管（低级键盘钩子）、消息泵与托盘。

设计要点：
- ``RegisterHotKey`` 成功时最省事、最稳，不会碰全局键盘流；
- 若某个组合键已被系统或其它程序注册（Windows 返回 1409），而绑定又允许
  “覆盖”，就改用 ``WH_KEYBOARD_LL`` 低级键盘钩子，在按键进入系统之前把它吞掉，
  从而实现真正的“我的快捷键优先”。
- 所有 Win32 窗口/钩子操作都在同一个独立消息泵线程里完成，界面线程只收队列消息。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from dataclasses import dataclass

import win32api
import win32con
import win32gui

from . import keys, winapi

WM_APP = 0x8000
WM_QS_FIRE = WM_APP + 1
WM_QS_JOB = WM_APP + 2
WM_QS_SHOW = WM_APP + 3
TRAY_CALLBACK_MESSAGE = WM_APP + 10

ERROR_HOTKEY_ALREADY_REGISTERED = 1409
DEBOUNCE_SECONDS = 0.25

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None


@dataclass
class HotkeyStatus:
    state: str  # registered / hook / conflict / error / disabled
    detail: str = ""

    @property
    def label(self) -> str:
        return {
            "registered": "已注册",
            "hook": "钩子接管",
            "conflict": "冲突",
            "error": "错误",
            "disabled": "未启用",
        }.get(self.state, self.state)

    @property
    def ok(self) -> bool:
        return self.state in ("registered", "hook")


class MessagePump(threading.Thread):
    """承载隐藏窗口、快捷键、钩子和托盘的消息泵线程。"""

    daemon = True

    def __init__(
        self,
        on_fire,
        on_tray_click=None,
        on_capture=None,
        on_tray_ready=None,
        on_show=None,
        log=None,
        icon_path: str = "",
        class_name: str = winapi.HIDDEN_WINDOW_CLASS,
    ) -> None:
        super().__init__(name="QuickSwitchMessagePump")
        self.on_fire = on_fire or (lambda binding_id: None)
        self.on_tray_click = on_tray_click or (lambda lparam: None)
        self.on_capture = on_capture or (lambda combo: None)
        self.on_tray_ready = on_tray_ready or (lambda ok: None)
        self.on_show = on_show or (lambda: None)
        self.log = log or (lambda message: None)
        self.icon_path = icon_path
        self.class_name = class_name

        self.hwnd: int = 0
        self.tray: winapi.TrayIcon | None = None
        self.hook: winapi.KeyboardHook | None = None
        self._ready = threading.Event()
        self._show_message = user32.RegisterWindowMessageW(winapi.SHOW_MESSAGE_NAME)

        self._lock = threading.Lock()
        self._jobs: list[tuple] = []
        self._fire_times: dict[str, float] = {}
        self._hotkey_ids: dict[int, str] = {}
        self._hook_table: dict[tuple[int, int], str] = {}
        self._bindings: dict[str, object] = {}
        self._capture_active = False

    # -- 线程主循环 ---------------------------------------------------------
    def run(self) -> None:  # pragma: no cover - 需要真实消息循环
        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = self.class_name
            wc.lpfnWndProc = self._wndproc
            try:
                win32gui.RegisterClass(wc)
            except win32gui.error:
                # 类已存在（同一进程里重复创建消息泵时），沿用即可
                pass
            self.hwnd = win32gui.CreateWindow(
                self.class_name,
                "QuickSwitch",
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                wc.hInstance,
                None,
            )
            self._ready.set()

            self.tray = winapi.TrayIcon(
                self.hwnd,
                TRAY_CALLBACK_MESSAGE,
                self.icon_path,
                "QuickSwitch · 窗口速切",
            )
            tray_ok = False
            try:
                tray_ok = self.tray.add()
            except Exception as exc:
                self.log(f"托盘图标创建失败：{exc}")
            self.on_tray_ready(tray_ok)

            win32gui.PumpMessages()
        except Exception as exc:  # pragma: no cover
            self.log(f"消息泵异常退出：{exc}")
        finally:
            self._ready.set()
            self._cleanup()

    def _wndproc(self, hwnd, message, wparam, lparam):  # pragma: no cover
        try:
            if message == WM_QS_JOB:
                self._run_jobs()
                return 0
            if message == win32con.WM_HOTKEY or message == WM_QS_FIRE:
                self._fire(int(wparam))
                return 0
            if self._show_message and message == self._show_message:
                self.on_show()
                return 0
            if message == TRAY_CALLBACK_MESSAGE:
                self.on_tray_click(int(lparam) & 0xFFFF)
                return 0
            if self.tray and self.tray.taskbar_created_message and message == self.tray.taskbar_created_message:
                try:
                    self.tray.ensure()
                except Exception:
                    pass
                return 0
            if message == win32con.WM_DESTROY:
                self._cleanup()
        except Exception:
            pass
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    def _cleanup(self) -> None:
        try:
            for hotkey_id in list(self._hotkey_ids):
                try:
                    win32gui.UnregisterHotKey(self.hwnd, hotkey_id)
                except Exception:
                    pass
            self._hotkey_ids.clear()
            self._hook_table.clear()
        finally:
            if self.hook:
                try:
                    self.hook.uninstall()
                except Exception:
                    pass
                self.hook = None
            if self.tray:
                try:
                    self.tray.remove()
                except Exception:
                    pass
                self.tray = None

    # -- 与界面线程交互 -----------------------------------------------------
    def submit(self, func, timeout: float = 10.0):
        """在消息泵线程执行 ``func`` 并等待结果。"""
        if not self._ready.wait(timeout):
            raise RuntimeError("消息泵未启动")
        if not self.hwnd:
            raise RuntimeError("消息泵窗口未创建")
        event = threading.Event()
        holder: dict = {"value": None, "error": None}
        with self._lock:
            self._jobs.append((func, holder, event))
        win32gui.PostMessage(self.hwnd, WM_QS_JOB, 0, 0)
        if not event.wait(timeout):
            raise TimeoutError("消息泵响应超时")
        if holder["error"] is not None:
            raise holder["error"]
        return holder["value"]

    def _run_jobs(self) -> None:  # pragma: no cover
        while True:
            with self._lock:
                if not self._jobs:
                    return
                func, holder, event = self._jobs.pop(0)
            try:
                holder["value"] = func()
            except Exception as exc:
                holder["error"] = exc
            finally:
                event.set()

    def request_stop(self) -> None:
        try:
            self.submit(self._stop_in_thread, timeout=3.0)
        except Exception:
            pass
        self.join(2.0)

    def _stop_in_thread(self) -> None:
        self._cleanup()
        user32.PostQuitMessage(0)

    # -- 快捷键应用 ---------------------------------------------------------
    def apply(self, bindings) -> dict:
        return self.submit(lambda: self._apply_in_thread(bindings))

    def _apply_in_thread(self, bindings) -> dict:
        statuses: dict[str, HotkeyStatus] = {}
        self._unregister_hotkeys()

        self._bindings = {binding.id: binding for binding in bindings}
        candidates: list[tuple[object, tuple[int, int]]] = []
        used: set[tuple[int, int]] = set()

        for index, binding in enumerate(bindings, start=1):
            if not binding.enabled:
                statuses[binding.id] = HotkeyStatus("disabled", "未启用")
                continue
            try:
                combo = keys.parse_hotkey(binding.hotkey)
            except ValueError as exc:
                statuses[binding.id] = HotkeyStatus("error", str(exc))
                continue
            if combo in used:
                statuses[binding.id] = HotkeyStatus("error", "快捷键重复")
                continue
            used.add(combo)

            try:
                win32gui.RegisterHotKey(
                    self.hwnd, index, combo[0] | keys.MOD_NOREPEAT, combo[1]
                )
            except win32gui.error as exc:
                code = getattr(exc, "winerror", None)
                if code is None and exc.args:
                    code = exc.args[0]
                if code == ERROR_HOTKEY_ALREADY_REGISTERED:
                    if binding.override:
                        candidates.append((binding, combo))
                        statuses[binding.id] = HotkeyStatus("hook", "等待钩子接管")
                    else:
                        statuses[binding.id] = HotkeyStatus(
                            "conflict", "快捷键已被系统或其它程序占用"
                        )
                else:
                    statuses[binding.id] = HotkeyStatus("error", f"注册失败（错误码 {code}）")
            else:
                self._hotkey_ids[index] = binding.id
                statuses[binding.id] = HotkeyStatus("registered", "系统热键")

        if candidates:
            try:
                self._install_hook()
            except Exception as exc:
                self.log(f"键盘钩子安装失败：{exc}")
                for binding, _combo in candidates:
                    statuses[binding.id] = HotkeyStatus(
                        "conflict", f"快捷键冲突，且钩子安装失败：{exc}"
                    )
            else:
                for binding, combo in candidates:
                    self._hook_table[combo] = binding.id
                    statuses[binding.id] = HotkeyStatus(
                        "hook", "与系统快捷键冲突 → 已用键盘钩子覆盖"
                    )

        if not self._hook_table and not self._capture_active:
            self._uninstall_hook()
        return statuses

    def _unregister_hotkeys(self) -> None:
        for hotkey_id in list(self._hotkey_ids):
            try:
                win32gui.UnregisterHotKey(self.hwnd, hotkey_id)
            except Exception:
                pass
        self._hotkey_ids.clear()
        self._hook_table.clear()

    def _install_hook(self) -> None:
        if self.hook:
            return
        hook = winapi.KeyboardHook(self._on_hook_key)
        hook.install()
        self.hook = hook

    def _uninstall_hook(self) -> None:
        if self.hook:
            try:
                self.hook.uninstall()
            except Exception:
                pass
            self.hook = None

    def _hook_needed(self) -> bool:
        return bool(self._hook_table) or self._capture_active

    # -- 事件分发 -----------------------------------------------------------
    def _fire(self, hotkey_id: int) -> None:
        binding_id = self._hotkey_ids.get(hotkey_id)
        if not binding_id:
            return
        now = time.monotonic()
        if now - self._fire_times.get(binding_id, 0.0) < DEBOUNCE_SECONDS:
            return
        self._fire_times[binding_id] = now
        try:
            self.on_fire(binding_id)
        except Exception as exc:
            self.log(f"触发快捷键失败：{exc}")

    def _on_hook_key(self, vk: int, mods: int) -> bool:
        """低级钩子回调：返回 True 时这次按键被吞掉。"""
        if self._capture_active:
            if not winapi.foregound_belongs_to_us():
                return False
            if vk == win32con.VK_ESCAPE:
                self._capture_active = False
                if not self._hook_needed():
                    self._uninstall_hook()
                self.on_capture(None)
                return True
            combo_text = keys.format_hotkey(mods, vk)
            self._capture_active = False
            if not self._hook_needed():
                self._uninstall_hook()
            self.on_capture(combo_text)
            return True

        binding_id = self._hook_table.get((mods, vk))
        if not binding_id:
            return False
        now = time.monotonic()
        if now - self._fire_times.get(binding_id, 0.0) < DEBOUNCE_SECONDS:
            return True  # 吞掉连发，避免连续触发
        self._fire_times[binding_id] = now
        try:
            self.on_fire(binding_id)
        except Exception:
            pass
        return True

    # -- 录制快捷键 ---------------------------------------------------------
    def begin_capture(self) -> bool:
        def _begin():
            self._capture_active = True
            self._install_hook()
            return True

        return bool(self.submit(_begin))

    def cancel_capture(self) -> None:
        def _cancel():
            self._capture_active = False
            if not self._hook_needed():
                self._uninstall_hook()

        try:
            self.submit(_cancel, timeout=3.0)
        except Exception:
            pass

    def notify(self, title: str, text: str, warning: bool = False) -> None:
        def _notify():
            if self.tray:
                self.tray.notify(title, text, warning=warning)

        try:
            self.submit(_notify, timeout=3.0)
        except Exception:
            pass


class HotkeyManager:
    """界面层使用的快捷键管理器 Facade。"""

    def __init__(
        self,
        on_fire,
        on_tray_click=None,
        on_capture=None,
        on_tray_ready=None,
        on_show=None,
        log=None,
        icon_path: str = "",
    ) -> None:
        self._tray_ready = threading.Event()
        self._tray_ok = False
        self._tray_callback = on_tray_ready
        self._log = log or (lambda message: None)
        self._pump = MessagePump(
            on_fire=on_fire,
            on_tray_click=on_tray_click,
            on_capture=on_capture,
            on_tray_ready=self._handle_tray_ready,
            on_show=on_show,
            log=log,
            icon_path=icon_path,
        )
        self._pump_ready = False

    def _handle_tray_ready(self, ok: bool) -> None:
        self.mark_tray(ok)
        if self._tray_callback:
            try:
                self._tray_callback(ok)
            except Exception:
                pass

    def start(self) -> None:
        self._pump.start()
        if not self._pump._ready.wait(5.0):
            raise RuntimeError("消息泵线程启动超时")
        self._pump_ready = True

    def wait_tray(self, timeout: float = 2.0) -> bool:
        self._tray_ready.wait(timeout)
        return self._tray_ok

    def mark_tray(self, ok: bool) -> None:
        self._tray_ok = ok
        self._tray_ready.set()

    def apply(self, bindings) -> dict:
        return self._pump.apply(bindings)

    def begin_capture(self) -> bool:
        return self._pump.begin_capture()

    def cancel_capture(self) -> None:
        self._pump.cancel_capture()

    def capture(self) -> None:
        """进入一次性的快捷键录制状态，结果通过 on_capture 回调返回。"""
        try:
            self._pump.begin_capture()
        except Exception as exc:
            self._log(f"无法进入录制状态：{exc}")

    def notify(self, title: str, text: str, warning: bool = False) -> None:
        self._pump.notify(title, text, warning)

    def stop(self) -> None:
        if self._pump_ready:
            self._pump.request_stop()
            self._pump_ready = False
