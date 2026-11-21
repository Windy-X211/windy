# -*- coding: utf-8 -*-
"""任务清单：屏幕右上角系统级鼠标穿透、半透明的小表格。

显示所有启用的母工作流；设置了"每天定时"的在开头画定时标记。
当天任务完成 → 打勾 ✓，失败 → 打叉 ✗，均标注完成时刻（小时:分钟）。

特性：
- 系统级鼠标穿透：Win32 扩展样式 WS_EX_TRANSPARENT | WS_EX_LAYERED，
  鼠标点击会穿透到窗口下方的按钮（Qt 层的 WA_TransparentForMouseEvents
  在 Windows 上仍会拦截系统点击，仅靠它无法点到下层按钮）。
- 背景透明度可在「⚙ 设置 → 界面与外观 → 任务清单透明度」中调整
  （0~100%，0=不透明，100=完全透明）：仅淡化卡片背景/边框，文字保持
  不透明可读；调整时窗口位置保持不变。
"""

from __future__ import annotations

import ctypes
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
)

import app_config


# 基础颜色（RGB 元组；最终 alpha 由透明度设置统一换算）
_BASE_BG = (245, 245, 245)       # 卡片背景
_BASE_BORDER = (180, 180, 180)   # 边框
_BASE_TITLE = (60, 60, 60)       # 标题
_BASE_TEXT = (40, 40, 40)        # 正文
_BASE_OK = (46, 160, 80)         # 绿 ✓
_BASE_FAIL = (220, 60, 60)       # 红 ✗
_BASE_TIMER = (230, 126, 34)     # 定时标记橙
_BASE_EMPTY = (153, 153, 153)    # 空列表灰色
_BG_ALPHA_BASE = 235             # 卡片背景基础 alpha（原半透明白）


def _scaled_alpha(base_alpha: int, pct: int) -> int:
    """按透明度百分比（0~100，0=不透明，100=完全透明）换算最终 alpha（0~255）。"""
    pct = max(0, min(100, int(pct)))
    return int(round(base_alpha * (100 - pct) / 100.0))


def _rgba(rgb, alpha: int) -> str:
    """生成 stylesheet 可用的 rgba(r,g,b,a) 字符串。"""
    r, g, b = rgb
    alpha = max(0, min(255, int(alpha)))
    return f"rgba({r},{g},{b},{alpha})"


class _TaskRow(QFrame):
    """任务清单中的一行。"""

    def __init__(self, text: str, status: Optional[str], time_str: str = "",
                 is_daily: bool = False, index: int = 0, scheduled: str = "",
                 parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;border:none;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(6)

        # 序号
        idx_lbl = QLabel(str(index) if index else "·")
        idx_lbl.setStyleSheet(
            f"color:{_rgba(_BASE_TEXT, 255)};font-size:12px;font-weight:bold;"
            f"background:transparent;")
        idx_lbl.setFixedWidth(14)
        lay.addWidget(idx_lbl)

        # 定时标记 + 预定时间
        if is_daily:
            timer_lbl = QLabel("⏰")
            timer_lbl.setStyleSheet(
                f"color:{_rgba(_BASE_TIMER, 255)};font-size:12px;background:transparent;")
            timer_lbl.setFixedWidth(18)
            lay.addWidget(timer_lbl)
            if scheduled:
                sch_lbl = QLabel(scheduled)
                sch_lbl.setStyleSheet(
                    f"color:{_rgba(_BASE_TIMER, 255)};font-size:11px;"
                    f"background:transparent;")
                sch_lbl.setFixedWidth(38)
                lay.addWidget(sch_lbl)
        else:
            spacer = QLabel("")
            spacer.setFixedWidth(18)
            lay.addWidget(spacer)

        # 文本
        text_lbl = QLabel(text)
        text_lbl.setStyleSheet(
            f"color:{_rgba(_BASE_TEXT, 255)};font-size:12px;background:transparent;")
        text_lbl.setWordWrap(False)
        lay.addWidget(text_lbl, 1)

        # 状态
        if status is not None:
            if status == "success":
                mark = "✓"
                color = _BASE_OK
            else:
                mark = "✗"
                color = _BASE_FAIL
            status_lbl = QLabel(f"{mark} {time_str}" if time_str else mark)
            status_lbl.setStyleSheet(
                f"color:{_rgba(color, 255)};font-size:11px;font-weight:bold;"
                f"background:transparent;")
            lay.addWidget(status_lbl)


class TaskListWidget(QWidget):
    """右上角任务清单悬浮小表格（系统级鼠标穿透、透明度可调）。"""

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        # 鼠标穿透（Qt 层）
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._tasks: List[Dict[str, Any]] = []
        self._layout_hide = False  # 预留
        # 透明度（0~100，0=不透明，100=完全透明），默认取设置值
        self._opacity_pct = int(app_config.load_settings().get("task_list_opacity", 0))

        self._build_ui()

    # ------------------------------------------------------------------
    # 系统级鼠标穿透
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_click_through(hwnd: int) -> None:
        """给窗口加 WS_EX_TRANSPARENT | WS_EX_LAYERED，实现系统级鼠标穿透。

        Qt 的 WA_TransparentForMouseEvents 只让 Qt 内部忽略鼠标事件，
        Windows 层面窗口仍会拦截点击（WM_NCHITTEST 返回 HTCLIENT），
        无法点到下方按钮。加上这两个扩展样式后，鼠标事件会穿透到下层窗口。
        """
        if not hwnd:
            return
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        try:
            user32 = ctypes.windll.user32
            GetWindowLongPtrW = user32.GetWindowLongPtrW
            SetWindowLongPtrW = user32.SetWindowLongPtrW
            GetWindowLongPtrW.restype = ctypes.c_ssize_t
            GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            SetWindowLongPtrW.restype = ctypes.c_ssize_t
            SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            if ex & (WS_EX_LAYERED | WS_EX_TRANSPARENT) != (WS_EX_LAYERED | WS_EX_TRANSPARENT):
                SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 透明度
    # ------------------------------------------------------------------
    def set_opacity(self, pct: int) -> None:
        """按设置值（0~100，0=不透明，100=完全透明）调整任务清单背景透明度。

        只淡化卡片背景/边框，文字保持不透明可读；
        仅刷新样式，不重建行、不 reposition，窗口位置保持不变。
        """
        pct = max(0, min(100, int(pct)))
        if pct == self._opacity_pct:
            return
        self._opacity_pct = pct
        self._refresh_styles()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._card = QFrame()
        self._card.setObjectName("taskCard")
        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(4, 4, 4, 4)
        card_lay.setSpacing(2)

        # 标题
        self._title = QLabel("📋 任务清单")
        card_lay.addWidget(self._title)

        # 行容器
        self._rows_widget = QWidget()
        self._rows_lay = QVBoxLayout(self._rows_widget)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(1)
        card_lay.addWidget(self._rows_widget)

        outer.addWidget(self._card)
        self._refresh_styles()
        self.adjustSize()

    def _refresh_styles(self) -> None:
        """按当前透明度刷新卡片背景/边框样式。

        仅背景与边框随透明度淡化；标题与正文文字始终保持不透明（可读）。
        行样式在 set_tasks 中固定为不透明，此处无需处理。
        """
        pct = self._opacity_pct
        bg_alpha = _scaled_alpha(_BG_ALPHA_BASE, pct)
        border_alpha = _scaled_alpha(255, pct)
        self._card.setStyleSheet(
            f"#taskCard{{background:{_rgba(_BASE_BG, bg_alpha)};"
            f"border:1px solid {_rgba(_BASE_BORDER, border_alpha)};border-radius:8px;}}"
        )
        self._title.setStyleSheet(
            f"color:{_rgba(_BASE_TITLE, 255)};font-size:12px;font-weight:bold;"
            f"padding:2px 6px;background:transparent;")

    def set_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        """设置任务清单数据。

        Args:
            tasks: 每项 {"id","label","is_daily","status","time"}
                  status: None=未完成, "success"=完成, "failed"=失败
                  time: "HH:MM" 完成时刻
        """
        self._tasks = tasks
        # 重建行（行文字始终不透明）
        while self._rows_lay.count():
            item = self._rows_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not tasks:
            empty = QLabel("（无启用的母工作流）")
            empty.setStyleSheet(
                f"color:{_rgba(_BASE_EMPTY, 255)};font-size:11px;"
                f"padding:6px 8px;background:transparent;")
            self._rows_lay.addWidget(empty)
        else:
            for t in tasks:
                row = _TaskRow(
                    t.get("label", "未命名"),
                    t.get("status"),
                    t.get("time", ""),
                    is_daily=t.get("is_daily", False),
                    index=t.get("index", 0),
                    scheduled=t.get("scheduled", ""),
                    parent=self._rows_widget,
                )
                self._rows_lay.addWidget(row)
        self._rows_lay.addStretch(1)
        self.adjustSize()
        # 保持右上角位置
        self._position_top_right()

    def showEvent(self, event) -> None:
        """每次显示时应用系统级鼠标穿透并重新定位到屏幕右上角。"""
        super().showEvent(event)
        self._apply_click_through(int(self.winId()))
        self._position_top_right()

    def _position_top_right(self) -> None:
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen is None:
            return
        self.adjustSize()
        margin = 12
        x = screen.right() - self.width() - margin
        y = screen.top() + margin
        self.move(x, y)
