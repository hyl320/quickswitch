"""Win32 封装：窗口枚举、前台激活、托盘、全局键盘钩子、开机自启、单实例。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
from dataclasses import dataclass

import win32api
import win32con
import win32gui
import win32process

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
except OSError:  # pragma: no cover - 极老系统才没有 dwmapi
    dwmapi = None

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
HHOOK = ctypes.c_void_p
HWND = wt.HWND

WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WH_KEYBOARD_LL = 13
ASFW_ANY = 0xFFFFFFFF
DWMWA_CLOAKED = 14

CREATE_NO_WINDOW = 0x08000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

EXCLUDED_CLASSES = {
    "Progman",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "WorkerW",
    "Windows.UI.Core.CoreWindow",
    "XamlExplorerHostIslandWindow",
    "MultitaskingViewFrame",
    "ForegroundStaging",
    "IME",
    "MSCTFIME UI",
}
EXCLUDED_PROCESSES = {
    "textinputhost.exe",
    "applicationframehost.exe",  # UWP 外壳窗口，真正的标题在子窗口里
    "shellexperiencehost.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
}

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
user32.AllowSetForegroundWindow.argtypes = [wt.DWORD]
user32.AllowSetForegroundWindow.restype = wt.BOOL
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
user32.GetForegroundWindow.restype = HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = HWND
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.PostMessageW.argtypes = [HWND, wt.UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.SendMessageW.argtypes = [HWND, wt.UINT, WPARAM, LPARAM]
user32.SendMessageW.restype = LRESULT
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE,
    wt.DWORD,
    wt.LPWSTR,
    ctypes.POINTER(wt.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    process: str
    minimized: bool = False

    def short_title(self, limit: int = 80) -> str:
        title = self.title.strip()
        return title if len(title) <= limit else title[: limit - 1] + "…"


_process_cache: dict[int, tuple[str, float]] = {}


def process_name(pid: int) -> str:
    """根据进程 PID 取可执行文件名（带 3 秒缓存）。"""
    if pid <= 0:
        return ""
    now = time.monotonic()
    cached = _process_cache.get(pid)
    if cached and now - cached[1] < 3.0:
        return cached[0]
    name = ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    else:
        # 提权进程可能打不开，退回 pywin32（需要更多权限，通常拿不到）
        try:
            handle2 = win32api.OpenProcess(0x0410, False, pid)  # VM_READ | QUERY_INFORMATION
            name = os.path.basename(win32process.GetModuleFileNameEx(handle2, 0))
            win32api.CloseHandle(handle2)
        except Exception:
            name = ""
    _process_cache[pid] = (name, now)
    return name


def _is_cloaked(hwnd: int) -> bool:
    if dwmapi is None:
        return False
    value = ctypes.c_int(0)
    try:
        hresult = dwmapi.DwmGetWindowAttribute(
            HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        return False
    return hresult == 0 and value.value != 0


def is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(win32gui.IsWindow(hwnd))


def window_pid(hwnd: int) -> int:
    if not is_window(hwnd):
        return 0
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return 0


def get_window_info(hwnd: int) -> WindowInfo | None:
    if not is_window(hwnd):
        return None
    try:
        title = win32gui.GetWindowText(hwnd)
        class_name = win32gui.GetClassName(hwnd)
    except Exception:
        return None
    pid = window_pid(hwnd)
    try:
        minimized = bool(win32gui.IsIconic(hwnd))
    except Exception:
        minimized = False
    return WindowInfo(
        hwnd=hwnd,
        title=title,
        class_name=class_name,
        pid=pid,
        process=process_name(pid),
        minimized=minimized,
    )


def list_windows(exclude_pids: set[int] | None = None) -> list[WindowInfo]:
    """按 Z 序（从上到下）返回所有可切换的顶层窗口。"""
    exclude = set(exclude_pids or ())
    exclude.add(os.getpid())
    found: list[WindowInfo] = []

    def _callback(hwnd: int, _param) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title.strip():
                return True
            class_name = win32gui.GetClassName(hwnd)
            if class_name in EXCLUDED_CLASSES:
                return True
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if ex_style & win32con.WS_EX_TOOLWINDOW:
                return True
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            if pid in exclude:
                return True
            proc = process_name(pid)
            if proc.lower() in EXCLUDED_PROCESSES:
                return True
            if _is_cloaked(hwnd):
                return True
            found.append(
                WindowInfo(
                    hwnd=hwnd,
                    title=title,
                    class_name=class_name,
                    pid=pid,
                    process=proc,
                    minimized=bool(win32gui.IsIconic(hwnd)),
                )
            )
        except Exception:
            # 枚举期间窗口可能被销毁，忽略单个窗口的异常
            pass
        return True

    win32gui.EnumWindows(_callback, None)
    return found


def foreground_window() -> int:
    try:
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def foreground_window_info() -> WindowInfo | None:
    return get_window_info(foreground_window())


def foregound_belongs_to_us() -> bool:
    hwnd = foreground_window()
    return bool(hwnd) and window_pid(hwnd) == os.getpid()


def _tap_alt() -> None:
    """轻点一下 Alt，解除 Windows 的前台锁定限制。"""
    try:
        user32.keybd_event(win32con.VK_MENU, 0, 0, 0)
        time.sleep(0.015)
        user32.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.015)
    except Exception:
        pass


def activate_window(hwnd: int, verify_timeout: float = 0.35) -> bool:
    """把窗口切到前台（包含最小化恢复与前台锁绕过）。"""
    if not is_window(hwnd):
        return False
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        if foreground_window() == hwnd:
            return True

        # 尽量拿到前台权限；失败也无妨，后面还有组合手段
        try:
            user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:
            pass

        current_tid = kernel32.GetCurrentThreadId()
        attached: list[int] = []
        for other in (foreground_window(), hwnd):
            if not other:
                continue
            try:
                tid = win32process.GetWindowThreadProcessId(other)[0]
            except Exception:
                continue
            if tid and tid != current_tid and tid not in attached:
                try:
                    if user32.AttachThreadInput(current_tid, tid, True):
                        attached.append(tid)
                except Exception:
                    pass

        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        finally:
            for tid in attached:
                try:
                    user32.AttachThreadInput(current_tid, tid, False)
                except Exception:
                    pass

        deadline = time.monotonic() + verify_timeout
        while time.monotonic() < deadline:
            if foreground_window() == hwnd:
                return True
            time.sleep(0.02)

        # 最后的兜底：模拟 Alt 按键，然后再次尝试
        _tap_alt()
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            if foreground_window() == hwnd:
                return True
            time.sleep(0.02)
        return False
    except Exception:
        return False


def launch_command(command: str, cwd: str = "") -> subprocess.Popen:
    """静默启动一条命令（支持 shell 语法），返回 Popen 对象。"""
    command = command.strip()
    if not command:
        raise ValueError("启动命令为空")
    return subprocess.Popen(
        command,
        cwd=cwd or None,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=CREATE_NO_WINDOW,
    )


def wait_for_window(predicate, timeout: float = 8.0, interval: float = 0.15) -> WindowInfo | None:
    """轮询等待满足条件的窗口出现。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for window in list_windows():
            try:
                if predicate(window):
                    return window
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def open_path(path: str) -> None:
    os.startfile(path)  # noqa: S606 - 打开配置文件/目录，路径来自本程序


# ---------------------------------------------------------------------------
# 开机自启（HKCU\...\Run）
# ---------------------------------------------------------------------------

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_VALUE = "QuickSwitch"


def _launcher_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --hidden'
    executable = sys.executable
    if os.path.basename(executable).lower() == "python.exe":
        pythonw = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.exists(pythonw):
            executable = pythonw
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "QuickSwitch.pyw")
    return f'"{executable}" "{entry}" --hidden'


def is_startup_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _type = winreg.QueryValueEx(key, _STARTUP_VALUE)
    except OSError:
        return False
    return bool(value)


def set_startup(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _STARTUP_VALUE, 0, winreg.REG_SZ, _launcher_command())
        else:
            try:
                winreg.DeleteValue(key, _STARTUP_VALUE)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------

_mutex_handle = None
HIDDEN_WINDOW_CLASS = "QuickSwitchHiddenWindow"
SHOW_MESSAGE_NAME = "QuickSwitch.ShowMainWindow"


def ensure_single_instance() -> bool:
    """返回 True 表示当前进程是唯一实例。"""
    global _mutex_handle
    if _mutex_handle:
        return True
    _mutex_handle = kernel32.CreateMutexW(None, False, "Local\\QuickSwitch.SingleInstance")
    if not _mutex_handle:
        return True
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def notify_running_instance() -> bool:
    hwnd = user32.FindWindowW(HIDDEN_WINDOW_CLASS, None)
    if not hwnd:
        return False
    message = user32.RegisterWindowMessageW(SHOW_MESSAGE_NAME)
    if not message:
        return False
    user32.PostMessageW(HWND(hwnd), message, 0, 0)
    return True


# ---------------------------------------------------------------------------
# 托盘图标
# ---------------------------------------------------------------------------

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002

WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wt.HICON),
    ]


shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wt.BOOL
user32.DestroyIcon.argtypes = [wt.HICON]


class TrayIcon:
    """通过 Shell_NotifyIcon 管理托盘图标（需在拥有窗口的线程里调用）。"""

    def __init__(self, hwnd: int, callback_message: int, icon_path: str, tip: str) -> None:
        self.hwnd = hwnd
        self.callback_message = callback_message
        self.icon_path = icon_path
        self.tip = tip
        self.added = False
        self._hicon = self._load_icon(icon_path)
        self.taskbar_created_message = user32.RegisterWindowMessageW("TaskbarCreated")

    @staticmethod
    def _load_icon(path: str) -> int:
        if path and os.path.exists(path):
            try:
                handle = win32gui.LoadImage(
                    0,
                    path,
                    win32con.IMAGE_ICON,
                    0,
                    0,
                    win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
                )
                if handle:
                    return int(handle)
            except Exception:
                pass
        try:
            return int(win32gui.LoadIcon(0, win32con.IDI_APPLICATION))
        except Exception:
            return 0

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = HWND(self.hwnd)
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = self.callback_message
        data.hIcon = self._hicon
        data.szTip = self.tip[:127]
        return data

    def add(self) -> bool:
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        ok = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))
        self.added = ok
        return ok

    def ensure(self) -> bool:
        """任务栏重启后重新添加图标。"""
        if not self.added:
            return self.add()
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        ok = bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)))
        if not ok:
            self.added = False
            ok = self.add()
        return ok

    def update_tip(self, tip: str) -> None:
        self.tip = tip
        if not self.added:
            return
        data = self._data(NIF_TIP)
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def notify(self, title: str, text: str, warning: bool = False) -> None:
        if not self.added:
            return
        data = self._data(NIF_INFO | NIF_ICON | NIF_TIP | NIF_MESSAGE)
        data.szInfoTitle = title[:63]
        data.szInfo = text[:255]
        data.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def remove(self) -> None:
        if self.added:
            data = NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            data.hWnd = HWND(self.hwnd)
            data.uID = 1
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            self.added = False
        if self._hicon:
            try:
                user32.DestroyIcon(wt.HICON(self._hicon))
            except Exception:
                pass
            self._hicon = 0


# ---------------------------------------------------------------------------
# 低级键盘钩子（用于覆盖被系统/其它程序占用的快捷键）
# ---------------------------------------------------------------------------

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, ctypes.c_void_p, wt.DWORD]
user32.SetWindowsHookExW.restype = HHOOK
user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, WPARAM, LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [HHOOK]
user32.UnhookWindowsHookEx.restype = wt.BOOL


def current_modifiers() -> int:
    """读取当前按住的修饰键（跨线程可靠，不受钩子状态影响）。"""
    from . import keys as keys_module

    mods = 0
    if user32.GetAsyncKeyState(0x11) & 0x8000:  # VK_CONTROL
        mods |= keys_module.MOD_CONTROL
    if user32.GetAsyncKeyState(0x12) & 0x8000:  # VK_MENU
        mods |= keys_module.MOD_ALT
    if user32.GetAsyncKeyState(0x10) & 0x8000:  # VK_SHIFT
        mods |= keys_module.MOD_SHIFT
    if (user32.GetAsyncKeyState(0x5B) | user32.GetAsyncKeyState(0x5C)) & 0x8000:
        mods |= keys_module.MOD_WIN
    return mods


class KeyboardHook:
    """全局低级键盘钩子。必须在有消息循环的线程中安装。

    ``handler(vk, mods) -> bool``：返回 True 表示吞掉这次按键。
    """

    def __init__(self, handler) -> None:
        self._handler = handler
        self._hook = None
        self._proc = _HOOKPROC(self._call)

    @property
    def installed(self) -> bool:
        return bool(self._hook)

    def install(self) -> None:
        if self._hook:
            return
        handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hook = handle

    def uninstall(self) -> None:
        if self._hook:
            try:
                user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None

    def _call(self, n_code, w_param, l_param):
        try:
            if n_code >= 0 and (int(w_param) in (WM_KEYDOWN, WM_SYSKEYDOWN)):
                struct = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(struct.vkCode)
                from . import keys as keys_module

                if not keys_module.is_modifier_vk(vk):
                    if self._handler(vk, current_modifiers()):
                        return 1
        except Exception:
            pass
        try:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        except Exception:
            return 0
