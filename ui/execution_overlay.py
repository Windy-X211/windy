# -*- coding: utf-8 -*-
"""执行覆盖层：窗口隐藏主内容时仅显示暂停/不被截图按钮 + 进度文本 + 进度条。

进度文本每行带勾选标记与颜色：已完成绿色、未完成浅灰色。字体大小/颜色可在设置中调整。
执行时窗口背景完全透明，文字/按钮直接浮在桌面上（无边框、无卡片背景）。
"""

from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QProgressBar,
    QScrollArea, QFrame,
)

import app_config


class ExecutionOverlay(QWidget):
    """本身就是容器（不再套一层 _card），避免双层布局导致重影。"""
    pauseClicked = Signal()
    hideCaptureToggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._opacity_pct = 85
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background:transparent;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(8)

        self._title = QLabel("执行中…")
        self._title.setStyleSheet(
            "color:#2c3e50;font-weight:bold;background:transparent;padding:4px 8px;"
        )
        lay.addWidget(self._title)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self._list_widget = QWidget()
        self._list_widget.setStyleSheet("background:transparent;")
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(3)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_widget)
        lay.addWidget(self._scroll, 1)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress.setStyleSheet(
            "QProgressBar{background:rgba(0,0,0,0);border:1px solid rgba(255,255,255,100);"
            "border-radius:4px;text-align:center;color:white;} "
            "QProgressBar::chunk{background:rgba(46,139,87,220);border-radius:3px;}"
        )
        lay.addWidget(self._progress)

        btn_row = QWidget()
        btn_row.setStyleSheet("background:transparent;")
        h = QVBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._btn_pause = QPushButton("⏸ 暂停")
        self._btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_pause.setStyleSheet(
            "QPushButton{background:#e74c3c;color:white;border-radius:6px;padding:8px 20px;font-weight:bold;}"
            "QPushButton:hover{background:#ff5c41;}"
        )
        self._btn_hide = QPushButton()
        self._btn_hide.setCheckable(True)
        self._btn_hide.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_hide.setStyleSheet(
            "QPushButton{background:#95a5a6;color:white;border-radius:6px;"
            "padding:8px 16px;font-weight:bold;}"
            "QPushButton:hover{background:#7f8c8d;}"
            "QPushButton:checked{background:#3498db;}"
            "QPushButton:checked:hover{background:#2e86c1;}"
        )
        h.addWidget(self._btn_pause, alignment=Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self._btn_hide, alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(btn_row)

        self._row_labels: List[QLabel] = []
        self._btn_pause.clicked.connect(self.pauseClicked.emit)
        self._btn_hide.toggled.connect(self._on_hide_toggled)
        self._update_hide_btn_text(True)
        self._btn_hide.setChecked(True)

        self._settings = app_config.load_settings()
        self._apply_font()

    # ------------------------------------------------------------------
    def apply_execution_opacity(self, opacity_pct: int) -> None:
        """保留接口；当前背景始终全透明，只刷新行样式。"""
        self._opacity_pct = max(0, min(100, int(opacity_pct)))
        self._refresh_row_styles()

    def _refresh_row_styles(self) -> None:
        """重新应用所有步骤行的样式。"""
        color_done = self._settings.get("progress_color_done", "#2E8B57")
        color_pending = self._settings.get("progress_color_pending", "#B0B0B0")
        for lbl in self._row_labels:
            text = lbl.text()
            parts = text.split(" ", 1)
            mark = parts[0] if len(parts) == 2 else ""
            color = color_done if mark == "●" else color_pending
            lbl.setStyleSheet(
                f"color:{color};background:transparent;padding:2px 6px;"
            )

    # ------------------------------------------------------------------
    def _apply_font(self) -> None:
        size = self._settings.get("progress_font_size", 12)
        for lbl in self._row_labels:
            f = lbl.font()
            f.setPointSize(int(size))
            lbl.setFont(f)

    def _update_hide_btn_text(self, on: bool) -> None:
        self._btn_hide.setText("🚫 不被截图" if on else "📷 被截图")

    def _on_hide_toggled(self, checked: bool) -> None:
        self._update_hide_btn_text(checked)
        self.hideCaptureToggled.emit(checked)

    def reload_settings(self) -> None:
        self._settings = app_config.load_settings()
        self._apply_font()
        self._refresh_row_styles()

    def set_steps(self, steps: List[dict]) -> None:
        from step_model import step_display_summary
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._row_labels.clear()
        color_pending = self._settings.get("progress_color_pending", "#B0B0B0")
        for s in steps:
            lbl = QLabel(f"○ {step_display_summary(s)}")
            lbl.setStyleSheet(
                f"color:{color_pending};background:transparent;padding:2px 6px;"
            )
            self._list_layout.insertWidget(self._list_layout.count() - 1, lbl)
            self._row_labels.append(lbl)
        self._apply_font()
        # 步骤刷新后立即把进度条对齐到 0/N，确保执行一开始就显示正确的比例
        self.update_progress(0, len(steps))
        self._progress.show()

    def set_step_mark(self, index: int, mark: str, color: str) -> None:
        if 0 <= index < len(self._row_labels):
            text = self._row_labels[index].text()
            parts = text.split(" ", 1)
            body = parts[1] if len(parts) > 1 else ""
            self._row_labels[index].setText(f"{mark} {body}")
            self._row_labels[index].setStyleSheet(
                f"color:{color};background:transparent;padding:2px 6px;"
            )

    def update_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self._progress.setValue(0)
            return
        self._progress.setRange(0, total)
        self._progress.setValue(done)
        self._progress.setFormat(f"{done}/{total}")

    def set_hide_capture(self, on: bool) -> None:
        self._btn_hide.blockSignals(True)
        self._update_hide_btn_text(on)
        self._btn_hide.setChecked(on)
        self._btn_hide.blockSignals(False)

    def set_paused(self, paused: bool) -> None:
        """更新暂停按钮显示：暂停时显示「已暂停」，否则显示「⏸ 暂停」。"""
        self._btn_pause.blockSignals(True)
        if paused:
            self._btn_pause.setText("⏸ 已暂停")
            self._btn_pause.setStyleSheet(
                "QPushButton{background:#f39c12;color:white;border-radius:6px;"
                "padding:8px 20px;font-weight:bold;}"
                "QPushButton:hover{background:#e67e22;}"
            )
        else:
            self._btn_pause.setText("⏸ 暂停")
            self._btn_pause.setStyleSheet(
                "QPushButton{background:#e74c3c;color:white;border-radius:6px;"
                "padding:8px 20px;font-weight:bold;}"
                "QPushButton:hover{background:#ff5c41;}"
            )
        self._btn_pause.blockSignals(False)

    def clear(self) -> None:
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._row_labels.clear()
        self._progress.setValue(0)
