"""窗口匹配规则与目标选择。"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from .config import Binding
from .winapi import WindowInfo


@dataclass
class CompiledRule:
    binding_id: str
    process: str
    title_re: re.Pattern | None
    class_sub: str
    exclude_re: re.Pattern | None
    index: int

    def matches(self, window: WindowInfo) -> bool:
        if self.process and not fnmatch.fnmatchcase(window.process.lower(), self.process):
            return False
        if self.class_sub and self.class_sub not in window.class_name.lower():
            return False
        if self.title_re and not self.title_re.search(window.title):
            return False
        if self.exclude_re and self.exclude_re.search(window.title):
            return False
        return True


def compile_rule(binding: Binding) -> CompiledRule:
    """把绑定编译成匹配规则，正则非法时抛出 ``ValueError``。"""
    title_re = None
    exclude_re = None
    if binding.title:
        try:
            title_re = re.compile(binding.title, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"标题正则无效：{exc}") from exc
    if binding.exclude_title:
        try:
            exclude_re = re.compile(binding.exclude_title, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"排除标题正则无效：{exc}") from exc
    return CompiledRule(
        binding_id=binding.id,
        process=binding.process.strip().lower(),  # fnmatch 不区分大小写：两边都转小写
        title_re=title_re,
        class_sub=binding.window_class.strip().lower(),
        exclude_re=exclude_re,
        index=max(0, int(binding.index or 0)),
    )


def find_matches(rule: CompiledRule, windows: list[WindowInfo]) -> list[WindowInfo]:
    """按传入顺序（Z 序，最上层优先）返回所有匹配窗口。"""
    result: list[WindowInfo] = []
    for window in windows:
        try:
            if rule.matches(window):
                result.append(window)
        except Exception:
            continue
    return result


def choose_target(
    rule: CompiledRule,
    matches: list[WindowInfo],
    foreground_hwnd: int,
) -> WindowInfo | None:
    """从匹配结果中选出本次要切换到的窗口。

    - ``index > 0``：按窗口句柄（约等于创建顺序）固定取第 N 个，保证每次都是同一个窗口。
    - ``index == 0``：循环切换；若当前前台窗口就是匹配项之一，则切到下一个。
    """
    if not matches:
        return None

    if rule.index > 0:
        ordered = sorted(matches, key=lambda w: w.hwnd)
        if rule.index <= len(ordered):
            return ordered[rule.index - 1]
        return None

    hwnds = [w.hwnd for w in matches]
    if foreground_hwnd in hwnds:
        position = hwnds.index(foreground_hwnd)
        return matches[(position + 1) % len(matches)]
    return matches[0]


def describe_matches(matches: list[WindowInfo], limit: int = 3) -> str:
    if not matches:
        return "没有匹配窗口"
    titles = [w.short_title(28) for w in matches[:limit]]
    text = " / ".join(titles)
    if len(matches) > limit:
        text += f" …（共 {len(matches)} 个）"
    return text
