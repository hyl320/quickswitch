"""端到端验证：真实热键 / 钩子触发 → 切换到记事本窗口。"""

import os
import subprocess
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32api
import win32con
import win32gui

from quickswitch import keys as keys_mod
from quickswitch import selftest as selftest_mod
from quickswitch import winapi
from quickswitch.app import MainWindow, _top_level_hwnd

MODS = keys_mod.MOD_CONTROL | keys_mod.MOD_ALT | keys_mod.MOD_SHIFT
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (" — " + str(detail) if detail else ""))
    if not ok:
        failures.append(name)


def fg_process() -> str:
    info = winapi.get_window_info(winapi.foreground_window())
    return (info.process if info else "").lower()


def press(mods, vk, hold=0.05):
    vk_list = []
    if mods & keys_mod.MOD_CONTROL:
        vk_list.append(win32con.VK_CONTROL)
    if mods & keys_mod.MOD_ALT:
        vk_list.append(win32con.VK_MENU)
    if mods & keys_mod.MOD_SHIFT:
        vk_list.append(win32con.VK_SHIFT)
    if mods & keys_mod.MOD_WIN:
        vk_list.append(win32con.VK_LWIN)
    for v in vk_list:
        win32api.keybd_event(v, 0, 0, 0)
        time.sleep(0.01)
    win32api.keybd_event(vk, 0, 0, 0)
    time.sleep(hold)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    for v in reversed(vk_list):
        win32api.keybd_event(v, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.01)


# 1. 先由另一个线程占用 F10，用来验证钩子接管
occupier = selftest_mod._ComboOccupier(MODS, VK_F10)
occupier.start()
occupier.ready.wait(3.0)
check("占位线程注册 F10", occupier.registered)

# 2. 启动记事本作为切换目标
notepad_pids_before = selftest_mod._notepad_pids()
subprocess.Popen(["notepad.exe"])
notepad = winapi.wait_for_window(lambda w: w.process.lower() == "notepad.exe", timeout=10.0)
check("记事本窗口就绪", notepad is not None)
if not notepad:
    occupier.stop()
    raise SystemExit(1)

root = tk.Tk()
app = MainWindow(root)
app._show_window()

result = {"step": 0}


def statuses():
    return {bid: s.state for bid, s in app._statuses.items()}


def step1():
    st = statuses()
    check("F9 走系统热键注册", st.get("e2e-f9") == "registered", st)
    check("F10 冲突后走钩子接管", st.get("e2e-f10") == "hook", st)
    # 把前台切回本程序，避免误判
    winapi.activate_window(_top_level_hwnd(root))
    time.sleep(0.4)
    check("前置：前台不是记事本", winapi.foreground_window() != notepad.hwnd)
    press(MODS, VK_F9)
    root.after(1600, step2)


def step2():
    check(
        "按下 F9 后切到记事本（RegisterHotKey 路径）",
        fg_process() == "notepad.exe",
        f"foreground_process={fg_process()}",
    )
    winapi.activate_window(_top_level_hwnd(root))
    time.sleep(0.4)
    press(MODS, VK_F10)
    root.after(1600, step3)


def step3():
    check(
        "按下 F10 后切到记事本（键盘钩子覆盖路径）",
        fg_process() == "notepad.exe",
        f"foreground_process={fg_process()}",
    )
    # 顺带验证前台切换后再次按同一键仍可切回
    winapi.activate_window(_top_level_hwnd(root))
    time.sleep(0.3)
    press(MODS, VK_F11)
    root.after(1600, finish)


def finish():
    check(
        "未绑定的 F11 不产生动作",
        fg_process() != "notepad.exe",
        f"foreground_process={fg_process()}",
    )
    print("E2E_FAILURES", failures)
    app._quit()


root.after(1400, step1)
root.mainloop()

try:
    selftest_mod._close_window(notepad.hwnd)
except Exception:
    pass
for pid in selftest_mod._notepad_pids() - notepad_pids_before:
    selftest_mod._kill_pid(pid)
occupier.stop()
time.sleep(0.3)
print("DONE failures=", len(failures))
raise SystemExit(1 if failures else 0)
