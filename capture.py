# -*- coding: utf-8 -*-
"""截图与 hide-on-capture 机制。

通过 mss 截图；截图前调用已注册的 hide 钩子把代理窗口（主窗口、悬浮球）隐藏，
截图后恢复——避免代理自身出现在视觉模型截图里。

注意：Win11 上 SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) 会让窗口完全不可见，
故改用 ShowWindow(SW_HIDE/SW_SHOW) 在截图瞬间临时隐藏。
"""

from __future__ import annotations

import ctypes
import io
import time
from typing import Callable, List, Optional, Tuple

import mss
from PIL import Image, ImageDraw

# Win32
user32 = ctypes.windll.user32
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
GA_ROOT = 2  # GetAncestor: 获取最顶层属主窗口

# POINT structure for GetCursorPos
class POINT(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long)
    ]

# 隐藏后等待 DWM 重绘的时间（秒）。Win11 高 DPI/多显示器下 0.05s 偶有残留，
# 提到 0.12s 确保窗口从合成链中移除后再截图。
_CAPTURE_HIDE_DELAY = 0.12

# 注册的隐藏/显示钩子
_hide_hooks: List[Callable[[], None]] = []
_show_hooks: List[Callable[[], None]] = []

# 截图隐藏开关（True=截图前隐藏代理窗口）
_hide_on_capture = True

# 是否在截图上绘制红色十字光标（仅自主代理模式截图应开启）
_draw_cursor = False


def register_capture_window(hide_fn: Callable[[], None], show_fn: Callable[[], None]) -> None:
    """注册一对隐藏/显示钩子。主窗口、悬浮球各注册一次。"""
    if hide_fn not in _hide_hooks:
        _hide_hooks.append(hide_fn)
    if show_fn not in _show_hooks:
        _show_hooks.append(show_fn)


def unregister_capture_window(hide_fn: Callable[[], None], show_fn: Callable[[], None]) -> None:
    if hide_fn in _hide_hooks:
        _hide_hooks.remove(hide_fn)
    if show_fn in _show_hooks:
        _show_hooks.remove(show_fn)


def set_hide_on_capture(on: bool) -> None:
    global _hide_on_capture
    _hide_on_capture = on


def is_hide_on_capture() -> bool:
    return _hide_on_capture


def set_draw_cursor(on: bool) -> None:
    """全局开关：是否在截图上绘制红色十字光标。默认关闭。"""
    global _draw_cursor
    _draw_cursor = on


def is_draw_cursor() -> bool:
    return _draw_cursor


def hide_capture_windows() -> None:
    for fn in list(_hide_hooks):
        try:
            fn()
        except Exception:
            pass


def show_capture_windows() -> None:
    for fn in list(_show_hooks):
        try:
            fn()
        except Exception:
            pass


def get_real_screen_size() -> Tuple[int, int]:
    """获取物理屏幕尺寸（高 DPI 修正后）。"""
    try:
        SM_CXSCREEN = 0
        SM_CYSCREEN = 1
        w = user32.GetSystemMetrics(SM_CXSCREEN)
        h = user32.GetSystemMetrics(SM_CYSCREEN)
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    # 回退到 mss
    try:
        with mss.mss() as sct:
            mon = sct.monitors[0]
            return mon["width"], mon["height"]
    except Exception:
        return 1920, 1080


def _draw_cursor_on_image(img: Image.Image) -> Image.Image:
    """在图像上绘制实时鼠标光标位置。

    使用 Win32 GetCursorPos 获取当前鼠标坐标，在对应位置绘制红色十字光标，
    使自主代理模型能够看到当前鼠标位置与输出坐标在画面中的真实位置。
    """
    try:
        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        cx, cy = pt.x, pt.y
        draw = ImageDraw.Draw(img)
        # 十字光标参数
        r = 10          # 内圈半径
        gap = 3         # 中心空心间隙
        arm = 8         # 十字臂延伸长度
        color = "#FF3333"
        # 外圈
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=2)
        # 中心点
        draw.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=color)
        # 十字线（四向延伸，中心留间隙）
        draw.line([cx, cy - r - arm, cx, cy - gap], fill=color, width=2)
        draw.line([cx, cy + gap, cx, cy + r + arm], fill=color, width=2)
        draw.line([cx - r - arm, cy, cx - gap, cy], fill=color, width=2)
        draw.line([cx + gap, cy, cx + r + arm, cy], fill=color, width=2)
        return img
    except Exception:
        return img


def capture_screen(hide: Optional[bool] = None,
                   draw_cursor: Optional[bool] = None) -> bytes:
    """截取整个主屏，返回 PNG 字节。

    截图前根据 _hide_on_capture 标志决定是否隐藏代理窗口，截图后恢复。
    是否绘制红色十字光标由全局 _draw_cursor 决定，draw_cursor 参数可临时覆盖。
    hide 参数保留兼容性，但行为由 _hide_on_capture 控制。
    """
    do_hide = _hide_on_capture if hide is None else hide
    do_draw = _draw_cursor if draw_cursor is None else draw_cursor
    if do_hide:
        hide_capture_windows()
        time.sleep(_CAPTURE_HIDE_DELAY)
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(monitor)
            # mss 返回 BGRA，转 PIL RGBA
            img = Image.frombytes("RGBA", shot.size, shot.bgra, "raw", "BGRA")
            # 转 RGB PNG
            if img.mode != "RGB":
                img = img.convert("RGB")
            # 仅自主代理模式在截图上绘制红色十字光标
            if do_draw:
                img = _draw_cursor_on_image(img)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        # 截图失败返回空，上层应处理
        raise
    finally:
        if do_hide:
            show_capture_windows()


def make_hide_show_hooks(hwnd: int) -> Tuple[Callable[[], None], Callable[[], None]]:
    """为给定窗口句柄构造 hide/show 钩子。

    用 GetAncestor(GA_ROOT) 取真实顶层 HWND——Qt 的 winId() 在某些
    Frameless/Tool 窗口上可能返回内部代理句柄，直接 ShowWindow 会失效。
    """
    if not hwnd:
        def noop() -> None:
            pass
        return noop, noop
    try:
        root = user32.GetAncestor(hwnd, GA_ROOT)
        if root:
            hwnd = root
    except Exception:
        pass

    def hide_fn() -> None:
        try:
            user32.ShowWindow(hwnd, SW_HIDE)
        except Exception:
            pass

    def show_fn() -> None:
        try:
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        except Exception:
            pass

    return hide_fn, show_fn
