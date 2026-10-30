"""Clawd Agents — 桌面上干活的像素小 agent。

透明无边框置顶窗口，20 种动作随机切换。
点一下小人，切到它绑定的那个窗口。

模式：hide 捉迷藏（自由乱跑，躲进窗口后面） / right 贴右边 / orbit 绕屏幕一圈 / area 在指定区域游走
"""

import ctypes
import json
import math
import os
import random
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
import winreg
from ctypes import wintypes

KEY = "#ff00ff"
BODY = "#d97757"
LIMB = "#c4663f"
EYE = "#191410"
CRATE = "#a8492c"
PAPER = "#f3e7da"
PAPER_EDGE = "#c9ab93"
BUBBLE = "#2a1f18"
DOT = "#ffd9b5"

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    "mode": "hide",
    "count": 7,
    "size": 36,
    "speed": 1.0,
    "fps": 45,
    "area": [0.72, 0.08, 0.24, 0.84],
    "targets": [],
}

PARTS = {
    "body": (2.0, 0.6, 8.0, 7.0),
    "eye_l": (3.6, 2.4, 1.3, 2.6),
    "eye_r": (7.1, 2.4, 1.3, 2.6),
    "arm_l": (0.35, 3.2, 1.75, 1.8),
    "arm_r": (9.9, 3.2, 1.75, 1.8),
    "leg_1": (2.52, 7.6, 1.35, 2.0),
    "leg_2": (4.39, 7.6, 1.35, 2.0),
    "leg_3": (6.26, 7.6, 1.35, 2.0),
    "leg_4": (8.13, 7.6, 1.35, 2.0),
}

# (动作, 权重, 时长范围)
STATES = [
    ("walk", 6.0, (2.5, 6.0)),
    ("type", 2.0, (2.0, 4.5)),
    ("fast", 0.9, (1.5, 3.0)),
    ("dance", 1.0, (1.6, 3.0)),
    ("flip", 0.5, (0.8, 0.8)),
    ("backflip", 0.4, (0.95, 0.95)),
    ("cheer", 0.6, (1.2, 2.0)),
    ("think", 0.7, (2.0, 3.6)),
    ("read", 0.6, (2.4, 4.5)),
    ("carry", 0.6, (2.4, 4.5)),
    ("hammer", 0.6, (1.8, 3.4)),
    ("sleep", 0.5, (3.0, 6.0)),
    ("stretch", 0.6, (1.2, 2.0)),
    ("look", 0.7, (1.5, 3.0)),
    ("shrug", 0.5, (0.8, 1.4)),
    ("wave", 0.7, (1.2, 2.2)),
    ("spin", 0.3, (2.2, 2.2)),
    ("wiggle", 0.6, (1.0, 2.0)),
    ("nod", 0.6, (1.0, 1.8)),
    ("jump", 0.7, (1.0, 2.0)),
]

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
GW_OWNER = 4
SW_RESTORE = 9


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def raise_timer_resolution() -> None:
    """默认 15.6ms 的定时器精度会把 after(33) 凑成 46.8ms，等于锁死在 21 帧。"""
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def dpi_scale() -> float:
    hdc = user32.GetDC(None)
    dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
    user32.ReleaseDC(None, hdc)
    return dpi / 96.0 if dpi else 1.0


def list_windows() -> list[dict]:
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def walk(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if title in ("Clawd Agents", "Clawd 设置"):
            return True
        found.append({"hwnd": hwnd, "title": title})
        return True

    user32.EnumWindows(walk, 0)
    return found


def focus_window(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


dwmapi = ctypes.WinDLL("dwmapi")
DWMWA_EXTENDED_FRAME_BOUNDS = 9
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


def window_rects() -> list[tuple[int, int, int, int]]:
    """屏幕上可见窗口的物理屏幕矩形（l, t, r, b），用于捉迷藏模式。"""
    rects = []
    sw, sh = screen_size()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def walk(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value in ("Clawd Agents", "Clawd 设置"):
            return True

        # 输入法宿主、NVIDIA 覆盖层一类窗口不当成“可以躲的窗口”
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex & (WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
            return True

        r = RECT()
        ok = dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)
        )
        if ok != 0 and not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        if r.right - r.left < 160 or r.bottom - r.top < 120:
            return True
        if r.right <= 0 or r.bottom <= 0 or r.left >= sw or r.top >= sh:
            return True
        rects.append((r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumWindows(walk, 0)
    return rects


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ClawdAgents"


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except OSError:
                    pass
    except OSError:
        pass


def smooth(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def compute_pose(state: str, local: float, prog: float, s: float, base_rate: float) -> dict:
    """把每个动作翻译成一组身体参数。长度单位统一按 size=36 缩放。"""
    k = math.sin
    p = dict(bob=0.0, arm_l=0.0, arm_r=0.0, rot=0.0, sway=0.0,
             eye_dx=0.0, leg_amp=0.42, leg_squash=1.0, eyes="auto",
             rate=base_rate, prop=None, zzz=False)

    if state == "walk":
        p["bob"] = -1.4 * abs(k(local * base_rate * 0.5)) * s

    elif state == "type":
        p["bob"] = -1.2 * (0.5 + 0.5 * k(local * 6.0)) * s
        p["arm_l"] = -2.0 * (0.5 + 0.5 * k(local * 16)) * s
        p["arm_r"] = -2.0 * (0.5 + 0.5 * k(local * 16 + math.pi)) * s
        p["rate"] = base_rate * 0.35

    elif state == "fast":
        p["bob"] = -1.8 * (0.5 + 0.5 * k(local * 11.0)) * s
        p["arm_l"] = -2.4 * (0.5 + 0.5 * k(local * 30)) * s
        p["arm_r"] = -2.4 * (0.5 + 0.5 * k(local * 30 + math.pi)) * s
        p["rate"] = base_rate * 0.2

    elif state == "dance":
        p["rot"] = k(local * 9.0) * 0.17
        p["bob"] = -2.4 * abs(k(local * 9.0)) * s
        p["arm_l"] = -2.6 * (0.5 + 0.5 * k(local * 9.0)) * s
        p["arm_r"] = -2.6 * (0.5 + 0.5 * k(local * 9.0 + 2.2)) * s
        p["rate"] = base_rate * 1.7
        p["leg_amp"] = 0.6

    elif state == "flip":
        p["rot"] = math.pi * 2 * smooth(prog)
        p["bob"] = -7.0 * s * k(math.pi * min(1.0, prog))
        p["arm_l"] = -2.2 * s
        p["arm_r"] = -2.2 * s
        p["rate"] = base_rate * 0.2

    elif state == "backflip":
        p["rot"] = -math.pi * 2 * smooth(prog)
        p["bob"] = -7.0 * s * k(math.pi * min(1.0, prog))
        p["arm_l"] = -2.2 * s
        p["arm_r"] = -2.2 * s
        p["rate"] = base_rate * 0.2

    elif state == "cheer":
        hop = max(0.0, k(local * 7.0))
        p["bob"] = -9.0 * hop * s
        p["arm_l"] = -2.8 * hop * s
        p["arm_r"] = -2.8 * hop * s
        p["rate"] = base_rate * 0.5

    elif state == "think":
        p["bob"] = -0.7 * (0.5 + 0.5 * k(local * 2.4)) * s
        p["arm_l"] = -1.0 * s
        p["arm_r"] = -1.0 * s
        p["rate"] = base_rate * 0.8
        p["prop"] = "bubble"

    elif state == "read":
        p["arm_l"] = -4.2 * s
        p["arm_r"] = -4.2 * s
        p["bob"] = -0.8 * (0.5 + 0.5 * k(local * 2.0)) * s
        p["rate"] = base_rate * 0.6
        p["prop"] = "paper"

    elif state == "carry":
        p["arm_l"] = -3.2 * s
        p["arm_r"] = -3.2 * s
        p["sway"] = k(local * 3.3) * 2.2 * s
        p["rate"] = base_rate * 0.9
        p["prop"] = "crate"

    elif state == "hammer":
        hit = k(local * 12.0)
        p["arm_l"] = -1.0 * s
        p["arm_r"] = (-3.0 + 4.4 * max(0.0, -hit)) * s
        p["bob"] = 0.9 * max(0.0, hit) * s
        p["rate"] = base_rate * 0.4

    elif state == "sleep":
        p["eyes"] = "closed"
        p["bob"] = (1.4 + 0.5 * k(local * 1.5)) * s
        p["leg_squash"] = 0.45
        p["rate"] = base_rate * 0.15
        p["zzz"] = True

    elif state == "stretch":
        raise_ = 0.5 + 0.5 * k(local * 2.0 - math.pi / 2)
        p["arm_l"] = -5.5 * raise_ * s
        p["arm_r"] = -5.5 * raise_ * s
        p["bob"] = -1.6 * raise_ * s
        p["leg_squash"] = 1.0 + 0.06 * raise_
        p["rate"] = base_rate * 0.4

    elif state == "look":
        p["eye_dx"] = k(local * 1.6) * 1.15
        p["rot"] = k(local * 1.6) * 0.03
        p["rate"] = base_rate * 0.7

    elif state == "shrug":
        lift = 0.5 + 0.5 * k(local * 5.0)
        p["arm_l"] = -3.6 * lift * s
        p["arm_r"] = -3.6 * lift * s
        p["bob"] = 0.9 * lift * s
        p["rate"] = base_rate * 0.3

    elif state == "wave":
        p["arm_r"] = (-4.0 + 2.2 * k(local * 10.0)) * s
        p["rot"] = 0.03 * k(local * 5.0)
        p["rate"] = base_rate * 0.8

    elif state == "spin":
        p["rot"] = math.pi * 2 * smooth(prog)
        p["rate"] = base_rate * 0.5

    elif state == "wiggle":
        p["rot"] = k(local * 14.0) * 0.1
        p["sway"] = k(local * 7.0) * 1.6 * s
        p["rate"] = base_rate * 1.2
        p["leg_amp"] = 0.55

    elif state == "nod":
        p["bob"] = -2.2 * max(0.0, k(local * 9.0)) * s
        p["arm_l"] = -1.0 * s
        p["arm_r"] = -1.0 * s
        p["rate"] = base_rate * 0.5

    elif state == "jump":
        hop = abs(k(local * 5.0))
        p["bob"] = -7.0 * hop * s
        p["arm_l"] = -1.6 * hop * s
        p["arm_r"] = -1.6 * hop * s
        p["rate"] = base_rate * 0.6

    return p


class Agent:
    def __init__(self, canvas: tk.Canvas, index: int) -> None:
        self.canvas = canvas
        self.index = index
        self.phase = index * 1.37
        self.size = 36
        self.walk_rate = 6.0
        self.pos = (0.0, 0.0)
        self.state = "walk"
        self.prev_state = "walk"
        self.state_start = 0.0
        self.state_until = random.uniform(0.4, 3.0)

        # 捉迷藏模式：屏幕坐标、目标点、当前速度、休息/隐身/探头的截止时间
        self.spos = (0.0, 0.0)
        self.rt = None
        self.rspeed = 0.0
        self.rest_until = 0.0
        self.hide_until = 0.0
        self.was_hidden = False
        self.peek_until = 0.0
        self.peek_x = 0.0
        self.peek_top = None

        self.visible = True
        self.clipped = False
        self.rotated = False
        self.fill: dict[int, str] = {}

        self.poly = {}
        for name in PARTS:
            if name.startswith("eye"):
                color = EYE
            elif name.startswith("arm") or name.startswith("leg"):
                color = LIMB
            else:
                color = BODY
            self.poly[name] = canvas.create_polygon(
                0, 0, 0, 0, 0, 0, 0, 0, fill=color, outline="", tags=("clawd", f"a{index}")
            )

        self.prop = canvas.create_polygon(
            0, 0, 0, 0, 0, 0, 0, 0, fill=CRATE, outline="", state="hidden", tags=("clawd", f"a{index}")
        )
        self.bubble = canvas.create_polygon(
            0, 0, 0, 0, 0, 0, 0, 0, fill=BUBBLE, outline="", state="hidden", tags=("clawd", f"a{index}")
        )
        self.dots = [
            canvas.create_polygon(
                0, 0, 0, 0, 0, 0, 0, 0, fill=DOT, outline="", state="hidden",
                tags=("clawd", f"a{index}"),
            )
            for _ in range(3)
        ]
        self.shown: dict[int, bool] = {item: True for item in self.poly.values()}
        for item in [self.prop, self.bubble] + self.dots:
            self.shown[item] = False

    def set_size(self, size: int) -> None:
        self.size = size

    def items(self) -> list[int]:
        return list(self.poly.values()) + [self.prop, self.bubble] + self.dots

    def set_visible(self, on: bool) -> None:
        if on == self.visible:
            return
        self.visible = on
        for item in self.poly.values():
            self._show(item, on)
        if not on:
            self.clipped = False
            self.rotated = False
            for item in [self.prop, self.bubble] + self.dots:
                self._show(item, False)

    def _show(self, item, on: bool) -> None:
        if self.shown.get(item) is on:
            return
        self.shown[item] = on
        self.canvas.itemconfig(item, state="normal" if on else "hidden")

    def destroy(self) -> None:
        for item in self.items():
            self.canvas.delete(item)

    def choose_state(self, t: float) -> None:
        pool, weights = [], []
        for name, weight, span in STATES:
            if name == self.prev_state and name not in ("walk", "type"):
                continue
            pool.append((name, span))
            weights.append(weight)
        name, span = random.choices(pool, weights=weights, k=1)[0]
        self.prev_state = self.state
        self.state = name
        self.state_start = t
        self.state_until = t + random.uniform(*span)

    def _rot(self, x: float, y: float, cx: float, cy: float, ang: float) -> tuple[float, float]:
        ca, sa = math.cos(ang), math.sin(ang)
        dx, dy = x - cx, y - cy
        return cx + dx * ca - dy * sa, cy + dx * sa + dy * ca

    def _quad(self, item, x0, y0, x1, y1, cx, cy, rot, color=None) -> None:
        if color is not None:
            self._show(item, True)
            if self.fill.get(item) != color:
                self.fill[item] = color
                self.canvas.itemconfig(item, fill=color)
        if rot:
            a = self._rot(x0, y0, cx, cy, rot)
            b = self._rot(x1, y0, cx, cy, rot)
            d = self._rot(x1, y1, cx, cy, rot)
            e = self._rot(x0, y1, cx, cy, rot)
            self.canvas.coords(item, a[0], a[1], b[0], b[1], d[0], d[1], e[0], e[1])
        else:
            self.canvas.coords(item, x0, y0, x1, y0, x1, y1, x0, y1)

    def update(self, cx: float, cy: float, t: float, cut: float | None = None) -> None:
        self.pos = (cx, cy)
        if not self.visible:
            return

        if cut is None:
            if self.clipped:
                for item in self.poly.values():
                    self._show(item, True)
                self.clipped = False
        else:
            self.clipped = True

        if t >= self.state_until:
            self.choose_state(t)

        u = self.size / 12.0
        s = self.size / 36.0
        local = t + self.phase
        prog = (t - self.state_start) / max(1e-3, self.state_until - self.state_start)
        p = compute_pose(self.state, local, prog, s, self.walk_rate)

        bob = p["bob"]
        rot = 0.0 if cut is not None else p["rot"]
        sway = p["sway"]
        step = math.sin(local * p["rate"])
        blink = (local % 5.6) > 5.2

        ox = cx - 6.0 * u
        oy = cy - 5.0 * u

        for name, (gx, gy, gw, gh) in PARTS.items():
            dy = bob
            if name == "arm_l":
                dy = bob + p["arm_l"]
            elif name == "arm_r":
                dy = bob + p["arm_r"]
            elif name.startswith("leg_"):
                ph = step if name in ("leg_1", "leg_4") else -step
                lift = max(0.0, ph)
                gh = gh * (1.0 - p["leg_amp"] * lift) * p["leg_squash"]
                gx = gx + ph * 0.22
                dy = bob + ((1.0 - p["leg_squash"]) * 2.0 * u)
            if name.startswith("eye"):
                gx = gx + p["eye_dx"]
                if p["eyes"] == "closed" or (p["eyes"] == "auto" and blink):
                    gh = gh * 0.12

            x0 = ox + (gx + sway / u) * u
            y0 = oy + gy * u + dy
            item = self.poly[name]
            if cut is not None:
                if y0 >= cut:
                    self._show(item, False)
                    continue
                bottom = min(y0 + gh * u, cut)
                self._show(item, True)
                self._quad(item, x0, y0, x0 + gw * u, bottom, cx, cy, rot)
            else:
                self._quad(item, x0, y0, x0 + gw * u, y0 + gh * u, cx, cy, rot)

        self._extras(p, local, ox, oy, u, s, bob, cx, cy, rot, cut)
        if rot and not self.rotated:
            self.canvas.tag_raise(f"a{self.index}")
        self.rotated = bool(rot)

    def _extras(self, p, local, ox, oy, u, s, bob, cx, cy, rot, cut=None) -> None:
        c = self.canvas
        if cut is not None:
            self._show(self.prop, False)
            self._show(self.bubble, False)
            for item in self.dots:
                self._show(item, False)
            return
        kind = p["prop"]

        if kind == "crate":
            px = ox + 4.0 * u
            py = oy - 2.4 * u + bob
            self._quad(self.prop, px, py, px + 4.0 * u, py + 2.6 * u, cx, cy, rot, CRATE)
        elif kind == "paper":
            px = ox + 7.0 * u
            py = oy - 2.6 * u + bob
            self._quad(self.prop, px, py, px + 4.2 * u, py + 3.2 * u, cx, cy, rot, PAPER)
        else:
            self._show(self.prop, False)

        if kind == "bubble":
            bx = ox + 8.0 * u
            by = oy - 2.8 * u + bob
            self._quad(self.bubble, bx, by, bx + 4.6 * u, by + 2.2 * u, cx, cy, rot, BUBBLE)
            for k, item in enumerate(self.dots):
                lit = ((local * 2.2 + k * 0.3) % 1.0) < 0.6
                qx = bx + (0.6 + k * 1.3) * u
                qy = by + 0.7 * u
                d = 0.7 * u
                if lit:
                    self._quad(item, qx, qy, qx + d, qy + d, cx, cy, rot, DOT)
                else:
                    self._show(item, False)
        elif p["zzz"]:
            for k, item in enumerate(self.dots):
                phase = (local * 0.7 + k * 0.33) % 1.0
                size = (0.5 + 0.5 * phase) * u
                qx = ox + (8.4 + k * 1.1 + phase * 1.2) * u
                qy = oy - 1.0 * u - phase * 3.4 * u + bob
                self._quad(item, qx, qy, qx + size, qy + size, cx, cy, rot, DOT)
        else:
            self._show(self.bubble, False)
            for item in self.dots:
                self._show(item, False)

    def hit(self, x: float, y: float) -> bool:
        if not self.visible:
            return False
        reach = self.size * 0.75
        return abs(x - self.pos[0]) <= reach and abs(y - self.pos[1]) <= reach


class App:
    def __init__(self) -> None:
        enable_dpi_awareness()
        raise_timer_resolution()
        self.cfg = self.load_config()
        self.screen_w, self.screen_h = screen_size()
        self.scale = dpi_scale()

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", KEY)
        self.root.configure(bg=KEY)

        self.canvas = tk.Canvas(
            self.root, bg=KEY, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.win_x = self.win_y = 0
        self.win_w, self.win_h = self.screen_w, self.screen_h
        self.update_window_geometry()

        self.agents: list[Agent] = []
        self.build_agents()

        self.rects: list[tuple[int, int, int, int]] = []
        self.rect_t = 0.0
        self.last_t = 0.0
        self.peek_owner: int | None = None
        self.peek_cool = 0.0
        self.reset_roam()

        self.t_epoch = time.perf_counter()
        self.frame_ms = self.fps_ms()
        self.frame_period = self.fps_period()
        self.next_ms = 0.0

        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Button-3>", self.on_right)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.settings = None
        self.tick()

    # ---------- 配置 ----------

    def load_config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as fh:
                    cfg.update(json.load(fh))
            except Exception:
                pass
        return cfg

    def save_config(self) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def fps_ms(self) -> int:
        fps = max(10, min(60, int(self.cfg.get("fps", 30))))
        return max(16, round(1000 / fps))

    def fps_period(self) -> float:
        fps = max(10, min(60, int(self.cfg.get("fps", 30))))
        return 1000.0 / fps

    # ---------- 窗口范围 ----------
    #
    # 分层透明窗口的合成开销和面积成正比，整屏开窗必然掉帧。
    # 按模式只开需要的那块：贴右边只开右侧窄条，区域模式只开区域，
    # 绕屏跑才需要全屏。

    def update_window_geometry(self) -> None:
        px = int(self.cfg["size"]) * self.scale
        mode = self.cfg["mode"]

        if mode in ("orbit", "hide"):
            x, y, w, h = 0, 0, self.screen_w, self.screen_h
        elif mode == "area":
            ax, ay, aw, ah = self.cfg["area"]
            pad = px * 1.6
            x = max(0, int(ax * self.screen_w - pad))
            y = max(0, int(ay * self.screen_h - pad))
            w = min(self.screen_w - x, int(aw * self.screen_w + pad * 2))
            h = min(self.screen_h - y, int(ah * self.screen_h + pad * 2))
        else:  # right
            w = min(self.screen_w, int(px * 4.0) + 24)
            x, y, h = self.screen_w - w, 0, self.screen_h

        self.win_x, self.win_y, self.win_w, self.win_h = x, y, w, h
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.canvas.configure(width=w, height=h)

    # ---------- 小人 ----------

    def build_agents(self) -> None:
        for agent in self.agents:
            agent.destroy()
        self.agents = [
            Agent(self.canvas, i) for i in range(int(self.cfg["count"]))
        ]
        self.apply_size()
        self.reset_roam()

    def apply_size(self) -> None:
        px = int(int(self.cfg["size"]) * self.scale)
        for agent in self.agents:
            agent.set_size(px)

    def reset_roam(self) -> None:
        px = int(self.cfg["size"]) * self.scale
        self.peek_owner = None
        self.peek_cool = 0.0
        for agent in self.agents:
            agent.spos = (
                random.uniform(px, max(px + 1, self.screen_w - px)),
                random.uniform(px, max(px + 1, self.screen_h - px)),
            )
            agent.rt = None
            agent.rspeed = 0.0
            agent.rest_until = 0.0
            agent.hide_until = 0.0
            agent.was_hidden = False
            agent.peek_until = 0.0
            agent.peek_top = None
            agent.set_visible(True)

    # ---------- 运动 ----------

    def perimeter(self, s: float) -> tuple[float, float]:
        inset = int(self.cfg["size"]) * self.scale * 1.8
        w = self.screen_w - 2 * inset
        h = self.screen_h - 2 * inset
        d = (s % 1.0) * 2 * (w + h)
        if d < w:
            return inset + d, inset
        d -= w
        if d < h:
            return self.screen_w - inset, inset + d
        d -= h
        if d < w:
            return self.screen_w - inset - d, self.screen_h - inset
        d -= w
        return inset, self.screen_h - inset - d

    def end_peek(self, agent: Agent, t: float) -> None:
        y = agent.peek_top if agent.peek_top is not None else agent.spos[1]
        agent.spos = (agent.peek_x, y)
        agent.peek_until = 0.0
        agent.peek_top = None
        agent.rt = None
        agent.rest_until = t + 0.2
        if self.peek_owner == agent.index:
            self.peek_owner = None
            self.peek_cool = t + random.uniform(3.5, 7.0)

    def free_point(self, margin: float) -> tuple[float, float]:
        """尽量挑一个不在窗口底下的落脚点，别让小人一直闷在窗口后面。"""
        fallback = (self.screen_w / 2, self.screen_h / 2)
        for _ in range(14):
            x = random.uniform(margin, max(margin + 1, self.screen_w - margin))
            y = random.uniform(margin, max(margin + 1, self.screen_h - margin))
            fallback = (x, y)
            for rect in self.rects:
                if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                    break
            else:
                return x, y
        return fallback

    def exit_point(self, cx: float, cy: float, rect: tuple[int, int, int, int], margin: float) -> tuple[float, float]:
        """从窗口里钻出来时最近的窗口外落点；只考虑落在屏幕里的那几条边。"""
        l, tp, r, b = rect
        cands = [
            (cy - tp, cx, tp - margin),
            (b - cy, cx, b + margin),
            (cx - l, l - margin, cy),
            (r - cx, r + margin, cy),
        ]
        cands.sort(key=lambda c: c[0])
        for _, x, y in cands:
            if margin <= x <= self.screen_w - margin and margin <= y <= self.screen_h - margin:
                return x, y
        _, x, y = cands[0]
        return (
            min(max(margin, x), max(margin + 1, self.screen_w - margin)),
            min(max(margin, y), max(margin + 1, self.screen_h - margin)),
        )

    def roam(self, agent: Agent, t: float, dt: float) -> tuple[float, float, float | None, bool]:
        """捉迷藏：自由乱跑，撞进窗口就隐身，偶尔从窗口上沿探头，鼠标一靠近就逃。"""
        px = int(self.cfg["size"]) * self.scale
        speed_mul = float(self.cfg["speed"])
        margin = px * 0.9

        mx, my = cursor_pos()

        if agent.peek_until > t and agent.peek_top is not None:
            hx, hy = agent.peek_x, agent.peek_top
            threat = math.hypot(hx - mx, hy - my) < px * 3.2
            if not threat:
                agent.state = "look"
                agent.state_start = t
                agent.state_until = agent.peek_until
                return agent.peek_x, agent.peek_top, agent.peek_top, True
            self.end_peek(agent, t)  # 探头时被摸到，直接跳下来跑
        elif agent.peek_until:
            self.end_peek(agent, t)

        cx, cy = agent.spos
        threat = math.hypot(cx - mx, cy - my) < px * 3.2

        if threat:
            fdx, fdy = cx - mx, cy - my
            d = math.hypot(fdx, fdy)
            if d < 1e-3:
                fdx, fdy, d = 1.0, 1.0, 1.414
            run = px * 2.4
            agent.rt = (
                min(max(margin, cx + fdx / d * run), max(margin + 1, self.screen_w - margin)),
                min(max(margin, cy + fdy / d * run), max(margin + 1, self.screen_h - margin)),
            )
            agent.rspeed = px * 3.4 * speed_mul
            agent.rest_until = 0.0
        elif agent.rt is None and t >= agent.rest_until:
            agent.rt = self.free_point(margin)
            roll = random.random()
            burst = 1.9 if roll < 0.2 else 0.5 if roll < 0.5 else 1.0
            agent.rspeed = random.uniform(30, 58) * (px / 45.0) * speed_mul * burst

        if agent.rt is not None:
            tx, ty = agent.rt
            dx, dy = tx - cx, ty - cy
            dist = math.hypot(dx, dy)
            step = agent.rspeed * dt
            if dist <= step or dist < 1.0:
                cx, cy = tx, ty
                agent.rt = None
                agent.rest_until = t + random.uniform(0.3, 2.0)
            else:
                cx += dx / dist * step
                cy += dy / dist * step

        agent.spos = (cx, cy)

        # EnumWindows 是自上而下的 Z 序：第一个命中的就是盖在最上面的那个窗口，
        # 只趴它的边，避免趴在被别的窗口挡住的那条边上（看起来像趴在窗口里面）
        inside = None
        for rect in self.rects:
            if not (rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3]):
                continue
            if rect[1] < px * 1.4:
                # 顶到屏幕顶的窗口无从探头，直接穿过，别让小人凭空消失
                continue
            inside = rect
            break

        if inside is None:
            agent.hide_until = 0.0
            if agent.was_hidden:
                agent.was_hidden = False
                agent.rt = None
                agent.rest_until = t + random.uniform(0.2, 0.8)
            return cx, cy, None, True

        if not agent.hide_until:
            agent.hide_until = t + random.uniform(0.4, 1.2)
            agent.was_hidden = True
            agent.rt = self.exit_point(cx, cy, inside, margin)
            agent.rspeed = max(agent.rspeed, px * 2.6 * speed_mul)
            return cx, cy, None, False

        can_peek = self.peek_owner is None and t >= self.peek_cool
        if can_peek and t >= agent.hide_until and random.random() < dt * 1.5:
            self.peek_owner = agent.index
            agent.rt = None
            agent.peek_top = inside[1] - 1.0
            agent.peek_x = min(max(cx, inside[0] + px * 0.7), inside[2] - px * 0.7)
            agent.peek_until = t + random.uniform(0.9, 1.8)
            self.peek_cool = agent.peek_until + random.uniform(3.5, 7.0)
        return cx, cy, None, False

    def position(self, i: int, t: float) -> tuple[float, float]:
        n = max(1, len(self.agents))
        mode = self.cfg["mode"]
        speed = float(self.cfg["speed"])
        px = int(self.cfg["size"]) * self.scale

        if mode == "orbit":
            return self.perimeter(i / n + t * 0.028 * speed)

        if mode == "area":
            ax, ay, aw, ah = self.cfg["area"]
            x0, y0 = ax * self.screen_w, ay * self.screen_h
            w, h = aw * self.screen_w, ah * self.screen_h
            cx = x0 + w * (0.5 + 0.36 * math.sin(t * 0.33 * speed + i * 2.1))
            cy = y0 + h * (0.5 + 0.36 * math.sin(t * 0.47 * speed + i * 3.7))
            return cx, cy

        base_x = self.screen_w - px * 0.95
        base_y = self.screen_h * (0.09 + i * 0.82 / max(1, n - 1))
        amp = px * (0.26 + (i % 3) * 0.08)
        x = base_x + amp * math.sin(t * (1.1 + 0.18 * (i % 3)) * speed + i * 1.7)
        y = base_y + px * 0.11 * math.sin(t * 1.6 * speed + i)
        return x, y

    def schedule(self) -> None:
        """按绝对时间轴排下一帧；after() 的毫秒取整误差累积起来会掉到 56 帧。"""
        now = time.perf_counter() * 1000.0
        if self.next_ms <= 0.0:
            self.next_ms = now
        self.next_ms += self.frame_period
        lag = self.next_ms - now
        if lag < -3.0 * self.frame_period:
            self.next_ms = now + self.frame_period
            lag = self.frame_period
        self.root.after(max(1, int(round(lag))), self.tick)

    def tick(self) -> None:
        t = time.perf_counter() - self.t_epoch
        dt = min(0.06, max(0.0, t - self.last_t))
        self.last_t = t
        mode = self.cfg["mode"]

        if mode == "hide":
            if t >= self.rect_t:
                self.rects = window_rects()
                self.rect_t = t + 0.4
            for agent in self.agents:
                cx, cy, cut, visible = self.roam(agent, t, dt)
                agent.walk_rate = 3.2 + agent.rspeed / 42.0
                agent.set_visible(visible)
                crop = cut - self.win_y if cut is not None else None
                agent.update(cx - self.win_x, cy - self.win_y, t, crop)
            self.schedule()
            return

        speed = float(self.cfg["speed"])
        rate = (5.0 if mode == "right" else 11.0 if mode == "orbit" else 8.0) * speed
        for agent in self.agents:
            agent.walk_rate = rate
        for i, agent in enumerate(self.agents):
            cx, cy = self.position(i, t)
            agent.set_visible(True)
            agent.update(cx - self.win_x, cy - self.win_y, t)
        self.schedule()

    # ---------- 交互 ----------

    def on_click(self, event: tk.Event) -> None:
        for agent in self.agents:
            if agent.hit(event.x, event.y):
                self.jump_to(agent.index)
                return

    def jump_to(self, index: int) -> None:
        needles = self.cfg.get("targets") or []
        if index < len(needles) and needles[index]:
            needle = needles[index].lower()
            for win in list_windows():
                if needle in win["title"].lower():
                    focus_window(win["hwnd"])
                    return

        windows = list_windows()
        if index < len(windows):
            focus_window(windows[index]["hwnd"])

    def on_right(self, event: tk.Event) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="设置", command=self.open_settings)
        menu.add_separator()
        menu.add_command(label="退出", command=self.quit)
        menu.tk_popup(event.x_root, event.y_root)

    # ---------- 设置面板 ----------

    def open_settings(self) -> None:
        if self.settings and self.settings.winfo_exists():
            self.settings.lift()
            return

        win = tk.Toplevel(self.root)
        win.title("Clawd 设置")
        win.attributes("-topmost", True)
        try:
            win.attributes("-transparentcolor", "")
        except Exception:
            pass
        win.configure(bg="#1d1815")
        self.settings = win

        font = tkfont.Font(family="Microsoft YaHei UI", size=10)
        label_fg = "#e8d7c8"
        common = dict(bg="#1d1815", fg=label_fg, font=font, anchor="w")

        def row(text: str) -> None:
            tk.Label(win, text=text, **common).pack(fill="x", padx=14, pady=(10, 0))

        row("运动模式")
        self.mode_var = tk.StringVar(value=self.cfg["mode"])
        for value, text in [
            ("hide", "捉迷藏（躲到窗口后面）"),
            ("right", "贴着屏幕右边"),
            ("orbit", "绕屏幕一圈"),
            ("area", "在指定区域游走"),
        ]:
            tk.Radiobutton(
                win, text=text, value=value, variable=self.mode_var,
                bg="#1d1815", fg=label_fg, selectcolor="#3a2d26",
                activebackground="#1d1815", activeforeground=label_fg,
                font=font, anchor="w", command=self.on_mode,
            ).pack(fill="x", padx=26)

        self.speed_var = tk.DoubleVar(value=self.cfg["speed"])
        row("速度")
        tk.Scale(
            win, from_=0.2, to=3.0, resolution=0.1, orient="horizontal",
            variable=self.speed_var, bg="#1d1815", fg=label_fg, highlightthickness=0,
            troughcolor="#3a2d26", font=font, command=lambda _: self.on_speed(),
        ).pack(fill="x", padx=14)

        self.size_var = tk.IntVar(value=int(self.cfg["size"]))
        row("大小")
        tk.Scale(
            win, from_=24, to=64, resolution=2, orient="horizontal",
            variable=self.size_var, bg="#1d1815", fg=label_fg, highlightthickness=0,
            troughcolor="#3a2d26", font=font, command=lambda _: self.on_size(),
        ).pack(fill="x", padx=14)

        self.count_var = tk.IntVar(value=int(self.cfg["count"]))
        row("数量")
        tk.Scale(
            win, from_=1, to=12, resolution=1, orient="horizontal",
            variable=self.count_var, bg="#1d1815", fg=label_fg, highlightthickness=0,
            troughcolor="#3a2d26", font=font, command=lambda _: self.on_count(),
        ).pack(fill="x", padx=14)

        self.fps_var = tk.IntVar(value=int(self.cfg["fps"]))
        self.fps_label = tk.StringVar(value=f"帧率  {int(self.cfg['fps'])} FPS")
        tk.Label(win, textvariable=self.fps_label, **common).pack(fill="x", padx=14, pady=(10, 0))
        tk.Scale(
            win, from_=10, to=60, resolution=5, orient="horizontal",
            variable=self.fps_var, bg="#1d1815", fg=label_fg, highlightthickness=0,
            troughcolor="#3a2d26", font=font, command=lambda _: self.on_fps(),
        ).pack(fill="x", padx=14)

        row("启动")
        self.auto_var = tk.BooleanVar(value=autostart_enabled())
        tk.Checkbutton(
            win, text="开机自启动", variable=self.auto_var,
            bg="#1d1815", fg=label_fg, selectcolor="#3a2d26",
            activebackground="#1d1815", activeforeground=label_fg,
            font=font, anchor="w", command=self.on_autostart,
        ).pack(fill="x", padx=26)

        tk.Button(
            win, text="退出", command=self.quit,
            bg="#3a2d26", fg=label_fg, relief="flat", font=font,
        ).pack(fill="x", padx=14, pady=14)

        win.geometry(f"280x660+{self.screen_w - 320}+60")

    def on_mode(self) -> None:
        self.cfg["mode"] = self.mode_var.get()
        self.update_window_geometry()
        if self.cfg["mode"] == "hide":
            self.rects = window_rects()
            self.reset_roam()
        else:
            for agent in self.agents:
                agent.set_visible(True)
        self.save_config()

    def on_speed(self) -> None:
        self.cfg["speed"] = round(float(self.speed_var.get()), 2)
        self.save_config()

    def on_size(self) -> None:
        self.cfg["size"] = int(self.size_var.get())
        self.apply_size()
        self.update_window_geometry()
        self.save_config()

    def on_count(self) -> None:
        self.cfg["count"] = int(self.count_var.get())
        self.build_agents()
        self.save_config()

    def on_fps(self) -> None:
        self.cfg["fps"] = int(self.fps_var.get())
        self.frame_ms = self.fps_ms()
        self.frame_period = self.fps_period()
        self.next_ms = 0.0
        self.fps_label.set(f"帧率  {self.cfg['fps']} FPS")
        self.save_config()

    def on_autostart(self) -> None:
        set_autostart(bool(self.auto_var.get()))

    # ---------- 生命周期 ----------

    def quit(self) -> None:
        self.save_config()
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    App().run()


if __name__ == "__main__":
    main()
