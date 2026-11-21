# -*- coding: utf-8 -*-
"""悬浮球：无边框置顶可拖动圆球，单击切换主窗口显示/隐藏。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtGui import QAction, QPainter, QColor, QBrush, QPen, QPaintEvent, QMouseEvent
from PySide6.QtWidgets import QMenu, QWidget


class FloatingBall(QWidget):
    toggled = Signal()  # 单击（非拖动）时发射，主窗口据此切换显示/隐藏
    stay_on_top_toggled = Signal(bool)  # 右键菜单「置顶」切换
    exit_requested = Signal()  # 右键菜单「退出程序」
    controlCenterRequested = Signal()  # 右键菜单「控制中心」
    openErroredMasterWorkflow = Signal()  # 打开出错母工作流
    taskListToggleRequested = Signal(bool)  # 右键菜单「任务清单」开关（True=显示）

    BALL_SIZE = 56
    DRAG_THRESHOLD = 5

    # 预设颜色常量
    COLOR_DEFAULT = (46, 120, 220)      # 蓝色（默认）
    COLOR_GREEN = (46, 180, 100)        # 绿色（母工作流启用）
    COLOR_RED = (220, 60, 60)           # 红色（执行出错）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self.BALL_SIZE, self.BALL_SIZE)
        self._press_pos: QPoint | None = None
        self._dragged = False
        self._hover = False
        self._on_top = False  # 主窗口置顶状态
        self._color = self.COLOR_DEFAULT  # (r, g, b) 当前颜色
        self._error_label = ""  # 出错母工作流标签，用于右键菜单打开
        self._task_list_shown = True  # 任务清单当前是否显示（默认打开）

        # 右键菜单动作
        self._act_top = QAction("置顶", self)
        self._act_top.setCheckable(True)
        self._act_top.setChecked(False)
        self._act_top.triggered.connect(self._on_top_action)

    def set_color(self, color_name: str) -> None:
        """设置悬浮球颜色。
        
        Args:
            color_name: "default" | "green" | "red"
        """
        color_map = {
            "default": self.COLOR_DEFAULT,
            "green": self.COLOR_GREEN,
            "red": self.COLOR_RED,
        }
        new_color = color_map.get(color_name)
        if new_color and new_color != self._color:
            self._color = new_color
            self.update()

    def set_error_label(self, label: str) -> None:
        """设置出错母工作流标签，右键菜单显示对应入口。"""
        self._error_label = label
        self.update()

    def set_task_list_shown(self, shown: bool) -> None:
        """更新任务清单显示状态，用于右键菜单勾选态同步。"""
        self._task_list_shown = shown

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 外圈
        r = self.rect().adjusted(2, 2, -2, -2)
        r_base, g_base, b_base = self._color
        hover_color = QColor(
            min(r_base + 30, 255),
            min(g_base + 30, 255),
            min(b_base + 30, 255),
        )
        normal_color = QColor(r_base, g_base, b_base, 220)
        color = hover_color if self._hover else normal_color
        p.setBrush(QBrush(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(r)
        # 中心图标（简易三横线代表菜单）
        p.setPen(QPen(QColor(255, 255, 255), 2))
        cx = self.width() // 2
        cy = self.height() // 2
        p.drawLine(cx - 10, cy - 6, cx + 10, cy - 6)
        p.drawLine(cx - 10, cy, cx + 10, cy)
        p.drawLine(cx - 10, cy + 6, cx + 10, cy + 6)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = False
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self._press_pos
            # 判断是否拖动
            if not self._dragged:
                moved = (event.globalPosition().toPoint() - (self.frameGeometry().topLeft() + self._press_pos)).manhattanLength()
                if moved > self.DRAG_THRESHOLD:
                    self._dragged = True
            if self._dragged:
                self.move(new_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._dragged:
                self.toggled.emit()
            self._press_pos = None
            self._dragged = False
            event.accept()

    def contextMenuEvent(self, event) -> None:
        """右键弹出菜单：控制中心 / 打开出错母工作流 / 置顶切换 / 退出程序。"""
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;color:black;border:1px solid #ccc;padding:4px;}"
            "QMenu::item{padding:6px 24px;}"
            "QMenu::item:selected{background:#3498db;color:white;}"
        )

        # 出错母工作流入口（仅在出错时显示）
        if self._error_label:
            act_err = menu.addAction(f"⚠ 打开出错母工作流「{self._error_label}」")
            act_err.triggered.connect(self.openErroredMasterWorkflow.emit)
            menu.addSeparator()

        act_cc = menu.addAction("📋 控制中心")
        act_cc.triggered.connect(self.controlCenterRequested.emit)
        menu.addSeparator()
        act_tasklist = menu.addAction("📋 任务清单")
        act_tasklist.setCheckable(True)
        act_tasklist.setChecked(self._task_list_shown)
        act_tasklist.toggled.connect(lambda checked: self.taskListToggleRequested.emit(checked))
        menu.addSeparator()
        menu.addAction(self._act_top)
        menu.addSeparator()
        act_exit = menu.addAction("退出程序")
        act_exit.triggered.connect(self.exit_requested.emit)
        menu.exec(event.globalPos())

    def _on_top_action(self, checked: bool) -> None:
        """置顶动作：更新内部状态并发射信号。"""
        self._on_top = checked
        self._act_top.setChecked(checked)
        self._act_top.setText("取消置顶" if checked else "置顶")
        self.stay_on_top_toggled.emit(checked)
