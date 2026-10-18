"""纯逻辑单元测试（不依赖真实窗口 / 图形界面）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quickswitch import config as config_mod  # noqa: E402
from quickswitch import keys as keys_mod  # noqa: E402
from quickswitch import matcher as matcher_mod  # noqa: E402
from quickswitch.winapi import WindowInfo  # noqa: E402


# ---------------------------------------------------------------------------
# 快捷键解析
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected_mods, expected_vk",
    [
        ("Ctrl+1", keys_mod.MOD_CONTROL, 0x31),
        ("ctrl+alt+f12", keys_mod.MOD_CONTROL | keys_mod.MOD_ALT, 0x7B),
        ("Shift+Alt+Tab", keys_mod.MOD_SHIFT | keys_mod.MOD_ALT, 0x09),
        ("Win+Space", keys_mod.MOD_WIN, 0x20),
        ("Ctrl+Shift+Left", keys_mod.MOD_CONTROL | keys_mod.MOD_SHIFT, 0x25),
        ("Alt+`", keys_mod.MOD_ALT, 0xC0),
    ],
)
def test_parse_hotkey(text, expected_mods, expected_vk):
    assert keys_mod.parse_hotkey(text) == (expected_mods, expected_vk)


@pytest.mark.parametrize(
    "text",
    ["", "Ctrl", "Ctrl+", "Ctrl+NoSuchKey", "Ctrl+A+B"],
)
def test_parse_hotkey_invalid(text):
    with pytest.raises(ValueError):
        keys_mod.parse_hotkey(text)


def test_format_and_normalize():
    mods = keys_mod.MOD_CONTROL | keys_mod.MOD_SHIFT | keys_mod.MOD_ALT
    assert keys_mod.format_hotkey(mods, 0x31) == "Ctrl+Alt+Shift+1"
    assert keys_mod.normalize_hotkey("shift+ctrl+1") == "Ctrl+Shift+1"
    assert keys_mod.normalize_hotkey("control+alt+delete") == "Ctrl+Alt+Delete"


def test_increment_hotkey():
    assert keys_mod.increment_hotkey("Ctrl+1", 1) == "Ctrl+2"
    assert keys_mod.increment_hotkey("Ctrl+8", 2) is None
    assert keys_mod.increment_hotkey("Ctrl+F1", 1) == "Ctrl+F2"
    assert keys_mod.increment_hotkey("Ctrl+F24", 1) is None
    assert keys_mod.increment_hotkey("Ctrl+Enter", 1) is None


def test_modifier_detection():
    for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA2, 0xA4):
        assert keys_mod.is_modifier_vk(vk)
    assert not keys_mod.is_modifier_vk(0x31)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _make_binding(**overrides) -> config_mod.Binding:
    data = dict(
        id="b1",
        name="终端",
        hotkey="Ctrl+1",
        process="WindowsTerminal.exe",
        index=1,
        when=config_mod.WHEN_ACTIVATE,
    )
    data.update(overrides)
    return config_mod.Binding(**data)


def test_config_roundtrip():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "config.json")
        original = config_mod.Config(
            bindings=[_make_binding()],
            start_with_windows=True,
            start_minimized=True,
            close_to_tray=False,
            notifications=False,
        )
        config_mod.save(original, path)
        assert os.path.exists(path)
        loaded = config_mod.load(path)

    assert len(loaded.bindings) == 1
    binding = loaded.bindings[0]
    assert binding.name == "终端"
    assert binding.process == "WindowsTerminal.exe"
    assert binding.index == 1
    assert loaded.start_with_windows is True
    assert loaded.start_minimized is True
    assert loaded.close_to_tray is False
    assert loaded.notifications is False


def test_config_load_missing_returns_empty():
    with tempfile.TemporaryDirectory() as temp_dir:
        cfg = config_mod.load(os.path.join(temp_dir, "none.json"))
    assert cfg.bindings == []


def test_config_unknown_when_falls_back():
    binding = config_mod.Binding.from_dict({"id": "x", "hotkey": "Ctrl+1", "when": "bogus"})
    assert binding.when == config_mod.WHEN_ACTIVATE_OR_LAUNCH


def test_config_save_creates_backup():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "config.json")
        config_mod.save(config_mod.Config(), path)
        config_mod.save(config_mod.Config(bindings=[_make_binding()]), path)
        assert os.path.exists(path + ".bak")


def test_validate_bindings_duplicate_hotkey():
    bindings = [
        _make_binding(id="a", name="A", hotkey="Ctrl+1"),
        _make_binding(id="b", name="B", hotkey="ctrl+1"),
    ]
    problems = config_mod.validate_bindings(bindings)
    assert any("重复" in message for _bid, message in problems)


def test_validate_bindings_bad_regex_and_missing_launch():
    bindings = [
        _make_binding(id="a", title="([", when=config_mod.WHEN_ACTIVATE),
        _make_binding(id="b", when=config_mod.WHEN_ACTIVATE_OR_LAUNCH, launch=""),
    ]
    problems = config_mod.validate_bindings(bindings)
    messages = " ".join(message for _bid, message in problems)
    assert "正则" in messages
    assert "启动命令" in messages


def test_validate_bindings_ok():
    bindings = [
        _make_binding(
            id="a",
            hotkey="Ctrl+Alt+1",
            title="项目.*",
            when=config_mod.WHEN_ACTIVATE_OR_LAUNCH,
            launch="code D:\\proj",
        )
    ]
    assert config_mod.validate_bindings(bindings) == []


def test_target_summary_contains_cycle_and_index():
    assert "循环" in config_mod.target_summary(_make_binding(index=0, hotkey="Ctrl+2"))
    assert "第2个" in config_mod.target_summary(_make_binding(index=2, hotkey="Ctrl+2"))


# ---------------------------------------------------------------------------
# 窗口匹配
# ---------------------------------------------------------------------------

def _windows() -> list[WindowInfo]:
    return [
        WindowInfo(11, "agent-alpha - 终端", "CASCADIA_HOSTING_WINDOW_CLASS", 1, "WindowsTerminal.exe"),
        WindowInfo(22, "agent-beta - 终端", "CASCADIA_HOSTING_WINDOW_CLASS", 1, "WindowsTerminal.exe"),
        WindowInfo(33, "quickswitch - Visual Studio Code", "Chrome_WidgetWin_1", 2, "Code.exe"),
        WindowInfo(44, "agent-gamma - 终端", "ConsoleWindowClass", 3, "conhost.exe"),
    ]


def test_rule_process_and_title():
    rule = matcher_mod.compile_rule(
        _make_binding(process="WindowsTerminal.exe", title="agent-", when=config_mod.WHEN_ACTIVATE)
    )
    assert [w.hwnd for w in matcher_mod.find_matches(rule, _windows())] == [11, 22]


def test_rule_glob_and_exclude():
    rule = matcher_mod.compile_rule(
        _make_binding(process="*Terminal.exe", exclude_title="beta", when=config_mod.WHEN_ACTIVATE)
    )
    assert [w.hwnd for w in matcher_mod.find_matches(rule, _windows())] == [11]


def test_rule_class_substring():
    rule = matcher_mod.compile_rule(
        _make_binding(process="", window_class="cascadia", when=config_mod.WHEN_ACTIVATE)
    )
    assert [w.hwnd for w in matcher_mod.find_matches(rule, _windows())] == [11, 22]


def test_choose_cycle():
    rule = matcher_mod.compile_rule(
        _make_binding(process="WindowsTerminal.exe", index=0)
    )
    matches = matcher_mod.find_matches(rule, _windows())
    assert matcher_mod.choose_target(rule, matches, 0).hwnd == 11
    assert matcher_mod.choose_target(rule, matches, 11).hwnd == 22
    assert matcher_mod.choose_target(rule, matches, 22).hwnd == 11
    assert matcher_mod.choose_target(rule, [], 11) is None


def test_choose_fixed_index_sorted_by_hwnd():
    rule = matcher_mod.compile_rule(_make_binding(process="WindowsTerminal.exe", index=2))
    matches = matcher_mod.find_matches(rule, _windows())
    assert matcher_mod.choose_target(rule, matches, 0).hwnd == 22
    out_of_range = matcher_mod.compile_rule(
        _make_binding(process="WindowsTerminal.exe", index=5)
    )
    assert matcher_mod.choose_target(out_of_range, matches, 0) is None


def test_compile_rule_bad_regex():
    with pytest.raises(ValueError):
        matcher_mod.compile_rule(_make_binding(title="(["))


def test_config_json_is_valid_example():
    example = os.path.join(config_mod.app_dir(), "config.example.json")
    with open(example, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["bindings"]
    config_mod.Config.from_dict(data)
