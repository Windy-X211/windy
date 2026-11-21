# -*- coding: utf-8 -*-
"""消息横幅组件：在父控件顶部显示一条带图标、强调色条与投影的提示，自动消失，无声音。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QHBoxLayout, QSizePolicy,
)


class ToastBanner(QFrame):
    """在父控件顶部出现的消息横幅，自动消失，无声音。

    用法::

        ToastBanner.show_message(self, "设置已保存", "success")
        ToastBanner.show_message(self, "至少保留一个预设", "warning")
    """

    DURATION = 2500  # 显示时长（毫秒）

    def __init__(self, parent: QWidget, text: str, level: str = "success"):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setObjectName("toast_banner")

        # 类型配色：强调色 / 渐变起始 / 渐变结束 / 文字色 / 图标
        palette = {
            "success": ("#27ae60", "#f2fbf5", "#e7f7ee", "#1e7e46", "✓"),
            "warning": ("#e67e22", "#fffaf0", "#fdf2df", "#b95f0c", "⚠"),
            "info":    ("#3498db", "#f0f7fe", "#e4f0fc", "#1d6fb8", "ℹ"),
        }
        accent, bg_from, bg_to, text_color, icon = palette.get(
            level, palette["success"])

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        # 柔和线性渐变背景 + 圆角 + 细边
        self.setStyleSheet(
            f"#toast_banner{{"
            f"  background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            f"    stop:0 {bg_from}, stop:1 {bg_to});"
            f"  border:1px solid {accent}33;"
            f"  border-left:4px solid {accent};"
            f"  border-radius:8px;"
            f"  padding:4px 6px;"
            f"}}"
        )
        # 注：不使用 QGraphicsDropShadowEffect 投影——
        # 它会扩展绘制区域超出窗口矩形，导致 Windows 分层窗口
        # UpdateLayeredWindowIndirect 报“参数错误”。层次感改由边框/渐变实现。

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 6, 14, 6)
        layout.setSpacing(10)
        layout.addStretch(1)

        # 圆形图标
        icon_lbl = QLabel(icon)
        icon_lbl.setFixedSize(22, 22)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(
            f"border:none;border-radius:11px;background:{accent};"
            f"color:white;font-size:12px;font-weight:bold;"
        )
        layout.addWidget(icon_lbl)

        self._label = QLabel(text)
        self._label.setStyleSheet(
            f"border:none;background:{bg_from};color:{text_color};"
            f"border-radius:12px;padding:4px 12px;"
            f"font-size:13px;font-weight:600;"
        )
        self._label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

        layout.addStretch(1)

        # 淡入动画
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(220)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # 自动消失定时器
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DURATION)
        self._timer.timeout.connect(self._start_fade_out)

        self._fade_out = QPropertyAnimation(self, b"windowOpacity")
        self._fade_out.setDuration(280)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_out.finished.connect(self.deleteLater)

        self.setMinimumHeight(40)

    def showEvent(self, event):
        super().showEvent(event)
        self._anim.start()
        self._timer.start()

    def _start_fade_out(self):
        self._fade_out.start()

    @staticmethod
    def show_message(parent: QWidget, text: str, level: str = "success") -> ToastBanner:
        """在父控件顶部显示一条消息横幅。

        Parameters
        ----------
        parent : QWidget
            父控件（对话框/窗口等）。
        text : str
            消息文本。
        level : str
            "success" / "warning" / "info"。
        """
        banner = ToastBanner(parent, text, level)
        # 作为浮动窗口浮于父控件之上：定位到父控件顶部居中，避免被其他 UI 遮挡
        banner.adjustSize()
        parent_tl = parent.mapToGlobal(parent.rect().topLeft())
        x = parent_tl.x() + (parent.width() - banner.width()) // 2
        y = parent_tl.y() + 12
        banner.move(x, y)
        banner.show()
        banner.raise_()
        return banner