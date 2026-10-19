"""手工 GUI 验收脚本（开发用）。

用法（需要真实桌面）：
    python tests/gui_capture_qa.py

会依次打开主窗口、绑定编辑框（含快捷键录制）和批量绑定框，
截图保存到本地 docs/（该目录已被 .gitignore 忽略）并验证「录制」与「保存」是否正确。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32api
import win32con
import win32gui
import win32ui
from PIL import Image

from quickswitch import keys as keys_mod
from quickswitch import winapi
from quickswitch.app import BatchBindDialog, BindingDialog, MainWindow, _top_level_hwnd

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
os.makedirs(OUT_DIR, exist_ok=True)
MODS = keys_mod.MOD_CONTROL | keys_mod.MOD_ALT | keys_mod.MOD_SHIFT
VK_F8 = 0x77
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def press(mods: int, vk: int) -> None:
    held = []
    for flag, code in (
        (keys_mod.MOD_CONTROL, win32con.VK_CONTROL),
        (keys_mod.MOD_ALT, win32con.VK_MENU),
        (keys_mod.MOD_SHIFT, win32con.VK_SHIFT),
    ):
        if mods & flag:
            held.append(code)
            win32api.keybd_event(code, 0, 0, 0)
            time.sleep(0.01)
    win32api.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    for code in reversed(held):
        win32api.keybd_event(code, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.01)


def capture_window(hwnd: int, path: str) -> None:
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)
    ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
    info = bitmap.GetInfo()
    bits = bitmap.GetBitmapBits(True)
    Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1).save(
        path
    )
    win32gui.DeleteObject(bitmap.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    print(f"SHOT {path} ({width}x{height})", flush=True)


def widget_hwnd(widget) -> int:
    widget.update_idletasks()
    return _top_level_hwnd(widget)


root = tk.Tk()
app = MainWindow(root)
app._show_window()
status = {"editor_shot": False, "batch_shot": False}


def open_batch() -> None:
    windows = app._windows[:4]
    if len(windows) < 2:
        check("批量绑定：至少两个窗口", False, "窗口太少")
        app._quit()
        return
    dialog = BatchBindDialog(app, windows)

    def finish() -> None:
        try:
            capture_window(widget_hwnd(dialog), os.path.join(OUT_DIR, "dialog-batch.png"))
            status["batch_shot"] = True
        finally:
            dialog.destroy()

    dialog.after(1100, finish)
    dialog.run()
    root.after(300, finish_all)


def open_editor() -> None:
    dialog = BindingDialog(app, app.config.bindings[0])

    def start_recording() -> None:
        winapi.activate_window(widget_hwnd(dialog))
        time.sleep(0.5)
        dialog._toggle_record()
        time.sleep(0.3)
        press(MODS, VK_F8)

    def verify_and_shot() -> None:
        check(
            "录制快捷键（钩子捕获）",
            dialog.var_hotkey.get() == "Ctrl+Alt+Shift+F8",
            dialog.var_hotkey.get(),
        )
        try:
            capture_window(widget_hwnd(dialog), os.path.join(OUT_DIR, "dialog-binding.png"))
            status["editor_shot"] = True
        except Exception as exc:
            check("绑定编辑框截图", False, repr(exc))
        try:
            dialog._save()
        except Exception as exc:
            check("编辑框保存", False, repr(exc))
        else:
            saved = dialog.result
            check(
                "编辑框保存",
                saved is not None and saved.hotkey == "Ctrl+Alt+Shift+F8",
                str(saved),
            )
        if dialog.winfo_exists():
            dialog.destroy()

    dialog.after(1100, start_recording)
    dialog.after(1900, verify_and_shot)
    dialog.run()
    winapi.activate_window(widget_hwnd(app.root))
    root.after(400, open_batch)


def finish_all() -> None:
    check("绑定编辑框截图", status["editor_shot"])
    check("批量绑定框截图", status["batch_shot"])
    print("GUI_QA_FAILURES", failures, flush=True)
    app._quit()


root.after(1300, open_editor)
root.mainloop()
raise SystemExit(1 if failures else 0)
