# -*- coding: utf-8 -*-
"""执行器：把 Step 参数转化为键鼠操作。

显式 dispatch 字典分发，零 exec/eval。
- 鼠标操作：归一化坐标 → 像素，pyautogui 执行
- 输入：剪贴板置 CF_UNICODETEXT + ctrl+v（中文可靠）
- 按键：含 + → hotkey 组合；否则 press 单键
- 拖动：moveTo(起点)+mouseDown+moveTo(终点)+mouseUp
- 等待：sleep

无法识别的参数抛 UnrecognizedParam，由上层反馈规划模型。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Tuple

import ctypes

import capture
import pyautogui

# 关闭 fail-safe（自动化频繁移动鼠标，避免误触角落中止）
pyautogui.FAILSAFE = False
# 每个动作后短暂停顿，让界面有时间响应
pyautogui.PAUSE = 0.1

# 剪贴板
CF_UNICODETEXT = 13

# 按键名归一化映射（pyautogui 接受的键名）
_KEY_ALIASES = {
    "enter": "enter", "return": "enter",
    "esc": "esc", "escape": "esc",
    "tab": "tab", "space": "space",
    "backspace": "backspace", "bs": "backspace",
    "delete": "delete", "del": "delete",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5",
    "f6": "f6", "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
}


class UnrecognizedParam(Exception):
    """执行器无法识别的步骤参数。"""


# ---------------------------------------------------------------------------
# 坐标转换
# ---------------------------------------------------------------------------

def norm_to_pixel(coord: Optional[Tuple[float, float]],
                  screen_size: Optional[Tuple[int, int]] = None) -> Tuple[int, int]:
    """归一化坐标 (0~1) → 像素坐标。"""
    if coord is None or len(coord) != 2:
        raise UnrecognizedParam(f"坐标无效：{coord}")
    x, y = float(coord[0]), float(coord[1])
    # 允许微小浮点误差（如 1.0000000000000002），超出范围后 clamp 到屏幕内
    EPS = 1e-6
    if not (-EPS <= x <= 1.0 + EPS and -EPS <= y <= 1.0 + EPS):
        raise UnrecognizedParam(f"坐标越界：{coord}")
    w, h = screen_size or capture.get_real_screen_size()
    # 限制在屏幕内
    px = max(0, min(int(x * w), w - 1))
    py = max(0, min(int(y * h), h - 1))
    return px, py


# ---------------------------------------------------------------------------
# 剪贴板输入
# ---------------------------------------------------------------------------

def _set_clipboard_text(text: str) -> bool:
    """通过 Win32 API 设置剪贴板文本（支持中文）。

    必须用 c_void_p / c_size_t 声明返回类型，否则在 64 位系统上句柄会被
    截断为 int（32 位），导致 GlobalAlloc/SetClipboardData 返回错误句柄而失败。
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # 声明 64 位安全的返回类型与参数类型
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    GMEM_MOVEABLE = 0x0002
    data = text + "\0"
    buf = data.encode("utf-16-le")
    # CF_UNICODETEXT 需要以字节计的长度（含末尾 \0\0）
    size = len(buf) + 2

    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not h:
        return False
    try:
        lock = kernel32.GlobalLock(h)
        if not lock:
            return False
        try:
            ctypes.memmove(lock, buf, len(buf))
            ctypes.memmove(ctypes.c_void_p(lock + len(buf)), b"\x00\x00", 2)
        finally:
            kernel32.GlobalUnlock(h)

        if not user32.OpenClipboard(0):
            return False
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, h):
                # 失败：需自行释放内存
                return False
            # 成功：剪贴板接管内存所有权，不可再 GlobalFree
            h = 0
            return True
        finally:
            user32.CloseClipboard()
    finally:
        if h:
            kernel32.GlobalFree(h)


# ---------------------------------------------------------------------------
# 单步执行
# ---------------------------------------------------------------------------

def execute_step(step: Dict[str, Any],
                 screen_size: Optional[Tuple[int, int]] = None,
                 scroll_amount: int = 5,
                 move_duration: float = 0.0) -> bool:
    """执行单步。成功返回 True；参数无法识别抛 UnrecognizedParam。"""
    op = step.get("op", "")
    handler = _DISPATCH.get(op)
    if handler is None:
        raise UnrecognizedParam(f"未知操作类型：{op}")
    return handler(step, screen_size, scroll_amount, move_duration)


def click_coord(coord: Tuple[float, float],
                screen_size: Optional[Tuple[int, int]] = None,
                move_duration: float = 0.0) -> None:
    """单独暴露：在归一化 coord 处单击（用于长输入步骤先聚焦输入框）。"""
    x, y = norm_to_pixel(coord, screen_size)
    pyautogui.click(x, y, duration=move_duration)


def _do_click(step: Dict[str, Any], screen_size, scroll_amount, move_duration=0.0) -> bool:
    x, y = norm_to_pixel(step.get("coord"), screen_size)
    pyautogui.click(x, y, duration=move_duration)
    return True


def _do_double(step: Dict[str, Any], screen_size, scroll_amount, move_duration=0.0) -> bool:
    x, y = norm_to_pixel(step.get("coord"), screen_size)
    pyautogui.doubleClick(x, y, duration=move_duration)
    return True


def _do_right(step: Dict[str, Any], screen_size, scroll_amount, move_duration=0.0) -> bool:
    x, y = norm_to_pixel(step.get("coord"), screen_size)
    pyautogui.rightClick(x, y, duration=move_duration)
    return True


def _do_scroll_up(step: Dict[str, Any], screen_size, scroll_amount, move_duration=0.0) -> bool:
    x, y = norm_to_pixel(step.get("coord"), screen_size)
    pyautogui.moveTo(x, y, duration=move_duration)
    pyautogui.scroll(int(scroll_amount))
    return True


def _do_scroll_down(step: Dict[str, Any], screen_size, scroll_amount, move_duration=0.0) -> bool:
    x, y = norm_to_pixel(step.get("coord"), screen_size)
    pyautogui.moveTo(x, y, duration=move_duration)
    pyautogui.scroll(-int(scroll_amount))
    return True


def _do_drag(step: Dict[str, Any], screen_size, scroll_amount, move_duration=0.0) -> bool:
    start = step.get("start_coord")
    end = step.get("end_coord")
    if not start or not end:
        raise UnrecognizedParam(f"拖动缺少起止坐标：start={start}, end={end}")
    sx, sy = norm_to_pixel(start, screen_size)
    ex, ey = norm_to_pixel(end, screen_size)
    d = max(move_duration, 0.1)  # 拖动至少需要少量时间，保证 mouseDown 到位
    pyautogui.moveTo(sx, sy, duration=d)
    pyautogui.mouseDown(sx, sy, button="left")
    pyautogui.moveTo(ex, ey, duration=d * 2)
    pyautogui.mouseUp(ex, ey, button="left")
    return True


def _set_clipboard_text_fallback(text: str) -> bool:
    """备用方案：pyperclip（内部走 tkinter/ctypes，跨平台且对中文友好）。"""
    try:
        import pyperclip
        pyperclip.copy(text)
        # 校验是否真的写进去
        if pyperclip.paste() == text:
            return True
    except Exception:
        pass
    # 最后兜底：tkinter clipboard_clear/append
    try:
        import tkinter as tk  # type: ignore
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception:
        return False


def _do_input(step: Dict[str, Any], screen_size, scroll_amount, move_duration=0.0) -> bool:
    text = step.get("text")
    if text is None:
        raise UnrecognizedParam("输入步骤缺少 text")
    return paste_text_chunk(text)


def paste_text_chunk(text: str) -> bool:
    """把一段文本写入剪贴板并按 ctrl+v 粘贴（分段长输入的最小单元）。

    成功返回 True；写剪贴板三级容错均失败抛 UnrecognizedParam。
    """
    if text == "":
        return True  # 空段跳过
    # 主方案 Win32 → 备用 pyperclip → 兜底 tkinter
    if not _set_clipboard_text(text):
        if not _set_clipboard_text_fallback(text):
            raise UnrecognizedParam(
                f"无法设置剪贴板文本（Win32/pyperclip/tkinter 均失败）：{text}"
            )
    pyautogui.hotkey("ctrl", "v")
    return True


def _do_long_input(step: Dict[str, Any], screen_size, scroll_amount, move_duration=0.0) -> bool:
    """执行「长输入」步骤正文粘贴：
    - 如果 step 中 text_full（模型已经流式生成完，一次性粘贴），走 _do_input
    - 否则分段粘贴由 worker 通过 paste_text_chunk 独立调用（边生成边粘）
    """
    text = step.get("text") or step.get("text_full")
    if text:
        return paste_text_chunk(text)
    # 没有预生成文本：默认走 paste_text_chunk 但什么也不做（worker 会处理）
    return True


def _do_key(step: Dict[str, Any], screen_size, scroll_amount, move_duration=0.0) -> bool:
    key = step.get("key")
    if not key:
        raise UnrecognizedParam("按键步骤缺少 key")
    key = key.strip()
    if "+" in key:
        parts = [_KEY_ALIASES.get(p.strip().lower(), p.strip().lower()) for p in key.split("+")]
        parts = [p for p in parts if p]
        if not parts:
            raise UnrecognizedParam(f"按键组合无效：{key}")
        pyautogui.hotkey(*parts)
    else:
        k = _KEY_ALIASES.get(key.lower(), key.lower())
        pyautogui.press(k)
    return True


def _do_wait(step: Dict[str, Any], screen_size, scroll_amount, move_duration=0.0) -> bool:
    seconds = step.get("seconds")
    if seconds is None:
        seconds = 1.0
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 1.0
    time.sleep(max(0.0, min(seconds, 60.0)))
    return True


_DISPATCH: Dict[str, Callable[[Dict[str, Any], Optional[Tuple[int, int]], int, float], bool]] = {
    "单击": _do_click,
    "双击": _do_double,
    "右键": _do_right,
    "上滑": _do_scroll_up,
    "下滑": _do_scroll_down,
    "拖动": _do_drag,
    "输入": _do_input,
    "长输入": _do_long_input,
    "按键": _do_key,
    "等待": _do_wait,
}
