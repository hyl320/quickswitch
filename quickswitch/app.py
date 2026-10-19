"""QuickSwitch 图形界面（Tkinter）。

界面线程只负责显示；所有 Win32 快捷键/窗口操作都由消息泵线程或工作线程完成，
两者通过 ``queue.Queue`` 通信。
"""

from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import win32api
import win32con
import win32gui

from . import config as config_mod
from . import hotkeys as hotkeys_mod
from . import keys as keys_mod
from . import matcher as matcher_mod
from . import winapi
from .config import Binding, Config, WHEN_ACTIVATE, WHEN_ACTIVATE_OR_LAUNCH, WHEN_LAUNCH

APP_TITLE = "QuickSwitch · 窗口速切"
APP_VERSION = "1.2.0"

# Dense Pro 视觉主题（经典桌面工具风：浅灰、方角、紧凑、深蓝选中）
THEME = {
    "bg": "#e8e8e8",
    "panel": "#ffffff",
    "header": "#d4d0c8",
    "border": "#9a9a9a",
    "border_dark": "#7f7f7f",
    "text": "#111111",
    "muted": "#555555",
    "off": "#888888",
    "accent": "#0a246a",
    "ok": "#075e1e",
    "hook": "#8a4b00",
    "bad": "#a40000",
    "ui_family": "Segoe UI",
    "mono_family": "Consolas",
}
ICON_FILENAME = "quickswitch.ico"

STATE_TAGS = {
    "registered": "state_ok",
    "hook": "state_hook",
    "conflict": "state_bad",
    "error": "state_bad",
    "disabled": "state_muted",
}


def icon_path() -> str:
    return os.path.join(config_mod.resource_dir(), "assets", ICON_FILENAME)


# ---------------------------------------------------------------------------
# 通用对话框
# ---------------------------------------------------------------------------

class ModalDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str) -> None:
        super().__init__(parent)
        self.withdraw()
        self.configure(bg=THEME["bg"])
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self._parent = parent

    def run(self, focus_widget: tk.Widget | None = None):
        self.update_idletasks()
        parent = self._parent
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
        except Exception:
            px = py = 0
            pw = ph = 0
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = px + max(0, (pw - w) // 2)
        y = py + max(0, (ph - h) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        if focus_widget is not None:
            focus_widget.focus_set()
        self.wait_window()
        return self.result



class ToolTip:
    """Treeview 悬停提示（显示完整标题等被截断的信息）。"""

    def __init__(self, widget, text_provider, delay: int = 450) -> None:
        self.widget = widget
        self.provider = text_provider
        self.delay = delay
        self._job = None
        self._tip = None
        self._item = None
        widget.bind("<Motion>", self._on_motion, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _on_motion(self, event):
        item = self.widget.identify_row(event.y)
        if not item:
            self._hide()
            return
        if self._tip is not None and self._item == item:
            return
        self._hide()
        self._job = self.widget.after(self.delay, lambda: self._show(item, event))

    def _show(self, item, event) -> None:
        self._job = None
        try:
            text = self.provider(item)
        except Exception:
            return
        if not text:
            return
        self._item = item
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        try:
            self._tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(
            self._tip,
            text=text,
            justify="left",
            bg="#ffffe1",
            fg="#111111",
            relief="solid",
            bd=1,
            padx=6,
            pady=4,
            font=(THEME["ui_family"], 9),
            wraplength=620,
        ).pack()
        x = int(event.x_root + 16)
        y = int(event.y_root + 18)
        self._tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _event=None) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None
            self._item = None


class BindingDialog(ModalDialog):
    """新增 / 编辑一条快捷键绑定。"""

    def __init__(self, app: "MainWindow", binding: Binding | None, preset_window=None) -> None:
        super().__init__(app.root, "编辑快捷键绑定" if binding else "新增快捷键绑定")
        self.app = app
        self._editing_id = binding.id if binding else None
        self._recording = False
        self._record_job = None
        self._capture_job = None
        self._status_job = None

        source = binding or Binding()
        if preset_window is not None and not binding:
            source.process = preset_window.process
            source.title = re.escape(preset_window.title.strip())

        self.var_name = tk.StringVar(value=source.name)
        self.var_hotkey = tk.StringVar(value=source.hotkey)
        self.var_enabled = tk.BooleanVar(value=source.enabled)
        self.var_process = tk.StringVar(value=source.process)
        self.var_title = tk.StringVar(value=source.title)
        self.var_class = tk.StringVar(value=source.window_class)
        self.var_exclude = tk.StringVar(value=source.exclude_title)
        self.var_index_mode = tk.StringVar(value="fixed" if source.index > 0 else "cycle")
        self.var_index = tk.IntVar(value=source.index if source.index > 0 else 1)
        self.var_override = tk.BooleanVar(value=source.override)
        self.var_when = tk.StringVar(value=source.when)
        self.var_cwd = tk.StringVar(value=source.launch_cwd)
        self.var_launch_gap = tk.StringVar(value=f"{source.launch_gap:g}")
        self.var_wait = tk.StringVar(value=f"{source.wait_seconds:g}")
        self.var_record_hint = tk.StringVar(value="点击右侧「录制」后按下组合键")
        self.var_test = tk.StringVar(value="")

        self._build_ui(source)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self._center_width = 700

    # -- UI -----------------------------------------------------------------
    def _build_ui(self, source: Binding) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)

        basic = ttk.LabelFrame(outer, text="  基本信息  ", padding=10)
        basic.pack(fill="x")
        basic.columnconfigure(1, weight=1)

        ttk.Label(basic, text="名称").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(basic, textvariable=self.var_name, width=32).grid(
            row=0, column=1, sticky="we", padx=(8, 8), pady=4
        )
        tk.Checkbutton(
            basic, text="启用", variable=self.var_enabled,
            bg=THEME["bg"], activebackground=THEME["bg"], selectcolor="#ffffff",
            bd=0, highlightthickness=0, font=(THEME["ui_family"], 9),
        ).grid(row=0, column=2, sticky="w", pady=4)

        ttk.Label(basic, text="快捷键").grid(row=1, column=0, sticky="w", pady=4)
        hotkey_row = ttk.Frame(basic)
        hotkey_row.grid(row=1, column=1, sticky="we", padx=(8, 8), pady=4)
        self.hotkey_entry = ttk.Entry(
            hotkey_row, textvariable=self.var_hotkey, state="readonly", width=22, font=("Consolas", 10)
        )
        self.hotkey_entry.pack(side="left")
        ttk.Button(hotkey_row, text="录制", width=6, command=self._toggle_record).pack(
            side="left", padx=(6, 0)
        )
        self.record_hint_label = ttk.Label(
            hotkey_row, textvariable=self.var_record_hint, foreground=THEME["muted"]
        )
        self.record_hint_label.pack(side="left", padx=(10, 0))
        tk.Checkbutton(
            basic, text="冲突时覆盖", variable=self.var_override,
            bg=THEME["bg"], activebackground=THEME["bg"], selectcolor="#ffffff",
            bd=0, highlightthickness=0, font=(THEME["ui_family"], 9),
        ).grid(row=1, column=2, sticky="w", pady=4)

        match = ttk.LabelFrame(outer, text="  切换目标（窗口匹配，标题支持正则）  ", padding=10)
        match.pack(fill="x", pady=(10, 0))
        match.columnconfigure(1, weight=1)

        rows = (
            ("进程名", self.var_process, "如 Code.exe、WindowsTerminal.exe、*Terminal*.exe"),
            ("标题正则", self.var_title, "如 项目A|quickswitch（留空表示不限）"),
            ("窗口类", self.var_class, "一般留空；如 CASCADIA_HOSTING_WINDOW_CLASS"),
            ("排除标题", self.var_exclude, "如 下载|设置（留空表示不排除）"),
        )
        for row_index, (label, var, hint) in enumerate(rows):
            ttk.Label(match, text=label).grid(row=row_index, column=0, sticky="w", pady=3)
            entry = ttk.Entry(match, textvariable=var)
            entry.grid(row=row_index, column=1, sticky="we", padx=(8, 8), pady=3)
            ttk.Label(match, text=hint, foreground=THEME["muted"]).grid(
                row=row_index, column=2, sticky="w", pady=3
            )

        mode_row = ttk.Frame(match)
        mode_row.grid(row=len(rows), column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Label(mode_row, text="匹配到多个窗口：").pack(side="left")
        ttk.Radiobutton(
            mode_row, text="循环切换（同一快捷键反复按）", variable=self.var_index_mode, value="cycle"
        ).pack(side="left")
        ttk.Radiobutton(
            mode_row, text="固定第", variable=self.var_index_mode, value="fixed"
        ).pack(side="left", padx=(12, 2))
        ttk.Spinbox(mode_row, from_=1, to=99, width=4, textvariable=self.var_index).pack(side="left")
        ttk.Label(mode_row, text="个（按窗口创建顺序）").pack(side="left")

        fill_row = ttk.Frame(match)
        fill_row.grid(row=len(rows) + 1, column=0, columnspan=3, sticky="we", pady=(8, 0))
        ttk.Button(
            fill_row, text="从当前前台窗口填充…", command=self._fill_from_foreground
        ).pack(side="left")
        ttk.Label(
            fill_row,
            text="点击后 3 秒内切到目标窗口，会自动填入进程名和标题",
            foreground=THEME["muted"],
        ).pack(side="left", padx=(8, 0))

        launch = ttk.LabelFrame(outer, text="  找不到窗口时（可选）  ", padding=10)
        launch.pack(fill="x", pady=(10, 0))
        launch.columnconfigure(1, weight=1)

        when_row = ttk.Frame(launch)
        when_row.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        for when in (WHEN_ACTIVATE, WHEN_ACTIVATE_OR_LAUNCH, WHEN_LAUNCH):
            ttk.Radiobutton(
                when_row, text=config_mod.WHEN_LABELS[when], variable=self.var_when, value=when
            ).pack(side="left", padx=(0, 12))

        ttk.Label(launch, text="启动命令").grid(row=1, column=0, sticky="nw", pady=3)
        command_box = ttk.Frame(launch)
        command_box.grid(row=1, column=1, sticky="nsew", padx=(8, 8), pady=3)
        command_box.columnconfigure(0, weight=1)
        command_box.rowconfigure(0, weight=1)
        self.launch_text = tk.Text(
            command_box,
            height=4,
            width=46,
            wrap="none",
            relief=tk.SUNKEN,
            borderwidth=1,
            bg="#ffffff",
            fg=THEME["text"],
            font=(THEME["mono_family"], 9),
        )
        self.launch_text.grid(row=0, column=0, sticky="nsew")
        launch_scroll = ttk.Scrollbar(
            command_box, orient="vertical", command=self.launch_text.yview
        )
        launch_scroll.grid(row=0, column=1, sticky="ns")
        self.launch_text.configure(yscrollcommand=launch_scroll.set)
        self.launch_text.insert("1.0", source.launch)
        ttk.Label(
            launch,
            text="每行一条，按顺序执行\n例如：wt.exe -w 0 nt ...\ncode D:\\proj",
            foreground=THEME["muted"],
            justify="left",
        ).grid(row=1, column=2, sticky="nw", pady=3)

        ttk.Label(launch, text="工作目录").grid(row=2, column=0, sticky="w", pady=3)
        cwd_row = ttk.Frame(launch)
        cwd_row.grid(row=2, column=1, sticky="we", padx=(8, 8), pady=3)
        cwd_row.columnconfigure(0, weight=1)
        ttk.Entry(cwd_row, textvariable=self.var_cwd).grid(row=0, column=0, sticky="we")
        ttk.Button(cwd_row, text="浏览…", width=7, command=self._browse_cwd).grid(
            row=0, column=1, padx=(6, 0)
        )
        ttk.Label(launch, text="等待窗口出现").grid(row=3, column=0, sticky="w", pady=3)
        wait_row = ttk.Frame(launch)
        wait_row.grid(row=3, column=1, sticky="w", padx=(8, 8), pady=3)
        ttk.Spinbox(wait_row, from_=1, to=120, width=5, textvariable=self.var_wait).pack(side="left")
        ttk.Label(wait_row, text="秒（启动后自动切换过去）").pack(side="left", padx=(6, 0))
        ttk.Label(launch, text="命令间隔").grid(row=4, column=0, sticky="w", pady=3)
        gap_row = ttk.Frame(launch)
        gap_row.grid(row=4, column=1, sticky="w", padx=(8, 8), pady=3)
        ttk.Spinbox(
            gap_row, from_=0, to=30, increment=0.5, width=5, textvariable=self.var_launch_gap
        ).pack(side="left")
        ttk.Label(gap_row, text="秒（多条命令之间的等待，默认 0.8）").pack(
            side="left", padx=(6, 0)
        )

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(12, 0))
        ttk.Label(bottom, textvariable=self.var_test, foreground="#005a9e").pack(side="left")
        ttk.Button(bottom, text="取消", width=8, command=self._cancel).pack(side="right")
        ttk.Button(bottom, text="保存", width=8, command=self._save).pack(side="right", padx=(0, 8))
        ttk.Button(bottom, text="测试切换", width=10, command=self._test).pack(
            side="right", padx=(0, 8)
        )

    # -- 录制快捷键 ---------------------------------------------------------
    def _toggle_record(self) -> None:
        if self._recording:
            self._finish_record(None)
            return
        self._recording = True
        self.app.set_capture_target(self)
        self.var_record_hint.set("请在 8 秒内按下组合键…（Esc 取消）")
        self.app.manager.capture()
        self._record_job = self.after(8000, lambda: self._finish_record(None))

    def on_capture_result(self, combo: str | None) -> None:
        if not self._recording:
            return
        self._finish_record(combo)

    def _finish_record(self, combo: str | None) -> None:
        self._recording = False
        self.app.set_capture_target(None)
        if self._record_job:
            try:
                self.after_cancel(self._record_job)
            except Exception:
                pass
            self._record_job = None
        self.app.manager.cancel_capture()
        if combo:
            self.var_hotkey.set(combo)
            self.var_record_hint.set("已录制，可再次点击「录制」修改")
        else:
            self.var_record_hint.set("未录制到组合键")

    # -- 其它按钮 -----------------------------------------------------------
    def _browse_cwd(self) -> None:
        directory = filedialog.askdirectory(parent=self, title="选择工作目录")
        if directory:
            self.var_cwd.set(directory)

    def _fill_from_foreground(self) -> None:
        self.withdraw()
        self.var_record_hint.set("3 秒后捕获前台窗口…")
        self._capture_job = self.after(3000, self._capture_foreground_now)

    def _capture_foreground_now(self) -> None:
        self._capture_job = None
        window = winapi.foreground_window_info()
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        try:
            self.focus_force()
        except Exception:
            pass
        if not window or window.pid == os.getpid():
            self.var_test.set("没有捕获到前台窗口（仍是本程序），请重试")
            return
        self.var_process.set(window.process)
        self.var_title.set(re.escape(window.title.strip())[:200])
        self.var_class.set("")
        if not self.var_name.get().strip():
            self.var_name.set(window.title.strip()[:28] or window.process)
        self.var_test.set(
            f"已填入：{window.process} · {window.short_title(36)}（标题可按需精简）"
        )

    # -- 校验与保存 ---------------------------------------------------------
    def _collect(self, check_match: bool = True) -> Binding:
        hotkey = self.var_hotkey.get().strip()
        if not hotkey:
            raise ValueError("请先录制或填写快捷键")
        mods, vk = keys_mod.parse_hotkey(hotkey)  # 可能抛 ValueError
        del mods, vk
        binding = Binding(
            id=self._editing_id or config_mod.new_id(),
            name=self.var_name.get().strip(),
            hotkey=keys_mod.normalize_hotkey(hotkey),
            enabled=bool(self.var_enabled.get()),
            process=self.var_process.get().strip(),
            title=self.var_title.get().strip(),
            window_class=self.var_class.get().strip(),
            exclude_title=self.var_exclude.get().strip(),
            index=int(self.var_index.get() or 0) if self.var_index_mode.get() == "fixed" else 0,
            override=bool(self.var_override.get()),
            when=self.var_when.get(),
            launch="\n".join(self._launch_lines()),
            launch_cwd=self.var_cwd.get().strip(),
            launch_gap=_parse_float(self.var_launch_gap.get(), 0.8),
            wait_seconds=_parse_float(self.var_wait.get(), 8.0),
        )
        if self.var_index_mode.get() == "fixed" and binding.index < 1:
            raise ValueError("固定序号至少为 1")
        matcher_mod.compile_rule(binding)  # 校验正则
        if check_match and binding.when in (WHEN_ACTIVATE, WHEN_ACTIVATE_OR_LAUNCH):
            if not (binding.process or binding.title or binding.window_class):
                raise ValueError("切换窗口需要至少填写进程、标题或窗口类之一")
        if binding.when in (WHEN_ACTIVATE_OR_LAUNCH, WHEN_LAUNCH) and not binding.launch_commands():
            raise ValueError("所选模式需要至少一条启动命令（每行一条）")
        return binding

    def _launch_lines(self) -> list[str]:
        text = self.launch_text.get("1.0", "end")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return [line.strip() for line in text.split("\n") if line.strip()]

    def _test(self) -> None:
        try:
            binding = self._collect(check_match=False)
            rule = matcher_mod.compile_rule(binding)
        except ValueError as exc:
            self.var_test.set(f"✗ {exc}")
            return
        windows = [w for w in winapi.list_windows() if w.pid != os.getpid()]
        matches = matcher_mod.find_matches(rule, windows)
        if not matches:
            hint = "（会执行启动命令）" if binding.launch_commands() else ""
            self.var_test.set(f"✗ 当前没有匹配窗口{hint}")
            return
        target = matcher_mod.choose_target(rule, matches, winapi.foreground_window())
        if target is None:
            self.var_test.set("✗ 固定序号超出范围")
            return
        ok = winapi.activate_window(target.hwnd)
        prefix = "✓ 已切换到" if ok else "⚠ 找到但切换失败："
        self.var_test.set(f"{prefix} {target.short_title(40)}")

    def _save(self) -> None:
        try:
            binding = self._collect()
        except ValueError as exc:
            self.var_test.set(f"✗ {exc}")
            return
        if self.app.hotkey_taken(binding.hotkey, self._editing_id):
            self.var_test.set(f"✗ 快捷键 {binding.hotkey} 已被其它绑定使用")
            return
        self.result = binding
        self.destroy()

    def _cancel(self) -> None:
        if self._recording:
            self._finish_record(None)
        if self._capture_job:
            try:
                self.after_cancel(self._capture_job)
            except Exception:
                pass
        self.result = None
        self.destroy()


class BatchBindDialog(ModalDialog):
    """把多个已选窗口按顺序绑定到一组数字快捷键。"""

    def __init__(self, app: "MainWindow", windows: list) -> None:
        super().__init__(app.root, "批量绑定快捷键")
        self.app = app
        self.windows = sorted(windows, key=lambda w: (w.process.lower(), w.hwnd))
        self._recording = False
        self._record_job = None

        self.var_start = tk.StringVar(value="Ctrl+1")
        self.var_step = tk.IntVar(value=1)
        self.var_naming = tk.StringVar(value="标题")
        self.var_hint = tk.StringVar(value="")

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)

        info = ttk.Label(
            outer,
            text=f"将按「进程 → 创建顺序」为 {len(self.windows)} 个窗口生成绑定：",
        )
        info.pack(anchor="w")

        listing = tk.Text(
            outer, height=min(8, len(self.windows)), width=78,
            relief=tk.SUNKEN, bd=1, bg="#ffffff", fg=THEME["text"],
            font=(THEME["mono_family"], 9),
        )
        listing.pack(fill="x", pady=(6, 10))
        for window in self.windows:
            listing.insert("end", f"  {window.process}  |  {window.short_title(64)}\n")
        listing.configure(state="disabled")

        form = ttk.Frame(outer)
        form.pack(fill="x")

        ttk.Label(form, text="起始快捷键").grid(row=0, column=0, sticky="w", pady=4)
        hotkey_row = ttk.Frame(form)
        hotkey_row.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Entry(
            hotkey_row, textvariable=self.var_start, state="readonly", width=18, font=("Consolas", 10)
        ).pack(side="left")
        ttk.Button(hotkey_row, text="录制", width=6, command=self._toggle_record).pack(
            side="left", padx=(6, 0)
        )

        ttk.Label(form, text="步进").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(form, from_=1, to=9, width=4, textvariable=self.var_step).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=4
        )

        ttk.Label(form, text="名称来源").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(
            form,
            textvariable=self.var_naming,
            values=("标题", "进程名"),
            state="readonly",
            width=10,
        ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=4)

        ttk.Label(
            outer,
            text="说明：绑定使用「进程名 + 固定序号」，窗口关闭/重开可能导致序号变化，可在列表中继续微调。",
            foreground=THEME["muted"],
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(outer, textvariable=self.var_hint, foreground="#dc2626").pack(
            anchor="w", pady=(4, 0)
        )

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Button(bottom, text="取消", width=8, command=self._cancel).pack(side="right")
        ttk.Button(bottom, text="生成", width=8, command=self._generate).pack(
            side="right", padx=(0, 8)
        )

        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _toggle_record(self) -> None:
        if self._recording:
            self._finish_record(None)
            return
        self._recording = True
        self.app.set_capture_target(self)
        self.app.manager.capture()
        self.var_hint.set("请按下起始快捷键…（Esc 取消）")
        self._record_job = self.after(8000, lambda: self._finish_record(None))

    def on_capture_result(self, combo: str | None) -> None:
        if self._recording:
            self._finish_record(combo)

    def _finish_record(self, combo: str | None) -> None:
        self._recording = False
        self.app.set_capture_target(None)
        if self._record_job:
            try:
                self.after_cancel(self._record_job)
            except Exception:
                pass
            self._record_job = None
        self.app.manager.cancel_capture()
        if combo:
            self.var_start.set(combo)
            self.var_hint.set("")
        else:
            self.var_hint.set("未录制到组合键")

    def _generate(self) -> None:
        try:
            start = keys_mod.normalize_hotkey(self.var_start.get())
        except ValueError as exc:
            self.var_hint.set(f"起始快捷键无效：{exc}")
            return
        step = max(1, int(self.var_step.get() or 1))
        ordinals = self.app.process_ordinals()
        bindings: list[Binding] = []
        current = start
        for window in self.windows:
            if current is None:
                self.var_hint.set("快捷键序列已用尽（数字到 9 / F 键到 F24），请改用 F 键或减少勾选")
                return
            index = ordinals.get(window.hwnd, 1)
            name = (
                window.short_title(28)
                if self.var_naming.get() == "标题"
                else f"{os.path.splitext(window.process)[0]} #{index}"
            )
            bindings.append(
                Binding(
                    id=config_mod.new_id(),
                    name=name or window.process,
                    hotkey=current,
                    enabled=True,
                    process=window.process,
                    index=index,
                    override=True,
                    when=WHEN_ACTIVATE,
                )
            )
            current = keys_mod.increment_hotkey(current, step)
        self.result = bindings
        self.destroy()

    def _cancel(self) -> None:
        if self._recording:
            self._finish_record(None)
        self.result = None
        self.destroy()


def _parse_float(text: str, default: float) -> float:
    try:
        return max(0.0, min(120.0, float(str(text).strip())))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class MainWindow:
    def __init__(self, root: tk.Tk, start_hidden: bool = False) -> None:
        self.root = root
        self.start_hidden = start_hidden
        self._quitting = False
        self._events: queue.Queue = queue.Queue()
        self._windows: list[winapi.WindowInfo] = []
        self._statuses: dict[str, hotkeys_mod.HotkeyStatus] = {}
        self._binding_lookup: dict[str, Binding] = {}
        self._capture_target = None
        self._tray_ok = False
        self._tray_notified = False
        self._busy: set[str] = set()
        self._busy_lock = threading.Lock()
        self._config_error = ""

        self._load_config()
        self._setup_root()
        self._build_menu()
        self._build_ui()

        self.manager = hotkeys_mod.HotkeyManager(
            on_fire=self._on_fire,
            on_tray_click=self._on_tray_click,
            on_capture=self._on_capture,
            on_tray_ready=self._on_tray_ready,
            on_show=self._on_show_request,
            log=lambda message: self.log(message, "warn"),
            icon_path=icon_path(),
        )
        try:
            self.manager.start()
        except Exception as exc:  # pragma: no cover
            self.log(f"快捷键服务启动失败：{exc}", "error")

        self._tray_ok = self.manager.wait_tray(2.0)
        self._refresh_windows()
        self._refresh_bindings()
        self._apply(save=False, quiet=True)

        if self._config_error:
            self.log(self._config_error, "error")
        self.log(f"配置文件：{config_mod.config_path()}")
        if self.config.bindings:
            self.log(f"已加载 {len(self.config.bindings)} 条绑定。")
        else:
            self.log("还没有绑定：在右侧列表选中窗口 → 点击「绑定所选窗口」，然后按提示录制快捷键。")

        hidden = self.start_hidden or self.config.start_minimized
        if hidden and self._tray_ok:
            self.root.withdraw()
        self.root.after(40, self._poll_events)

    # -- 初始化 -------------------------------------------------------------
    def _load_config(self) -> None:
        try:
            self.config = config_mod.load()
        except Exception as exc:
            self.config = Config()
            self._config_error = (
                f"读取配置文件失败：{exc}。已临时使用空白配置，原文件不会被覆盖。"
            )

    def _setup_root(self) -> None:
        root = self.root
        root.title(APP_TITLE)
        root.geometry("1200x780")
        root.minsize(1000, 620)
        root.configure(bg=THEME["bg"])
        try:
            root.iconbitmap(default=icon_path())
        except Exception:
            pass

        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            try:
                tkfont.nametofont(name).configure(family=THEME["ui_family"], size=9)
            except Exception:
                pass
        try:
            tkfont.nametofont("TkHeadingFont").configure(
                family=THEME["ui_family"], size=9, weight="bold"
            )
        except Exception:
            pass
        try:
            tkfont.nametofont("TkFixedFont").configure(family=THEME["mono_family"], size=9)
        except Exception:
            pass

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            ".", background=THEME["bg"], foreground=THEME["text"],
            font=(THEME["ui_family"], 9),
        )
        style.configure("TFrame", background=THEME["bg"])
        style.configure("TLabel", background=THEME["bg"], foreground=THEME["text"])
        style.configure("Muted.TLabel", foreground=THEME["muted"])

        style.configure(
            "Tool.TButton", padding=(7, 1), font=(THEME["ui_family"], 9),
            background="#f2f2f2", foreground=THEME["text"],
            bordercolor=THEME["border"], lightcolor="#ffffff", darkcolor=THEME["border"],
            relief="raised", borderwidth=1,
        )
        style.map("Tool.TButton", background=[("pressed", "#d8d8d8"), ("active", "#fafafa")])
        style.configure(
            "Primary.TButton", padding=(7, 1), font=(THEME["ui_family"], 9),
            background="#dce8f5", foreground=THEME["text"],
            bordercolor="#6f96c0", lightcolor="#ffffff", darkcolor="#6f96c0",
            relief="raised", borderwidth=1,
        )
        style.map("Primary.TButton", background=[("pressed", "#c5d9ee"), ("active", "#e8f0fa")])
        style.configure(
            "TButton", padding=(6, 1), font=(THEME["ui_family"], 9),
            background="#f2f2f2", foreground=THEME["text"],
            bordercolor=THEME["border"], lightcolor="#ffffff", darkcolor=THEME["border"],
            relief="raised", borderwidth=1,
        )
        style.configure(
            "Treeview", background="#ffffff", fieldbackground="#ffffff",
            foreground=THEME["text"], rowheight=21, borderwidth=1, relief="solid",
            font=(THEME["ui_family"], 9),
        )
        style.configure(
            "Treeview.Heading", background="#e6e6e6", foreground=THEME["text"],
            relief="raised", borderwidth=1, font=(THEME["ui_family"], 9, "bold"),
            padding=(4, 1),
        )
        style.map("Treeview.Heading", background=[("active", "#dcdcdc")])
        style.map(
            "Treeview",
            background=[("selected", THEME["accent"])],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "TLabelframe", background=THEME["bg"], bordercolor=THEME["border"],
            relief="groove", borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label", background=THEME["bg"], foreground=THEME["text"],
            font=(THEME["ui_family"], 9, "bold"),
        )
        style.configure(
            "TEntry", fieldbackground="#ffffff", foreground=THEME["text"],
            bordercolor=THEME["border_dark"], lightcolor="#ffffff", darkcolor=THEME["border"],
            padding=1,
        )
        style.configure(
            "TSpinbox", fieldbackground="#ffffff", foreground=THEME["text"],
            bordercolor=THEME["border_dark"], lightcolor="#ffffff", darkcolor=THEME["border"],
            arrowcolor=THEME["text"], padding=1,
        )
        style.configure(
            "TCombobox", fieldbackground="#ffffff", foreground=THEME["text"],
            background="#f2f2f2", bordercolor=THEME["border_dark"], arrowcolor=THEME["text"],
            padding=2,
        )
        style.configure("TCheckbutton", background=THEME["bg"], foreground=THEME["text"])
        style.map("TCheckbutton", background=[("active", THEME["bg"])])
        style.configure("TRadiobutton", background=THEME["bg"], foreground=THEME["text"])
        style.map("TRadiobutton", background=[("active", THEME["bg"])])
        style.configure("TSeparator", background="#b8b8b8")

        root.report_callback_exception = self._report_callback_exception
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="保存并应用", accelerator="Ctrl+S", command=lambda: self._apply(save=True))
        file_menu.add_command(label="重新加载配置", accelerator="Ctrl+R", command=self._reload_config)
        file_menu.add_separator()
        file_menu.add_command(label="打开配置文件", command=self._open_config_file)
        file_menu.add_command(label="打开程序目录", command=lambda: winapi.open_path(config_mod.app_dir()))
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._quit)
        menubar.add_cascade(label="文件", menu=file_menu)

        tools = tk.Menu(menubar, tearoff=0)
        self.var_autostart = tk.BooleanVar(value=winapi.is_startup_enabled())
        self.var_start_min = tk.BooleanVar(value=self.config.start_minimized)
        self.var_close_tray = tk.BooleanVar(value=self.config.close_to_tray)
        self.var_notify = tk.BooleanVar(value=self.config.notifications)
        tools.add_checkbutton(
            label="开机自启（登录后自动运行）",
            variable=self.var_autostart,
            command=self._toggle_autostart,
        )
        tools.add_checkbutton(
            label="启动时最小化到托盘",
            variable=self.var_start_min,
            command=lambda: self._toggle_option("start_minimized", self.var_start_min),
        )
        tools.add_checkbutton(
            label="关闭窗口时最小化到托盘",
            variable=self.var_close_tray,
            command=lambda: self._toggle_option("close_to_tray", self.var_close_tray),
        )
        tools.add_checkbutton(
            label="托盘气泡通知",
            variable=self.var_notify,
            command=lambda: self._toggle_option("notifications", self.var_notify),
        )
        tools.add_separator()
        tools.add_command(label="打开日志文件", command=self._open_log_file)
        menubar.add_cascade(label="工具", menu=tools)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="使用说明", command=self._show_help)
        help_menu.add_command(label="快捷键冲突说明", command=self._show_conflict_help)
        help_menu.add_separator()
        help_menu.add_command(label="关于 QuickSwitch", command=self._show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.root.config(menu=menubar)

    def _make_pane(self, parent, title: str, hint: str = ""):
        """带经典灰色标题条的面板，返回 (pane, body, head)。"""
        pane = tk.Frame(parent, bg=THEME["bg"])
        pane.configure(highlightthickness=1, highlightbackground=THEME["border"])
        head = tk.Frame(pane, bg=THEME["header"], height=22)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(
            head, text=title, bg=THEME["header"], fg=THEME["text"],
            font=(THEME["ui_family"], 9, "bold"),
        ).pack(side="left", padx=(6, 0))
        if hint:
            tk.Label(
                head, text=hint, bg=THEME["header"], fg=THEME["muted"],
                font=(THEME["ui_family"], 8),
            ).pack(side="right", padx=(0, 6))
        body = tk.Frame(pane, bg=THEME["panel"])
        body.pack(fill="both", expand=True)
        return pane, body, head

    def _status_segment(self, parent, side: str = "left", expand: bool = False):
        label = tk.Label(
            parent, text="", relief=tk.SUNKEN, bd=1, anchor="w",
            bg="#f2f2f2", fg=THEME["text"], padx=6, pady=0,
            font=(THEME["ui_family"], 9),
        )
        label.pack(side=side, fill="x", expand=expand, padx=(0, 2))
        return label

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(6, 4, 6, 4))
        toolbar.pack(fill="x")
        for text, command in (
            ("＋ 新增", self._add_binding),
            ("编辑", self._edit_selected),
            ("删除", self._delete_selected),
        ):
            ttk.Button(toolbar, text=text, command=command, style="Tool.TButton").pack(
                side="left", padx=(0, 3)
            )
        ttk.Button(toolbar, text="↑", width=3, command=lambda: self._move_selected(-1)).pack(
            side="left", padx=(0, 2)
        )
        ttk.Button(toolbar, text="↓", width=3, command=lambda: self._move_selected(1)).pack(
            side="left", padx=(0, 3)
        )
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(
            toolbar, text="保存并应用", command=lambda: self._apply(save=True),
            style="Primary.TButton",
        ).pack(side="left")
        ttk.Button(
            toolbar, text="重新加载", command=self._reload_config, style="Tool.TButton"
        ).pack(side="left", padx=(3, 0))
        tk.Checkbutton(
            toolbar, text="开机自启", variable=self.var_autostart,
            command=self._toggle_autostart, bg=THEME["bg"], activebackground=THEME["bg"],
            selectcolor="#ffffff", bd=0, highlightthickness=0,
            font=(THEME["ui_family"], 9),
        ).pack(side="right")

        self._banner = tk.Frame(
            self.root, bg="#ffffe1", highlightthickness=1, highlightbackground="#c8b560"
        )
        tk.Label(
            self._banner,
            text=(
                "还没有快捷键绑定：① 右侧「刷新」 ② 选中目标窗口 ③ 点「绑定所选」，"
                "再录制快捷键（例如 Ctrl+Alt+1）"
            ),
            bg="#ffffe1", fg="#333333",
        ).pack(side="left", padx=8, pady=4)
        banner_help = tk.Label(
            self._banner, text="查看使用说明", bg="#ffffe1", fg="#005a9e",
            cursor="hand2", font=(THEME["ui_family"], 9, "underline"),
        )
        banner_help.pack(side="right", padx=(0, 8))
        banner_help.bind("<Button-1>", lambda _e: self._show_help())
        self._banner.pack(fill="x", padx=4)

        paned = ttk.PanedWindow(self.root, orient="horizontal")
        self._paned = paned
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        # 左：快捷键绑定
        left_pane, left_body, _ = self._make_pane(paned, "快捷键绑定", "双击编辑 · 右键更多")
        paned.add(left_pane, weight=3)
        left_body.columnconfigure(0, weight=1)
        left_body.rowconfigure(0, weight=1)

        self.tree_bindings = ttk.Treeview(
            left_body,
            columns=("hotkey", "name", "target", "status"),
            show="headings",
            selectmode="browse",
        )
        for column, title, width, stretch in (
            ("hotkey", "快捷键", 166, False),
            ("name", "名称", 116, False),
            ("target", "目标窗口", 164, True),
            ("status", "状态", 76, False),
        ):
            self.tree_bindings.heading(column, text=title, anchor="w")
            self.tree_bindings.column(column, width=width, stretch=stretch, anchor="w")
        self.tree_bindings.grid(row=0, column=0, sticky="nsew")
        bind_scroll = ttk.Scrollbar(left_body, orient="vertical", command=self.tree_bindings.yview)
        bind_scroll.grid(row=0, column=1, sticky="ns")
        self.tree_bindings.configure(yscrollcommand=bind_scroll.set)
        self.tree_bindings.tag_configure("state_ok", foreground=THEME["text"])
        self.tree_bindings.tag_configure("state_hook", foreground=THEME["hook"])
        self.tree_bindings.tag_configure("state_bad", foreground=THEME["bad"])
        self.tree_bindings.tag_configure("state_muted", foreground=THEME["off"])
        ToolTip(self.tree_bindings, self._binding_tooltip)
        self.tree_bindings.bind("<Double-1>", lambda _e: self._edit_selected())
        self.tree_bindings.bind("<Delete>", lambda _e: self._delete_selected())

        self._bind_menu = tk.Menu(self.root, tearoff=0)
        self._bind_menu.add_command(label="编辑", command=self._edit_selected)
        self._bind_menu.add_command(label="立即切换一次", command=self._test_selected)
        self._bind_menu.add_separator()
        self._bind_menu.add_command(label="删除", command=self._delete_selected)
        self.tree_bindings.bind("<Button-3>", self._show_bind_menu)

        # 右：当前窗口
        right_pane, right_body, _ = self._make_pane(paned, "当前窗口", "双击绑定")
        paned.add(right_pane, weight=2)
        right_body.columnconfigure(0, weight=1)
        right_body.rowconfigure(1, weight=1)

        filter_row = tk.Frame(right_body, bg=THEME["panel"])
        filter_row.grid(row=0, column=0, columnspan=2, sticky="we", padx=3, pady=3)
        tk.Label(filter_row, text="过滤", bg=THEME["panel"], fg=THEME["muted"]).pack(side="left")
        self.var_filter = tk.StringVar()
        ttk.Entry(filter_row, textvariable=self.var_filter, width=16).pack(
            side="left", padx=(4, 4)
        )
        self.var_filter.trace_add("write", lambda *_a: self._populate_window_tree())
        ttk.Button(filter_row, text="刷新", command=self._refresh_windows).pack(side="left")
        ttk.Button(filter_row, text="绑定所选", command=self._bind_selected_windows).pack(
            side="right"
        )

        self.tree_windows = ttk.Treeview(
            right_body,
            columns=("title", "process", "min"),
            show="headings",
            selectmode="extended",
        )
        for column, title, width, stretch in (
            ("title", "窗口标题", 150, True),
            ("process", "进程", 150, False),
            ("min", "最小化", 74, False),
        ):
            self.tree_windows.heading(column, text=title, anchor="w")
            self.tree_windows.column(column, width=width, stretch=stretch, anchor="w")
        self.tree_windows.grid(row=1, column=0, sticky="nsew")
        win_scroll = ttk.Scrollbar(right_body, orient="vertical", command=self.tree_windows.yview)
        win_scroll.grid(row=1, column=1, sticky="ns")
        self.tree_windows.configure(yscrollcommand=win_scroll.set)
        self.tree_windows.tag_configure("minimized", foreground=THEME["off"])
        ToolTip(self.tree_windows, self._window_tooltip)
        self.tree_windows.bind("<Double-1>", lambda _e: self._bind_selected_windows())

        self._window_menu = tk.Menu(self.root, tearoff=0)
        self._window_menu.add_command(label="绑定到快捷键…", command=self._bind_selected_windows)
        self._window_menu.add_command(label="立即切换到该窗口", command=self._activate_selected_window)
        self._window_menu.add_separator()
        self._window_menu.add_command(label="复制窗口标题", command=self._copy_window_title)
        self.tree_windows.bind("<Button-3>", self._show_window_menu)

        # 运行日志
        log_pane, log_body, log_head = self._make_pane(self.root, "运行日志")
        log_pane.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(log_head, text="清空", command=self._clear_log, style="Tool.TButton").pack(
            side="right", padx=(3, 3)
        )
        ttk.Button(log_head, text="日志文件", command=self._open_log_file, style="Tool.TButton").pack(
            side="right", padx=(3, 0)
        )
        log_body.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_body, height=5, wrap="none", relief=tk.SUNKEN, bd=1,
            bg="#ffffff", fg=THEME["text"], font=(THEME["mono_family"], 9),
            insertbackground=THEME["text"], state="disabled",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.tag_configure("time", foreground=THEME["off"])
        self.log_text.tag_configure("warn", foreground=THEME["hook"])
        self.log_text.tag_configure("error", foreground=THEME["bad"])

        # 状态栏（分段沉雕面板）
        status_bar = tk.Frame(self.root, bg=THEME["bg"])
        status_bar.pack(fill="x", padx=4, pady=(0, 4))
        self.seg_config = self._status_segment(status_bar, expand=True)
        self.seg_ok = self._status_segment(status_bar)
        self.seg_hook = self._status_segment(status_bar)
        self.seg_off = self._status_segment(status_bar)
        self.seg_bad = self._status_segment(status_bar)
        self.seg_windows = self._status_segment(status_bar)
        self.seg_right = self._status_segment(status_bar, side="right")

        self.root.bind("<Control-s>", lambda _e: self._apply(save=True))
        self.root.bind("<Control-r>", lambda _e: self._reload_config())
        self.root.bind("<F5>", lambda _e: self._refresh_windows())

        self._update_status()

    # -- 日志 ---------------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        if threading.current_thread() is not threading.main_thread():
            self._events.put(("log", (level, message)))
            return
        self._append_log(message, level)

    def _append_log(self, message: str, level: str = "info") -> None:
        if not hasattr(self, "log_text"):
            return
        try:
            self.log_text.configure(state="normal")
            stamp = time.strftime("%H:%M:%S")
            self.log_text.insert("end", f"[{stamp}] ", "time")
            tag = level if level in ("warn", "error") else ""
            self.log_text.insert("end", message + "\n", tag)
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > 600:
                self.log_text.delete("1.0", "120.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            return
        try:
            logging.getLogger("quickswitch").log(
                {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}.get(
                    level, logging.INFO
                ),
                message,
            )
        except Exception:
            pass

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # -- 窗口列表 -----------------------------------------------------------
    def _refresh_windows(self) -> None:
        try:
            self._windows = winapi.list_windows()
        except Exception as exc:
            self.log(f"枚举窗口失败：{exc}", "error")
            self._windows = []
        self._populate_window_tree()

    def _populate_window_tree(self) -> None:
        keyword = self.var_filter.get().strip().lower()
        selected = set(self.tree_windows.selection())
        self.tree_windows.delete(*self.tree_windows.get_children())
        count = 0
        for window in self._windows:
            if keyword and keyword not in window.title.lower() and keyword not in window.process.lower():
                continue
            self.tree_windows.insert(
                "",
                "end",
                iid=str(window.hwnd),
                values=(
                    window.short_title(100),
                    window.process,
                    "是" if window.minimized else "",
                ),
                tags=("minimized",) if window.minimized else (),
            )
            count += 1
        keep = [item for item in selected if self.tree_windows.exists(item)]
        if keep:
            self.tree_windows.selection_set(keep)
        self._window_count = count

    def _selected_windows(self) -> list[winapi.WindowInfo]:
        selected: list[winapi.WindowInfo] = []
        by_hwnd = {window.hwnd: window for window in self._windows}
        for item in self.tree_windows.selection():
            try:
                hwnd = int(item)
            except ValueError:
                continue
            window = by_hwnd.get(hwnd)
            if window:
                selected.append(window)
        return selected

    def process_ordinals(self) -> dict[int, int]:
        """按（进程名, 窗口句柄）排序后，每个窗口在同进程内的序号（1 起）。"""
        ordered = sorted(self._windows, key=lambda w: (w.process.lower(), w.hwnd))
        counters: dict[str, int] = {}
        result: dict[int, int] = {}
        for window in ordered:
            key = window.process.lower()
            counters[key] = counters.get(key, 0) + 1
            result[window.hwnd] = counters[key]
        return result

    # -- 绑定列表 -----------------------------------------------------------
    def _refresh_bindings(self) -> None:
        self._binding_lookup = {binding.id: binding for binding in self.config.bindings}
        selected = self.tree_bindings.selection()
        self.tree_bindings.delete(*self.tree_bindings.get_children())
        for binding in self.config.bindings:
            status = self._statuses.get(binding.id)
            if not binding.enabled:
                label = "未启用"
                tag = "state_muted"
            elif status is None:
                label = "待应用"
                tag = "state_muted"
            else:
                label = status.label
                tag = STATE_TAGS.get(status.state, "state_muted")
            self.tree_bindings.insert(
                "",
                "end",
                iid=binding.id,
                values=(
                    binding.hotkey or "—",
                    binding.display_name(),
                    binding.match_summary(),
                    label,
                ),
                tags=(tag,),
            )
        keep = [item for item in selected if self.tree_bindings.exists(item)]
        if keep:
            self.tree_bindings.selection_set(keep)
        if self.config.bindings:
            self._banner.pack_forget()
        else:
            self._banner.pack(fill="x", padx=4, before=self._paned)
        self._update_status()

    def _binding_tooltip(self, item: str) -> str:
        binding = self._binding_lookup.get(item)
        if binding is None:
            return ""
        status = self._statuses.get(item)
        when = config_mod.WHEN_LABELS.get(binding.when, binding.when)
        lines = [
            binding.display_name(),
            f"快捷键：{binding.hotkey}",
            f"匹配：{binding.match_summary()}",
            f"动作：{when}",
        ]
        if status is not None:
            lines.append(f"状态：{status.label}" + (f"（{status.detail}）" if status.detail else ""))
        commands = binding.launch_commands()
        if commands:
            preview = "；".join(commands[:3]) + ("…" if len(commands) > 3 else "")
            lines.append(f"启动（{len(commands)} 条）：{preview}")
        return "\n".join(lines)

    def _window_tooltip(self, item: str) -> str:
        try:
            hwnd = int(item)
        except ValueError:
            return ""
        window = next((w for w in self._windows if w.hwnd == hwnd), None)
        if window is None:
            return ""
        return (
            f"{window.title}\n"
            f"进程：{window.process}\n"
            f"类名：{window.class_name}\n"
            f"句柄：0x{window.hwnd:X}"
        )

    def _selected_binding(self) -> Binding | None:
        selection = self.tree_bindings.selection()
        if not selection:
            return None
        return self._binding_lookup.get(selection[0])

    def _update_status(self) -> None:
        statuses = list(self._statuses.values())
        ok = sum(1 for status in statuses if status.ok)
        bad = sum(1 for status in statuses if status.state in ("conflict", "error"))
        enabled = sum(1 for binding in self.config.bindings if binding.enabled)
        hook = sum(1 for status in statuses if status.state == "hook")
        self.seg_config.configure(text=f"配置：{config_mod.config_path()}")
        self.seg_ok.configure(text=f"已注册 {ok}")
        self.seg_hook.configure(text=f"钩子 {hook}")
        self.seg_off.configure(text=f"启用 {enabled}")
        self.seg_bad.configure(text=f"异常 {bad}")
        self.seg_windows.configure(text=f"窗口 {len(self._windows)}")
        tray = "托盘就绪" if self._tray_ok else "托盘不可用"
        self.seg_right.configure(text=f"{tray} · v{APP_VERSION}")

    def hotkey_taken(self, hotkey: str, exclude_id: str | None) -> bool:
        try:
            combo = keys_mod.parse_hotkey(hotkey)
        except ValueError:
            return False
        for binding in self.config.bindings:
            if binding.id == exclude_id:
                continue
            try:
                if keys_mod.parse_hotkey(binding.hotkey) == combo:
                    return True
            except ValueError:
                continue
        return False

    # -- 快捷键应用 ---------------------------------------------------------
    def _apply(self, save: bool = True, quiet: bool = False) -> bool:
        problems = config_mod.validate_bindings(self.config.bindings)
        by_binding: dict[str, list[str]] = {}
        for binding_id, message in problems:
            by_binding.setdefault(binding_id, []).append(message)

        try:
            statuses = self.manager.apply(self.config.bindings)
        except Exception as exc:
            self.log(f"应用快捷键失败：{exc}", "error")
            return False
        self._statuses = statuses
        for binding_id, messages in by_binding.items():
            self._statuses[binding_id] = hotkeys_mod.HotkeyStatus(
                "error", "；".join(messages)
            )

        for binding in self.config.bindings:
            if binding.enabled and binding.launch_cwd and not os.path.isdir(binding.launch_cwd):
                self.log(
                    f"「{binding.display_name()}」工作目录不存在：{binding.launch_cwd}", "warn"
                )

        if save:
            try:
                path = config_mod.save(self.config)
                if not quiet:
                    self.log(f"配置已保存：{path}")
            except Exception as exc:
                self.log(f"保存配置失败：{exc}", "error")
                return False

        self._refresh_bindings()
        ok = sum(1 for status in statuses.values() if status.ok)
        bad = sum(1 for status in statuses.values() if status.state in ("conflict", "error"))
        if not quiet:
            self.log(f"快捷键已应用：正常 {ok} 个，异常 {bad} 个。")
        for binding_id, messages in by_binding.items():
            binding = self.config.binding_by_id(binding_id)
            name = binding.display_name() if binding else binding_id
            self.log(f"「{name}」配置问题：{'；'.join(messages)}", "error")
        for binding_id, status in statuses.items():
            if status.state in ("conflict", "error"):
                binding = self.config.binding_by_id(binding_id)
                name = binding.display_name() if binding else binding_id
                self.log(f"「{name}」快捷键不可用：{status.detail}", "warn")

        return True

    def _reload_config(self) -> None:
        try:
            config = config_mod.load()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"重新加载配置失败：\n{exc}", parent=self.root)
            return
        self.config = config
        self.var_start_min.set(config.start_minimized)
        self.var_close_tray.set(config.close_to_tray)
        self.var_notify.set(config.notifications)
        self.log("配置已重新加载。")
        self._apply(save=False)

    # -- 绑定操作 -----------------------------------------------------------
    def _add_binding(self) -> None:
        dialog = BindingDialog(self, None)
        binding = dialog.run()
        if not binding:
            return
        self.config.bindings.append(binding)
        self._apply(save=True)
        if self.tree_bindings.exists(binding.id):
            self.tree_bindings.selection_set(binding.id)
            self.tree_bindings.see(binding.id)

    def _edit_selected(self) -> None:
        binding = self._selected_binding()
        if not binding:
            messagebox.showinfo(APP_TITLE, "请先在左侧列表中选择一条绑定。", parent=self.root)
            return
        dialog = BindingDialog(self, binding)
        updated = dialog.run()
        if not updated:
            return
        index = self.config.bindings.index(binding)
        self.config.bindings[index] = updated
        self._apply(save=True)
        if self.tree_bindings.exists(updated.id):
            self.tree_bindings.selection_set(updated.id)

    def _delete_selected(self) -> None:
        binding = self._selected_binding()
        if not binding:
            return
        if not messagebox.askyesno(
            APP_TITLE, f"删除绑定「{binding.display_name()}」？", parent=self.root
        ):
            return
        self.config.bindings.remove(binding)
        self._apply(save=True)

    def _move_selected(self, delta: int) -> None:
        binding = self._selected_binding()
        if not binding:
            return
        index = self.config.bindings.index(binding)
        new_index = index + delta
        if new_index < 0 or new_index >= len(self.config.bindings):
            return
        self.config.bindings[index], self.config.bindings[new_index] = (
            self.config.bindings[new_index],
            self.config.bindings[index],
        )
        self._apply(save=True)
        self.tree_bindings.selection_set(binding.id)

    def _test_selected(self) -> None:
        binding = self._selected_binding()
        if binding:
            self._execute(binding)

    def _show_bind_menu(self, event) -> None:
        item = self.tree_bindings.identify_row(event.y)
        if item:
            self.tree_bindings.selection_set(item)
            self._bind_menu.tk_popup(event.x_root, event.y_root)

    def _bind_selected_windows(self) -> None:
        windows = self._selected_windows()
        if not windows:
            messagebox.showinfo(
                APP_TITLE, "请先在右侧「当前窗口」列表中选择要绑定的窗口。", parent=self.root
            )
            return
        if len(windows) == 1:
            dialog = BindingDialog(self, None, preset_window=windows[0])
            binding = dialog.run()
            if binding:
                self.config.bindings.append(binding)
                self._apply(save=True)
            return
        dialog = BatchBindDialog(self, windows)
        bindings = dialog.run()
        if bindings:
            self.config.bindings.extend(bindings)
            self._apply(save=True)

    def _activate_selected_window(self) -> None:
        windows = self._selected_windows()
        if not windows:
            return
        target = windows[0]
        if winapi.activate_window(target.hwnd):
            self.log(f"已切换：{target.short_title(60)}")
        else:
            self.log(f"切换到「{target.short_title(60)}」失败", "warn")

    def _copy_window_title(self) -> None:
        windows = self._selected_windows()
        if not windows:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(windows[0].title)
        self.log("窗口标题已复制到剪贴板。")

    def _show_window_menu(self, event) -> None:
        item = self.tree_windows.identify_row(event.y)
        if item:
            if item not in self.tree_windows.selection():
                self.tree_windows.selection_set(item)
            self._window_menu.tk_popup(event.x_root, event.y_root)

    # -- 快捷键触发 ---------------------------------------------------------
    def _on_fire(self, binding_id: str) -> None:
        self._events.put(("fire", binding_id))

    def _on_tray_click(self, lparam: int) -> None:
        self._events.put(("tray", lparam))

    def _on_capture(self, combo: str | None) -> None:
        self._events.put(("capture", combo))

    def _on_tray_ready(self, ok: bool) -> None:
        self._events.put(("tray_ready", ok))

    def _on_show_request(self) -> None:
        self._events.put(("show", None))

    def set_capture_target(self, dialog) -> None:
        self._capture_target = dialog

    def _start_execute(self, binding_id: str) -> None:
        binding = self._binding_lookup.get(binding_id)
        if not binding:
            return
        with self._busy_lock:
            if binding_id in self._busy:
                return
            self._busy.add(binding_id)
        thread = threading.Thread(
            target=self._execute, args=(binding,), name=f"QuickSwitch-{binding_id}", daemon=True
        )
        thread.start()

    def _execute(self, binding: Binding) -> None:
        try:
            rule = None
            matches: list[winapi.WindowInfo] = []
            if binding.when != WHEN_LAUNCH:
                try:
                    rule = matcher_mod.compile_rule(binding)
                except ValueError as exc:
                    self.log(f"「{binding.display_name()}」规则无效：{exc}", "error")
                    return
                matches = matcher_mod.find_matches(rule, winapi.list_windows())
                target = matcher_mod.choose_target(rule, matches, winapi.foreground_window())
                if target is not None:
                    if winapi.activate_window(target.hwnd):
                        self.log(f"切换 → {target.short_title(60)}")
                    else:
                        self.log(f"找到「{target.short_title(60)}」但切换失败", "warn")
                    return
                if binding.when == WHEN_ACTIVATE:
                    if matches:
                        self.log(f"「{binding.display_name()}」匹配窗口无法切到前台", "warn")
                    else:
                        self.log(f"未找到匹配窗口：{binding.display_name()}", "warn")
                    return

            commands = binding.launch_commands()
            if not commands:
                self.log(f"「{binding.display_name()}」没有配置启动命令", "warn")
                return

            launched = 0
            for index, command in enumerate(commands, start=1):
                prefix = f"[{index}/{len(commands)}] " if len(commands) > 1 else ""
                self.log(f"启动{prefix}：{command}")
                try:
                    winapi.launch_command(command, binding.launch_cwd)
                    launched += 1
                except Exception as exc:
                    self.log(f"启动失败：{command}（{exc}）", "error")
                if index < len(commands) and binding.launch_gap > 0:
                    time.sleep(binding.launch_gap)
            if launched == 0:
                self.log("所有启动命令都失败了", "error")
                return
            if binding.when == WHEN_LAUNCH:
                return
            if rule is None:
                return
            window = winapi.wait_for_window(
                lambda w: rule.matches(w), timeout=binding.wait_seconds or 8.0
            )
            if window is None:
                self.log(f"已启动，但等待 {binding.wait_seconds:g} 秒仍未发现目标窗口", "warn")
                return
            if winapi.activate_window(window.hwnd):
                self.log(f"启动并切换 → {window.short_title(60)}")
            else:
                self.log(f"已启动，但切换到「{window.short_title(60)}」失败", "warn")
        except Exception as exc:  # 兜底，避免工作线程异常静默丢失
            self.log(f"执行绑定时出错：{exc}", "error")
        finally:
            with self._busy_lock:
                self._busy.discard(binding.id)

    # -- 事件循环 -----------------------------------------------------------
    def _poll_events(self) -> None:
        if self._quitting:
            return
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "log":
                    level, message = payload
                    self._append_log(str(message), str(level))
                elif kind == "fire":
                    self._start_execute(str(payload))
                elif kind == "tray":
                    self._handle_tray(int(payload))
                elif kind == "capture":
                    target = self._capture_target
                    self._capture_target = None
                    if target is not None:
                        try:
                            if target.winfo_exists():
                                target.on_capture_result(payload)
                        except Exception:
                            pass
                elif kind == "tray_ready":
                    self._tray_ok = bool(payload)
                    self._update_status()
                    if not self._tray_ok:
                        self.log("系统托盘不可用，关闭窗口将直接退出。", "warn")
                elif kind == "show":
                    self._show_window()
            except Exception as exc:
                self._append_log(f"处理界面事件出错：{exc}", "error")
        try:
            self.root.after(40, self._poll_events)
        except tk.TclError:
            pass

    def _handle_tray(self, lparam: int) -> None:
        if lparam in (winapi.WM_LBUTTONUP, winapi.WM_LBUTTONDBLCLK):
            self._show_window()
            return
        if lparam != winapi.WM_RBUTTONUP:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="显示主窗口", command=self._show_window)
        menu.add_command(label="保存并应用", command=lambda: self._apply(save=True))
        menu.add_command(label="重新加载配置", command=self._reload_config)
        menu.add_separator()
        menu.add_command(label="退出 QuickSwitch", command=self._quit)
        try:
            x, y = win32api.GetCursorPos()
            menu.tk_popup(int(x), int(y))
        finally:
            menu.grab_release()

    def _show_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self._refresh_windows()
        except tk.TclError:
            pass

    # -- 设置与帮助 ---------------------------------------------------------
    def _toggle_option(self, field_name: str, variable: tk.BooleanVar) -> None:
        setattr(self.config, field_name, bool(variable.get()))
        self._save_quiet()

    def _toggle_autostart(self) -> None:
        enabled = bool(self.var_autostart.get())
        try:
            winapi.set_startup(enabled)
            self.config.start_with_windows = enabled
            self._save_quiet()
            self.log(f"开机自启已{'开启' if enabled else '关闭'}。")
        except Exception as exc:
            self.var_autostart.set(not enabled)
            messagebox.showerror(APP_TITLE, f"设置开机自启失败：\n{exc}", parent=self.root)

    def _save_quiet(self) -> None:
        try:
            config_mod.save(self.config)
        except Exception as exc:
            self.log(f"保存配置失败：{exc}", "error")

    def _open_config_file(self) -> None:
        path = config_mod.config_path()
        if not os.path.exists(path):
            self._save_quiet()
        try:
            winapi.open_path(path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法打开配置文件：\n{exc}", parent=self.root)

    def _open_log_file(self) -> None:
        path = os.path.join(config_mod.app_dir(), "logs", "quickswitch.log")
        if not os.path.exists(path):
            self.log("日志文件尚未生成。")
            return
        try:
            winapi.open_path(path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法打开日志文件：\n{exc}", parent=self.root)

    def _show_help(self) -> None:
        messagebox.showinfo(
            "QuickSwitch 使用说明",
            "1. 点右侧「刷新」，在「当前窗口」里选中目标窗口（可多选）。\n"
            "2. 点「绑定所选窗口」：一个窗口直接编辑；多个窗口会批量生成 Ctrl+1、Ctrl+2…\n"
            "3. 在编辑框里点「录制」，按下你想要的组合键（Esc 取消）。\n"
            "4. 保存后立即生效，窗口开关不影响使用；找不到窗口时可按配置自动启动。\n\n"
            "小技巧：\n"
            "· 想切回同一个窗口用「固定第 N 个」；想在多个终端间轮换用「循环切换」。\n"
            "· 按 Ctrl+S 保存，Ctrl+R 重新加载配置，F5 刷新窗口列表。\n"
            "· 关闭窗口默认收进托盘，双击托盘图标可重新打开。\n\n"
            "注意：Ctrl+1 这类组合是全局生效的，会覆盖浏览器/IDE 里的同名快捷键，"
            "建议优先使用 Ctrl+Alt+数字 或 Ctrl+Shift+数字。",
            parent=self.root,
        )

    def _show_conflict_help(self) -> None:
        messagebox.showinfo(
            "快捷键冲突说明",
            "QuickSwitch 会先向系统注册快捷键：\n"
            "· 注册成功 → 状态显示「已注册」，按键不会传给其它程序。\n"
            "· 组合键已被系统或别的软件占用 → 若勾选了「冲突时覆盖」，"
            "程序会安装低级键盘钩子，在按键进入系统前把它拦下来，状态显示「钩子接管」。\n"
            "· 若未勾选覆盖 → 状态显示「冲突」，请换一个组合键。\n\n"
            "无法覆盖的组合：Ctrl+Alt+Del、Win+L 等由系统安全机制直接处理的按键。\n"
            "另外，如果目标程序以管理员身份运行，请也让 QuickSwitch 以管理员身份运行，"
            "否则钩子可能无法拦截发送到高权限窗口的按键。",
            parent=self.root,
        )

    def _show_about(self) -> None:
        messagebox.showinfo(
            "关于 QuickSwitch",
            f"QuickSwitch v{APP_VERSION}\n"
            "Windows 窗口/IDE/Agent 快捷切换器\n\n"
            "· 全局快捷键切换任意窗口\n"
            "· 与系统快捷键冲突时可用键盘钩子覆盖\n"
            "· 找不到窗口时自动启动\n"
            "· 配置保存于程序目录 config.json\n\n"
            f"Python {sys.version.split()[0]} · pywin32 · Tkinter",
            parent=self.root,
        )

    # -- 退出 ---------------------------------------------------------------
    def _on_close(self) -> None:
        if self._tray_ok and self.config.close_to_tray and not self._quitting:
            self.root.withdraw()
            if self.config.notifications and not self._tray_notified:
                self._tray_notified = True
                self.manager.notify("QuickSwitch 仍在运行", "双击托盘图标可以重新打开窗口。")
            return
        self._quit()

    def _quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            self.manager.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        import traceback

        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        self._append_log(f"界面异常：{exc_value}", "error")
        try:
            logging.getLogger("quickswitch").error("界面异常\n%s", detail)
        except Exception:
            pass
        if not getattr(self, "_exception_dialog_shown", False):
            self._exception_dialog_shown = True
            try:
                messagebox.showerror(
                    APP_TITLE,
                    f"操作失败：{exc_value}\n\n详细信息已写入运行日志。",
                    parent=self.root,
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _setup_logging() -> None:
    log_dir = os.path.join(config_mod.app_dir(), "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "quickswitch.log"),
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger = logging.getLogger("quickswitch")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
    except Exception:
        pass


def _capture_window_png(hwnd: int, path: str) -> None:
    """把指定窗口内容保存为 PNG（用 PrintWindow，不受遮挡影响）。"""
    import ctypes as _ctypes

    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)
    _ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
    info = bitmap.GetInfo()
    bits = bitmap.GetBitmapBits(True)
    Image.frombuffer(
        "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
    ).save(path)
    win32gui.DeleteObject(bitmap.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)


def _top_level_hwnd(widget: tk.Misc) -> int:
    try:
        hwnd = widget.winfo_id()
        root = ctypes.windll.user32.GetAncestor(hwnd, 2)  # GA_ROOT
        return int(root or hwnd)
    except Exception:
        return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    # PyInstaller --windowed / pythonw 下 stdout/stderr 为 None，避免 --selftest / --smoke 崩溃
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout

    parser = argparse.ArgumentParser(prog="QuickSwitch", add_help=True)
    parser.add_argument("--hidden", action="store_true", help="启动时隐藏到托盘")
    parser.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--screenshot", default="", help=argparse.SUPPRESS)
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.selftest:
        from .selftest import run_selftest

        return run_selftest()

    _enable_dpi_awareness()
    if not winapi.ensure_single_instance():
        winapi.notify_running_instance()
        return 0

    _setup_logging()
    root = tk.Tk()
    app = MainWindow(root, start_hidden=args.hidden)

    if args.smoke:
        shot = args.screenshot or os.path.join(config_mod.app_dir(), "docs", "screenshot.png")

        def _smoke_quit() -> None:
            try:
                hwnd = _top_level_hwnd(root)
                os.makedirs(os.path.dirname(os.path.abspath(shot)), exist_ok=True)
                _capture_window_png(hwnd, shot)
                print(f"SMOKE_SCREENSHOT {shot}")
            except Exception as exc:
                print(f"SMOKE_SCREENSHOT_ERROR {exc}")
            finally:
                app._quit()

        root.after(1800, _smoke_quit)

    try:
        root.mainloop()
    finally:
        try:
            app.manager.stop()
        except Exception:
            pass
    return 0

