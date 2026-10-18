"""快捷键字符串与 Windows 虚拟键码之间的转换。

组合键统一使用 ``Ctrl+Alt+Shift+Win+主键`` 的字符串形式，
例如 ``Ctrl+1``、``Ctrl+Shift+F2``、``Ctrl+Alt+W``。
"""

from __future__ import annotations

# RegisterHotKey 修饰键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# 显示顺序固定，保证同一个组合键的字符串形式稳定
MODIFIER_ORDER = (
    ("Ctrl", MOD_CONTROL),
    ("Alt", MOD_ALT),
    ("Shift", MOD_SHIFT),
    ("Win", MOD_WIN),
)

# 主键名称 -> 虚拟键码
KEY_TOKENS: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "prtsc": 0x2C,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "pause": 0x13,
    "numlock": 0x90,
    "scrolllock": 0x91,
    # OEM 符号键
    ";": 0xBA,
    ":": 0xBA,
    "=": 0xBB,
    "+": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "~": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "|": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
    '"': 0xDE,
    # 小键盘
    "numpad0": 0x60,
    "numpad1": 0x61,
    "numpad2": 0x62,
    "numpad3": 0x63,
    "numpad4": 0x64,
    "numpad5": 0x65,
    "numpad6": 0x66,
    "numpad7": 0x67,
    "numpad8": 0x68,
    "numpad9": 0x69,
    "numpad*": 0x6A,
    "numpad+": 0x6B,
    "numpad-": 0x6D,
    "numpad.": 0x6E,
    "numpad/": 0x6F,
}
for _i in range(1, 25):
    KEY_TOKENS[f"f{_i}"] = 0x6F + _i
for _i in range(26):
    KEY_TOKENS[chr(ord("a") + _i)] = 0x41 + _i
for _i in range(10):
    KEY_TOKENS[str(_i)] = 0x30 + _i

# 虚拟键码 -> 展示名称
VK_NAMES: dict[int, str] = {
    0x08: "Backspace",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x20: "Space",
    0x21: "PageUp",
    0x22: "PageDown",
    0x23: "End",
    0x24: "Home",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x2C: "PrintScreen",
    0x2D: "Insert",
    0x2E: "Delete",
    0x13: "Pause",
    0x90: "NumLock",
    0x91: "ScrollLock",
    0xBA: ";",
    0xBB: "=",
    0xBC: ",",
    0xBD: "-",
    0xBE: ".",
    0xBF: "/",
    0xC0: "`",
    0xDB: "[",
    0xDC: "\\",
    0xDD: "]",
    0xDE: "'",
    0x60: "Numpad0",
    0x61: "Numpad1",
    0x62: "Numpad2",
    0x63: "Numpad3",
    0x64: "Numpad4",
    0x65: "Numpad5",
    0x66: "Numpad6",
    0x67: "Numpad7",
    0x68: "Numpad8",
    0x69: "Numpad9",
    0x6A: "Numpad*",
    0x6B: "Numpad+",
    0x6D: "Numpad-",
    0x6E: "Numpad.",
    0x6F: "Numpad/",
}
for _i in range(1, 25):
    VK_NAMES[0x6F + _i] = f"F{_i}"
for _i in range(26):
    VK_NAMES[0x41 + _i] = chr(ord("A") + _i)
for _i in range(10):
    VK_NAMES[0x30 + _i] = str(_i)

_MOD_ALIASES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "ctl": MOD_CONTROL,
    "strg": MOD_CONTROL,
    "alt": MOD_ALT,
    "option": MOD_ALT,
    "shift": MOD_SHIFT,
    "shft": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "meta": MOD_WIN,
    "super": MOD_WIN,
    "cmd": MOD_WIN,
}

MODIFIER_VKS = {
    0x10,  # VK_SHIFT
    0x11,  # VK_CONTROL
    0x12,  # VK_MENU
    0x5B,  # VK_LWIN
    0x5C,  # VK_RWIN
    0xA0,  # VK_LSHIFT
    0xA1,  # VK_RSHIFT
    0xA2,  # VK_LCONTROL
    0xA3,  # VK_RCONTROL
    0xA4,  # VK_LMENU
    0xA5,  # VK_RMENU
}


def is_modifier_vk(vk: int) -> bool:
    """该虚拟键码是否是修饰键（Ctrl / Alt / Shift / Win）。"""
    return vk in MODIFIER_VKS


def vk_name(vk: int) -> str:
    """虚拟键码的展示名称，未知键返回 ``VK_0xNN``。"""
    name = VK_NAMES.get(vk)
    if name:
        return name
    return f"VK_0x{vk:02X}"


def format_hotkey(mods: int, vk: int) -> str:
    """把修饰键 + 主键格式化成 ``Ctrl+Alt+1`` 这样的字符串。"""
    parts = [name for name, flag in MODIFIER_ORDER if mods & flag]
    parts.append(vk_name(vk))
    return "+".join(parts)


def parse_hotkey(text: str) -> tuple[int, int]:
    """解析组合键字符串，返回 ``(modifiers, virtual_key)``。

    解析失败抛出 ``ValueError``，消息可直接展示给用户。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("快捷键不能为空")

    tokens = [t.strip() for t in text.replace(" ", "").split("+")]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise ValueError("快捷键不能为空")

    mods = 0
    main: str | None = None
    for token in tokens:
        low = token.lower()
        if low in _MOD_ALIASES:
            mods |= _MOD_ALIASES[low]
            continue
        if low in KEY_TOKENS:
            if main is not None:
                raise ValueError(f"只能有一个主键：{text}")
            main = token
            continue
        raise ValueError(f"无法识别的按键：{token}")

    if main is None:
        raise ValueError("缺少主键，例如 Ctrl+1 里的 1")
    return mods, KEY_TOKENS[main.lower()]


def normalize_hotkey(text: str) -> str:
    """把用户输入的组合键转成规范写法。"""
    return format_hotkey(*parse_hotkey(text))


def increment_hotkey(text: str, step: int = 1) -> str | None:
    """把组合键的主键按顺序递增（数字 0-9、F1-F24、字母 A-Z）。

    无法递增（例如主键是 Enter）时返回 ``None``。
    """
    if step <= 0:
        return None
    mods, vk = parse_hotkey(text)
    new_vk = vk + step
    if 0x30 <= vk <= 0x39 and 0x30 <= new_vk <= 0x39:
        pass
    elif 0x70 <= vk <= 0x87 and 0x70 <= new_vk <= 0x87:  # F1..F24
        pass
    elif 0x41 <= vk <= 0x5A and 0x41 <= new_vk <= 0x5A:
        pass
    else:
        return None
    return format_hotkey(mods, new_vk)
