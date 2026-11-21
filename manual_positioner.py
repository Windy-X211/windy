# -*- coding: utf-8 -*-
"""手动定位器：通过鼠标中键在屏幕上拾取坐标或框选模板区域。

提供两个函数：
- pick_coordinate() → (x_norm, y_norm)
- pick_template_area() → (template_path, cx_norm, cy_norm, size_px)

使用 Windows 低层鼠标钩子（WH_MOUSE_LL）全局监听鼠标中键，
配合屏幕顶部居中的半透明提示窗口。
"""

from __future__ import annotations

import ctypes
import io
import os
from ctypes import wintypes
from typing import Optional, Tuple

from PIL import Image

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QLabel, QVBoxLayout, QWidget,
)

import app_config
import capture

# ---------------------------------------------------------------------------
# Windows API 常量与类型
# ---------------------------------------------------------------------------

WH_MOUSE_LL = 14
WM_MBUTTONDOWN = 0x0207
WM_KEYDOWN = 0x0100
VK_ESCAPE = 0x1B


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, wintypes.INT, wintypes.WPARAM, wintypes.LPARAM
)

user32 = ctypes.windll.user32
user32.SetWindowsHookExW.argtypes = [
    wintypes.INT, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD,
]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK, wintypes.INT, wintypes.WPARAM, wintypes.LPARAM,
]
user32.CallNextHookEx.restype = ctypes.c_long
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# 屏幕顶部居中提示窗口
# ---------------------------------------------------------------------------

class _TipOverlay(QWidget):
    """无边框、透明背景、屏幕顶部居中的小提示条，自动消失。"""

    def __init__(
        self,
        text: str,
        timeout_ms: int = 30000,
    ) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._label = QLabel(text, self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setWordWrap(True)
        self._label.setMinimumWidth(200)
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        self._label.setFont(font)
        self._label.setStyleSheet("color:white; padding:8px 24px;")
        self.setStyleSheet(
            "background:rgba(0,0,0,160); border-radius:8px;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)
        self.adjustSize()
        self._center_top()

        if timeout_ms > 0:
            QTimer.singleShot(timeout_ms, self.hide)

    def _center_top(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        sg = screen.availableGeometry()
        x = sg.x() + (sg.width() - self.width()) // 2
        y = sg.y() + 30
        self.move(x, y)

    def set_overlay_text(self, text: str) -> None:
        self._label.setText(text)
        self._label.adjustSize()
        self.adjustSize()
        self._center_top()


# ---------------------------------------------------------------------------
# 低层鼠标钩子
# ---------------------------------------------------------------------------

def _install_hook(result_list: list) -> Tuple[any, any]:
    """安装 WH_MOUSE_LL 钩子，返回 (hHook, hook_proc)。

    hook_proc 必须保持存活（不被 GC），否则钩子会失效。
    """
    @HOOKPROC
    def hook_proc(nCode, wParam, lParam):
        if nCode >= 0:
            if wParam == WM_MBUTTONDOWN:
                struct = ctypes.cast(
                    lParam, ctypes.POINTER(MSLLHOOKSTRUCT)
                ).contents
                result_list.append((struct.pt.x, struct.pt.y))
                # 返回 1 阻止事件继续传递（避免误操作）
                return 1
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    hHook = user32.SetWindowsHookExW(WH_MOUSE_LL, hook_proc, None, 0)
    return hHook, hook_proc


def _wait_for_result(
    result_list: list,
    expected_count: int = 1,
) -> bool:
    """使用 QEventLoop + 定时器等待钩子结果，不忙轮询。

    返回 True 表示已收集到足够数量的点，False 表示超时。
    """
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(100)

    def _poll():
        if len(result_list) >= expected_count:
            timer.stop()
            loop.quit()

    timer.timeout.connect(_poll)
    timer.start()

    # 60 秒超时
    QTimer.singleShot(60000, loop.quit)

    loop.exec()
    return len(result_list) >= expected_count


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def pick_coordinate() -> Optional[Tuple[float, float]]:
    """拾取坐标：鼠标中键点击目标位置，返回归一化 (x, y)。

    在主线程中调用（会阻塞但正确处理 Qt 事件）。
    """
    overlay = _TipOverlay("请按下鼠标中键定位目标位置")
    overlay.show()

    result: list = []
    hHook, _ = _install_hook(result)

    try:
        ok = _wait_for_result(result, expected_count=1)
        if not ok or not result:
            return None
        px, py = result[0]
        sw, sh = capture.get_real_screen_size()
        if sw <= 0 or sh <= 0:
            return None
        return (px / sw, py / sh)
    finally:
        if hHook:
            user32.UnhookWindowsHookEx(hHook)
        overlay.hide()
        overlay.deleteLater()


def pick_template_area(save_dir: Optional[str] = None,
                       filename: Optional[str] = None,
                       temp: bool = False) -> Optional[Tuple[str, float, float, int]]:
    """框选模板区域：两次中键分别标记左上角和右下角。

    返回 (template_path, cx_norm, cy_norm, size_px) 或 None。
    模板文件保存在 save_dir 下（默认使用兜底模板目录）。
    若提供 filename，则使用该文件名（不含扩展名）；否则使用 _manual_template。
    temp=True 时保存到临时文件（`{base}.temp.png`，用于预览暂存，
    正式覆盖由调用方在确认保存时决定），避免未确认就覆盖正式模板。
    """
    if save_dir is None:
        save_dir = app_config.templates_fallback_dir()
    base = filename or "_manual_template"
    overlay = _TipOverlay("请按下鼠标中键标记模板左上角")
    overlay.show()

    result: list = []
    hHook, _ = _install_hook(result)

    try:
        # 等待第一次点击
        ok = _wait_for_result(result, expected_count=1)
        if not ok or not result:
            return None
        # 更新提示
        overlay.set_overlay_text("请按下鼠标中键标记模板右下角")
        # 等待第二次点击
        ok = _wait_for_result(result, expected_count=2)
        if not ok or len(result) < 2:
            return None
        x1, y1 = result[0]
        x2, y2 = result[1]
    finally:
        if hHook:
            user32.UnhookWindowsHookEx(hHook)
        overlay.hide()
        overlay.deleteLater()

    # 计算中心点和大小
    cx_px = (x1 + x2) / 2.0
    cy_px = (y1 + y2) / 2.0
    size_px = max(abs(x2 - x1), abs(y2 - y1))
    if size_px < 10:
        size_px = 50

    # 截图并裁剪
    try:
        shot_bytes = capture.capture_screen()
        img = Image.open(io.BytesIO(shot_bytes))
        sw, sh = img.size
    except Exception:
        return None

    cx_norm = cx_px / sw
    cy_norm = cy_px / sh

    half = int(size_px) // 2
    left = max(0, int(cx_px) - half)
    top = max(0, int(cy_px) - half)
    right = min(sw, left + int(size_px))
    bottom = min(sh, top + int(size_px))
    cropped = img.crop((left, top, right, bottom))

    # 保存（只保存裁剪后的模板，不保留全图）
    app_config._ensure_dir(save_dir)
    if temp:
        # 临时预览文件，正式覆盖由调用方确认后决定
        tpl_path = os.path.join(save_dir, f"{base}.temp.png")
    else:
        tpl_path = os.path.join(save_dir, f"{base}.png")
    cropped.save(tpl_path, "PNG")

    return (tpl_path, cx_norm, cy_norm, int(size_px))