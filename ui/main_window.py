# -*- coding: utf-8 -*-
"""主窗口：背景图渲染、三视图堆叠（聊天/代理/执行覆盖）、顶部与底部控制栏、信号中枢。

所有 UI→Worker 的请求经 MainWindow 的信号发出；Worker→UI 的更新经槽方法处理。
UI 永不直接调用模型/截图/执行。
"""

from __future__ import annotations

import os
import re
import time
import sys
import copy
import ctypes
import tempfile
import shutil
import threading
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QSize, QTimer, QPropertyAnimation, QEasingCurve, QEvent, QThread, QObject, Property, QRectF
from PySide6.QtGui import (
    QPainter, QPixmap, QColor, QLinearGradient, QKeyEvent, QCloseEvent, QPalette,
    QIcon, QFont, QGuiApplication, QKeySequence,
)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QStackedWidget, QComboBox, QLineEdit, QPlainTextEdit, QFrame,
    QToolButton, QSizePolicy, QMessageBox, QApplication, QGraphicsOpacityEffect,
    QGridLayout, QAbstractButton, QListWidget, QListWidgetItem, QAbstractItemView,
    QDialog, QScrollArea,
)

import app_config
import capture
import agent_overlay
import model_client
from step_model import step_summary
from ui.chat_view import ChatView, CollapsibleStepsWidget, CodeBubbleWidget, CodeResultWidget, ThinkingBubbleWidget
from ui.agent_panel import AgentPanel
from ui.execution_overlay import ExecutionOverlay
from ui.settings_dialog import SettingsDialog
from ui.history_panel import HistoryPanel
from ui.workflow_panel import WorkflowPanel
from ui.control_center import ControlCenterDialog
from ui.model_config_dialog import ModelConfigDialog
from ui.task_list import TaskListWidget
from ui.video_background import VideoBackground, is_video_file, load_image_pixmap
from master_workflow_executor import MasterWorkflowExecutor


class _ImageViewerCanvas(QWidget):
    """内置图片查看器画布：保持原比例自适应填充窗口，滚轮缩放，拖拽可自由移动（不受边界束缚）。

    - zoom=1 未拖动时，图片等比缩放至完整铺进窗口（不被裁剪/遮挡）。
    - 滚轮向上放大、向下缩小；按住左键可把图片拖到窗口内任意位置，无边界限制。
    """

    def __init__(self, pixmap: QPixmap, parent=None) -> None:
        super().__init__(parent)
        self._pix = pixmap
        self._zoom = 1.0
        self._min_zoom = 0.1
        self._max_zoom = 16.0
        self._off_x = 0          # 拖动累积偏移（相对窗口中心的像素偏移）
        self._off_y = 0
        self._press = None       # 正在拖拽时的按下点 (x, y)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _fit_scale(self) -> float:
        """把图片完整铺进当前画布（contain）所需的最小缩放。"""
        w, h = self.width(), self.height()
        if w < 10 or h < 10:
            return 1.0
        pw, ph = self._pix.width(), self._pix.height()
        if pw <= 0 or ph <= 0:
            return 1.0
        return min(w / pw, h / ph)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(43, 43, 43))  # 深色底
        if self._pix.isNull():
            p.setPen(QColor(221, 221, 221))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "无法加载图片")
            p.end()
            return
        scale = self._fit_scale() * self._zoom
        sw = max(1, int(self._pix.width() * scale))
        sh = max(1, int(self._pix.height() * scale))
        scaled = self._pix.scaled(sw, sh,
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
        # 以窗口中心为基准 + 用户拖拽偏移 → 图片可自由移动
        x = self.width() // 2 - scaled.width() // 2 + self._off_x
        y = self.height() // 2 - scaled.height() // 2 + self._off_y
        p.drawPixmap(x, y, scaled)
        p.end()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self.update()

    def wheelEvent(self, ev) -> None:
        delta = ev.angleDelta().y()
        if delta:
            factor = 1.15 if delta > 0 else 1.0 / 1.15
            self._zoom = max(self._min_zoom, min(self._max_zoom, self._zoom * factor))
            self.update()
        ev.accept()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            pos = ev.position().toPoint()
            self._press = (pos.x(), pos.y())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, ev) -> None:
        if self._press is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            pos = ev.position().toPoint()
            # 图片跟随鼠标自由移动，无边界限制
            self._off_x += pos.x() - self._press[0]
            self._off_y += pos.y() - self._press[1]
            self._press = (pos.x(), pos.y())
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._press = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)


class _ImageViewerDialog(QDialog):
    """包一条画布展示图片：集团定义于 _ImageViewerCanvas。"""

    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        import os as _os
        self.setWindowTitle(f"查看图片：{_os.path.basename(path)}")
        self.resize(720, 600)
        pix = load_image_pixmap(path)
        canvas = _ImageViewerCanvas(pix, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(canvas)


# 附件：识别图片以缩略图预览，其他文件按文本内容收集发送
# 支持全部常见图片格式（与 model_client.IMAGE_EXTS 保持一致）
# 发送时按文件类型分类处理（图片保留路径引用、文本类读取内容）
_ATTACH_IMAGE_EXTS = model_client.IMAGE_EXTS
_ATTACH_MAX_TEXT_CHARS = 200000  # 文本附件最多采集的字符数（过长截断并提示）


def _file_size_human_attach(size: int) -> str:
    """字节数转为人类可读大小（B/KB/MB/GB）。"""
    if size < 1024:
        return f"{size} B"
    for unit in ("KB", "MB", "GB"):
        size /= 1024.0
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"


def _render_attach_text(content) -> str:
    """把含内嵌图片标记(@@EMBED_IMAGE@@路径)的消息文本渲染为友好展示文字（用于气泡）。"""
    if not isinstance(content, str) or model_client.EMBED_IMAGE_PREFIX not in content:
        return content
    lines = []
    for ln in content.splitlines():
        stripped = ln.strip()
        if stripped.startswith(model_client.EMBED_IMAGE_PREFIX):
            path = stripped[len(model_client.EMBED_IMAGE_PREFIX):].strip()
            lines.append(f"🖼 图片已附：{os.path.basename(path)}")
        else:
            lines.append(ln)
    return "\n".join(lines)


class _DragDropEdit(QPlainTextEdit):
    """输入框：拦截文件拖放，把文件路径以 filesDropped 信号发出，
    避免 QPlainTextEdit 默认把 URL 当作文本插入（只输入文件地址）。"""

    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        md = event.mimeData()
        if md.hasUrls() and any(
            u.isLocalFile() and u.toLocalFile()
            for u in md.urls()
        ):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        md = event.mimeData()
        if md.hasUrls():
            local = [
                u.toLocalFile()
                for u in md.urls()
                if u.isLocalFile() and u.toLocalFile()
            ]
            if local:
                event.acceptProposedAction()
                self.filesDropped.emit(local)
                return
        super().dropEvent(event)


class _AttachmentChipFrame(QFrame):
    """可双击打开的附件项容器：双击触发 activated 信号（由主窗口决定查看内容或系统打开）。"""

    activated = Signal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._chip_path = path

    def mouseDoubleClickEvent(self, event) -> None:
        self.activated.emit(self._chip_path)
        super().mouseDoubleClickEvent(event)


def _read_text_attachment(path: str) -> Optional[str]:
    """按字符读取文本附件内容；二进制/无法解码时返回 None（改用路径引用）。"""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="strict") as f:
            data = f.read()
    except (UnicodeDecodeError, OSError, PermissionError):
        return None
    if not data:
        return data
    if len(data) > _ATTACH_MAX_TEXT_CHARS:
        data = data[:_ATTACH_MAX_TEXT_CHARS]
        data += "\n…(内容过长，已截断)"
    return data


def _set_label_color(lbl, css_hex: str, point_size: float) -> None:
    """用 QPalette 给 QLabel 设置字体大小与文字颜色（不设局部样式表，
    避免遮蔽附件的 QToolTip 白底圆角样式）。"""
    f = lbl.font()
    f.setPointSizeF(point_size)
    lbl.setFont(f)
    pal = lbl.palette()
    pal.setColor(QPalette.ColorRole.WindowText, QColor(css_hex))
    lbl.setPalette(pal)


def _lighten_hex(hex_color: str, amount: float) -> str:
    """把 #rrggbb 颜色向白色混合 amount（0~1），返回新的 #rrggbb。"""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


class _ChatModeSwitch(QAbstractButton):
    """聊天页顶部的「标准 / Pro / cos」三点位滑动开关（自绘指示灯样式）。

    状态：0=标准，1=Pro，2=cos。单击循环切换，滑钮动画滑到对应段位。
    """

    # 类属性默认值：保证属性元调用（metacall）在任何时机访问都安全
    _offset = 0.0
    _state = 0

    LABELS = ("标准", "Pro", "Cos")
    COLORS = ("#3498db", "#16a085", "#8e44ad")  # 标准蓝 / Pro 绿 / Cos 紫

    stateChanged = Signal(int)  # 0=标准 1=Pro 2=Cos

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(114, 26)
        # 支杆偏移动画（offset 0..2 对应三段位）
        self._anim = QPropertyAnimation(self, b"knobOffset")
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._state = 0
        self._offset = 0.0

    def state(self) -> int:
        return self._state

    def setState(self, st: int) -> None:
        st = int(st) % 3
        if st == self._state:
            return
        self._state = st
        self._animate(st)
        self.stateChanged.emit(st)

    def _get_knob_offset(self) -> float:
        return self._offset

    def _set_knob_offset(self, v: float) -> None:
        self._offset = v
        self.update()

    knobOffset = Property(float, _get_knob_offset, _set_knob_offset)

    def _animate(self, st: int) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(float(st))
        self._anim.start()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.setState((self._state + 1) % 3)
        super().mousePressEvent(e)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        active = max(0, min(2, round(self._offset)))
        color = QColor(self.COLORS[active])
        # 轨道
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#e0e4ea"))
        p.drawRoundedRect(0, 0, w - 2, h - 2, h / 2, h / 2)
        # 滑动高亮段
        seg = (w - 6) / 3.0
        hx = 3 + self._offset * seg
        p.setBrush(color)
        p.drawRoundedRect(hx, 3, seg, h - 6, (h - 6) / 2, (h - 6) / 2)
        # 三段文字（当前段白字，其余灰字）
        f = p.font()
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        for i, label in enumerate(self.LABELS):
            cx = 3 + i * seg + seg / 2
            p.setPen(QColor("white") if i == active else QColor("#7f8c8d"))
            p.drawText(
                QRectF(cx - 21, 0, 42, h),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
        p.end()


class _ProgramProSwitch(QAbstractButton):
    """编程模式下的「标准 / Pro」两段位滑动开关（自绘）。

    状态：0=标准，1=Pro。单击循环切换，滑钮动画滑到对应段位。
    """

    # 类属性默认值：保证属性元调用（metacall）在任何时机访问都安全
    _offset = 0.0
    _state = 0

    LABELS = ("标准", "Pro")
    COLORS = ("#3498db", "#16a085")  # 标准蓝 / Pro 绿

    stateChanged = Signal(int)  # 0=标准 1=Pro

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(76, 26)
        self._anim = QPropertyAnimation(self, b"knobOffset")
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._state = 0
        self._offset = 0.0

    def state(self) -> int:
        return self._state

    def setState(self, st: int) -> None:
        st = int(st) % 2
        if st == self._state:
            return
        self._state = st
        self._animate(st)
        self.stateChanged.emit(st)

    def _get_knob_offset(self) -> float:
        return self._offset

    def _set_knob_offset(self, v: float) -> None:
        self._offset = v
        self.update()

    knobOffset = Property(float, _get_knob_offset, _set_knob_offset)

    def _animate(self, st: int) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(float(st))
        self._anim.start()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.setState((self._state + 1) % 2)
        super().mousePressEvent(e)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        active = max(0, min(1, round(self._offset)))
        color = QColor(self.COLORS[active])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#e0e4ea"))
        p.drawRoundedRect(0, 0, w - 2, h - 2, h / 2, h / 2)
        seg = (w - 6) / 2.0
        hx = 3 + self._offset * seg
        p.setBrush(color)
        p.drawRoundedRect(hx, 3, seg, h - 6, (h - 6) / 2, (h - 6) / 2)
        f = p.font()
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        for i, label in enumerate(self.LABELS):
            cx = 3 + i * seg + seg / 2
            p.setPen(QColor("white") if i == active else QColor("#7f8c8d"))
            p.drawText(
                QRectF(cx - 21, 0, 42, h),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
        p.end()


class CompletionToast(QWidget):
    """右下角弹框提示：任务/工作流执行完成。

    特性：
    - 无边框、系统提示音、淡入淡出动画
    - 5 秒后自动关闭
    - 跟随主窗口右下角位置（以主窗口为锚）
    """

    WIDTH = 340
    HEIGHT = 140
    MARGIN = 20

    def __init__(self, parent: QWidget):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self._parent = parent
        self._build_ui()
        # 动画效果
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_effect)
        self._fade_in = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_in.setDuration(300)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_out = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_out.setDuration(350)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_out.finished.connect(self.close)
        # 自动关闭
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.setInterval(5000)
        self._close_timer.timeout.connect(self._start_fade_out)

    def _build_ui(self) -> None:
        self._root = QFrame(self)
        self._root.setGeometry(0, 0, self.WIDTH, self.HEIGHT)
        self._root.setStyleSheet(
            "QFrame{"
            "  background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            "    stop:0 #ffffff,stop:1 #f5f6fa);"
            "  border-radius:14px;"
            "  border:1px solid rgba(155,89,182,0.35);"
            "}"
        )
        layout = QVBoxLayout(self._root)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)
        # 标题：✅ 图标 + "执行完成"
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        icon = QLabel("✅")
        icon.setFont(QFont("Segoe UI Emoji", 16))
        title_row.addWidget(icon)
        title_label = QLabel("执行完成")
        title_label.setStyleSheet(
            "font-weight:bold;font-size:15px;color:#6c3483;"
            "padding-top:2px;"
        )
        title_row.addWidget(title_label)
        title_row.addStretch(1)
        layout.addLayout(title_row)
        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background:rgba(155,89,182,0.2);max-height:1px;min-height:1px;")
        layout.addWidget(sep)
        # 目标标签（多行，支持长文本省略）
        self._name_label = QLabel()
        self._name_label.setStyleSheet(
            "font-size:13px;color:#2c3e50;font-weight:600;"
            "padding:2px 0;"
        )
        self._name_label.setWordWrap(True)
        self._name_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(self._name_label, 1)
        # 用时
        self._time_label = QLabel()
        self._time_label.setStyleSheet("font-size:12px;color:#7f8c8d;")
        layout.addWidget(self._time_label)

    def show_with(self, label_text: str, elapsed_seconds: float, kind: str) -> None:
        """显示提示。kind="任务"/"工作流" 用于区分语义。"""
        name_max = 38
        label_text = label_text.strip() or "未命名"
        if len(label_text) > name_max:
            label_text = label_text[:name_max] + "…"
        self._name_label.setText(f"📌 {kind}：{label_text}")
        self._time_label.setText(self._format_elapsed(elapsed_seconds))
        self._position_to_corner()
        self.show()
        self._fade_in.start()
        self._close_timer.start()
        self._play_sound()

    def _format_elapsed(self, seconds: float) -> str:
        if seconds < 60:
            return f"⏱ 执行用时：{seconds:.1f} 秒"
        minutes = int(seconds // 60)
        secs = seconds - minutes * 60
        return f"⏱ 执行用时：{minutes} 分 {secs:.0f} 秒"

    def _position_to_corner(self) -> None:
        """将弹框定位到屏幕右下角（任务栏上方的可用区域）。"""
        # 优先使用主窗口所在屏幕；若取不出来用主屏
        screen = None
        parent = self._parent
        if parent is not None and parent.windowHandle() is not None:
            screen = parent.windowHandle().screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        sg = screen.availableGeometry()
        x = sg.right() - self.WIDTH - self.MARGIN
        y = sg.bottom() - self.HEIGHT - self.MARGIN
        self.move(x, y)

    @staticmethod
    def _play_sound() -> None:
        """播放系统提示音（Windows 默认通知音）。"""
        try:
            if sys.platform.startswith("win"):
                import winsound
                # MB_ICONASTERISK = 0x40：系统"提示"音
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:
                QApplication.beep()
        except Exception:
            # 音效失败静默跳过，不影响提示展示
            try:
                QApplication.beep()
            except Exception:
                pass

    def _start_fade_out(self) -> None:
        try:
            if self._fade_in.state() == QPropertyAnimation.State.Running:
                self._fade_in.stop()
            self._fade_out.start()
        except Exception:
            self.close()

    def mousePressEvent(self, event) -> None:
        """点击弹框：立即消失。"""
        self._close_timer.stop()
        self._start_fade_out()
        super().mousePressEvent(event)


class CodeRunnerThread(QThread):
    """在后台线程中运行 Python 脚本，捕获 stdout/stderr/returncode。"""

    resultReady = Signal(str, str, int)  # stdout, stderr, returncode

    def __init__(self, python_exe: str, script_path: str, cwd: str, parent=None):
        super().__init__(parent)
        self._python = python_exe
        self._script = script_path
        self._cwd = cwd

    def run(self) -> None:
        try:
            import subprocess
            result = subprocess.run(
                [self._python, self._script],
                cwd=self._cwd,
                capture_output=True,
                text=True,
            )
            self.resultReady.emit(
                result.stdout or "",
                result.stderr or "",
                result.returncode,
            )
        except Exception as e:
            self.resultReady.emit("", str(e), -1)


class _HotkeySignal(QObject):
    """线程安全信号：监听线程触发 → 自动投递到主线程执行停止逻辑。"""
    triggered = Signal()


class _CommandConsoleWidget(QListWidget):
    """Pro 编程智能体控制台。

    每条命令行是一个列表项；含 #H 序号的命令在左侧附一个可点击的小圆圈，
    单击由 ○ 变 ✓ 表示选中，供「撤销选中命令」批量还原。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "QListWidget{background:white;color:#333;border:none;"
            "font-family:Consolas,monospace;font-size:12px;}"
        )
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._handle_rows = []  # [{"handle","btn","item"}]

    def append_line(self, text: str) -> None:
        m = re.search(r"#(H\d+)", text)
        if m:
            handle = m.group(1)
            item = QListWidgetItem()
            row = QWidget()
            row.setStyleSheet("background:transparent;")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(2, 2, 4, 2)
            lay.setSpacing(6)
            btn = QToolButton()
            btn.setText("○")
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QToolButton{color:#888;border:none;font-size:14px;font-weight:bold;}"
                "QToolButton:checked{color:#16a085;}"
            )
            btn.clicked.connect(lambda checked, b=btn: b.setText("\u2713" if checked else "○"))
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(btn)
            lay.addWidget(label, 1)
            self._handle_rows.append({"handle": handle, "btn": btn, "item": item})
            self.addItem(item)
            self.setItemWidget(item, row)
            # 让行高跟随行控件的实际内容高度，否则文字会被纵向裁切（只显示半截）
            row.adjustSize()
            item.setSizeHint(row.sizeHint())
        else:
            row = QListWidgetItem(text)
            row.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.addItem(row)
        self.scrollToBottom()

    def checked_handles(self) -> List[str]:
        return [r["handle"] for r in self._handle_rows if r["btn"].isChecked()]

    def remove_handles(self, handles) -> None:
        """撤销后移除对应命令行，并清掉其选中状态。"""
        hs = set(handles)
        for r in list(self._handle_rows):
            if r["handle"] in hs:
                self.takeItem(self.row(r["item"]))
                self._handle_rows.remove(r)


class MainWindow(QMainWindow):
    # UI → Worker 信号
    goalSubmitted = Signal(str)
    planConfirmed = Signal(list, bool)
    consultAnswered = Signal(str)
    pauseRequested = Signal()
    resumeRequested = Signal()
    replanWhilePausedRequested = Signal()  # 暂停态输入提示 → worker 立即重规划剩余步骤
    injectSubmitted = Signal(str)
    stopRequested = Signal()
    modeSwitched = Signal(str)
    interfaceSelected = Signal(str)
    hideOnCaptureToggled = Signal(bool)
    newConversationRequested = Signal()
    reloadSettingsRequested = Signal()
    workflowExecuted = Signal(list, str)  # steps, goal_text
    goalSet = Signal(str)                  # 设置 goal（工作流编辑用）
    stepsUpdated = Signal(list)            # 暂停时编辑后的剩余步骤 → worker 更新
    fixCodeRequested = Signal(str)         # 代码运行出错 → worker 自动修复
    setReplanRequested = Signal(bool)      # 母工作流工作流块：设置是否允许执行中自动重规划
    # 母工作流执行（跨线程安全）
    _masterNodeExecRequested = Signal(str, str, object, object)
    # 母工作流整体执行完成回调（后台线程发出，跨线程排队）
    _masterCompleted = Signal(str, bool, str)  # (mwf_id, success, end_time "HH:MM")
    # 悬浮球颜色状态
    ballColorChanged = Signal(str)  # "default" | "green" | "red"
    ballErrorLabelChanged = Signal(str)  # 出错母工作流标签
    busyResetRequested = Signal()  # 重置 worker 繁忙状态
    proChatToggled = Signal(bool)  # Pro 真人聊天开关
    cosChatToggled = Signal(bool)  # cos 角色扮演聊天开关
    proProgrammingToggled = Signal(bool)  # Pro 编程智能体开关
    proProgrammingProjectChanged = Signal(str)  # Pro 编程项目文件夹

    def __init__(self):
        super().__init__()
        self.setWindowTitle("")
        self.setMinimumSize(420, 600)
        self.resize(460, 680)
        self._settings = app_config.load_settings()
        self._apply_window_title()

        # 状态
        self._mode = "chat"
        self._ui_state = "idle"  # idle|awaiting_confirm|executing|awaiting_consult|paused
        self._current_stream_bubble = None
        self._current_code_bubble: Optional[CodeBubbleWidget] = None
        self._last_code_bubble: Optional[CodeBubbleWidget] = None
        self._pending_code_auto_fix_bubble: Optional[CodeBubbleWidget] = None
        # 模型运行状态条（对话区顶部）
        self._model_status_state: str = "idle"  # idle|connecting|streaming|done
        self._model_status_kind: str = ""       # 当前输出类型（chat/programming）
        self._model_status_tag: str = ""        # 接口名
        self._model_status_base: str = ""       # 接口地址
        self._model_status_model: str = ""      # 模型名
        self._model_status_start: float = 0.0   # 本次输出开始时间（time.time）
        self._model_status_tokens: int = 0      # 已累计 token（估算）
        self._model_status_window = []          # (时间, 累计token) 快照，用于瞬时速度滑动窗口
        self._model_status_timer = QTimer(self)
        self._model_status_timer.setInterval(200)
        self._model_status_timer.timeout.connect(self._on_model_status_tick)
        self._programming_buffer: str = ""  # 编程模式流式输出缓冲
        self._parsed_up_to: int = 0  # 编程模式 buffer 已处理到的位置
        self._code_start_pos: int = 0  # 当前代码围栏内容的起始位置（跨分块时保护内容不丢失）
        self._current_code_lang: str = ""  # 当前代码围栏声明的语言（如 html/css/javascript）
        self._has_code_bubble: bool = False  # 流式阶段是否已创建过代码气泡
        self._stream_state: str = "normal"  # "normal" | "thinking" | "code"
        self._current_thinking_bubble: Optional[ThinkingBubbleWidget] = None  # 当前流式思考气泡
        self._in_thinking_block: bool = False  # 是否正在思考块中（聊天模式用）
        self._thinking_shown_during_streaming: bool = False  # 流式阶段是否已显示思考气泡
        self._session = self._new_session_obj()
        self._settings_dialog: Optional[SettingsDialog] = None
        self._model_config_dialog: Optional[ModelConfigDialog] = None
        self._exp_dialog: Optional[object] = None  # 经验库管理窗口（懒创建、非模态）
        self._history_panel: Optional[HistoryPanel] = None
        self._workflow_panel: Optional[WorkflowPanel] = None
        self._control_center_dialog: Optional[ControlCenterDialog] = None
        self._master_executor = MasterWorkflowExecutor(self._execute_master_workflow_node)
        self._pending_master_callback = None  # 母工作流节点执行完成回调
        self._mwf_error_label = ""  # 记录出错母工作流的标签，用于右键菜单打开
        self._exec_block_name = ""  # 当前执行的母工作流块名称（日志用）
        self._exec_mwf_label = ""  # 当前执行的母工作流标签（日志用）
        self._exec_block_seq = 0  # 当前执行的母工作流块序号（日志用）
        self._exec_block_indent = 0  # 当前执行的母工作流块缩进层级（日志用）
        self._masterNodeExecRequested.connect(self._on_execute_master_node, Qt.ConnectionType.QueuedConnection)

        # 任务清单：右上角悬浮表格
        self._task_list = TaskListWidget()
        self._task_results: Dict[str, Dict[str, str]] = {}  # mwf_id -> {"date","status","time"}
        self._masterCompleted.connect(self._on_master_workflow_completed)
        self._master_executor.set_on_completed(self._emit_master_completed)
        self._editing_workflow_id: Optional[str] = None  # 正在编辑的工作流 ID（"__new__"=新建）
        self._executing_workflow_id: Optional[str] = None  # 正在执行的工作流 ID（执行完成后保存坐标）
        self._goal_text: str = ""  # 当前目标任务文本
        self._pending_attachments: List[str] = []  # 拖入对话框、待随下一条消息发送的附件本地路径
        self._plan_from_worker = False  # 规划是否来自 worker（True=worker 在 _await_confirm 中等待）
        # 执行提示相关：记录本次执行的 kind/label/开始时间
        self._exec_label: str = ""
        self._exec_kind: str = "任务"
        self._exec_start_at: float = 0.0
        self._toast: Optional[CompletionToast] = None
        self._saved_geometry = None
        self._executing_overlay_mode: bool = False  # 执行时=真：主窗口隐藏+独立overlay显示
        self._paused_done_count: int = 0  # 暂停时已完成步骤数（恢复时切分剩余步骤用）

        # 自主代理模式浮窗（右下角思考过程）
        self._agent_overlay = agent_overlay.AgentOverlay()
        self._agent_overlay.set_stop_callback(self._on_agent_overlay_stop)
        self._agent_overlay.set_stop_shortcut(self._settings.get("stop_shortcut", "Ctrl+."))
        self._saved_hide_on_capture = True
        self._saved_was_visible = False
        self._saved_was_minimized = False

        # 窗口顶部提示：3 秒后自动清除
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.setInterval(3000)
        self._hint_timer.timeout.connect(self._clear_top_hint)

        # 工作流编辑自动保存：防抖 timer（300ms）
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(300)
        self._autosave_timer.timeout.connect(self._autosave_workflow)

        # 背景缓存：图片用 _bg_pixmap，视频用 _video_bg
        self._bg_pixmap = QPixmap()
        self._bg_scaled = QPixmap()
        self._bg_scaled_key: tuple = (0, 0)
        self._video_bg: Optional[VideoBackground] = None
        self._load_background()

        self._build_ui()
        # 恢复 Pro 编程开关初始状态（若上次保存为开启，静默设置，避免误发提示）
        if self._pro_prog_enabled:
            self._btn_pro_prog.blockSignals(True)
            self._btn_pro_prog.setState(1)
            self._btn_pro_prog.blockSignals(False)
            self._pro_prog_project = self._settings.get("pro_programming_project", "") or ""
        # 模型状态条初始显示当前接口
        self._reset_model_status()
        # 工作流数据改动 → 防抖触发自动保存
        self._agent_panel.workflowDataChanged.connect(self._autosave_timer.start)
        self._agent_panel.saveRequested.connect(self._on_save_requested)
        capture.set_hide_on_capture(self._settings.get("hide_on_capture", True))
        # 启动所有已启用的母工作流
        QTimer.singleShot(500, self._start_enabled_master_workflows)
        # 注册全局热键（Ctrl+. 停止，窗口隐藏时也能工作）
        self._init_global_hotkey()

    # ------------------------------------------------------------------
    # 会话对象
    # ------------------------------------------------------------------
    def _new_session_obj(self) -> dict:
        return {
            "id": app_config.new_session_id(),
            "title": "新对话",
            "mode": self._mode,
            "interface_tag": app_config.active_tag(),
            "messages": [],
            "steps": [],
        }

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        # 用 GridLayout 让 ExecOverlay 叠在顶栏+stack+底栏 的上方（同一 cell 的先后 z-order）
        root = QGridLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        content = QWidget()
        content.setObjectName("contentRoot")
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 顶部栏
        self._top_bar = self._build_top_bar()
        lay.addWidget(self._top_bar)

        # 堆叠视图（ChatView / AgentPanel；ExecOverlay 不再放入 stack）
        self._stack = QStackedWidget()
        self._chat_view = ChatView()
        self._chat_view.messageDeleteRequested.connect(self._on_message_delete)
        self._chat_view.messageRecallRequested.connect(self._on_message_recall)
        self._chat_view.messageUndoRequested.connect(self._on_pro_message_undo)
        self._chat_view.attachmentActivated.connect(self._open_attachment)
        self._chat_view.set_bubble_opacity(int(self._settings.get("bubble_opacity", 0)))
        # 对话页 = 顶部模型状态条 + 聊天滚动区
        self._chat_page = QWidget()
        self._chat_page.setObjectName("chatPage")
        chat_lay = QVBoxLayout(self._chat_page)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(0)
        self._model_status_bar = self._build_model_status_bar()
        chat_lay.addWidget(self._model_status_bar)
        chat_lay.addWidget(self._chat_view, 1)
        self._agent_panel = AgentPanel()
        self._stack.addWidget(self._chat_page)     # 0
        self._stack.addWidget(self._agent_panel)   # 1
        lay.addWidget(self._stack, 1)

        # Pro 编程控制台：可折叠（点击标题栏箭头收起/展开），白色主题，显示在底部栏上方
        self._pro_console_container = QWidget()
        self._pro_console_container.setObjectName("proConsoleWrap")
        self._pro_console_container.setStyleSheet(
            "#proConsoleWrap{background:white;border:1px solid #ddd;border-radius:6px;}"
        )
        cw = QVBoxLayout(self._pro_console_container)
        cw.setContentsMargins(6, 4, 6, 6)
        cw.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        self._pro_console_toggle = QToolButton()
        self._pro_console_toggle.setText("▾")
        self._pro_console_toggle.setAutoRaise(True)
        self._pro_console_toggle.setToolTip("折叠/展开智能体控制台")
        self._pro_console_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pro_console_toggle.clicked.connect(self._toggle_pro_console)
        head.addWidget(self._pro_console_toggle)
        pro_console_lbl = QLabel("智能体控制台")
        pro_console_lbl.setStyleSheet("color:#333;font-size:12px;font-weight:bold;")
        head.addWidget(pro_console_lbl)
        self._pro_console_token_lbl = QLabel("上下文 --")
        self._pro_console_token_lbl.setStyleSheet(
            "color:#888;font-size:11px;background:#f5f6f7;border:1px solid #e0e0e0;"
            "border-radius:4px;padding:1px 6px;"
        )
        self._pro_console_token_lbl.setToolTip("当前发送给模型的上下文估算 token 数（k=千，m=百万，b=十亿）")
        head.addWidget(self._pro_console_token_lbl)
        head.addStretch(1)
        self._pro_console_undo_btn = QPushButton("↺ 撤销选中命令")
        self._pro_console_undo_btn.setToolTip("撤销已勾选（✓）的命令：先单击命令行左侧 ○ 打钩")
        self._pro_console_undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pro_console_undo_btn.setStyleSheet(
            "QPushButton{background:#f2f6fc;color:#1a5fb4;border:1px solid #c9d9f0;"
            "border-radius:4px;padding:2px 8px;font-size:11px;}"
            "QPushButton:hover{background:#e4eefb;}"
        )
        self._pro_console_undo_btn.clicked.connect(self._undo_selected_console_cmd)
        head.addWidget(self._pro_console_undo_btn)
        cw.addLayout(head)
        self._pro_console = _CommandConsoleWidget()
        self._pro_console.setMaximumHeight(180)
        self._pro_console.hide()
        cw.addWidget(self._pro_console)
        self._pro_console_collapsed = True                     # 默认折叠，仅显示标题栏
        self._pro_console_toggle.setText("▸")
        self._pro_console_container.hide()
        lay.addWidget(self._pro_console_container, 0)

        # 底部栏
        self._bottom_bar = self._build_bottom_bar()
        lay.addWidget(self._bottom_bar)

        # ExecOverlay 叠在 content 上方（同 grid 同一格，z-order 由加入顺序决定）
        self._exec_overlay = ExecutionOverlay(central)
        self._exec_overlay.hide()

        # 窗口顶部提示：默认空白，点击按钮后短暂显示
        self._top_hint = QLabel("")
        self._top_hint.setObjectName("topHint")
        self._top_hint.setStyleSheet(
            "#topHint{background:rgba(44,62,80,200);color:white;border-radius:13px;"
            "padding:6px 18px;font-size:13px;font-weight:bold;}"
        )
        self._top_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._top_hint.adjustSize()
        self._top_hint.setVisible(False)

        root.addWidget(content, 0, 0)
        root.addWidget(self._exec_overlay, 0, 0)
        root.addWidget(
            self._top_hint, 0, 0,
            alignment=Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
        )

        self._apply_view_for_mode()
        self._agent_panel.stepsConfirmed.connect(self._on_steps_confirmed)
        self._agent_panel.resumeExecRequested.connect(self._on_pause_clicked)
        self._exec_overlay.pauseClicked.connect(self._on_pause_clicked)
        self._exec_overlay.hideCaptureToggled.connect(self.hideOnCaptureToggled.emit)

    def _build_model_status_bar(self) -> QWidget:
        """对话区顶部的模型运行状态条：显示连接接口、实时计时与 token/s。"""
        bar = QFrame()
        bar.setObjectName("modelStatusBar")
        bar.setFixedHeight(26)
        bar.setStyleSheet(
            "#modelStatusBar{background:rgba(255,255,255,190);"
            "border-bottom:1px solid #d0d4dc;}"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 2, 10, 2)
        lay.setSpacing(6)
        self._model_status_widget = QLabel("")
        self._model_status_widget.setStyleSheet(
            "color:#34495e;font-size:12px;font-weight:bold;")
        self._model_status_widget.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._model_status_widget, 1)
        return bar

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(44)
        bar.setStyleSheet(
            "#topbar{background:rgba(255,255,255,210);border-bottom:1px solid #d0d4dc;}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)

        self._btn_history = QToolButton()
        self._btn_history.setText("≡ 历史")
        self._btn_history.setStyleSheet(self._btn_style("#7f8c8d"))
        self._btn_history.clicked.connect(self._open_history)

        self._btn_settings = QToolButton()
        self._btn_settings.setText("⚙ 设置")
        self._btn_settings.setStyleSheet(self._btn_style("#7f8c8d"))
        self._btn_settings.clicked.connect(self._open_settings)

        self._btn_new = QToolButton()
        self._btn_new.setText("✚ 新对话")
        self._btn_new.setStyleSheet(self._btn_style("#27ae60"))
        self._btn_new.clicked.connect(self._new_conversation)

        h.addWidget(self._btn_history)
        h.addStretch(1)
        h.addWidget(self._btn_settings)
        h.addWidget(self._btn_new)
        return bar

    def _build_bottom_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("bottombar")
        bar.setStyleSheet(
            "#bottombar{background:rgba(255,255,255,220);border-top:1px solid #d0d4dc;}"
        )
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(4)

        # 控制行
        ctrl = QHBoxLayout()
        self._mode_combo = QComboBox()
        self._mode_combo.wheelEvent = lambda e: None
        self._mode_combo.addItem("自主代理模式", "agent_mode")
        self._mode_combo.addItem("规划模式", "agent")
        self._mode_combo.addItem("聊天模式", "chat")
        self._mode_combo.addItem("编程模式", "programming")
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        # 默认聊天模式
        self._mode_combo.setCurrentIndex(self._mode_combo.findData("chat"))

        # 聊天模式三点位滑动开关：标准 / Pro / cos，放在「聊天模式」字样旁
        self._btn_chat_mode = _ChatModeSwitch()
        self._btn_chat_mode.setToolTip("聊天模式：标准 / Pro 真人双模型 / cos 角色扮演（单击循环切换）")
        self._btn_chat_mode.stateChanged.connect(self._on_chat_mode_changed)

        # 人设按钮：仅 cos 模式下出现，显示当前人设标签，点击进入人设切换/编辑窗口
        self._btn_persona = QPushButton("人设")
        self._btn_persona.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_persona.setStyleSheet(
            "QPushButton{background:#8e44ad;color:white;border:none;"
            "border-radius:5px;padding:4px 10px;font-weight:bold;}"
            "QPushButton:hover{background:#9b59b6;}"
        )
        self._btn_persona.clicked.connect(self._open_persona_dialog)
        self._btn_persona.hide()

        # Pro 编程智能体开关：标准/Pro，仅编程模式下显示
        self._pro_prog_project = ""
        self._pro_prog_enabled = bool(self._settings.get("pro_programming_enabled", False))
        self._btn_pro_prog = _ProgramProSwitch()
        self._btn_pro_prog.setToolTip("编程模式：标准 / Pro 编程智能体（单击循环切换）")
        self._btn_pro_prog.stateChanged.connect(self._on_pro_prog_state_changed)
        self._btn_pro_prog.hide()

        # 项目文件夹下拉框 + IDE 按钮：Pro 编程开启时显示
        self._proj_combo = QComboBox()
        self._proj_combo.setEditable(True)
        self._proj_combo.setMinimumWidth(180)
        self._proj_combo.addItem("＋ 新建项目…", "__new__")
        self._populate_recent_projects()
        current = self._settings.get("pro_programming_project", "") or ""
        if current:
            self._proj_combo.setCurrentText(current)
        self._proj_combo.currentIndexChanged.connect(self._on_project_changed)
        self._proj_combo.lineEdit().returnPressed.connect(self._on_project_typed)
        self._proj_combo.hide()

        self._ide_dialog = None
        self._pro_schedule_widget = None  # 当前对话区的 [schedule] 步骤清单（None=无）
        self._pro_thinking_bubble = None  # 当前一次需求的"智能体思考"大折叠框（复用累积）
        self._btn_ide = QPushButton("IDE")
        self._btn_ide.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_ide.setStyleSheet(
            "QPushButton{background:#16a085;color:white;border:none;"
            "border-radius:5px;padding:4px 10px;font-weight:bold;}"
            "QPushButton:hover{background:#1abc9c;}"
        )
        self._btn_ide.clicked.connect(self._open_ide_dialog)
        self._btn_ide.hide()

        # Pro 编程工具行：项目下拉框 + IDE 按钮放独立一行，避免挤占主控制行导致遮挡
        self._pro_toolbar = QWidget()
        pro_row = QHBoxLayout(self._pro_toolbar)
        pro_row.setContentsMargins(0, 0, 0, 0)
        pro_row.setSpacing(6)
        pro_lbl = QLabel("项目：")
        pro_lbl.setStyleSheet("color:#555;font-size:12px;")
        self._btn_choose_proj = QPushButton("选择项目文件夹")
        self._btn_choose_proj.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_choose_proj.setStyleSheet(
            "QPushButton{background:#5d6d7e;color:white;border:none;"
            "border-radius:5px;padding:4px 10px;font-weight:bold;}"
            "QPushButton:hover{background:#85929e;}"
        )
        self._btn_choose_proj.clicked.connect(self._choose_new_project)
        pro_row.addWidget(pro_lbl)
        pro_row.addWidget(self._proj_combo, 1)
        pro_row.addWidget(self._btn_choose_proj)
        pro_row.addWidget(self._btn_ide)
        pro_row.addStretch(1)
        self._pro_toolbar.hide()
        v.addWidget(self._pro_toolbar)

        # Pro 编程输出控制台面板（仅 Pro 编程会话使用，在 _build_ui 中创建并挂载）
        self._pro_console_bar = None

        self._btn_pause = QPushButton("⏸ 暂停")
        self._btn_pause.setCheckable(True)
        self._btn_pause.setStyleSheet(self._btn_style("#e74c3c"))
        self._btn_pause.clicked.connect(self._on_pause_clicked)

        # 经验库按钮：仅自主代理模式显示，点击打开经验库管理界面
        self._btn_exp_lib = QPushButton("📚 经验库")
        self._btn_exp_lib.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_exp_lib.setStyleSheet(
            "QPushButton{background:#5b2c6f;color:white;border:none;"
            "border-radius:5px;padding:5px 10px;font-weight:bold;}"
            "QPushButton:hover{background:#6c3483;}"
        )
        self._btn_exp_lib.setToolTip(
            "管理自主代理经验库：常用经验会自动加入系统提示词，"
            "模型遇困时可搜索经验、任务完成后可沉淀新经验"
        )
        self._btn_exp_lib.clicked.connect(self._open_experience_dialog)
        self._btn_exp_lib.hide()

        # 用「模型配置」按钮代替旧的接口下拉框：支持为 4 种模型分别配置预设
        self._btn_model_config = QPushButton("⚙ 模型配置")
        self._btn_model_config.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_model_config.setStyleSheet(
            "QPushButton{background:#8e44ad;color:white;border:none;"
            "border-radius:5px;padding:5px 10px;font-weight:bold;}"
            "QPushButton:hover{background:#9b59b6;}"
            "QPushButton:pressed{padding-top:6px;padding-left:11px;}"
        )
        self._btn_model_config.clicked.connect(self._open_model_config)

        ctrl.addWidget(self._mode_combo)
        ctrl.addWidget(self._btn_chat_mode)
        ctrl.addWidget(self._btn_pro_prog)
        ctrl.addWidget(self._btn_persona)
        ctrl.addWidget(self._btn_pause)
        ctrl.addWidget(self._btn_exp_lib)
        ctrl.addStretch(1)
        ctrl.addWidget(self._btn_model_config)

        # 输入行
        in_row = QHBoxLayout()
        self._input = _DragDropEdit()
        # 初始提示按当前默认模式（聊天）设置；切换模式时由 _update_input_placeholder 同步更新
        self._update_input_placeholder()
        self._input.setFixedHeight(54)
        self._input.setStyleSheet(
            "QPlainTextEdit{background:rgba(255,255,255,235);border:1px solid #ccc;"
            "border-radius:6px;padding:6px;}"
        )
        self._btn_send = QPushButton("发送")
        self._btn_send.setFixedWidth(64)
        self._btn_send.setStyleSheet(self._btn_style("#2e8b57", pad=True))
        self._btn_send.clicked.connect(self._on_send)
        # 拖入文件/图片 → 加入待发送附件（子类拦截，不插入文件地址文本）
        self._input.filesDropped.connect(self._import_attachment_files)
        self._input.installEventFilter(self)

        # 控制中心按钮（取代原工作流按钮）
        self._btn_control_center = QPushButton("🎛")
        self._btn_control_center.setFixedWidth(40)
        self._btn_control_center.setStyleSheet(self._btn_style("#8e44ad", pad=True))
        self._btn_control_center.setToolTip("控制中心（工作流/脚本库/母工作流）")
        self._btn_control_center.clicked.connect(self._open_control_center)

        in_row.addWidget(self._input, 1)
        in_row.addWidget(self._btn_control_center, alignment=Qt.AlignmentFlag.AlignTop)
        in_row.addWidget(self._btn_send, alignment=Qt.AlignmentFlag.AlignTop)

        # 未发送附件预览条（置于模式选择下拉框之上，发送后移除）
        self._attachment_bar = QWidget()
        self._attachment_bar.setStyleSheet(
            "QWidget#attachmentBar{background:rgba(255,255,255,200);border-radius:8px;}"
            "QToolTip{background-color:#ffffff;color:#000000;"
            "border:1px solid #b0b0b0;border-radius:8px;padding:4px 8px;}"
        )
        self._attachment_bar.setObjectName("attachmentBar")
        self._attachment_lay = QVBoxLayout(self._attachment_bar)
        self._attachment_lay.setContentsMargins(6, 6, 6, 6)
        self._attachment_lay.setSpacing(4)
        self._attachment_bar.setVisible(False)
        v.addWidget(self._attachment_bar)
        v.addLayout(ctrl)
        v.addLayout(in_row)
        # 初始化人设按钮标签（显示当前选中的角色标签）
        self._update_persona_button()
        return bar

    # ------------------------------------------------------------------
    # 背景图 / 背景视频
    # ------------------------------------------------------------------
    def _load_background(self) -> None:
        # 停止旧的视频背景（若有）
        if self._video_bg is not None:
            self._video_bg.stop()
            self._video_bg = None

        path = self._settings.get("background_image", "")
        if not path or not os.path.isfile(path):
            self._bg_pixmap = QPixmap()
            self._bg_scaled = QPixmap()
            self._bg_scaled_key = (0, 0)
            self.update()
            return

        if is_video_file(path):
            # 视频背景：创建 VideoBackground，接收帧信号更新 _bg_pixmap
            self._bg_pixmap = QPixmap()
            self._bg_scaled = QPixmap()
            self._bg_scaled_key = (0, 0)
            self._video_bg = VideoBackground(path, parent=self)
            self._video_bg.frameReady.connect(self._on_video_frame)
            self._video_bg.failed.connect(self._on_video_failed)
            # 视频不可用时用 fallback 首帧（VideoBackground 内已发出首帧）
            self.update()
        else:
            # 图片背景：用 load_image_pixmap 兼容 Qt 插件缺失的格式（jpg/jpeg）
            self._bg_pixmap = load_image_pixmap(path)
            self._bg_scaled = QPixmap()
            self._bg_scaled_key = (0, 0)
            self.update()

    def _on_video_frame(self, pm: QPixmap) -> None:
        """收到视频帧：更新 _bg_pixmap 并重绘（清除缩放缓存）。"""
        self._bg_pixmap = pm
        self._bg_scaled = QPixmap()
        self._bg_scaled_key = (0, 0)
        self.update()

    def _on_video_failed(self, msg: str) -> None:
        """视频解码失败：回退到 fallback 首帧或清空背景。"""
        if self._video_bg is not None:
            fb = self._video_bg.fallback_pixmap
            if fb is not None and not fb.isNull():
                self._bg_pixmap = fb
                self._bg_scaled = QPixmap()
                self._bg_scaled_key = (0, 0)
                self.update()
                return
        # 无 fallback：清空
        self._bg_pixmap = QPixmap()
        self._bg_scaled = QPixmap()
        self._bg_scaled_key = (0, 0)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self._bg_pixmap.isNull():
            key = (self.width(), self.height())
            if key != self._bg_scaled_key or self._bg_scaled.isNull():
                pw, ph = self._bg_pixmap.width(), self._bg_pixmap.height()
                sw, sh = self.width(), self.height()
                if pw > 0 and ph > 0 and sw > 0 and sh > 0:
                    scale = max(sw / pw, sh / ph)
                    dw, dh = int(pw * scale) + 1, int(ph * scale) + 1
                    self._bg_scaled = self._bg_pixmap.scaled(
                        dw, dh,
                        Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self._bg_scaled_key = key
            if not self._bg_scaled.isNull():
                # 中心对齐：背景图中心与窗口中心对齐
                # max 缩放保证填满窗口，溢出部分从四边均匀裁剪，不变形
                dx = (self.width() - self._bg_scaled.width()) // 2
                dy = (self.height() - self._bg_scaled.height()) // 2
                p.drawPixmap(dx, dy, self._bg_scaled)
                # 叠加半透明遮罩，提升内容可读性
                p.fillRect(self.rect(), QColor(255, 255, 255, 40))
                return
        # 无背景：垂直渐变
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0, QColor("#2c3e50"))
        grad.setColorAt(1, QColor("#4a6278"))
        p.fillRect(self.rect(), grad)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._bg_scaled_key = (0, 0)
        self.update()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)

    # ------------------------------------------------------------------
    # Worker 连接
    # ------------------------------------------------------------------
    def connect_worker(self, worker) -> None:
        self._worker = worker  # 保留引用，供 _undo_selected_console_cmd / _on_pro_message_undo 调用
        # UI → Worker
        self.goalSubmitted.connect(worker.submitGoal)
        self.planConfirmed.connect(worker.confirmPlan)
        self.consultAnswered.connect(worker.answerConsult)
        self.pauseRequested.connect(worker.pause)
        self.resumeRequested.connect(worker.resume)
        self.replanWhilePausedRequested.connect(worker.replanRemainingWhilePaused)
        self.injectSubmitted.connect(worker.inject)
        self.stopRequested.connect(worker.stop)
        self.modeSwitched.connect(worker.switchMode)
        # 启动时把 UI 当前模式显式同步给 worker：worker 有独立初始值，
        # 若不同步，界面显示“聊天模式”但发消息会按 worker 默认模式执行
        # （代理规划），出现“显示聊天、实际走代理”的模式错位。
        worker.switchMode(self._mode)
        self.interfaceSelected.connect(worker.selectInterface)
        self.hideOnCaptureToggled.connect(worker.setHideOnCapture)
        self.proChatToggled.connect(worker.setProChat)
        self.cosChatToggled.connect(worker.setCosChat)
        self.proProgrammingToggled.connect(worker.setProProgramming)
        self.proProgrammingProjectChanged.connect(worker.setProProgrammingProject)
        # 启动时同步一次 Pro 编程开关与项目给 worker
        worker.setProProgramming(self._pro_prog_enabled)
        worker.setProProgrammingProject(self._pro_prog_project)
        self.newConversationRequested.connect(worker.newConversation)
        self.reloadSettingsRequested.connect(worker.reloadSettings)
        self.workflowExecuted.connect(worker.executeSteps)
        self.setReplanRequested.connect(worker.setReplanEnabled)
        self.goalSet.connect(worker.setGoal)
        self.stepsUpdated.connect(worker.replaceRemainingSteps)
        self.fixCodeRequested.connect(worker.fixCode)
        self.busyResetRequested.connect(worker.resetBusy)

        # Worker → UI
        worker.streamChunk.connect(self._on_stream_chunk)
        worker.assistantMessage.connect(self._on_assistant_message)
        worker.modelCallStarted.connect(self._begin_model_connecting)
        worker.planReady.connect(self._on_plan_ready)
        worker.consultRequested.connect(self._on_consult_requested)
        worker.stepStatusChanged.connect(self._on_step_status)
        worker.stepCoordUpdated.connect(self._on_step_coord_updated)
        worker.progressTextChanged.connect(self._on_progress_text)
        worker.progressBarChanged.connect(self._on_progress_bar)
        worker.executionStarted.connect(self._on_execution_started)
        worker.executionFinished.connect(self._on_execution_finished)
        worker.screenshotTaken.connect(self._on_screenshot_taken)
        worker.agentAction.connect(self._on_agent_action)
        worker.agentModelTime.connect(self._on_agent_model_time)
        worker.agentTaskTime.connect(self._on_agent_task_time)
        worker.interfaceChanged.connect(self._on_worker_interface_changed)
        worker.proConsole.connect(self._on_pro_console)
        worker.proAgentMessage.connect(self._on_pro_agent_message)
        worker.proAgentStream.connect(self._on_pro_agent_stream)
        worker.proAgentHistory.connect(self._on_pro_agent_history)
        worker.proToolCall.connect(self._on_pro_tool_call)
        worker.proSchedule.connect(self._on_pro_schedule)
        worker.proScheduleDone.connect(self._on_pro_schedule_done)
        worker.proScheduleSkip.connect(self._on_pro_schedule_skip)
        worker.proScheduleAllDone.connect(self._on_pro_schedule_all_done)
        worker.proContextTokens.connect(self._on_pro_context_tokens)
        worker.statusMessage.connect(self._on_status_message)
        worker.error.connect(self._on_error)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if obj is self._input and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    return False  # 换行
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    def _import_attachment_files(self, paths: List[str]) -> None:
        """把外部拖入的文件/图片复制到本地缓存目录，显示在输入框上方的附件预览条。

        复制成功后的本地路径存入 _pending_attachments，待发送时随消息提交给模型；
        发送后会自动移入该历史记录的文件夹并从缓存删除；发送前可在这里移除单个附件。
        """
        ud = app_config.cache_dir(self._settings)
        os.makedirs(ud, exist_ok=True)
        added = []
        ts = time.strftime("%Y%m%d-%H%M%S")
        for i, src in enumerate(paths):
            if not os.path.isfile(src):
                continue
            base, ext = os.path.splitext(os.path.basename(src))
            dst = os.path.join(ud, f"{base}_{ts}_{i}{ext}")
            try:
                shutil.copy2(src, dst)
            except Exception:
                dst = src  # 复制失败则引用原文件
            added.append(dst)
        if not added:
            return
        self._pending_attachments.extend(added)
        self._rebuild_attachment_bar()
        self._show_top_hint(f"已添加 {len(added)} 个附件，发送后随消息提交")

    def _delete_cache_file(self, path: str) -> None:
        """删除缓存目录中的文件副本。仅在文件确实位于 cache 目录时删除，
        避免误删外部（复制失败时）引用原文件。"""
        cd = os.path.normpath(os.path.abspath(app_config.cache_dir(self._settings)))
        if os.path.normpath(os.path.abspath(path)).startswith(cd + os.sep):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass

    def _purge_cache(self) -> None:
        """清空缓存目录中所有未发送的文件（关闭窗口时调用，避免残留占用磁盘）。"""
        cd = app_config.cache_dir(self._settings)
        try:
            if os.path.isdir(cd):
                for name in os.listdir(cd):
                    p = os.path.join(cd, name)
                    try:
                        if os.path.isfile(p):
                            os.remove(p)
                    except OSError:
                        pass
        except OSError:
            pass

    def _rebuild_attachment_bar(self) -> None:
        """根据 _pending_attachments 重建附件预览条（图片缩略图、其他文件图标），并切换显隐。"""
        # 清空现有条目
        while self._attachment_lay.count():
            item = self._attachment_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not self._pending_attachments:
            self._attachment_bar.setVisible(False)
            return
        for p in self._pending_attachments:
            self._attachment_lay.addWidget(self._make_attachment_chip(p))
        self._attachment_bar.setVisible(True)

    def _make_attachment_chip(self, path: str) -> QWidget:
        """创建单个附件项：图片显示缩略图，其他文件显示文件图标，带移除按钮，可双击打开。

        悬浮提示只显示「文件名 + 双击查看」；控件自身不设局部样式表（避免遮蔽
        附件条容器声明的白底圆角 QToolTip），外观改用 QPalette 实现。
        """
        tip = f"{os.path.basename(path)}\n双击查看"
        chip = _AttachmentChipFrame(path)
        chip.activated.connect(self._open_attachment)
        chip.setToolTip(tip)
        chip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        chip.setAutoFillBackground(True)
        _chip_pal = chip.palette()
        _chip_pal.setColor(
            QPalette.ColorRole.Window, QColor(255, 255, 255, 180)
        )
        chip.setPalette(_chip_pal)
        h = QHBoxLayout(chip)
        h.setContentsMargins(6, 4, 6, 4)
        h.setSpacing(6)
        ext = os.path.splitext(path)[1].lower()
        if ext in _ATTACH_IMAGE_EXTS:
            thumb = load_image_pixmap(path)
            if not thumb.isNull():
                thumb = thumb.scaled(
                    40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                ic = QLabel()
                ic.setPixmap(thumb)
                ic.setFixedSize(40, 40)
                _pal = ic.palette()
                _pal.setColor(QPalette.ColorRole.Window, QColor("#1a1a1a"))
                ic.setPalette(_pal)
                ic.setAutoFillBackground(True)
            else:
                ic = QLabel("🖼")
                _set_label_color(ic, "#333333", 14)
        else:
            ic = QLabel("📄")
            _set_label_color(ic, "#333333", 14)
        ic.setToolTip(tip)
        h.addWidget(ic)
        name_lbl = QLabel(os.path.basename(path))
        _set_label_color(name_lbl, "#333333", 9)
        name_lbl.setToolTip(tip)
        name_lbl.setMaximumWidth(180)
        h.addWidget(name_lbl, 1)
        size_lbl = QLabel(_file_size_human_attach(os.path.getsize(path)))
        _set_label_color(size_lbl, "#999999", 8)
        size_lbl.setToolTip(tip)
        h.addWidget(size_lbl)

        def _remove():
            if path in self._pending_attachments:
                self._pending_attachments.remove(path)
            self._delete_cache_file(path)  # 发送前移除 → 一并删除缓存副本
            self._rebuild_attachment_bar()

        btn = QPushButton("✕")
        btn.setFixedSize(18, 18)
        btn.setStyleSheet(
            "QPushButton{background:transparent;color:#888;border:none;}"
            "QPushButton:hover{color:#e74c3c;}"
            "QToolTip{background-color:#ffffff;color:#000000;"
            "border:1px solid #b0b0b0;border-radius:8px;padding:4px 8px;}"
        )
        btn.setToolTip("移除")
        btn.clicked.connect(_remove)
        h.addWidget(btn)
        return chip

    def _open_attachment(self, path: str) -> None:
        """双击附件：图片用内置简易查看器；文本类文件查看字符内容（不运行）；
        其余二进制文件用系统默认程序打开。"""
        ext = os.path.splitext(path)[1].lower()
        if ext in _ATTACH_IMAGE_EXTS:
            self._show_image_viewer(path)
            return
        content = _read_text_attachment(path)
        if content is not None:
            self._show_text_viewer(path, content)
        else:
            try:
                os.startfile(path)
            except Exception:
                pass

    def _show_image_viewer(self, path: str) -> None:
        """内置图片查看器：保持比例自适应填充窗口，支持滚轮放大缩小（不调用系统查看器）。"""
        dlg = _ImageViewerDialog(path, self)
        dlg.exec()

    def _show_text_viewer(self, path: str, content: str) -> None:
        """在只读对话框中展示文本类附件的内容（字符内容，而非运行）。"""
        from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle(f"查看内容：{os.path.basename(path)}")
        dlg.resize(760, 640)
        lay = QVBoxLayout(dlg)
        te = QPlainTextEdit()
        te.setReadOnly(True)
        te.setStyleSheet(
            "QPlainTextEdit{font-family:'Consolas','Courier New',monospace;"
            "font-size:13px;background:white;color:#222;}"
        )
        te.setPlainText(content)
        lay.addWidget(te)
        dlg.exec()

    def _relocate_attachments(self, attachments: List[str]) -> List[str]:
        """发送时把缓存中的待发送附件移入该会话的历史子目录（history/<id>/），删除缓存源。

        移动后返回新路径列表；已在历史子目录或复制失败的外部引用保持原样。
        与 session.json 同目录存放，保证每条历史（json + 附件）集中、便于管理。
        """
        sid = str(self._session.get("id") or "").strip()
        if not sid:
            return attachments
        dst_dir = app_config.session_folder(sid, self._settings)
        try:
            os.makedirs(dst_dir, exist_ok=True)
        except OSError:
            return attachments
        out = []
        for src in attachments:
            dst = os.path.join(dst_dir, os.path.basename(src))
            if os.path.abspath(src) == os.path.abspath(dst):
                out.append(src)  # 已在历史目录，无需移动
                continue
            try:
                if os.path.isfile(src):
                    shutil.move(src, dst)
                    out.append(dst)
                else:
                    out.append(src)
            except Exception:
                out.append(src)  # 移动失败则保留原引用
        return out

    def _on_send(self) -> None:
        text = self._input.toPlainText().strip()
        # 组装发送内容：文本 + 待提交附件
        # 图片 → 以 @@EMBED_IMAGE@@ 标记行内嵌（call_model 读入内存转 base64 图像直传）；
        # 其他文本类文件 → 按字符读取内容；二进制 → 路径引用
        attachments = list(self._pending_attachments)
        # 发送：把缓存中的附件移入本会话历史子目录（history/<id>/）并删除缓存源
        attachments = self._relocate_attachments(attachments)
        content = text
        if attachments:
            blocks = []
            image_paths_for_bubble = []
            file_paths_for_bubble = []
            for p in attachments:
                ext = os.path.splitext(p)[1].lower()
                if ext in _ATTACH_IMAGE_EXTS:
                    blocks.append(f"{model_client.EMBED_IMAGE_PREFIX}{p}")
                    image_paths_for_bubble.append(p)
                else:
                    file_paths_for_bubble.append(p)
                    body = _read_text_attachment(p)
                    if body is not None:
                        blocks.append(f"文件（{os.path.basename(p)}）内容：\n{body}")
                    else:
                        blocks.append(f"文件（{os.path.basename(p)}）：{p}（二进制，暂以路径引用）")
            block = "\n".join(blocks)
            content = f"{content}\n\n[附加文件]\n{block}" if content else f"[附加文件]\n{block}"
        else:
            image_paths_for_bubble = []
            file_paths_for_bubble = []
        if not content:
            return
        self._pending_attachments = []
        self._rebuild_attachment_bar()  # 发送后隐藏附件预览条
        self._show_top_hint("消息已发送")
        self._input.clear()
        display = _render_attach_text(content)  # 气泡展示为友好文字（隐藏内嵌标记）
        # 记录到会话（附带附件结构，供历史重放时重建完整气泡）
        _user_msg = {"role": "user", "content": content}
        if image_paths_for_bubble or file_paths_for_bubble:
            _user_msg["attachment"] = {
                "text": text,
                "images": image_paths_for_bubble,
                "files": file_paths_for_bubble,
            }
        self._session["messages"].append(_user_msg)
        if self._session["title"] in ("新对话", ""):
            self._session["title"] = content[:20]
        self._goal_text = display  # 记录目标任务（展示文本）

        if self._mode == "chat":
            if image_paths_for_bubble or file_paths_for_bubble:
                self._chat_view.add_attachment_message(
                    text, image_paths_for_bubble, file_paths_for_bubble
                )
            else:
                self._chat_view.add_message(display, "user")
            self.goalSubmitted.emit(content)
        elif self._mode == "programming":
            bubble = self._chat_view.add_message(display, "user")
            # 一次新需求 → 开启一个统一的"智能体思考"大折叠框
            self._pro_thinking_bubble = None
            self._pro_schedule_widget = None
            # Pro 模式：提交前给项目快照，供用户消息右键「撤销」还原到本条消息发送前
            if self._pro_prog_enabled:
                snap = self._snapshot_project()
                bubble._pro_snap = snap
                # 快照路径也持久化到 session message（供加载历史时重建撤销能力）
                if snap and self._session["messages"]:
                    self._session["messages"][-1]["pro_snap"] = snap
            self.goalSubmitted.emit(content)
        elif self._mode == "agent_mode":
            # 自主代理模式：立即隐藏主窗口，避免对话界面闪烁
            # 禁用截图隐藏机制——主窗口已由 Qt 隐藏，浮窗有 WDA_EXCLUDEFROMCAPTURE
            self._saved_hide_on_capture = capture.is_hide_on_capture()
            capture.set_hide_on_capture(False)
            self._agent_overlay.clear()
            self._agent_overlay.set_goal(display)
            self._agent_overlay.show()
            self.hide()
            self._chat_view.add_message(display, "user")
            self.goalSubmitted.emit(content)
        else:
            # 规划模式
            if self._ui_state == "idle":
                self._chat_view.add_message(display, "user")
                self.goalSubmitted.emit(content)
            elif self._ui_state == "awaiting_confirm":
                # 通过对话提示模型修改：退出手动编辑态，注入补充信息并触发重新规划
                self._chat_view.add_message(display, "user")
                self._agent_panel.request_model_modify()
                self.injectSubmitted.emit(content)
                self.planConfirmed.emit([], False)
            elif self._ui_state == "paused":
                # 暂停编辑态：输入提示 → 规划模型立即重规划剩余步骤并显示（与执行前一致）
                self._chat_view.add_message(display, "user")
                self.injectSubmitted.emit(content)
                self.replanWhilePausedRequested.emit()
            elif self._ui_state == "executing":
                # 执行中补充信息
                self._chat_view.add_message(display, "user")
                self.injectSubmitted.emit(content)
            elif self._ui_state == "awaiting_consult":
                self._chat_view.add_message(display, "user")
                self.consultAnswered.emit(content)

    def _show_top_hint(self, text: str) -> None:
        """在窗口顶部显示提示，3 秒后自动清除为空白。"""
        self._top_hint.setText(text)
        self._top_hint.adjustSize()
        self._top_hint.raise_()
        self._top_hint.setVisible(True)
        self._hint_timer.start()

    def _clear_top_hint(self) -> None:
        """清除顶部提示（恢复默认空白）。"""
        self._hint_timer.stop()
        self._top_hint.setVisible(False)
        self._top_hint.setText("")

    def _on_chat_mode_changed(self, state: int) -> None:
        """聊天模式三点位开关：0=标准 1=Pro 2=cos，转发给 worker，仅在聊天模式下有意义。"""
        pro = (state == 1)
        cos = (state == 2)
        if self._mode != "chat":
            # 非聊天模式下切换：给出提示但允许用户先切到聊天再发送
            self._show_top_hint("请切换到「聊天模式」后使用聊天模式开关")
        elif cos:
            self._show_top_hint("已开启 cos 角色扮演：可在人设按钮设定角色")
        elif pro:
            self._show_top_hint("已开启 Pro 真人聊天")
        else:
            self._show_top_hint("已切换为标准聊天")
        # 人设按钮仅在 cos 模式下出现
        self._btn_persona.setVisible(cos and self._mode == "chat")
        self.proChatToggled.emit(pro)
        self.cosChatToggled.emit(cos)

    def _on_mode_changed(self, _idx: int) -> None:
        mode = self._mode_combo.currentData()
        if mode == self._mode:
            return
        self._mode = mode
        self._session["mode"] = mode
        self.modeSwitched.emit(mode)
        self._apply_view_for_mode()
        self._update_input_placeholder()
        self._update_chat_switch_visibility()
        # 切换模式后立即刷新状态条，显示当前模式对应的接口
        if self._model_status_state == "idle":
            self._reset_model_status()

    def _force_set_mode(self, mode: str) -> None:
        """UI 流程内部强制切换模式（不经下拉框用户操作）。

        同步下拉框显示、会话记录，并 emit modeSwitched 让 worker 的模式与 UI
        保持一致，避免出现“界面显示聊天模式、发消息却按代理模式执行”的错位。
        """
        self._mode = mode
        self._session["mode"] = mode
        self._mode_combo.blockSignals(True)
        self._mode_combo.setCurrentIndex(self._mode_combo.findData(mode))
        self._mode_combo.blockSignals(False)
        self.modeSwitched.emit(mode)
        self._update_input_placeholder()
        self._update_chat_switch_visibility()
        if self._model_status_state == "idle":
            self._reset_model_status()

    def _update_chat_switch_visibility(self) -> None:
        """聊天模式三点位开关仅在「聊天模式」下显示；人设按钮仅在 cos 模式下显示。
        经验库按钮仅在「自主代理模式」下显示。
        同时按当前模式刷新 Pro 编程相关控件显隐。"""
        self._btn_chat_mode.setVisible(self._mode == "chat")
        self._btn_persona.setVisible(self._mode == "chat" and self._btn_chat_mode.state() == 2)
        self._btn_exp_lib.setVisible(self._mode == "agent_mode")
        self._update_pro_prog_visibility()

    def _update_pro_prog_visibility(self) -> None:
        """Pro 编程开关仅在「编程模式」下显示；项目下拉与 IDE、控制台仅在 Pro 开启时显示。"""
        in_programming = self._mode == "programming"
        active = in_programming and self._btn_pro_prog.state() == 1
        self._btn_pro_prog.setVisible(in_programming)
        self._pro_toolbar.setVisible(active)
        self._proj_combo.setVisible(True)
        self._btn_ide.setVisible(bool(self._proj_combo.currentText().strip()))
        self._pro_console_container.setVisible(active)
        self._pro_console.setVisible(active and not self._pro_console_collapsed)
        if not active:
            self._pro_console_token_lbl.setText("上下文 --")
        # 仅 Pro 编程模式允许用户消息泡泡显示「撤销（还原项目）」
        self._chat_view.set_pro_undo(active)

    def _on_pro_prog_state_changed(self, state: int) -> None:
        """Pro 编程开关：0=标准 1=Pro，转发给 worker 并控制相关控件显隐。"""
        pro = (state == 1)
        if self._mode != "programming":
            self._show_top_hint("请切换到「编程模式」后使用 Pro 编程开关")
            return
        self._pro_prog_enabled = pro
        if pro:
            self._show_top_hint("已开启 Pro 编程智能体：选择项目文件夹后即可让模型新建/改造项目")
        else:
            self._show_top_hint("已切换为标准编程基础模式")
        self._update_pro_prog_visibility()
        self.proProgrammingToggled.emit(pro)
        s = self._settings
        s["pro_programming_enabled"] = pro
        app_config.save_settings(s)

    def _populate_recent_projects(self) -> None:
        """把最近项目历史填入下拉框（排除首项「＋ 新建项目…」）。"""
        for p in (self._settings.get("pro_programming_projects", []) or []):
            if p and self._proj_combo.findText(p) < 0 and p != "__new__":
                self._proj_combo.addItem(p)

    def _on_project_changed(self, idx: int) -> None:
        """用户从下拉框选择了一个项目（含「＋ 新建项目…」）。"""
        if idx < 0:
            return
        data = self._proj_combo.itemData(idx)
        if data == "__new__":
            self._choose_new_project()
            return
        text = self._proj_combo.currentText().strip()
        if text:
            self._set_project(text)

    def _on_project_typed(self) -> None:
        """用户在下拉框中输入路径并回车。"""
        text = self._proj_combo.currentText().strip()
        if text:
            self._set_project(text)

    def _choose_new_project(self) -> None:
        """新建项目：文件夹选择对话框选择一个目录（新建或已有空目录）。"""
        from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, "选择项目文件夹（可新建空目录后选择）")
        if path:
            self._set_project(path)

    def _set_project(self, path: str) -> None:
        """设置当前项目文件夹并持久化记录。"""
        path = (path or "").strip().rstrip("/\\")
        if not path:
            return
        self._proj_combo.setCurrentText(path)
        self._pro_prog_project = path
        self.proProgrammingProjectChanged.emit(path)
        # 更新设置并保存
        s = self._settings
        s["pro_programming_project"] = path
        recent = [x for x in (s.get("pro_programming_projects", []) or [])
                  if x and x != path]
        recent.insert(0, path)
        s["pro_programming_projects"] = recent[:10]
        app_config.save_settings(s)
        self._update_pro_prog_visibility()

    def _open_ide_dialog(self) -> None:
        """打开 IDE 窗口（非模态），与主窗口可同时操作。"""
        from ui.ide_dialog import IdeDialog
        if not self._pro_dir_exists():
            self._show_top_hint("请先选择有效的项目文件夹")
            return
        # 复用已打开的 IDE：若存在则置前，否则新建非模态窗口
        dlg = getattr(self, "_ide_dialog", None)
        if dlg is None or not dlg.isVisible():
            dlg = IdeDialog(self._proj_combo.currentText().strip(), self)
            dlg.setWindowModality(Qt.WindowModality.NonModal)
            self._ide_dialog = dlg
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            dlg.destroyed.connect(lambda: setattr(self, "_ide_dialog", None))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _open_experience_dialog(self) -> None:
        """打开经验库管理窗口（非模态，可边执行任务边管理）。"""
        from ui.experience_dialog import ExperienceDialog
        if self._exp_dialog is None or not self._exp_dialog.isVisible():
            self._exp_dialog = ExperienceDialog(self)
            self._exp_dialog.setWindowModality(Qt.WindowModality.NonModal)
            self._exp_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            self._exp_dialog.destroyed.connect(
                lambda: setattr(self, "_exp_dialog", None))
        self._exp_dialog.refresh_all()
        self._exp_dialog.show()
        self._exp_dialog.raise_()
        self._exp_dialog.activateWindow()

    def _pro_dir_exists(self) -> bool:
        p = self._proj_combo.currentText().strip()
        return bool(p) and os.path.isdir(p)

    def _on_pro_console(self, text: str) -> None:
        """Pro 编程控制台输出。"""
        if self._mode == "programming" and self._pro_prog_enabled:
            if not self._pro_console_container.isVisible():
                self._pro_console_container.show()
            self._pro_console.append_line(text)

    @staticmethod
    def _format_token_units(n: int) -> str:
        """把 token 数按 k（千）/ m（百万）/ b（十亿）单位缩写显示。"""
        try:
            n = int(n)
        except (TypeError, ValueError):
            return "--"
        if n <= 0:
            return "0"
        if n >= 1_000_000_000:
            return f"{n / 1_000_000_000:.2f}b"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.2f}m"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)

    def _on_pro_context_tokens(self, tokens: int) -> None:
        """更新智能体控制台标题栏的上下文 token 数。"""
        self._pro_console_token_lbl.setText(f"上下文 {self._format_token_units(tokens)}")
        self._pro_console_token_lbl.setToolTip(f"当前发送给模型的上下文估算 token 数：{tokens}")

    def _toggle_pro_console(self) -> None:
        """折叠/展开智能体控制台内容区。"""
        self._pro_console_collapsed = not self._pro_console_collapsed
        if self._pro_console_collapsed:
            self._pro_console.hide()
            self._pro_console_toggle.setText("▸")
        else:
            self._pro_console.show()
            self._pro_console_toggle.setText("▾")

    def _undo_selected_console_cmd(self) -> None:
        """撤销控制台中被勾选（☑）的命令：还原对应文件操作并移除该行。"""
        handles = self._pro_console.checked_handles()
        if not handles:
            self._show_top_hint("请先单击命令行左侧的 ○ 圆圈打钩，再点撤销")
            return
        worker = getattr(self, '_worker', None)
        if worker is None:
            self._show_top_hint("Worker 尚未连接，请稍后再试")
            return
        for h in handles:
            worker.proUndoCommand(h)
        self._pro_console.remove_handles(handles)

    def _snapshot_project(self):
        """把当前项目文件夹整体复制到 session 目录下的持久化快照中，返回快照根。

        不再用 tempfile — 那样程序重启后快照会被清理，用户历史里的「撤销」按钮就失效了。
        现在快照存到 session 目录（app_config.session_folder），和 session.json 同生命周期。
        shutil.copytree 在后台线程执行，避免阻塞 UI 主线程。
        """
        proj = self._proj_combo.currentText().strip()
        if not proj:
            return None
        session_id = self._session.get("id", "")
        if not session_id:
            # 新会话还没分配 id — 先生成一个让 app_config 初始化 session.json 的目录
            session_id = app_config.new_session_id()
            self._session["id"] = session_id
        snap_dir = os.path.join(app_config.session_folder(session_id), "snapshots")
        try:
            os.makedirs(snap_dir, exist_ok=True)
            # 用递增编号，每条用户消息对应一个快照目录
            existing = [int(x) for x in os.listdir(snap_dir)
                        if x.isdigit() and os.path.isdir(os.path.join(snap_dir, x))]
            next_idx = max(existing) + 1 if existing else 0
            snap = os.path.join(snap_dir, str(next_idx))
            if os.path.isdir(proj):
                import threading as _th
                _snap_result = [None]
                def _copy():
                    try:
                        shutil.copytree(proj, snap)
                        _snap_result[0] = snap
                    except Exception:
                        _snap_result[0] = None
                t = _th.Thread(target=_copy, daemon=True)
                t.start()
                t.join(timeout=10)
                if t.is_alive():
                    # 超时：复制仍在进行，返回 None 但不阻塞 UI
                    return None
                return _snap_result[0]
            else:
                os.makedirs(snap, exist_ok=True)
                return snap
        except Exception:
            return None

    def _on_pro_message_undo(self, widget) -> None:
        """右键用户泡泡「撤销」：还原项目到该条消息发送前，截断上下文，把消息回填到输入框。"""
        if not (self._mode == "programming" and self._pro_prog_enabled):
            return
        snap = getattr(widget, "_pro_snap", None)
        text = getattr(widget, "_text", "")
        # 先停掉正在运行的 Pro 会话，避免与回滚竞争；同时把 controller 的 history
        # 截断到该条用户消息之前，保证还原后上下文与项目状态一致
        worker = getattr(self, '_worker', None)
        if worker is not None:
            worker.resetProSession(truncate_before_user=text or None)
        proj = self._proj_combo.currentText().strip()
        if snap:
            if proj:
                if os.path.isdir(proj):
                    shutil.rmtree(proj, ignore_errors=True)
                os.makedirs(proj, exist_ok=True)
                try:
                    shutil.copytree(snap, proj, dirs_exist_ok=True)
                    self._show_top_hint("已还原项目到该消息发送前")
                except Exception as e:
                    self._show_top_hint(f"还原项目失败:{e}")
            else:
                self._show_top_hint("未选择项目，仅取回消息")
        else:
            self._show_top_hint("该消息无可还原快照，仅取回消息")
        # 无论还原是否成功，都要把该消息从视图移除并把文本取回输入框，
        # 保证「撤销」后消息一定回到对话框。
        self._chat_view.recall_widget(widget)  # 该消息已"回滚"，从视图移除，避免歧义
        if text:
            self._input.setPlainText(text)

    def _on_pro_agent_message(self, text: str, role: str) -> None:
        """Pro 编程对话气泡：role∈chat（对话消息）/summary（总结）/check（检查结果）。"""
        if not text or not text.strip():
            return
        # 每个 turn 结束时：把流式累积的正文做一次完整 markdown 重渲染
        if self._pro_thinking_bubble is not None:
            self._pro_thinking_bubble.finish_stream()
        if role == "summary":
            self._chat_view.add_message(f"〔总结〕\n{text.strip()}", "assistant")
        elif role == "check":
            self._chat_view.add_message(f"〔检查结果〕\n{text.strip()}", "assistant")
        elif role == "chat":
            # [chat]<start>对话内容</end>：普通 assistant 气泡，不附加标签前缀
            self._chat_view.add_message(text.strip(), "assistant")
        self._session["messages"].append(
            {"role": "assistant", "content": text.strip(), "pro_role": role})

    def _on_pro_agent_stream(self, chunk: str) -> None:
        """模型人话正文流式输出 → 实时送入智能体流式窗口（首个分块建立并展开）。"""
        if not chunk:
            return
        if self._pro_thinking_bubble is None:
            h = int(self._settings.get("pro_stream_window_height", 300))
            self._pro_thinking_bubble = self._chat_view.add_thinking_bubble(
                "", title="智能体", window_height=h)
            self._pro_thinking_bubble.expand()
        self._pro_thinking_bubble.append_agent_text(chunk)
        self._chat_view._scroll_to_bottom()

    def _on_pro_tool_call(self, func_name: str, description: str) -> None:
        """工具调用日志 → 追加到智能体流式窗口（灰色底块，样式区别于正文）。"""
        if not description:
            return
        # 确保 thinking bubble 已存在（即使模型还没输出任何正文）
        if self._pro_thinking_bubble is None:
            h = int(self._settings.get("pro_stream_window_height", 300))
            self._pro_thinking_bubble = self._chat_view.add_thinking_bubble(
                "", title="智能体", window_height=h)
            self._pro_thinking_bubble.expand()
        self._pro_thinking_bubble.append_tool_call(description)
        self._chat_view._scroll_to_bottom()

    def _on_pro_agent_history(self, text: str) -> None:
        """一段思考/汇报整段收尾：仅写入会话历史（显示已由流式完成）。"""
        if not text or not text.strip():
            return
        self._session["messages"].append(
            {"role": "assistant", "content": text.strip(), "pro_role": "agent"})

    def _on_pro_schedule(self, steps) -> None:
        """模型给出 [schedule] 计划 → 对话区创建可视化步骤清单（第 0 步转圈）。"""
        if not steps:
            return
        self._pro_schedule_widget = self._chat_view.add_steps_progress(list(steps))
        self._pro_schedule_widget.set_start()

    def _on_pro_schedule_done(self, index: int) -> None:
        """某一步完成：打钩并推进到下一步转圈。"""
        w = self._pro_schedule_widget
        if w is not None and 0 <= index:
            w.advance()

    def _on_pro_schedule_skip(self, index: int) -> None:
        """跳过整个剩余计划：从当前步起全部打叉（✗）。"""
        w = self._pro_schedule_widget
        if w is not None and 0 <= index:
            w.all_skip()

    def _on_pro_schedule_all_done(self) -> None:
        """进度收尾：剩余步骤全部打钩。"""
        w = self._pro_schedule_widget
        if w is not None:
            w.all_done()
        self._pro_schedule_widget = None

    def _update_persona_button(self) -> None:
        """把当前选中人设的标签显示到人设按钮上（未设置则显示默认文案）。"""
        persona = app_config.get_active_persona()
        tag = (persona or {}).get("tag") or "人设"
        self._btn_persona.setText(tag)

    def _open_persona_dialog(self) -> None:
        """打开人设切换/编辑窗口；关闭后刷新人设按钮标签。"""
        from ui.persona_dialog import PersonaDialog
        dlg = PersonaDialog(self)
        dlg.personaSelected.connect(self._update_persona_button)
        dlg.exec()
        self._update_persona_button()

    def _update_input_placeholder(self) -> None:
        """根据当前模式与交互状态，更新输入框内的提示文案。

        优先反映等待用户操作的交互状态（咨询回答、步骤修改），
        否则按当前模式给出对应的输入引导，保证切换模式后提示即时、切合。
        """
        if self._ui_state == "awaiting_consult":
            self._input.setPlaceholderText("请输入回答，回车发送")
            return
        if self._ui_state in ("awaiting_confirm", "paused"):
            self._input.setPlaceholderText("点「修改」手动编辑步骤，或在此输入提示让模型修改")
            return
        if self._mode == "chat":
            self._input.setPlaceholderText("输入消息开始聊天，回车发送（Shift+回车换行）")
        elif self._mode == "programming":
            self._input.setPlaceholderText("输入需求生成代码，回车发送（Shift+回车换行）")
        elif self._mode == "agent_mode":
            self._input.setPlaceholderText("输入目标任务，AI将自动截图、决策和执行（Shift+回车换行）")
        else:  # agent (规划模式)
            self._input.setPlaceholderText("输入目标任务，回车交给代理规划执行（Shift+回车换行）")

    def _apply_view_for_mode(self) -> None:
        # 执行期间主窗口已最小化，不操作 stack，避免 Windows 恢复窗口
        if self._executing_overlay_mode:
            return
        if self._ui_state == "awaiting_confirm" and self._mode == "agent":
            self._stack.setCurrentWidget(self._agent_panel)
        else:
            self._stack.setCurrentWidget(self._chat_page)

    def _on_steps_confirmed(self, steps, proceed: bool) -> None:
        if self._ui_state != "awaiting_confirm":
            # 执行完成后（idle）重新确认执行：直接提交步骤重新执行
            if self._ui_state == "idle" and steps and proceed:
                self._on_execute_steps(steps)
            return
        # 工作流编辑/新建模式
        if self._editing_workflow_id:
            goal = self._agent_panel.get_goal() or self._goal_text
            screen_state = self._agent_panel.get_screen_state()
            saved_ok = False
            exec_kind = "任务"
            exec_label = goal or "执行步骤"
            if self._editing_workflow_id == "__new__":
                # 新建工作流：弹出标签输入框
                from PySide6.QtWidgets import QInputDialog
                label, ok = QInputDialog.getText(
                    self, "保存工作流", "请输入工作流标签：",
                    text=goal[:20] if goal else "工作流",
                )
                if ok and label.strip():
                    label = label.strip()
                    wf = {
                        "label": label,
                        "goal": goal,
                        "screen_state": screen_state,
                        "steps": steps,
                    }
                    # 保存前剥离 user_skip（仅编辑界面使用，不写入文件）
                    for s in wf["steps"]:
                        s.pop("user_skip", None)
                    app_config.save_workflow(wf)
                    # 设置截图目录到工作流目录
                    self._agent_panel.set_screenshots_dir(
                        app_config.get_workflow_screenshots_dir(label))
                    msg = f"【已保存工作流】{label}"
                    self._chat_view.add_message(msg, "assistant")
                    self._session["messages"].append({"role": "assistant", "content": msg})
                    saved_ok = True
                    exec_kind = "工作流"
                    exec_label = label
            else:
                # 编辑现有工作流：更新 steps/goal/screen_state
                wf = app_config.load_workflow(self._editing_workflow_id)
                if wf:
                    wf["steps"] = steps
                    # 保存前剥离 user_skip（仅编辑界面使用，不写入文件）
                    for s in wf["steps"]:
                        s.pop("user_skip", None)
                    wf["goal"] = goal
                    wf["screen_state"] = screen_state
                    app_config.save_workflow(wf)
                    saved_ok = True
                    exec_kind = "工作流"
                    exec_label = wf.get("label", "") or goal or "执行步骤"
            # 保存工作流 ID，执行完成后回存坐标
            _exec_wf_id = self._editing_workflow_id if self._editing_workflow_id != "__new__" else (
                exec_label if saved_ok else None
            )
            self._editing_workflow_id = None
            if proceed:
                self._executing_workflow_id = _exec_wf_id
                self._goal_text = goal or "重新执行步骤"
                # 设置执行提示用的 kind/label
                self._exec_kind = exec_kind
                self._exec_label = exec_label
                # 重置步骤状态为 pending
                for s in steps:
                    s["status"] = "pending"
                    s["note"] = None
                self._session["steps"] = steps
                self._agent_panel.set_steps(steps)
                self._agent_panel.update_progress(0, len(steps))
                self._agent_panel.set_executing(True)
                if self._plan_from_worker:
                    # 新建工作流路径：worker 在 _await_confirm 中等待
                    # → 走 planConfirmed 让 orchestrate 继续执行
                    self.planConfirmed.emit(steps, True)
                else:
                    # 编辑已有工作流路径：worker 空闲
                    # → 走 workflowExecuted 直接执行
                    self.workflowExecuted.emit(steps, self._goal_text)
            else:
                # proceed=False：工作流模式下的修改请求
                if self._plan_from_worker:
                    self.planConfirmed.emit(steps, False)
            return
        # 正常代理模式（submitGoal → orchestrate → await_confirm）：走 planConfirmed
        if proceed:
            # 记录本次执行提示的 kind/label
            self._exec_kind = "任务"
            self._exec_label = self._goal_text or "执行步骤"
            # 同步 session 中的 steps 为确认后的步骤（worker 基于该列表写入坐标，
            # 否则执行完成后的 CollapsibleStepsWidget / 导出工作流拿不到视觉坐标）
            self._session["steps"] = steps
            # 更新面板显示（过滤掉已跳过的步骤后，面板行数与执行步骤一致）
            self._agent_panel.set_steps(steps)
            self._agent_panel.set_executing(True)
        self.planConfirmed.emit(steps, proceed)

    def _on_pause_clicked(self) -> None:
        if self._ui_state == "paused":
            # —— 恢复执行 ——
            # 0. 先原子地切回执行态（状态 + 按钮视觉），确保无论后续步骤同步是否出错，
            #    状态机都不会停留在"状态=paused 但按钮显示暂停"的失配态
            self._ui_state = "executing"
            self._btn_pause.setChecked(False)
            self._btn_pause.setText("⏸ 暂停")
            self._show_top_hint("已恢复执行")
            self._exec_overlay.set_paused(False)
            # 1. 从 agent_panel 取编辑后的全量步骤（保留未编辑行的变量信息）。
            #    这里涉及解析/重建 UI 行，任何异常都不能阻断恢复——只记录，不卡死状态机
            try:
                new_all_steps = self._agent_panel.get_edited_steps_preserving()
                if new_all_steps is not None:
                    # 切分：前 _paused_done_count 为已完成段（只读不允许改），剩余段发给 worker
                    done = min(self._paused_done_count, len(new_all_steps))
                    new_remaining = new_all_steps[done:]
                    # 同步更新 session 中的 steps（UI 侧）：已完成段沿用旧状态，剩余段刷新
                    old_steps = self._session.get("steps", [])
                    merged = list(old_steps[:done]) + list(new_remaining)
                    # 如果用户修改了剩余步骤的数量导致长度变化，按新步骤重排
                    for idx in range(done, len(merged)):
                        if idx < len(new_all_steps):
                            merged[idx] = new_all_steps[idx]
                    self._session["steps"] = merged
                    # 把新的剩余步骤发送给 worker（跨线程信号→安全）
                    self.stepsUpdated.emit(new_remaining)
                    # 对齐 agent_panel 和 overlay 的显示（确保后续 stepStatusChanged 索引正确）
                    self._agent_panel.set_steps(merged)
                    self._agent_panel.set_executing(True)
                    self._exec_overlay.set_steps(merged)
                # 2. 退出暂停编辑视觉
                self._agent_panel.exit_paused_edit_mode()
            except Exception as e:
                # 步骤同步失败不阻塞恢复，也绝不能让状态机停留在失配态
                print(f"[暂停/恢复] 恢复执行时同步步骤失败（不影响继续执行）：{e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
            self._paused_done_count = 0
            # 3. 发出 resume 信号
            self.resumeRequested.emit()
            # 4. 重新隐藏主窗口，显示 overlay
            if self._executing_overlay_mode:
                self._exec_overlay.show()
                # 先设透明度为 0 再 hide：避免最后一帧白底残留
                self.setWindowOpacity(0.0)
                QTimer.singleShot(30, self.hide)
        else:
            # —— 暂停 ——
            # 0. 先原子地切到暂停态（状态 + 按钮视觉）
            self._ui_state = "paused"
            # 暂停后回到代理面板：底部按钮显示「⏸ 暂停」，作为恢复执行的入口
            self._btn_pause.setChecked(False)
            self._btn_pause.setText("⏸ 暂停")
            self._show_top_hint("已暂停执行")
            self._exec_overlay.set_paused(True)
            self.pauseRequested.emit()
            # 暂停（聊天/编程模式下即停止生成）：复位模型状态条
            self._reset_model_status()
            # 暂停时：隐藏独立 overlay，恢复主窗口显示，并切换到代理面板（可编辑剩余步骤）
            if self._executing_overlay_mode:
                self._exec_overlay.hide()
                if hasattr(self, "_saved_geometry") and self._saved_geometry:
                    self.setGeometry(self._saved_geometry)
                # 先切到代理面板再 show，避免闪现 chat 界面
                self._stack.setCurrentWidget(self._agent_panel)
                # 恢复透明度后再显示（执行开始时设为了 0，避免白块）
                self.setWindowOpacity(1.0)
                self.showNormal()
                self.activateWindow()
                # 设置代理面板为暂停编辑模式（剩余步骤可编辑）
                steps = self._session.get("steps", [])
                self._agent_panel.set_steps(steps)
                self._paused_done_count = self._agent_panel.set_paused_edit_mode(steps)
        # —— 最终一致性保障 ——
        # 主窗口底部按钮始终显示「⏸ 暂停」：执行中点击=暂停，暂停编辑界面点击=恢复（继续入口）。
        # overlay 进度界面按暂停状态显示「已暂停/暂停」。
        self._btn_pause.setChecked(False)
        self._btn_pause.setText("⏸ 暂停")
        self._exec_overlay.set_paused(self._ui_state == "paused")

    def _open_model_config(self) -> None:
        """打开「模型配置」对话框，为 4 种模型分别选择接口预设。"""
        self._show_top_hint("已打开模型配置")
        if self._model_config_dialog is None:
            self._model_config_dialog = ModelConfigDialog(self)
            self._model_config_dialog.modelsConfigChanged.connect(self._on_models_config_changed)
        self._model_config_dialog.exec()

    def _on_models_config_changed(self, interfaces: dict) -> None:
        """模型配置保存后：刷新 worker 侧的 interfaces 并清理客户端缓存。"""
        # 把激活标签通知 worker（worker 内部也会按需 reload）
        active = interfaces.get("active", "")
        if active:
            self.interfaceSelected.emit(active)
        # invalidate 客户端缓存（可能换了接口，需重建 OpenAI 客户端）
        try:
            import model_client
            model_client.invalidate_cache()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Worker → UI 槽
    # ------------------------------------------------------------------
    def _on_stream_chunk(self, label: str, chunk: str) -> None:
        # 自主代理模式：转发到思考浮窗
        if self._mode == "agent_mode":
            self._agent_overlay.update_thinking(chunk)
            return

        # 规划模型的流式输出（label="planning"）不再呈现为对话消息气泡，
        # 仅保留 agent_worker 的控制台 print；规划结果由 planReady / replan 路径
        # 在 agent_panel 里直接显示步骤列表。
        if label == "planning":
            # 如果当前 bubble 是规划残留，结束它并清空（避免空气泡留着）
            if self._current_stream_bubble is not None:
                empty = not getattr(self._current_stream_bubble, "_text", "")
                self._chat_view.end_streaming_assistant(
                    self._current_stream_bubble,
                    final_text=None if empty else self._current_stream_bubble.text,
                )
                if empty:
                    self._chat_view._remove_bubble(self._current_stream_bubble)
                self._current_stream_bubble = None
            return
        # 所有其他输出（chat / programming / cos / long_input / executor 等）
        # 统一通过流式解析器：检测 THING_START/THING_END 折叠框和 ``` 代码围栏
        # 对话界面可见的模型输出（chat/programming/cos）：更新顶部运行状态条
        if label in ("chat", "programming", "cos"):
            self._model_status_tokens += self._estimate_tokens(chunk)
            self._record_token_snapshot()
            self._begin_model_streaming(label)
        self._programming_buffer += chunk
        self._process_stream_buffer()

    # ------------------------------------------------------------------
    # 流式标记解析（增量式状态机，适用于所有模式）
    # ------------------------------------------------------------------
    def _process_stream_buffer(self) -> None:
        """增量式解析 _programming_buffer，按 Markdown 标记分段实时创建/更新气泡。

        用于所有模式（chat / programming / agent 等）的流式输出。
        支持两种格式：
        1. 原生JSON：`{"reasoning": "思考内容", "content": "回答内容"}` 直接提取字段
        2. 标记分隔：`<think>...</think>` 提取思考块，回退兼容

        状态机：
          normal    → 检测思考开始（JSON reasoning字段 或 THING_START）/ ``` 代码围栏
          thinking  → 追加思考内容，检测结束
          code      → 累积代码内容，检测 ``` 围栏结束

        THING_START / THING_END 用于回退兼容标记分隔模式，用户可自行修改。
        """
        import re
        buf = self._programming_buffer
        pos = self._parsed_up_to
        if pos >= len(buf):
            return

        # ---- 思考标记常量（回退兼容标记分隔模式） ----
        THING_START = "<think>"  # 修改此处为你的思考开始标记，例如 " thinking" 或 "**thinking:**" 等
        THING_END = "</think>"    # 修改此处为你的思考结束标记，例如 " response" 或 "**answer:**" 等

        if self._stream_state == "normal":
            # ---- 1. 检测原生JSON格式字段 ----
            # 优先提取 reasoning 字段（深度思考）
            # 然后提取 content 字段（回答内容）
            # 搜索 `"reasoning":` 模式（深度思考，仅首次）
            if not self._thinking_shown_during_streaming:
                reasoning_match = re.search(r'"reasoning"\s*:\s*', buf[pos:])
                if reasoning_match:
                    # 标记之前的文本（通常是JSON开头{等）
                    match_start = pos + reasoning_match.start()
                    before = buf[pos:match_start]
                    if before.strip() and before.strip() != '{':
                        if self._current_stream_bubble is None:
                            self._current_stream_bubble = self._chat_view.start_streaming_assistant()
                        self._chat_view.append_stream(self._current_stream_bubble, before)
                        self._chat_view.end_streaming_assistant(self._current_stream_bubble)
                        self._current_stream_bubble = None
                    # 创建思考气泡
                    self._current_thinking_bubble = self._chat_view.add_thinking_bubble("")
                    self._stream_state = "thinking_json"
                    self._thinking_shown_during_streaming = True
                    self._in_thinking_block = True
                    # 跳过`"reasoning":`部分
                    self._parsed_up_to = match_start + len(reasoning_match.group(0))
                    self._process_stream_buffer()  # 继续处理后续内容
                    return

                # 回退：检测传统标记开始
                ti = buf.find(THING_START, pos)
                if ti >= 0:
                    # 标记之前的文本
                    before = buf[pos:ti]
                    if before.strip():
                        if self._current_stream_bubble is None:
                            self._current_stream_bubble = self._chat_view.start_streaming_assistant()
                        self._chat_view.append_stream(self._current_stream_bubble, before)
                        # 不传 final_text，避免覆盖气泡已累积的流式内容
                        self._chat_view.end_streaming_assistant(self._current_stream_bubble)
                        self._current_stream_bubble = None
                    # 创建思考气泡
                    self._current_thinking_bubble = self._chat_view.add_thinking_bubble("")
                    self._stream_state = "thinking"
                    self._thinking_shown_during_streaming = True
                    self._in_thinking_block = True
                    self._parsed_up_to = ti + len(THING_START)
                    self._process_stream_buffer()  # 继续处理后续内容
                    return

            # 已经处理完reasoning，检测content字段
            content_match = re.search(r'"content"\s*:\s*', buf[pos:])
            if content_match:
                # 标记之前的文本（通常是逗号等）
                match_start = pos + content_match.start()
                before = buf[pos:match_start]
                if before.strip() and before.strip() not in (',', '}'):
                    if self._current_stream_bubble is None:
                        self._current_stream_bubble = self._chat_view.start_streaming_assistant()
                    self._chat_view.append_stream(self._current_stream_bubble, before)
                    self._chat_view.end_streaming_assistant(self._current_stream_bubble)
                    self._current_stream_bubble = None
                # 进入content_json状态读取内容字符串
                self._stream_state = "content_json"
                # 跳过`"content":`部分
                self._parsed_up_to = match_start + len(content_match.group(0))
                self._process_stream_buffer()  # 继续处理后续内容
                return

            # ---- 2. 检测 ``` 代码围栏开始 ----
            m = re.search(r'(?:^|\n)```(\w*)\n', buf[pos:])
            if m:
                code_fence_start = pos + m.start()
                # 代码围栏之前的文本
                before = buf[pos:code_fence_start]
                if before.strip():
                    if self._current_stream_bubble is None:
                        self._current_stream_bubble = self._chat_view.start_streaming_assistant()
                    self._chat_view.append_stream(self._current_stream_bubble, before)
                    # 不传 final_text，避免覆盖气泡已累积的流式内容
                    self._chat_view.end_streaming_assistant(self._current_stream_bubble)
                    self._current_stream_bubble = None

                # 进入 code 状态，_code_start_pos 指向围栏后内容起始
                self._code_start_pos = code_fence_start + len(m.group(0))
                self._parsed_up_to = self._code_start_pos
                self._stream_state = "code"
                # 记录代码围栏声明的语言，供后续静态分析按语言检测
                self._current_code_lang = (m.group(1) or "").lower().strip()

                # 检查 ``` 围栏结束是否也在 buffer 中
                rest = buf[self._parsed_up_to:]
                close_m = re.search(r'(?:^|\n)```(\n|$)', rest)
                if close_m:
                    raw = buf[self._code_start_pos:self._parsed_up_to + close_m.start()]
                    if raw.strip():
                        cb = self._chat_view.add_code_bubble()
                        cb.set_code(raw.strip())
                        cb.set_language(self._current_code_lang)
                        self._connect_code_bubble_signals(cb)
                        self._has_code_bubble = True
                        self._last_code_bubble = cb
                    self._stream_state = "normal"
                    self._parsed_up_to = self._parsed_up_to + close_m.end()
                    self._process_stream_buffer()  # 可能还有后续代码围栏
                else:
                    # 关闭围栏尚未到达，立即创建流式代码气泡实时渲染
                    cb = self._chat_view.add_code_bubble()
                    cb.start_streaming()
                    cb.set_language(self._current_code_lang)
                    self._connect_code_bubble_signals(cb)
                    self._current_code_bubble = cb
                    self._last_code_bubble = cb
                    self._has_code_bubble = True
                    # 输出当前 buffer 中已有的代码内容（可能和代码围栏在同一 chunk 中）
                    initial = buf[self._code_start_pos:]
                    if initial.strip():
                        cb.append_code(initial)
                    self._parsed_up_to = len(buf)  # 标记已处理到末尾
                return

            elif pos < len(buf):
                # 没有代码围栏，作为普通文本流式
                # 注意：保留末尾 safe_margin 个字符不输出，以防标记跨分块
                # （如 ```` 被截断，下一块才补全，避免 pos 越过标记导致丢失）
                safe_margin = max(len(THING_START) + 12, 5)  # 更大的边距以容纳JSON字段
                safe_end = len(buf) - safe_margin
                if safe_end > pos:
                    text_to_output = buf[pos:safe_end]
                    if text_to_output.strip():
                        if self._current_stream_bubble is None:
                            self._current_stream_bubble = self._chat_view.start_streaming_assistant()
                        self._chat_view.append_stream(self._current_stream_bubble, text_to_output)
                    self._parsed_up_to = safe_end
                # 如果 safe_end <= pos，说明整个未处理区都在安全边际内，
                # 等待下一块数据到达后再尝试解析，避免输出部分标记内容

        elif self._stream_state == "thinking_json":
            # 原生JSON格式的reasoning字段：内容是JSON字符串，需要找到结束位置
            # 结束位置是 `"` 后可能跟 `,` 或 `}` 再跟其他字段（如 "content":）
            # 需要处理JSON转义：\" 不代表字符串结束
            current = buf[pos:]
            i = 0
            escaped = False
            while i < len(current):
                c = current[i]
                if escaped:
                    escaped = False
                elif c == '\\':
                    escaped = True
                elif c == '"':
                    # 找到字符串结束，用json.loads反转义
                    raw_content = buf[pos:pos + i]
                    if raw_content and self._current_thinking_bubble is not None:
                        try:
                            import json
                            unescaped = json.loads(f'"{raw_content}"')
                            self._current_thinking_bubble.append_thinking(unescaped)
                        except json.JSONDecodeError:
                            # 如果解析失败（转义序列不完整），追加原始内容
                            self._current_thinking_bubble.append_thinking(raw_content)
                    self._parsed_up_to = pos + i + 1  # 跳过结束引号
                    self._current_thinking_bubble = None
                    self._stream_state = "normal"
                    self._in_thinking_block = False
                    # 跳过引号后的空白和逗号/大括号，继续处理content等其他字段
                    while self._parsed_up_to < len(buf) and buf[self._parsed_up_to].isspace():
                        self._parsed_up_to += 1
                    if self._parsed_up_to < len(buf) and buf[self._parsed_up_to] in ',}':
                        self._parsed_up_to += 1
                        while self._parsed_up_to < len(buf) and buf[self._parsed_up_to].isspace():
                            self._parsed_up_to += 1
                    self._process_stream_buffer()  # 继续处理后续内容
                    return
                i += 1
            # 如果没找到结束引号，追加已有的内容（直接显示，后续再更正）
            if current and self._current_thinking_bubble is not None:
                self._current_thinking_bubble.append_thinking(current)
            self._parsed_up_to = len(buf)

        elif self._stream_state == "content_json":
            # 原生JSON格式的content字段：回答内容作为普通文本输出
            # 内容是JSON字符串，需要找到结束位置，处理转义后输出
            current = buf[pos:]
            i = 0
            escaped = False
            while i < len(current):
                c = current[i]
                if escaped:
                    escaped = False
                elif c == '\\':
                    escaped = True
                elif c == '"':
                    # 找到字符串结束，用json.loads反转义输出
                    raw_content = buf[pos:pos + i]
                    if raw_content.strip():
                        if self._current_stream_bubble is None:
                            self._current_stream_bubble = self._chat_view.start_streaming_assistant()
                        try:
                            import json
                            unescaped = json.loads(f'"{raw_content}"')
                            self._chat_view.append_stream(self._current_stream_bubble, unescaped)
                        except json.JSONDecodeError:
                            # 如果解析失败，输出原始内容
                            self._chat_view.append_stream(self._current_stream_bubble, raw_content)
                        self._chat_view.end_streaming_assistant(self._current_stream_bubble)
                        self._current_stream_bubble = None
                    self._parsed_up_to = pos + i + 1  # 跳过结束引号
                    self._stream_state = "normal"
                    # 跳过引号后的空白和逗号/大括号
                    while self._parsed_up_to < len(buf) and buf[self._parsed_up_to].isspace():
                        self._parsed_up_to += 1
                    if self._parsed_up_to < len(buf) and buf[self._parsed_up_to] in ',}':
                        self._parsed_up_to += 1
                        while self._parsed_up_to < len(buf) and buf[self._parsed_up_to].isspace():
                            self._parsed_up_to += 1
                    self._process_stream_buffer()  # 继续处理后续内容（可能有其他字段）
                    return
                i += 1
            # 如果没找到结束引号，追加已有的内容
            if current.strip():
                if self._current_stream_bubble is None:
                    self._current_stream_bubble = self._chat_view.start_streaming_assistant()
                self._chat_view.append_stream(self._current_stream_bubble, current)
                self._chat_view.end_streaming_assistant(self._current_stream_bubble)
                self._current_stream_bubble = None
            self._parsed_up_to = len(buf)

        elif self._stream_state == "thinking":
            # 检测 思考结束标记 THING_END（传统标记分隔模式）
            idx = buf.find(THING_END, pos)
            if idx >= 0:
                content = buf[pos:idx]
                if content and self._current_thinking_bubble is not None:
                    self._current_thinking_bubble.append_thinking(content)
                self._parsed_up_to = idx + len(THING_END)
                self._current_thinking_bubble = None
                self._stream_state = "normal"
                self._in_thinking_block = False
                # 继续处理后续内容（可能紧跟 ``` 代码围栏）
                self._process_stream_buffer()
            else:
                # 追加新内容到思考气泡
                newc = buf[pos:]
                if newc and self._current_thinking_bubble is not None:
                    self._current_thinking_bubble.append_thinking(newc)
                self._parsed_up_to = len(buf)

        elif self._stream_state == "code":
            # 检测 ``` 围栏结束
            # 从 _code_start_pos 开始搜索，而非 pos，以处理跨分块边界时
            # 结束围栏的反引号被分割（部分已在上一分块中作为代码输出）的情况
            rest = buf[self._code_start_pos:]
            close_m = re.search(r'(?:^|\n)```(\n|$)', rest)
            if close_m:
                # 使用 _code_start_pos 确保跨分块时获取完整代码内容
                raw = buf[self._code_start_pos:self._code_start_pos + close_m.start()]
                if raw.strip():
                    if self._current_code_bubble is not None:
                        # 已有流式气泡，用 set_code 结束流式模式并设置最终文本
                        self._current_code_bubble.set_code(raw.strip())
                        self._current_code_bubble = None
                    else:
                        cb = self._chat_view.add_code_bubble()
                        cb.set_code(raw.strip())
                        cb.set_language(self._current_code_lang)
                        self._connect_code_bubble_signals(cb)
                        self._has_code_bubble = True
                        self._last_code_bubble = cb
                self._parsed_up_to = self._code_start_pos + close_m.end()
                self._stream_state = "normal"
                self._process_stream_buffer()
            else:
                # 代码实时流式：追加新内容到现有代码气泡
                new_code = buf[pos:]
                if new_code and self._current_code_bubble is not None:
                    self._current_code_bubble.append_code(new_code)
                self._parsed_up_to = len(buf)

    def _on_assistant_message(self, text: str, role: str) -> None:
        # 母工作流后台执行，不显示到聊天界面
        if self._pending_master_callback is not None:
            return
        # 对话界面模型输出结束：状态条显示总用时与平均速度
        if role in ("assistant", "programming"):
            self._finish_model_output()
        # 记录流式阶段是否有缓冲内容（判断是否非流式输出）
        had_buffer = bool(self._programming_buffer)
        # 先 flush 剩余 buffer（流式阶段可能还有未处理完的标记）
        self._process_stream_buffer()
        # 最终 flush：安全边际可能留下未处理内容，全部输出
        # 注意：不传 final_text 给 end_streaming_assistant，否则会替换
        # 掉整个气泡已累积的文本，导致气泡只显示最后几个字。
        if self._parsed_up_to < len(self._programming_buffer):
            remaining = self._programming_buffer[self._parsed_up_to:]
            if remaining.strip():
                if self._current_stream_bubble is None:
                    self._current_stream_bubble = self._chat_view.start_streaming_assistant()
                self._chat_view.append_stream(self._current_stream_bubble, remaining)
                self._chat_view.end_streaming_assistant(self._current_stream_bubble)
                self._current_stream_bubble = None
            self._parsed_up_to = len(self._programming_buffer)
        # 结束未关闭的气泡（不传 final_text，避免覆盖已累积的流式内容）
        if self._current_stream_bubble is not None:
            self._chat_view.end_streaming_assistant(self._current_stream_bubble)
            self._current_stream_bubble = None
        if self._current_thinking_bubble is not None:
            self._current_thinking_bubble = None
        # 获取完整文本
        full_text = self._programming_buffer or text
        # 非流式输出（缓冲为空）：渲染所有分段
        if not had_buffer:
            segments = self._parse_programming_output(full_text)
            for seg_type, seg_content, seg_lang in segments:
                if seg_type == "code":
                    cb = self._chat_view.add_code_bubble()
                    cb.set_code(seg_content)
                    cb.set_language(seg_lang)
                    self._connect_code_bubble_signals(cb)
                    self._last_code_bubble = cb
                elif seg_type == "text" and seg_content.strip():
                    self._chat_view.add_message(seg_content, role)
        # 重置状态
        self._current_thinking_bubble = None
        self._in_thinking_block = False
        self._thinking_shown_during_streaming = False
        self._stream_state = "normal"
        self._has_code_bubble = False
        self._parsed_up_to = 0
        self._code_start_pos = 0
        self._programming_buffer = ""
        # 将输出按分段保存到历史记录（所有内容都要保存）
        segments = self._parse_programming_output(full_text)
        if segments:
            for seg_type, seg_content, seg_lang in segments:
                if seg_type == "code":
                    self._session["messages"].append({
                        "role": "assistant",
                        "type": "code",
                        "content": seg_content,
                        "language": seg_lang,
                    })
                elif seg_type == "thinking":
                    self._session["messages"].append({
                        "role": "assistant",
                        "type": "thinking",
                        "content": seg_content,
                    })
                elif seg_type == "text" and seg_content.strip():
                    self._session["messages"].append({
                        "role": "assistant",
                        "type": "text",
                        "content": seg_content,
                    })
        else:
            self._session["messages"].append({
                "role": "assistant",
                "type": "text",
                "content": full_text,
            })
        QTimer.singleShot(0, self._save_session)
        # 编程模式特有的逻辑
        if role == "programming":
            # 自动修复替换：清空旧气泡
            if self._pending_code_auto_fix_bubble is not None:
                old = self._pending_code_auto_fix_bubble
                self._pending_code_auto_fix_bubble = None
                self._last_code_bubble = None
                self._chat_view.remove_code_bubble(old)
            # 生成结束后自动运行静态分析
            QTimer.singleShot(300, self._run_static_analysis)
        # 暂停状态重置
        if self._ui_state == "paused" and self._mode in ("chat", "programming"):
            self._ui_state = "idle"
            self._btn_pause.setChecked(False)
            self._btn_pause.setText("⏸ 暂停")

    def _on_plan_ready(self, steps, screen_state: str, goal: str) -> None:
        self._session["steps"] = steps
        self._current_stream_bubble = None
        # 标记：规划来自 worker（worker 正在 _await_confirm 中等待确认）
        self._plan_from_worker = True
        if self._ui_state == "executing":
            # 执行中重规划：仅更新步骤列表，不切换视图、不要求确认
            self._exec_overlay.set_steps(steps)
            self._agent_panel.set_steps(steps)
            return
        if self._ui_state == "paused":
            # 暂停态重规划：仅刷新代理面板剩余步骤显示，保持暂停编辑模式（不退出暂停态）
            self._agent_panel.set_steps(steps)
            self._paused_done_count = self._agent_panel.set_paused_edit_mode(steps)
            return
        # 初始规划/修改：切到代理面板等待用户确认
        self._force_set_mode("agent")
        self._stack.setCurrentWidget(self._agent_panel)
        # 显示目标任务和屏幕状态（与工作流编辑一致）
        display_goal = goal or self._goal_text
        self._agent_panel.set_context(goal=display_goal, screen_state=screen_state, show=True)
        self._agent_panel.set_steps(steps)
        self._agent_panel.set_executing(False)
        self._ui_state = "awaiting_confirm"
        self._input.setPlaceholderText("点「修改」手动编辑步骤，或在此输入提示让模型修改")

    def _on_consult_requested(self, question: str, context: str) -> None:
        self._ui_state = "awaiting_consult"
        msg = "【需要你确认】" + question
        self._chat_view.add_message(msg, "assistant")
        self._session["messages"].append({"role": "assistant", "content": msg})
        # 系统提示音：让用户立即注意到需要咨询
        QApplication.beep()
        # 更新模型状态条：显示等待用户回复，而非待机状态
        self._model_status_state = "connecting"
        self._model_status_timer.stop()
        self._model_status_widget.setText("⏳ 接口连接中… 等待回复「继续」重试")
        self._model_status_widget.setToolTip("")
        # 执行期间主窗口已隐藏，弹出独立输入对话框让用户回答
        if self._executing_overlay_mode:
            from PySide6.QtWidgets import QInputDialog
            # 临时恢复主窗口透明度，使对话框可正常显示
            self.setWindowOpacity(1.0)
            self.showNormal()
            self.activateWindow()
            label_text = question
            if context:
                label_text = f"{question}\n（背景：{context}）"
            text, ok = QInputDialog.getText(
                self, "需要你的确认", label_text,
            )
            if ok and text.strip():
                self.consultAnswered.emit(text.strip())
            else:
                self.consultAnswered.emit("继续")
            # 重新隐藏主窗口，恢复 overlay
            self.setWindowOpacity(0.0)
            self.hide()
        else:
            self._input.setFocus()
            self._input.setPlaceholderText("请输入回答，回车发送")
            self._stack.setCurrentWidget(self._chat_page)

    def _on_step_status(self, index: int, status: str, note: str) -> None:
        self._agent_panel.set_step_status(index, status, note)
        # 同步更新 session 中的步骤状态（worker 修改的是自己的跨线程副本，
        # 需在 UI 侧同步，否则执行完成后的 CollapsibleStepsWidget 仍显示 pending）
        steps = self._session.get("steps", [])
        if 0 <= index < len(steps):
            steps[index]["status"] = status
            if note:
                steps[index]["note"] = note

    def _on_step_coord_updated(self, index: int, coord_data: dict) -> None:
        """worker 视觉定位完成后，把坐标同步到 session step 并刷新代理面板显示。"""
        steps = self._session.get("steps", [])
        if 0 <= index < len(steps):
            for k, v in coord_data.items():
                steps[index][k] = v
            # 直接刷新对应行的坐标显示（StepRow._step 与 session step 是同一引用）
            self._agent_panel.refresh_step_coord(index)

    def _on_progress_text(self, index: int, mark: str, color: str) -> None:
        self._agent_panel.set_step_mark(index, mark, color)
        self._exec_overlay.set_step_mark(index, mark, color)

    def _on_progress_bar(self, done: int, total: int) -> None:
        self._agent_panel.update_progress(done, total)
        self._exec_overlay.update_progress(done, total)

    def _on_execution_started(self) -> None:
        self._ui_state = "executing"
        self._plan_from_worker = False

        # 自主代理模式：使用右下角思考浮窗，不做标准执行 overlay
        if self._mode == "agent_mode":
            # 执行开始时确保全局热键有效（进入自主代理会隐藏主窗口）
            try:
                self._register_hotkey_from_settings()
            except Exception:
                pass
            # _on_send 已提前隐藏主窗口并显示浮窗，此处仅确保状态
            if self.isVisible():
                self.hide()
            if not self._agent_overlay.isVisible():
                self._agent_overlay.show()
            return

        if not self._exec_label:
            self._exec_label = self._goal_text or "执行步骤"
            self._exec_kind = "任务"
        self._exec_start_at = time.monotonic()
        # 执行开始时关闭所有辅助界面，只保留执行进度
        if self._workflow_panel:
            self._workflow_panel.close()
        if self._control_center_dialog:
            self._control_center_dialog.close()
        steps = self._session.get("steps", [])
        # 母工作流后台执行，不弹出 overlay
        if self._pending_master_callback is not None:
            self._executing_overlay_mode = False
            # 母工作流启动时最小化主窗口（后续块执行和母工作流结束都不恢复）
            if not self.isMinimized():
                self.showMinimized()
            # 控制台输出开始时间
            start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            label = self._exec_mwf_label or self._exec_label or "内联工作流"
            block_name = self._exec_block_name or ""
            block_seq = self._exec_block_seq or ""
            pad = "    " * self._exec_block_indent
            if block_name:
                seq_str = f"{block_seq} " if block_seq else ""
                print(f"{pad}[母工作流] {label} {seq_str}{block_name} 开始执行 {start_time}")
            else:
                print(f"{pad}[母工作流] 开始执行 {label} 时间={start_time}")
            # 切回聊天视图，避免显示代理面板
            self._force_set_mode("chat")
            self._stack.setCurrentWidget(self._chat_page)
            return
        self._exec_overlay.set_steps(steps)
        self._exec_overlay.set_hide_capture(self._settings.get("hide_on_capture", True))
        self._exec_overlay.apply_execution_opacity(self._settings.get("execution_opacity", 85))
        # 立即把进度条显示为 0/N（两侧都同步，不必等 worker 首条 progressBarChanged）
        self._on_progress_bar(0, len(steps))
        self._agent_panel.set_executing(True)
        # 方案：隐藏主窗口，把 ExecOverlay 作为独立顶层无边框透明窗口弹出
        self._executing_overlay_mode = True
        self._saved_geometry = self.geometry()
        self._saved_was_visible = self.isVisible()
        self._saved_was_minimized = self.isMinimized()
        # 必须先 hide 再 setParent(None) + setWindowFlags，否则 Windows 上仍带标题栏
        self._exec_overlay.hide()
        self._exec_overlay.setParent(None)
        self._exec_overlay.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self._exec_overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._exec_overlay.setGeometry(self._saved_geometry)
        # 隐藏主窗口，显示 overlay
        self._exec_overlay.show()
        # 注册 overlay 的截图隐藏钩子：执行时 overlay 是独立顶层窗口，
        # 若不注册，规划模型重规划截图时会截到 overlay 窗口本身。
        self._overlay_hide_hook, self._overlay_show_hook = capture.make_hide_show_hooks(
            int(self._exec_overlay.winId())
        )
        capture.register_capture_window(self._overlay_hide_hook, self._overlay_show_hook)
        # 先设透明度为 0 再 hide：避免主窗口最后一帧渲染留下 Windows 合成白块
        self.setWindowOpacity(0.0)
        self.hide()
        self._btn_pause.setText("⏸ 暂停")
        self._btn_pause.setChecked(False)
        self._exec_overlay.set_paused(False)

    def _on_execution_finished(self, summary: str) -> None:
        self._ui_state = "idle"
        self._plan_from_worker = False

        # 自主代理模式：隐藏思考浮窗并恢复主窗口，直接返回（不展示代理面板）
        if self._mode == "agent_mode":
            self._agent_overlay.hide()
            self._agent_overlay.clear()
            self._agent_overlay.set_status("")
            # 恢复截图隐藏机制
            capture.set_hide_on_capture(self._saved_hide_on_capture)
            self.show()
            self.raise_()
            self.activateWindow()
            # 如果 summary 是"已暂停"，在对话中显示提示
            if summary and "暂停" in summary:
                self._chat_view.add_message("⏸ 已暂停，可继续输入指令续传任务", "assistant")
            # 执行结束恢复空闲后，刷新输入框提示
            self._update_input_placeholder()
            self._ui_state = "idle"
            self._plan_from_worker = False
            self._exec_label = ""
            return
        # 执行结束恢复空闲后，刷新输入框提示为当前模式的默认文案
        self._update_input_placeholder()
        # 执行完毕后切换到代理面板，显示执行结果
        steps = self._session.get("steps", [])
        # 判断工作流是否成功（所有步骤 completed，无 failed）
        workflow_success = True
        for s in steps:
            if s.get("status") == "failed":
                workflow_success = False
                break
        # 记录本次执行是否由母工作流触发（决定是否跳过代理面板切换）
        _was_master_exec = self._pending_master_callback is not None
        # 通知母工作流执行器（如果有等待的回调）
        if self._pending_master_callback is not None:
            cb = self._pending_master_callback
            self._pending_master_callback = None
            msg = "执行完成" if workflow_success else "模板匹配失败，工作流已停止"
            cb(workflow_success, msg)
        # 母工作流后台执行，不操作主窗口 UI
        if _was_master_exec:
            self._executing_overlay_mode = False
            self._paused_done_count = 0
            # 控制台输出执行结果
            end_time = time.strftime("%Y-%m-%d %H:%M:%S")
            label = self._exec_mwf_label or self._exec_label or "内联工作流"
            status = "成功" if workflow_success else "失败"
            block_name = self._exec_block_name or ""
            block_seq = self._exec_block_seq or ""
            pad = "    " * self._exec_block_indent
            if block_name:
                seq_str = f"{block_seq} " if block_seq else ""
                print(f"{pad}[母工作流] {label} {seq_str}{block_name} {status} {end_time}")
                if not workflow_success:
                    print(f"{pad}[母工作流] {label} 执行出错，已终止")
            else:
                print(f"{pad}[母工作流] {label} {status} 结束时间={end_time}")
            # 出错时记录错误标签并更新悬浮球颜色
            if not workflow_success:
                if self._exec_mwf_label:
                    self._mwf_error_label = self._exec_mwf_label
                self._update_ball_color()
            self._exec_label = ""
            self._exec_block_name = ""
            self._exec_mwf_label = ""
            self._exec_block_seq = 0
            self._exec_block_indent = 0
            # 恢复默认：非母工作流块执行仍允许自动重规划
            self.setReplanRequested.emit(True)
            return

        # 关闭 overlay，恢复主窗口
        self._executing_overlay_mode = False
        self._paused_done_count = 0
        # 清理暂停编辑模式的视觉（高亮/只读）
        try:
            self._agent_panel.exit_paused_edit_mode()
        except Exception:
            pass
        # 注销 overlay 的截图隐藏钩子（overlay 即将重新挂回主窗口，不再是独立顶层窗口）
        if hasattr(self, "_overlay_hide_hook") and self._overlay_hide_hook is not None:
            capture.unregister_capture_window(self._overlay_hide_hook, self._overlay_show_hook)
            self._overlay_hide_hook = None
            self._overlay_show_hook = None
        self._exec_overlay.hide()
        # 把 overlay 重新挂回 central
        central = self.centralWidget()
        self._exec_overlay.setParent(central)
        self._exec_overlay.hide()
        # 恢复主窗口
        if hasattr(self, "_saved_geometry") and self._saved_geometry:
            self.setGeometry(self._saved_geometry)
        # 恢复透明度（执行开始/恢复执行时设为了 0，避免白块）
        self.setWindowOpacity(1.0)
        # 恢复执行前的可见性状态（与执行前一致）
        if self._saved_was_visible and not self._saved_was_minimized:
            self.showNormal()
            self.activateWindow()
        elif self._saved_was_minimized:
            self.showMinimized()
        # 如果执行前已隐藏，保持隐藏
        self._btn_pause.setChecked(False)
        self._btn_pause.setText("⏸ 暂停")
        self._exec_overlay.set_paused(False)
        # 右下角执行完成提示
        elapsed = max(0.0, time.monotonic() - self._exec_start_at)
        self._show_completion_toast(self._exec_label, elapsed, self._exec_kind)
        # 格式化执行用时：与 toast 保持同一口径
        if elapsed < 60:
            elapsed_text = f"{elapsed:.1f} 秒"
        else:
            minutes = int(elapsed // 60)
            secs = elapsed - minutes * 60
            elapsed_text = f"{minutes} 分 {secs:.0f} 秒"
        # 重置 label（下次执行若未显式设置，会在 _on_execution_started 里回填默认值）
        self._exec_label = ""
        # 执行完毕后切换到代理面板，显示执行结果
        steps = self._session.get("steps", [])
        # 执行完成后，将坐标回存到工作流文件
        if self._executing_workflow_id and steps:
            wf = app_config.load_workflow(self._executing_workflow_id)
            if wf:
                wf["steps"] = steps
                # 回存前剥离 user_skip（仅编辑界面使用，不写入文件）
                for s in wf["steps"]:
                    s.pop("user_skip", None)
                app_config.save_workflow(wf)
                # 恢复编辑状态，让"保存"按钮可以继续保存
                self._editing_workflow_id = self._executing_workflow_id
            self._executing_workflow_id = None
        if steps:
            self._chat_view.add_steps_summary(steps, show_actions=True)
            # 获取最后添加的 CollapsibleStepsWidget 并连接信号
            widget = self._find_last_steps_widget()
            if widget is not None:
                widget.exportRequested.connect(self._on_export_steps)
                widget.executeRequested.connect(self._on_execute_steps)
            # 把步骤快照嵌入 messages 数组（深拷贝，避免后续执行覆盖）
            self._session["messages"].append({
                "role": "assistant",
                "type": "steps",
                "steps": copy.deepcopy(steps),
            })
        done_text = f"执行完成，用时 {elapsed_text}"
        self._chat_view.add_message(done_text, "assistant")
        self._session["messages"].append({"role": "assistant", "content": done_text})
        # 会话保存延迟到事件循环空闲时，避免完成瞬间 UI 抖动
        QTimer.singleShot(0, self._save_session)
        # 执行完毕后切换到代理面板，显示执行结果
        self._stack.setCurrentWidget(self._agent_panel)
        if steps:
            self._agent_panel.set_steps(steps)
            # 同步每个步骤的状态标识（set_steps 新建的行都是 ○，需覆盖为执行结果）
            for idx, s in enumerate(steps):
                self._agent_panel.set_step_status(idx, s.get("status", "pending"), s.get("note", ""))
        self._agent_panel.set_executing(False)
        self._agent_panel.update_progress(0, 0)
        self._agent_panel.set_context(
            goal=self._agent_panel.get_goal() or self._goal_text,
            screen_state=self._agent_panel.get_screen_state(),
            show=True,
        )

    def _find_last_steps_widget(self) -> Optional[CollapsibleStepsWidget]:
        """获取对话区最后添加的 CollapsibleStepsWidget。"""
        layout = self._chat_view._layout
        for i in range(layout.count() - 2, -1, -1):  # 从倒数第二个往前找（最后是 stretch）
            item = layout.itemAt(i)
            if item and item.widget():
                row = item.widget()
                # row 是外层 QWidget，内含 CollapsibleStepsWidget
                child = row.findChild(CollapsibleStepsWidget)
                if child is not None:
                    return child
        return None

    def _on_status_message(self, text: str) -> None:
        # 自主代理模式：转发到思考浮窗状态栏
        if self._mode == "agent_mode":
            self._agent_overlay.set_status(text)
            return
        # 顶部栏已不再显示标题/状态提醒，状态消息忽略
        pass

    def _on_worker_interface_changed(self, tag: str) -> None:
        """Worker 侧切换激活接口后：记录到当前会话 tag。"""
        self._session["interface_tag"] = tag

    def _on_error(self, code: str, message: str) -> None:
        # 出错：复位模型状态条（避免停留在“正在输出”）
        self._reset_model_status()
        msg = f"【错误-{code}】{message}"
        self._chat_view.add_message(msg, "assistant")
        self._session["messages"].append({"role": "assistant", "content": msg})
        if code == "busy":
            from PySide6.QtWidgets import QMessageBox
            ret = QMessageBox.question(
                self, "任务繁忙",
                "当前有任务在执行中，但可能因异常导致状态卡住。\n"
                "是否要重置繁忙状态？\n\n"
                "注意：如果确实有任务正在运行，重置可能导致异常。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret == QMessageBox.StandardButton.Yes:
                self.busyResetRequested.emit()
                self._chat_view.add_message("✅ 已重置繁忙状态，可以重新执行。", "assistant")

    # ------------------------------------------------------------------
    # 自主代理模式：停止与快捷键
    # ------------------------------------------------------------------

    def _on_agent_overlay_stop(self) -> None:
        """浮窗停止按钮点击 → 复用暂停按钮逻辑（与快捷键一致）。"""
        self._agent_overlay.set_status("正在暂停...")
        self._request_pause()

    def _request_pause(self) -> None:
        """统一的「暂停/停止」入口：直接走对话界面暂停按钮的代码。

        仅在有正在执行或正在生成时触发（避免空闲态被错误置为暂停）。
        任意模式（聊天/规划/编程/自主代理）下都能暂停模型输出或任务执行。
        """
        active = (
            self._ui_state in ("executing", "awaiting_confirm", "awaiting_consult")
            or self._model_status_state in ("connecting", "streaming")
        )
        if active:
            print(f"[热键] 触发暂停: state={self._ui_state} model={self._model_status_state}")
            self._on_pause_clicked()

    def _on_stop_shortcut(self) -> None:
        """停止快捷键 → 触发暂停按钮逻辑（任何模式下均可暂停模型输出）。"""
        self._request_pause()

    def _on_screenshot_taken(self, png_bytes: bytes) -> None:
        """新截图完成 → 更新浮窗缩略图（仅自主代理模式）。"""
        if self._mode == "agent_mode" and self._agent_overlay.isVisible():
            self._agent_overlay.set_screenshot(png_bytes)

    def _on_agent_action(self, action: str, params: dict, result: str) -> None:
        """自主代理模式动作结果 → 更新浮窗可视化。"""
        if self._mode == "agent_mode" and self._agent_overlay.isVisible():
            self._agent_overlay.set_action_result(action, params, result)

    def _on_agent_model_time(self, seconds: float) -> None:
        """自主代理模式单次模型调用耗时（实时）→ 更新浮窗显示。"""
        if self._mode == "agent_mode" and self._agent_overlay.isVisible():
            self._agent_overlay.set_model_time(seconds)

    def _on_agent_task_time(self, seconds: int) -> None:
        """自主代理模式任务总用时（实时，整数秒）→ 更新浮窗显示。"""
        if self._mode == "agent_mode" and self._agent_overlay.isVisible():
            self._agent_overlay.set_task_time(seconds)

    # 模型运行状态条（对话区顶部）
    # ------------------------------------------------------------------
    # 瞬时速度滑动窗口（秒）与最大快照点数
    _INSTANT_WINDOW_SEC = 2.0
    _INSTANT_WINDOW_MAX_POINTS = 200

    def _record_token_snapshot(self) -> None:
        """记录 (时间, 累计token) 快照，供瞬时速度滑动窗口计算。"""
        now = time.time()
        w = self._model_status_window
        if w and w[-1][1] == self._model_status_tokens:
            return
        w.append((now, self._model_status_tokens))
        if len(w) > self._INSTANT_WINDOW_MAX_POINTS:
            del w[:len(w) - self._INSTANT_WINDOW_MAX_POINTS]

    def _instant_tps(self) -> float:
        """瞬时速度：最近 _INSTANT_WINDOW_SEC 秒窗口内的 token 速率。"""
        now = time.time()
        w = self._model_status_window
        if not w:
            return 0.0
        cutoff = now - self._INSTANT_WINDOW_SEC
        while len(w) > 1 and w[0][0] < cutoff:
            w.pop(0)
        if len(w) < 2:
            # 窗口数据不足：退回从开始到现在的平均速率作为瞬时近似
            dt = max(now - self._model_status_start, 0.0)
            return self._model_status_tokens / dt if dt > 0 else 0.0
        t0, tok0 = w[0]
        t1, tok1 = w[-1]
        dt = max(t1 - t0, 0.05)
        return max(0.0, (tok1 - tok0) / dt)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """估算 token 数：CJK 字符按 1 token，其余每 3 字符约 1 token。"""
        import re
        if not text:
            return 0
        cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]", text))
        other = len(text) - cjk
        return cjk + (other + 2) // 3

    def _resolve_interface_display(self, label: str) -> tuple:
        """解析 label 对应的接口名 / 地址 / 模型名，用于状态条展示。"""
        try:
            import app_config
            preset = app_config.get_preset_by_label(label)
            mm = app_config.get_model_map()
            tag = mm.get(label) or app_config.active_tag()
            return (tag, preset.get("base_url", ""), preset.get("model", ""))
        except Exception:
            return (label, "", "")

    def _status_text_connecting(self) -> str:
        tag = self._model_status_tag or self._model_status_kind
        model = f" · {self._model_status_model}" if self._model_status_model else ""
        return f"⏳ 正在连接接口「{tag}」{model}…"

    def _status_text_streaming(self) -> str:
        elapsed = max(time.time() - self._model_status_start, 0.0)
        tps = self._instant_tps()
        tag = self._model_status_tag or self._model_status_kind
        return (f"⚡ 正在输出 · 接口「{tag}」 · {elapsed:.1f}s · "
                f"{self._model_status_tokens} token · 瞬时 {tps:.1f} token/s")

    def _status_text_done(self) -> str:
        total = max(time.time() - self._model_status_start, 0.0)
        avg = self._model_status_tokens / total if total > 0 else 0.0
        tag = self._model_status_tag or self._model_status_kind
        return (f"✅ 输出完成 · 接口「{tag}」 · 总用时 {total:.1f}s · "
                f"平均 {avg:.1f} token/s · 共 {self._model_status_tokens} token")

    def _begin_model_connecting(self, label: str) -> None:
        """模型调用开始：显示“正在连接接口”，记录开始时间。"""
        self._model_status_kind = label
        self._model_status_tag, self._model_status_base, self._model_status_model = \
            self._resolve_interface_display(label)
        self._model_status_start = time.time()
        self._model_status_tokens = 0
        self._model_status_window = []
        self._model_status_state = "connecting"
        self._model_status_timer.stop()
        self._model_status_widget.setText(self._status_text_connecting())
        self._model_status_widget.setToolTip(self._model_status_base)

    def _begin_model_streaming(self, label: str) -> None:
        """收到首个输出 chunk：进入流式状态，启动计时刷新。"""
        if self._model_status_state in ("idle", "done"):
            # 兜底：未收到 modelCallStarted 时按当前 chunk 初始化
            self._begin_model_connecting(label)
        self._model_status_state = "streaming"
        self._model_status_timer.start()
        self._on_model_status_tick()

    def _on_model_status_tick(self) -> None:
        """流式计时刷新（200ms）：更新计时与 token/s。"""
        if self._model_status_state != "streaming":
            return
        self._model_status_widget.setText(self._status_text_streaming())

    def _finish_model_output(self) -> None:
        """输出结束：显示总用时与平均速度，保留直到下一条消息。"""
        if self._model_status_state not in ("connecting", "streaming"):
            return
        self._model_status_timer.stop()
        self._model_status_state = "done"
        self._model_status_widget.setText(self._status_text_done())

    def _reset_model_status(self) -> None:
        """复位状态条为待机（出错/停止/新对话时调用）。"""
        self._model_status_timer.stop()
        self._model_status_state = "idle"
        self._model_status_tokens = 0
        self._model_status_window = []
        tag = ""
        try:
            import app_config
            # 待机状态显示当前模式对应的接口，而非全局激活接口
            label = self._mode
            if label:
                preset = app_config.get_preset_by_label(label)
                mm = app_config.get_model_map()
                tag = mm.get(label) or app_config.active_tag()
            else:
                tag = app_config.active_tag()
        except Exception:
            pass
        self._model_status_tag = tag
        self._model_status_kind = ""
        self._model_status_widget.setText(f"💤 待机 · 当前接口「{tag or '未配置'}」")
        self._model_status_widget.setToolTip("")

    # ------------------------------------------------------------------
    # 编程模式：代码提取 / 导出 / 运行
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_code_from_markdown(text: str) -> str:
        """从模型输出的 Markdown（含 ``` 围栏）中提取代码块文本。

        若存在多个代码块，拼接所有代码块；若没有代码围栏，直接返回原文。
        """
        if not text:
            return ""
        import re
        blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
        if blocks:
            return "\n\n".join(b.strip("\n") for b in blocks)
        return text.strip()

    @staticmethod
    def _parse_programming_output(text: str) -> List[tuple]:
        """解析模型输出，按 Markdown 代码围栏划分为分段列表。

        支持两种格式：
        1. 原生JSON：`{"reasoning": "思考内容", "content": "回答内容"}` 直接提取字段
        2. 标记分隔：`<think>...</think>` 提取思考块，回退兼容

        返回 List[(type, content, lang)]：type 为 "text"、"code" 或 "thinking"，非代码段 lang 为空字符串。
        """
        if not text:
            return []
        import re
        import json
        segments: List[tuple] = []

        # 思考标记常量（回退兼容标记分隔模式，与 _process_stream_buffer 保持一致）
        # 用户可自行修改 THING_START 和 THING_END 字符串
        THING_START = "<think>"  # 修改此处为你的思考开始标记
        THING_END = "</think>"    # 修改此处为你的思考结束标记

        # 1. 尝试解析原生JSON格式：{"reasoning": "...", "content": "..."}
        text_stripped = text.strip()
        if text_stripped.startswith('{') and '"reasoning"' in text_stripped:
            try:
                data = json.loads(text_stripped)
                # 提取 reasoning 字段（深度思考）
                reasoning = data.get('reasoning', '')
                if reasoning and reasoning.strip():
                    segments.append(("thinking", reasoning.strip(), ""))
                # 提取 content 字段（回答内容）
                content = data.get('content', '')
                if content and content.strip():
                    segments.extend(
                        MainWindow._parse_code_fences_only(content))
                # 如果解析成功且找到了分段，直接返回
                if segments:
                    return segments
            except json.JSONDecodeError:
                # JSON解析失败，回退到传统标记解析
                pass

        # 2. 回退：传统标记解析 - 提取 thinking 块（最多一个，出现在开头）
        think_match = re.search(r'{}'.format(re.escape(THING_START)) + r'(.*?)' + r'{}'.format(re.escape(THING_END)), text, re.DOTALL)
        if think_match:
            # 思考块之前的内容
            before = text[:think_match.start()].strip()
            if before:
                segments.extend(
                    MainWindow._parse_code_fences_only(before))
            # 思考内容
            thinking_content = think_match.group(1).strip()
            if thinking_content:
                segments.append(("thinking", thinking_content, ""))
            # 思考块之后的内容
            after = text[think_match.end():].strip()
            if after:
                segments.extend(
                    MainWindow._parse_code_fences_only(after))
        else:
            segments.extend(
                MainWindow._parse_code_fences_only(text))

        if not segments:
            segments.append(("text", text.strip(), ""))
        return segments

    @staticmethod
    def _parse_code_fences_only(text: str) -> List[tuple]:
        """仅解析文本中的 Markdown 代码围栏，返回分段列表。

        不处理思考标记，适用于已去除思考块后的文本。
        """
        if not text:
            return []
        import re
        segments: List[tuple] = []
        pattern = r'```(\w*)\n(.*?)```'
        pos = 0
        for m in re.finditer(pattern, text, re.DOTALL):
            if m.start() > pos:
                before = text[pos:m.start()].strip()
                if before:
                    segments.append(("text", before, ""))
            segments.append(("code", m.group(2).strip(), (m.group(1) or "").lower().strip()))
            pos = m.end()
        after = text[pos:].strip()
        if after:
            segments.append(("text", after, ""))
        return segments

    class AutoNameThread(QThread):
        """后台线程调用写作模型自动生成脚本/工作流名称。"""

        namingDone = Signal(str, bool)  # generated_label, success

        def __init__(self, context: str, parent=None):
            super().__init__(parent)
            self._context = context

        def run(self) -> None:
            """调用写作模型生成简洁的名称。"""
            try:
                import model_client
                prompt = (
                    "请根据以下代码/目标，为它生成一个简洁、清晰、易读的中文名称（不超过8个字）：\n\n"
                    f"{self._context[:500]}\n\n"
                    "只返回名称，不要解释，不要标点，不要markdown。例如：数据分析、文件下载、界面美化。"
                )
                result = model_client.call_model("long_input", prompt, stream=False)
                label = result.strip()
                # 清理：去掉引号、换行、markdown，截取前12字
                label = label.replace('"', '').replace("'", "").replace("\n", " ").strip()
                if len(label) > 12:
                    label = label[:12]
                if not label:
                    import time
                    label = "code_" + time.strftime("%Y%m%d-%H%M%S")
                self.namingDone.emit(label, True)
            except Exception as e:
                import time
                label = "code_" + time.strftime("%Y%m%d-%H%M%S")
                self.namingDone.emit(label, False)


    @staticmethod
    def _is_web_code(code_text: str) -> bool:
        """检测代码是否为网页脚本（HTML 内容）。"""
        stripped = code_text.strip()
        return stripped.startswith("<!DOCTYPE") or stripped.startswith("<!doctype") or stripped.startswith("<html") or stripped.startswith("<HTML")

    def _on_code_export(self, code_text: str, fence_lang: str = "") -> None:
        """导出代码到脚本库，使用写作模型自动命名。脚本类型由代码语言自动判定，无需手动选择。"""
        if not code_text or not code_text.strip():
            return
        import time as _time

        # 脚本类型自动判定：优先按代码围栏声明的语言，其次按内容启发式
        fence = (fence_lang or "").lower().strip()
        if fence in ("html", "htm", "xml", "svg", "web"):
            language = "web"
        else:
            language = "web" if self._is_web_code(code_text) else "python"

        self._chat_view.add_message("【正在调用写作模型自动生成名称...】", "assistant")

        def _on_naming_done(label: str, success: bool):
            """命名完成回调。"""
            script = {
                "label": label,
                "language": language,
                "description": f"从编程模式导出（{_time.strftime('%Y-%m-%d %H:%M:%S')}）",
                "code": code_text,
            }
            try:
                script_id = app_config.save_script(script)
            except Exception as e:
                self._chat_view.add_message(f"【导出失败】{e}", "assistant")
                return
            lang_label = "网页脚本" if language == "web" else "Python 脚本"
            if success:
                msg = f"【已导出到脚本库】{lang_label}自动命名：{label}（id: {script_id}）"
            else:
                msg = f"【已导出到脚本库】{lang_label}自动命名失败，使用默认名称：{label}（id: {script_id}）"
            self._chat_view.add_message(msg, "assistant")
            self._session["messages"].append({"role": "assistant", "content": msg})
            if self._control_center_dialog is not None:
                self._control_center_dialog.refresh_scripts()

        self._name_thread = self.AutoNameThread(code_text, self)
        self._name_thread.namingDone.connect(_on_naming_done)
        self._name_thread.start()
        self._name_thread.finished.connect(self._name_thread.deleteLater)

    def _connect_code_bubble_signals(self, cb) -> None:
        """连接代码气泡的导出/运行信号到主窗口处理方法。"""
        cb.exportRequested.connect(self._on_code_export)
        cb.runRequested.connect(self._on_code_run)

    def _on_code_run(self, code_text: str) -> None:
        """运行代码。自动检测是否为网页脚本，网页脚本用浏览器打开，否则用 Python 执行。"""
        if not code_text or not code_text.strip():
            return
        if self._is_web_code(code_text):
            self._on_code_run_web(code_text)
            return
        # Python 代码：写入临时文件并用后台线程运行
        import tempfile, os
        try:
            fd, path = tempfile.mkstemp(suffix=".py", prefix="run_")
            os.close(fd)
            with open(path, "w", encoding="utf-8") as f:
                f.write(code_text)
        except OSError as e:
            self._chat_view.add_message(f"【运行失败：无法写入】{e}", "assistant")
            return

        self._chat_view.add_message("【正在运行】请稍候…", "assistant")

        import sys
        runner = CodeRunnerThread(sys.executable, path, os.path.dirname(path), self)
        runner.resultReady.connect(lambda stdout, stderr, rc: self._on_code_run_finished(
            stdout, stderr, rc, code_text, path))
        runner.start()

    def _on_code_run_web(self, code_text: str) -> None:
        """网页脚本：写入临时 .html 文件并使用默认浏览器打开。"""
        if not code_text or not code_text.strip():
            return
        import tempfile, os, webbrowser
        try:
            fd, path = tempfile.mkstemp(suffix=".html", prefix="web_")
            os.close(fd)
            with open(path, "w", encoding="utf-8") as f:
                f.write(code_text)
        except OSError as e:
            self._chat_view.add_message(f"【运行失败：无法写入临时文件】{e}", "assistant")
            return

        self._chat_view.add_message("【正在使用默认浏览器打开网页脚本...】", "assistant")
        webbrowser.open(f"file://{path}")

    def _on_code_run_finished(self, stdout: str, stderr: str,
                              returncode: int, code_text: str,
                              tmp_path: str = "") -> None:
        """代码运行完成后的回调（主线程）：显示可折叠结果 + 手动修复按钮。"""
        # 删除临时运行文件
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        is_error = returncode != 0 or bool(stderr.strip())
        if is_error:
            error_msg = stderr.strip() or f"退出码: {returncode}"
            if stdout.strip():
                error_msg = stdout.strip() + "\n" + error_msg
            output = error_msg
            error_text = error_msg
        else:
            output = stdout.strip() or "（无输出）"
            error_text = ""

        result_widget = self._chat_view.add_code_result(output, is_error, error_text)
        if is_error:
            fix_prompt = (
                "以下代码运行时出错，请根据错误信息修复代码：\n\n"
                f"```python\n{code_text}\n```\n\n"
                f"错误信息：\n```\n{error_text}\n```\n\n"
                "请输出修复后的完整代码。"
            )
            result_widget.fixRequested.connect(
                lambda _: self.fixCodeRequested.emit(fix_prompt))
        self._session["messages"].append({
            "role": "assistant",
            "type": "run_result",
            "content": f"【{'运行出错' if is_error else '运行成功'}】\n```\n{output}\n```",
            "run_output": output,
            "run_is_error": is_error,
            "run_error_text": error_text,
        })
        QTimer.singleShot(0, self._save_session)

    # 代码围栏语言 → 受支持检测器类别的映射
    _LANG_MAP = {
        "python": "python", "py": "python", "python3": "python",
        "html": "html", "htm": "html", "xml": "html", "svg": "html", "web": "html",
        "css": "css",
        "javascript": "javascript", "js": "javascript",
    }

    def _detect_code_language(self, code: str, fence_lang: str = "") -> str:
        """判定代码语言：优先用围栏声明，其次用内容启发式，默认按 Python 处理。

        返回 "" 表示明确声明了暂不支持的语言（bash/sql/json 等），应跳过静态分析。
        """
        lang = (fence_lang or "").lower().strip()
        mapped = self._LANG_MAP.get(lang)
        if mapped:
            return mapped
        if lang:
            # 有明确围栏语言但不支持 → 跳过静态分析，避免误用 Python 检测器
            return ""
        # 无围栏语言：按内容启发式判断
        if self._is_web_code(code):
            return "html"
        return "python"

    @staticmethod
    def _check_html_syntax(code: str) -> str:
        """轻量 HTML 语法检查：检测明显不配对的结束标签，返回错误信息；无错误返回空串。

        采用宽松策略：忽略 void 标签（<br> 等）与 HTML5 允许的隐式闭合，
        只对“结束标签缺少对应开始标签”这类明确错误报错，避免误报。
        """
        from html.parser import HTMLParser

        VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img",
                     "input", "link", "meta", "param", "source", "track", "wbr"}

        class _Checker(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)
                self.tag_counts = {}   # tag -> [open_count, close_count]
                self.close_only = []   # 缺少开始标签的结束标签 (行号, tag)

            def handle_starttag(self, tag, attrs):
                t = tag.lower()
                if t not in VOID_TAGS:
                    self.tag_counts.setdefault(t, [0, 0])[0] += 1

            def handle_startendtag(self, tag, attrs):
                pass  # <br/> 等自闭合标签无需配对

            def handle_endtag(self, tag):
                t = tag.lower()
                if t in VOID_TAGS:
                    return
                c = self.tag_counts.setdefault(t, [0, 0])
                c[1] += 1
                if c[1] > c[0]:
                    self.close_only.append((self.getpos()[0], t))

        try:
            checker = _Checker()
            checker.feed(code)
        except Exception:
            return "HTML 结构解析失败（存在无法解析的标签）"
        if checker.close_only:
            line, t = checker.close_only[0]
            return f"第 {line} 行：结束标签 </{t}> 缺少对应的开始标签"
        return ""

    @staticmethod
    def _check_brace_balance(code: str, lang_name: str) -> str:
        """检查代码中括号（()、[]、{}）与引号是否配对，返回错误信息；无错误返回空串。

        跳过字符串字面量、行注释 // 与块注释 /* */ 内的内容，避免误报。
        """
        pairs = {"}": "{", ")": "(", "]": "["}
        opens = set(pairs.values())
        stack = []
        i, n = 0, len(code)
        quote = None
        while i < n:
            ch = code[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in ('"', "'", "`"):
                quote = ch
                i += 1
                continue
            if ch == "/" and i + 1 < n and code[i + 1] == "/":
                j = code.find("\n", i)
                i = n if j < 0 else j + 1
                continue
            if ch == "/" and i + 1 < n and code[i + 1] == "*":
                j = code.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
            if ch in opens:
                stack.append((ch, i))
            elif ch in pairs:
                if not stack or stack[-1][0] != pairs[ch]:
                    line = code.count("\n", 0, i) + 1
                    return f"第 {line} 行：多余的 '{ch}'（{lang_name} 括号不配对）"
                stack.pop()
            i += 1
        if stack:
            ch, pos = stack[-1]
            line = code.count("\n", 0, pos) + 1
            return f"第 {line} 行：'{ch}' 未闭合（{lang_name} 括号不配对）"
        return ""

    @staticmethod
    def _check_css_syntax(code: str) -> str:
        """CSS 语法检查：括号/引号配对（忽略注释），返回错误信息；无错误返回空串。"""
        return MainWindow._check_brace_balance(code, "CSS")

    @staticmethod
    def _check_javascript_syntax(code: str) -> str:
        """JavaScript 语法检查：优先用 node --check；node 不可用时退回括号配对检查。"""
        import subprocess, tempfile, os
        path = None
        try:
            fd, path = tempfile.mkstemp(suffix=".mjs", prefix="js_check_")
            os.close(fd)
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
            proc = subprocess.run(
                ["node", "--check", path],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                return ""
            err = (proc.stderr or proc.stdout or "").strip()
            m = re.search(r":(\d+):(\d+)(?:\n(.*))?", err, re.DOTALL)
            if m:
                line, col = m.group(1), m.group(2)
                msg = (m.group(3) or err).strip()
                return f"第 {line} 行第 {col} 列：{msg[:300]}"
            return (err or "JavaScript 语法错误")[:400]
        except FileNotFoundError:
            # node 未安装：退回括号配对检查
            return MainWindow._check_brace_balance(code, "JavaScript")
        except subprocess.TimeoutExpired:
            return ""
        except OSError:
            return ""
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _run_static_analysis(self) -> None:
        """对最后生成的代码按语言执行静态分析，有错则自动修复。

        - Python：ast.parse 语法检查
        - HTML / CSS / JavaScript：对应的轻量语法检查
        - 其他语言或未知围栏：跳过，避免误用 Python 检测器误报
        """
        cb = self._last_code_bubble
        if cb is None or not cb.code_text.strip():
            return
        code = cb.code_text
        # 跳过已标记自动修复中的气泡
        if self._pending_code_auto_fix_bubble is not None:
            return
        fence_lang = getattr(cb, "language", "") or ""
        language = self._detect_code_language(code, fence_lang)

        if language == "python":
            import ast
            try:
                ast.parse(code)
                return  # 语法正确，无需修复
            except SyntaxError as e:
                error_msg = f"第 {e.lineno} 行：{e.msg}\n{e.text.strip() if e.text else ''}"
        elif language == "html":
            error_msg = self._check_html_syntax(code)
            if not error_msg:
                return
        elif language == "css":
            error_msg = self._check_css_syntax(code)
            if not error_msg:
                return
        elif language == "javascript":
            error_msg = self._check_javascript_syntax(code)
            if not error_msg:
                return
        else:
            # 不支持的代码语言（bash/sql/json 等）：跳过静态分析，避免误报
            return

        fix_prompt = (
            "以下代码存在语法错误，请修复：\n\n"
            f"```{language}\n{code}\n```\n\n"
            f"错误信息：\n```\n{error_msg}\n```\n\n"
            "请输出修复后的完整代码。"
        )
        self._pending_code_auto_fix_bubble = cb
        self._last_code_bubble = None
        self.fixCodeRequested.emit(fix_prompt)

    # ------------------------------------------------------------------
    # 消息删除
    # ------------------------------------------------------------------
    def _on_message_delete(self, widget) -> None:
        """从对话视图中移除指定气泡，并从会话中删除对应消息（不计入模型上下文）。"""
        self._chat_view.remove_widget(widget)
        content = ""
        if hasattr(widget, '_text') and widget._text:
            content = widget._text
        elif hasattr(widget, 'code_text') and widget.code_text:
            content = widget.code_text
        elif hasattr(widget, '_output') and widget._output:
            content = widget._output
        if content:
            for i, msg in enumerate(self._session["messages"]):
                msg_content = msg.get("content", "")
                if content == msg_content or content in msg_content or msg_content in content:
                    del self._session["messages"][i]
                    break
        QTimer.singleShot(0, self._save_session)

    def _on_message_recall(self, widget) -> None:
        """撤回用户消息：删除该消息及其对应的所有回复，不计入模型上下文。"""
        content = getattr(widget, '_text', '')
        if not content:
            return
        # 从会话中删除用户消息及其后续回复（直到下一条用户消息）
        for i, msg in enumerate(self._session["messages"]):
            if msg.get("role") == "user" and msg.get("content") == content:
                # 删除用户消息
                del self._session["messages"][i]
                # 删除后续的所有 assistant 消息（直到下一条 user 消息）
                while i < len(self._session["messages"]):
                    if self._session["messages"][i].get("role") == "user":
                        break
                    del self._session["messages"][i]
                break
        # 从视图移除气泡
        self._chat_view.recall_widget(widget)
        QTimer.singleShot(0, self._save_session)

    # ------------------------------------------------------------------
    # 设置 / 历史 / 新对话
    # ------------------------------------------------------------------
    def _open_settings(self) -> None:
        self._show_top_hint("已打开设置")
        if self._settings_dialog is None:
            self._settings_dialog = SettingsDialog(self)
            self._settings_dialog.settingsChanged.connect(self._on_settings_changed)
            self._settings_dialog.interfacesChanged.connect(self._on_iface_external_changed)
        self._settings_dialog.exec()

    def _apply_window_title(self) -> None:
        """根据设置应用窗口标题；留空则不显示标题文字。"""
        title = self._settings.get("window_title", "")
        self.setWindowTitle(title)

    def _on_settings_changed(self) -> None:
        self._settings = app_config.load_settings()
        self._apply_window_title()
        self._load_background()
        self._agent_panel.reload_settings()
        self._exec_overlay.reload_settings()
        # 任务清单透明度（0=不透明，100=完全透明）
        self._task_list.set_opacity(int(self._settings.get("task_list_opacity", 0)))
        # 消息泡泡透明度（0=不透明，100=完全透明）
        self._chat_view.set_bubble_opacity(int(self._settings.get("bubble_opacity", 0)))
        # Pro 智能体流式窗口高度（设置变更后实时生效于当前活跃窗口）
        if self._pro_thinking_bubble is not None:
            try:
                self._pro_thinking_bubble.set_window_height(
                    int(self._settings.get("pro_stream_window_height", 300)))
            except Exception:
                pass
        hide_on = self._settings.get("hide_on_capture", True)
        capture.set_hide_on_capture(hide_on)
        # 重新注册停止快捷键（用户可能修改了设置）
        self._register_hotkey_from_settings()
        # 同步浮窗停止按钮上的快捷键标签
        self._agent_overlay.set_stop_shortcut(self._settings.get("stop_shortcut", "Ctrl+."))
        self.reloadSettingsRequested.emit()

    def _on_iface_external_changed(self, tag: str) -> None:
        """设置对话框修改了激活预设 → 通知 worker 并清理缓存。"""
        self.interfaceSelected.emit(tag)
        # 清理客户端缓存（可能换了接口地址或 key）
        try:
            import model_client
            model_client.invalidate_cache()
        except Exception:
            pass

    def _open_history(self) -> None:
        self._show_top_hint("已打开历史记录")
        if self._history_panel is None:
            self._history_panel = HistoryPanel(self)
            self._history_panel.loadRequested.connect(self._on_load_session)
            self._history_panel.deleteRequested.connect(self._on_delete_sessions)
        self._history_panel.refresh()
        self._history_panel.show()
        self._history_panel.raise_()

    def _on_delete_sessions(self, session_ids: list) -> None:
        """批量删除多个历史会话文件（来自历史面板的多选删除）。"""
        if not session_ids:
            return
        # 若会话数>1，弹确认防止误删；单个也确认一下（删除不可恢复）
        from PySide6.QtWidgets import QMessageBox
        n = len(session_ids)
        msg = "确定要删除这条历史对话吗？" if n == 1 else f"确定要删除选中的 {n} 条历史对话吗？\n删除后无法恢复。"
        ret = QMessageBox.question(
            self, "删除历史对话", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        for sid in session_ids:
            if not sid:
                continue
            # 如果当前打开的就是被删除的会话 → 清空
            if self._session.get("id") == sid:
                self.stopRequested.emit()
                self._flush_autosave()
                self._session = self._new_session_obj()
                self._chat_view.clear_messages()
                self._agent_panel.clear_steps()
                self._agent_panel.set_context(show=False)
                self._current_stream_bubble = None
                self._current_thinking_bubble = None
                self._in_thinking_block = False
                self._thinking_shown_during_streaming = False
                self._stream_state = "normal"
                self._has_code_bubble = False
                self._parsed_up_to = 0
                self._code_start_pos = 0
                self._programming_buffer = ""
                self._ui_state = "idle"
                self._editing_workflow_id = None
                self._goal_text = ""
                self.newConversationRequested.emit()
                self._apply_view_for_mode()
            app_config.delete_session(sid)
        # 刷新历史面板列表（若还显示）
        if self._history_panel and self._history_panel.isVisible():
            self._history_panel.refresh()

    def _on_load_session(self, session_id: str) -> None:
        # 加载历史前：先把之前的工作流编辑 flush 保存
        self._flush_autosave()
        session = app_config.load_session(session_id)
        if not session:
            return
        self._session = session
        self._chat_view.clear_messages()
        # 按顺序恢复消息和步骤列表
        has_steps_in_messages = False
        for m in session.get("messages", []):
            msg_type = m.get("type", "")
            if msg_type == "steps":
                # 步骤列表消息：添加可折叠步骤列表
                has_steps_in_messages = True
                steps_snapshot = m.get("steps", [])
                self._chat_view.add_steps_summary(steps_snapshot, show_actions=True)
                widget = self._find_last_steps_widget()
                if widget is not None:
                    widget.exportRequested.connect(self._on_export_steps)
                    widget.executeRequested.connect(self._on_execute_steps)
            elif msg_type == "code":
                # 代码消息：还原代码气泡
                content = m.get("content", "")
                code = self._extract_code_from_markdown(content)
                cb = self._chat_view.add_code_bubble()
                cb.set_code(code)
                cb.set_language(m.get("language", ""))
                self._connect_code_bubble_signals(cb)
            elif msg_type == "thinking":
                # 思考消息：还原可折叠思考气泡
                content = m.get("content", "")
                if content:
                    self._chat_view.add_thinking_bubble(content)
            elif msg_type == "run_result":
                # 运行结果气泡：还原可折叠 + 修复按钮
                rw = self._chat_view.add_code_result(
                    m.get("run_output", ""),
                    m.get("run_is_error", False),
                    m.get("run_error_text", ""),
                )
                if m.get("run_is_error", False):
                    code_text = self._last_code_bubble.code_text if self._last_code_bubble else ""
                    fix_prompt = (
                        "以下代码运行时出错，请根据错误信息修复代码：\n\n"
                        f"```python\n{code_text}\n```\n\n"
                        f"错误信息：\n```\n{m.get('run_error_text', '')}\n```\n\n"
                        "请输出修复后的完整代码。"
                    ) if code_text else ""
                    if fix_prompt:
                        rw.fixRequested.connect(
                            lambda _, fp=fix_prompt: self.fixCodeRequested.emit(fp))
            else:
                content = m.get("content", "")
                role = m.get("role", "assistant")
                pro_role = m.get("pro_role")
                # 历史含附件结构的用户消息 → 重建完整附件气泡
                att = m.get("attachment")
                if role == "user" and isinstance(att, dict) and (att.get("images") or att.get("files")):
                    self._chat_view.add_attachment_message(
                        att.get("text", ""),
                        att.get("images") or [],
                        att.get("files") or [],
                    )
                elif role == "user":
                    shown = _render_attach_text(content)
                    self._chat_view.add_message(shown, role)
                    # Pro 模式历史用户消息：如果有持久化的项目快照路径，
                    # 重建 _pro_snap 属性，让撤销还原能力也能恢复
                    if self._mode == "programming":
                        snap = m.get("pro_snap")
                        if snap and os.path.isdir(snap):
                            # 找到刚创建的 user bubble 并挂上
                            row_count = self._chat_view._layout.count()
                            # 向上找最后一个 BubbleWidget（用户消息靠右布局）
                            for i in range(row_count - 1, -1, -1):
                                item = self._chat_view._layout.itemAt(i)
                                if item and item.widget():
                                    row = item.widget()
                                    # row 里有一个 BubbleWidget
                                    bw = row.findChild(self._chat_view.BubbleWidget)
                                    if bw is not None:
                                        bw._pro_snap = snap
                                        break
                else:
                    # assistant 消息：如果是 Pro 模式的总结/检查结果，加回标签前缀
                    if pro_role == "summary":
                        self._chat_view.add_message(f"〔总结〕\n{content}", role)
                    elif pro_role == "check":
                        self._chat_view.add_message(f"〔检查结果〕\n{content}", role)
                    else:
                        self._chat_view.add_message(content, role)
        # 旧会话兼容：messages 中没有 steps 类型，但 session["steps"] 存在
        if not has_steps_in_messages:
            steps = session.get("steps", [])
            if steps:
                self._chat_view.add_steps_summary(steps, show_actions=True)
                widget = self._find_last_steps_widget()
                if widget is not None:
                    widget.exportRequested.connect(self._on_export_steps)
                    widget.executeRequested.connect(self._on_execute_steps)
        # 恢复目标任务文本（从第一条 user 消息）
        goal_text = ""
        for m in session.get("messages", []):
            if m.get("role") == "user" and m.get("type") != "steps":
                goal_text = m.get("content", "")
                break
        self._goal_text = goal_text
        # 设置代理面板的步骤（用于重新执行）
        steps = session.get("steps", [])
        if steps:
            self._agent_panel.set_steps(steps)
            self._agent_panel.set_context(goal=goal_text, screen_state="", show=True)

        # 恢复 Pro 编程 controller 的持久化 state（history + schedule + 控制变量）
        pro_state = session.get("pro_state")
        if pro_state and self._mode == "programming":
            worker = getattr(self, '_worker', None)
            if worker is not None:
                worker.restoreProState(pro_state)

        # 恢复 Pro 编程模式的可视化计划进度条
        # 如果 state 里有活跃计划（schedule_cur 不是 -1 且 schedule_steps 非空），
        # 在对话区末尾重建一个进度条（当前步转圈，前面的步打钩，后面的步待执行）
        if pro_state and self._mode == "programming":
            pro_steps = pro_state.get("schedule_steps") or []
            pro_cur = pro_state.get("schedule_cur", -1)
            if pro_steps and pro_cur >= 0:
                psw = self._chat_view.add_steps_progress(pro_steps)
                # 把已经完成的步（cur 之前的）标记为完成
                for _ in range(pro_cur):
                    psw.advance()
                self._pro_schedule_widget = psw
            self._pro_thinking_bubble = None  # 下一个 agent 流式窗口会自动创建

        if self._history_panel:
            self._history_panel.close()

    # ------------------------------------------------------------------
    # 工作流
    # ------------------------------------------------------------------
    def _autosave_workflow(self) -> None:
        """工作流编辑模式下：把当前面板中的 goal/screen_state/steps 自动保存到磁盘。

        I/O 部分（JSON 读写）用 QTimer.singleShot(0, ...) 推迟到事件循环空闲时执行，
        避免用户编辑时因磁盘写入造成 GUI 短暂卡顿。
        """
        if not self._editing_workflow_id:
            return
        # 先尝试解析当前步骤（保留未编辑行的变量化信息）——这一步在当前栈做，保证数据新鲜
        steps = self._agent_panel.get_edited_steps_preserving()
        if steps is None:
            return  # 步骤解析失败（如空行），不保存，避免覆盖成空内容
        goal = self._agent_panel.get_goal() or self._goal_text
        screen_state = self._agent_panel.get_screen_state()
        editing_id = self._editing_workflow_id

        # 捕获快照，延迟到空闲时做实际 I/O
        def _do_save():
            # 保存前再次检查：用户可能已切出编辑模式（id 被清空）
            if editing_id != self._editing_workflow_id and editing_id != "__new__":
                # 用户切到了另一个工作流，但快照对应的是老 id —— 仍然保存（保证之前修改不丢失）
                pass
            if editing_id == "__new__":
                # 使用写作模型自动生成工作流名称
                context = goal or "未命名工作流"
                def _on_workflow_naming_done(label: str, success: bool):
                    if not success or not label.strip():
                        label = (goal[:20] if goal else "未命名工作流").strip() or "未命名工作流"
                    wf = {
                        "label": label,
                        "goal": goal,
                        "screen_state": screen_state,
                        "steps": steps,
                    }
                    new_id = app_config.save_workflow(wf)
                    self._agent_panel.show_save_hint()
                    # 设置截图目录到工作流目录
                    self._agent_panel.set_screenshots_dir(
                        app_config.get_workflow_screenshots_dir(label))
                    # 如果用户还在编辑同一个新建工作流，更新 id 指向新分配的
                    if self._editing_workflow_id == "__new__":
                        self._editing_workflow_id = new_id
                    # 刷新控制中心工作流列表
                    if self._control_center_dialog is not None:
                        self._control_center_dialog.refresh_workflows()

                self._name_thread = AutoNameThread(context, self)
                self._name_thread.namingDone.connect(_on_workflow_naming_done)
                self._name_thread.start()
                self._name_thread.finished.connect(self._name_thread.deleteLater)
            else:
                wf = app_config.load_workflow(editing_id)
                if wf:
                    wf["steps"] = steps
                    wf["goal"] = goal
                    wf["screen_state"] = screen_state
                    app_config.save_workflow(wf)
                    self._agent_panel.show_save_hint()

        QTimer.singleShot(0, _do_save)

    def _show_completion_toast(self, label: str, elapsed: float, kind: str) -> None:
        """在右下角显示执行完成的 Toast 提示（伴随系统提示音，5 秒自动关闭）。"""
        if self._toast is not None:
            # 上一条还在显示：先立即淡出
            try:
                self._toast.close()
            except Exception:
                pass
            self._toast = None
        self._toast = CompletionToast(self)
        self._toast.show_with(label, elapsed, kind)

    def _flush_autosave(self) -> None:
        """立即执行一次待保存的自动保存（切换视图/关闭窗口前调用）。"""
        if self._autosave_timer.isActive():
            self._autosave_timer.stop()
            self._autosave_workflow()
        else:
            self._autosave_workflow()

    def _on_save_requested(self) -> None:
        """保存按钮点击：保存到文件，无可保存内容时提示。"""
        if not self._editing_workflow_id:
            self._agent_panel.show_save_error("当前没有正在编辑的工作流")
            return
        self._flush_autosave()

    def _open_workflows(self) -> None:
        # 打开工作流列表前：flush 自动保存，避免列表中显示旧内容
        self._flush_autosave()
        if self._workflow_panel is None:
            self._workflow_panel = WorkflowPanel(self)
            self._workflow_panel.newRequested.connect(self._on_workflow_new)
            self._workflow_panel.editRequested.connect(self._on_workflow_edit)
            self._workflow_panel.executeRequested.connect(self._on_workflow_execute)
            # labelEdited 在面板内部已直接保存 + refresh，这里仅做额外响应（如更新标题）
            self._workflow_panel.labelEdited.connect(lambda _wid, _label: None)
            self._workflow_panel.controlCenterRequested.connect(self._open_control_center)
        self._workflow_panel.refresh()
        self._workflow_panel.show()
        self._workflow_panel.raise_()

    def _open_control_center(self) -> None:
        """打开控制中心对话框（工作流/脚本库/母工作流编排）。"""
        self._show_top_hint("已打开控制中心")
        if self._control_center_dialog is None:
            self._control_center_dialog = ControlCenterDialog(self)
            # 工作流信号
            self._control_center_dialog.workflowNewRequested.connect(self._on_workflow_new)
            self._control_center_dialog.workflowEditRequested.connect(self._on_workflow_edit)
            self._control_center_dialog.workflowExecuteRequested.connect(self._on_workflow_execute)
            # 脚本信号
            self._control_center_dialog.scriptEditRequested.connect(self._on_script_edit)
            self._control_center_dialog.scriptExecuteRequested.connect(self._on_script_execute)
            # 母工作流信号
            self._control_center_dialog.masterWorkflowSaved.connect(
                self._on_master_workflow_saved)
            self._control_center_dialog.runRequested.connect(
                self._on_master_workflow_run)
            self._control_center_dialog.stopRequested.connect(
                self._on_master_workflow_stop)
            self._control_center_dialog.inlineWorkflowRequested.connect(
                self._on_workflow_execute_inline)
        self._control_center_dialog.show()
        self._control_center_dialog.raise_()
        self._control_center_dialog.activateWindow()

    def _on_master_workflow_run(self, mwf_id: str) -> None:
        """启动母工作流执行。"""
        self._master_executor.run_master_workflow(mwf_id)

    def _on_master_workflow_stop(self, mwf_id: str) -> None:
        """停止母工作流执行。"""
        self._master_executor.stop_master_workflow(mwf_id)

    def _on_master_workflow_saved(self) -> None:
        """母工作流保存后：清除错误状态，更新悬浮球颜色与任务清单。"""
        self._mwf_error_label = ""
        self._update_ball_color()
        self._refresh_task_list()

    def _start_enabled_master_workflows(self) -> None:
        """启动所有已启用的母工作流（程序启动时自动调用）。"""
        self._master_executor.run_all_enabled()
        self._update_ball_color()
        # 程序启动默认打开任务清单
        self._refresh_task_list()
        self._task_list.show()

    def _emit_master_completed(self, mwf_id: str, success: bool, end_time: str) -> None:
        """母工作流整体完成回调（后台线程）：派发到主线程处理。"""
        self._masterCompleted.emit(mwf_id, success, end_time)

    def _on_master_workflow_completed(self, mwf_id: str, success: bool, end_time: str) -> None:
        """母工作流整体执行完成（主线程）。

        - 更新任务清单状态（✓/✗ + 完成时刻）
        - 未设置"每天定时"的母工作流：执行完成后禁用
        """
        mwf = app_config.load_master_workflow(mwf_id)
        is_daily = app_config.is_daily_master_workflow(mwf)
        self._task_results[mwf_id] = {
            # "date" 仅每天定时任务用做"当天"判断，非定时任务常驻显示
            "date": time.strftime("%Y-%m-%d") if is_daily else "",
            "status": "success" if success else "failed",
            "time": end_time,
        }
        # 未设定"每天定时"的母工作流：执行完成后默认禁用
        if mwf and not is_daily and mwf.get("enabled", True):
            mwf["enabled"] = False
            app_config.save_master_workflow(mwf)
            self._update_ball_color()
            # 同步控制中心的状态显示
            if self._control_center_dialog:
                self._control_center_dialog.refresh()
        self._refresh_task_list()

    def _refresh_task_list(self) -> None:
        """读取所有启用的母工作流，刷新右上角任务清单。"""
        today = time.strftime("%Y-%m-%d")
        tasks: List[Dict[str, Any]] = []
        for mwf in app_config.list_master_workflows():
            mwf_id = mwf.get("id", "")
            # 已启用 或 已有完成记录的（完成后禁用的未定时工作流）都显示
            if not mwf.get("enabled", True) and mwf_id not in self._task_results:
                continue
            full = app_config.load_master_workflow(mwf_id) or {}
            is_daily = app_config.is_daily_master_workflow(full)
            res = self._task_results.get(mwf_id)
            status = None
            done_time = ""
            if res:
                # 每天定时任务仅当天有效；非定时任务常驻显示
                if (not is_daily) or res.get("date") == today:
                    status = res.get("status")
                    done_time = res.get("time", "")
            tasks.append({
                "id": mwf_id,
                "label": mwf.get("label", "未命名"),
                "is_daily": is_daily,
                "scheduled": app_config.get_master_workflow_schedule_time(full) if is_daily else "",
                "status": status,
                "time": done_time,
            })
        # 按任务清单中实际显示顺序编号
        for idx, t in enumerate(tasks, start=1):
            t["index"] = idx
        self._task_list.set_tasks(tasks)

    def _toggle_task_list(self, shown: bool) -> None:
        """切换任务清单显隐。"""
        if shown:
            self._refresh_task_list()
            self._task_list.show()
        else:
            self._task_list.hide()

    def _update_ball_color(self) -> None:
        """根据母工作流状态更新悬浮球颜色。"""
        mwfs = app_config.list_master_workflows()
        has_enabled = any(mwf.get("enabled", True) for mwf in mwfs)
        if self._mwf_error_label:
            self.ballColorChanged.emit("red")
            self.ballErrorLabelChanged.emit(self._mwf_error_label)
        elif has_enabled:
            self.ballColorChanged.emit("green")
            self.ballErrorLabelChanged.emit("")
        else:
            self.ballColorChanged.emit("default")
            self.ballErrorLabelChanged.emit("")

    def _open_errored_master_workflow(self) -> None:
        """打开出错母工作流。"""
        if not self._mwf_error_label:
            return
        # 查找匹配的母工作流 ID
        for mwf in app_config.list_master_workflows():
            if mwf.get("label") == self._mwf_error_label:
                self._open_control_center()
                if self._control_center_dialog:
                    self._control_center_dialog.select_master_workflow(mwf.get("id", ""))
                return
        # 未找到，清空错误标记
        self._mwf_error_label = ""
        self._update_ball_color()

    def _execute_master_workflow_node(self, workflow_id: str, goal: str,
                                       on_finish, node=None) -> None:
        """母工作流节点执行回调（可能从后台线程调用）。
        通过信号派发到主线程安全执行。"""
        self._masterNodeExecRequested.emit(workflow_id, goal, on_finish, node)

    def _on_execute_master_node(self, workflow_id: str, goal: str,
                                 on_finish, node=None) -> None:
        """母工作流节点执行（主线程安全）。

        工作流节点：存储 on_finish 回调，等待实际执行完成后再调用。
        goal 节点：暂维持立即回调（"已提交"），后续可扩展为等待模型输出。
        """
        # 提取块名称和母工作流标签
        if node and isinstance(node, dict):
            self._exec_block_name = node.get("_block_name", "")
            self._exec_mwf_label = node.get("_mwf_label", "")
            self._exec_block_seq = node.get("_block_seq", 0)
            self._exec_block_indent = node.get("_block_indent", 0)
            replan_enabled = node.get("_replan_enabled", False)
        else:
            self._exec_block_name = ""
            self._exec_mwf_label = ""
            self._exec_block_seq = 0
            self._exec_block_indent = 0
            replan_enabled = False

        # 同步到 worker：关闭重规划时模板匹配失败/无法定位直接结束执行，不调用规划模型
        self.setReplanRequested.emit(replan_enabled)

        if workflow_id:
            self._pending_master_callback = on_finish
            self._on_workflow_execute(workflow_id)
            # 注意：on_finish 不立即调用，而是在 _on_execution_finished 中调用
        elif node and node.get("_inline_steps"):
            # 内联工作流：直接执行节点中存储的步骤
            self._pending_master_callback = on_finish
            inline_goal = node.get("_inline_goal", goal or "内联工作流")
            inline_steps = node.get("_inline_steps", [])
            self._on_workflow_execute_inline(inline_steps, inline_goal)
        elif replan_enabled and goal:
            # 重规划模式：存储 on_finish 回调，提交目标到规划模型
            self._pending_master_callback = on_finish
            self._editing_workflow_id = "__new__"
            self._goal_text = goal
            self._chat_view.add_message(goal, "user")
            self.goalSubmitted.emit(goal)
            # on_finish 不立即调用，在 _on_execution_finished 中调用
        elif goal:
            self._editing_workflow_id = "__new__"
            self._goal_text = goal
            self._chat_view.add_message(goal, "user")
            self.goalSubmitted.emit(goal)
            if on_finish:
                on_finish(True, "已提交")
        else:
            if on_finish:
                on_finish(False, "无效的节点配置")

    def _on_export_steps(self, steps: list) -> None:
        """导出步骤为工作流：弹出输入框让用户自定义标签。

        默认标签使用代理面板中目标任务（goal），
        屏幕状态（screen_state）从模型输出解析后显示在代理面板中。
        """
        from PySide6.QtWidgets import QInputDialog
        # 默认标签：优先使用代理面板中的目标任务
        default_label = (self._agent_panel.get_goal()
                         or self._goal_text
                         or self._session.get("title", "工作流"))
        label, ok = QInputDialog.getText(
            self, "导出工作流", "请输入工作流标签：",
            text=default_label,
        )
        if not ok or not label.strip():
            return
        # 屏幕状态：从代理面板获取（由模型输出解析后设置）
        screen_state = self._agent_panel.get_screen_state()
        wf = {
            "label": label.strip(),
            "goal": (self._agent_panel.get_goal()
                     or self._goal_text or ""),
            "screen_state": screen_state,
            "steps": steps,
        }
        app_config.save_workflow(wf)
        msg = f"【已导出工作流】{label.strip()}"
        self._chat_view.add_message(msg, "assistant")
        self._session["messages"].append({"role": "assistant", "content": msg})

    def _on_execute_steps(self, steps: list) -> None:
        """直接执行步骤（跳过规划/确认）。"""
        goal = self._goal_text or "重新执行步骤"
        # 记录执行提示：从历史记录或导出步骤中执行
        self._exec_kind = "任务"
        self._exec_label = goal
        # 重置步骤状态为 pending，清掉旧的状态标记和进度
        for s in steps:
            s["status"] = "pending"
            s["note"] = None
        # 同步到 session（_on_execution_started 会从 session 读步骤给 exec_overlay）
        self._session["steps"] = steps
        # 重置 AgentPanel 进度条 + 步骤标记
        self._agent_panel.set_steps(steps)
        self._agent_panel.update_progress(0, len(steps))
        # 切到代理面板（执行中会再切到 exec_overlay）
        self._force_set_mode("agent")
        self._stack.setCurrentWidget(self._agent_panel)
        self.workflowExecuted.emit(steps, goal)

    def _on_workflow_new(self) -> None:
        """新建工作流：手动创建，无需规划模型初步规划。"""
        # 切换到新建前：先保存前一个工作流的修改
        self._flush_autosave()
        from PySide6.QtWidgets import QInputDialog
        goal, ok = QInputDialog.getText(self, "新建工作流", "请输入目标任务：")
        if not ok or not goal.strip():
            return
        # 标记：确认后保存为工作流
        self._editing_workflow_id = "__new__"
        self._goal_text = goal.strip()
        self._plan_from_worker = False
        # 设 goal（供重规划使用）
        self.goalSet.emit(goal.strip())
        # 直接显示代理面板，任务和屏幕状态从代理面板中读取
        self._agent_panel.set_context(goal=goal.strip(), screen_state="", show=True)
        self._agent_panel.set_steps([])
        self._agent_panel.set_executing(False)
        self._force_set_mode("agent")
        self._stack.setCurrentWidget(self._agent_panel)
        self._ui_state = "awaiting_confirm"
        self._input.setPlaceholderText("新建工作流：点「修改」手动编辑步骤，或在此输入提示让模型修改")
        # 延迟一帧定位主窗口到管理界面旁
        QTimer.singleShot(0, self._position_next_to_management)

    def _position_next_to_management(self) -> None:
        """将主窗口（代理面板）定位到工作流管理界面的右侧，确保不超出屏幕。

        如果管理界面右侧空间不够，则定位到左侧。
        如果管理界面不可见或已关闭，则回退到 _bring_main_to_front。
        """
        # 找一个可见的管理界面作为参考
        ref = None
        for candidate in (self._workflow_panel, self._control_center_dialog):
            if candidate is not None and candidate.isVisible():
                ref = candidate
                break
        if ref is None:
            self._bring_main_to_front()
            return

        self.showNormal()
        self.raise_()
        self.activateWindow()
        QApplication.setActiveWindow(self)

        # 获取屏幕几何
        screen = QGuiApplication.screenAt(ref.mapToGlobal(ref.rect().center()))
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        screen_geo = screen.availableGeometry()

        ref_geo = ref.frameGeometry()
        mw_geo = self.frameGeometry()

        # 尝试放在右侧：ref 右边 + 间距
        gap = 10
        x = ref_geo.right() + gap
        y = ref_geo.top()
        # 如果右侧超出屏幕，尝试左侧
        if x + mw_geo.width() > screen_geo.right():
            x = ref_geo.left() - gap - mw_geo.width()
        # 如果左侧也超出（或左侧为负），选空间更大的一侧
        if x < screen_geo.left():
            space_right = screen_geo.right() - ref_geo.right()
            space_left = ref_geo.left() - screen_geo.left()
            if space_right >= space_left:
                x = screen_geo.right() - mw_geo.width()
            else:
                x = screen_geo.left()
        # 纵向对齐：尽量与 ref 顶部对齐，但确保不超出屏幕上下
        y = max(screen_geo.top(), min(y, screen_geo.bottom() - mw_geo.height()))
        # 最终边界裁剪
        x = max(screen_geo.left(), min(x, screen_geo.right() - mw_geo.width()))
        self.move(x, y)

        # Win32 API 强制置顶
        try:
            hwnd = int(self.winId())
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self._agent_panel.setFocus()

    def _bring_main_to_front(self) -> None:
        """将主窗口提升到顶层并激活。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()
        QApplication.setActiveWindow(self)
        # Win32 API 强制置顶（Windows 焦点锁定策略绕过）
        try:
            hwnd = int(self.winId())
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self._agent_panel.setFocus()

    def _on_workflow_edit(self, wf_id: str) -> None:
        """编辑工作流：加载到代理面板，进入 awaiting_confirm 状态。"""
        # 切换到另一个工作流前：先保存前一个工作流的修改
        self._flush_autosave()
        wf = app_config.load_workflow(wf_id)
        if not wf:
            return
        steps = wf.get("steps", [])
        goal = wf.get("goal", "")
        screen_state = wf.get("screen_state", "")
        label = wf.get("label", "工作流")
        self._editing_workflow_id = wf_id
        self._goal_text = goal
        # 设置截图目录到工作流目录
        self._agent_panel.set_screenshots_dir(app_config.get_workflow_screenshots_dir(label))
        # 编辑已有工作流：worker 空闲，不走 orchestrate/await_confirm
        self._plan_from_worker = False
        # 设置 goal（供重规划使用）
        self.goalSet.emit(goal)
        # 显示目标任务 + 屏幕状态（可编辑）
        self._agent_panel.set_context(goal=goal, screen_state=screen_state, show=True)
        self._agent_panel.set_steps(steps)
        self._agent_panel.set_executing(False)
        # 切到代理面板
        self._force_set_mode("agent")
        self._stack.setCurrentWidget(self._agent_panel)
        self._ui_state = "awaiting_confirm"
        self._input.setPlaceholderText(f"编辑工作流「{label}」：点「修改」手动编辑，或在此输入提示让模型修改")
        # 延迟一帧定位主窗口到管理界面旁
        QTimer.singleShot(0, self._position_next_to_management)

    def _on_workflow_execute(self, wf_id: str) -> None:
        """直接执行工作流（跳过规划/确认）。"""
        wf = app_config.load_workflow(wf_id)
        if not wf:
            return
        steps = wf.get("steps", [])
        label = wf.get("label", "工作流")
        goal = wf.get("goal", label)
        self._goal_text = goal
        # 设置截图目录到工作流目录
        self._agent_panel.set_screenshots_dir(app_config.get_workflow_screenshots_dir(label))
        # 记录：本次执行是工作流，用工作流标签作为标题
        self._exec_kind = "工作流"
        self._exec_label = label
        self._executing_workflow_id = wf_id  # 执行完成后回存坐标
        self._chat_view.add_message(f"▶ 执行工作流「{label}」", "user")
        self._session["messages"].append({"role": "user", "content": f"▶ 执行工作流「{label}」"})
        # 重置步骤状态为 pending，清掉旧的状态标记和进度
        for s in steps:
            s["status"] = "pending"
            s["note"] = None
        # 同步到 session（_on_execution_started 会从 session 读步骤给 exec_overlay）
        self._session["steps"] = steps
        # 重置 AgentPanel 进度条 + 步骤标记
        self._agent_panel.set_steps(steps)
        self._agent_panel.update_progress(0, len(steps))
        # 切到代理面板（执行中会再切到 exec_overlay）
        self._force_set_mode("agent")
        self._stack.setCurrentWidget(self._agent_panel)
        self.workflowExecuted.emit(steps, goal)
        # 延迟一帧定位主窗口到管理界面旁
        QTimer.singleShot(0, self._position_next_to_management)

    def _on_workflow_execute_inline(self, steps: list, goal: str, replan_enabled=None) -> None:
        """直接执行内联工作流步骤（母工作流中的新建工作流，跳过规划/确认）。

        工作流块内的"执行"按钮触发时走后台执行：不显示代理面板/执行浮层，
        并依据块的「重规划」开关决定是否允许执行中自动调用规划模型重规划。
        """
        label = goal or "内联工作流"
        self._goal_text = goal
        # 记录：本次执行是内联工作流
        self._exec_kind = "工作流"
        self._exec_label = label
        self._executing_workflow_id = None  # 内联工作流不回存到文件
        # 不添加到聊天界面和历史记录
        # 重置步骤状态为 pending
        for s in steps:
            s["status"] = "pending"
            s["note"] = None
        # 同步到 session
        self._session["steps"] = steps
        # 工作流块"执行"按钮（replan_enabled 显式传入）：后台执行，无需代理面板。
        # 母工作流运行时调用的路径已先设置 _pending_master_callback，此处不覆盖。
        if self._pending_master_callback is None:
            self._pending_master_callback = lambda success, msg: None
        # 若块未开启重规划，则模板匹配失败/无法定位时直接结束执行，不调用规划模型
        if replan_enabled is not None:
            self.setReplanRequested.emit(replan_enabled)
        # 母工作流执行不需要显示代理面板，直接交给 worker 执行
        self.workflowExecuted.emit(steps, goal)

    def _on_script_edit(self, script_id: str) -> None:
        """编辑脚本：查看脚本代码详情。"""
        script = app_config.load_script(script_id)
        if not script:
            return
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QPlainTextEdit, QPushButton, QLabel
        dlg = QDialog(self)
        dlg.setWindowTitle(f"查看脚本 - {script.get('label', '')}")
        dlg.setMinimumSize(600, 400)
        dlg_lay = QVBoxLayout(dlg)
        info_lbl = QLabel(f"脚本名称：{script.get('label', '')}  |  语言：{script.get('language', 'python')}")
        info_lbl.setStyleSheet("font-weight:bold;color:#333;padding:4px;")
        dlg_lay.addWidget(info_lbl)
        code_edit = QPlainTextEdit()
        code_edit.setPlainText(script.get("code", ""))
        code_edit.setReadOnly(True)
        code_edit.setStyleSheet(
            "QPlainTextEdit{background:#1e1e2e;color:#cdd6f4;border:none;"
            "font-family:Consolas,'Courier New',monospace;font-size:13px;}"
        )
        dlg_lay.addWidget(code_edit, 1)
        btn_close = QPushButton("关闭")
        btn_close.setStyleSheet(
            "QPushButton{background:#95a5a6;color:white;border-radius:5px;padding:6px 14px;}"
        )
        btn_close.clicked.connect(dlg.accept)
        dlg_lay.addWidget(btn_close)
        dlg.exec()

    def _on_script_execute(self, script_id: str) -> None:
        """执行脚本库中的脚本。"""
        script = app_config.load_script(script_id)
        if not script:
            return
        code = script.get("code", "")
        if not code.strip():
            self._chat_view.add_message("【脚本为空】无法执行空脚本", "assistant")
            return
        label = script.get("label", "未命名")
        language = script.get("language", "python")
        self._chat_view.add_message(f"▶ 执行脚本「{label}」", "user")
        # 重用代码运行逻辑
        self._run_script_code(code, label, language)

    def _run_script_code(self, code_text: str, label: str = "", language: str = "python") -> None:
        """运行脚本代码。网页脚本用浏览器打开，Python 脚本用后台线程执行。"""
        if language == "web" or self._is_web_code(code_text):
            self._on_code_run_web(code_text)
            return
        import tempfile
        try:
            fd, path = tempfile.mkstemp(suffix=".py", prefix=f"script_{label}_")
            os.close(fd)
            with open(path, "w", encoding="utf-8") as f:
                f.write(code_text)
        except OSError as e:
            self._chat_view.add_message(f"【运行失败：无法写入】{e}", "assistant")
            return

        self._chat_view.add_message("【正在运行脚本】请稍候…", "assistant")

        import sys
        runner = CodeRunnerThread(sys.executable, path, os.path.dirname(path), self)
        runner.resultReady.connect(lambda stdout, stderr, rc: self._on_code_run_finished(
            stdout, stderr, rc, code_text, path))
        runner.start()

    def _new_conversation(self) -> None:
        self.stopRequested.emit()
        self._show_top_hint("已新建对话")
        # 新对话前：先 flush 工作流自动保存（必须在清空 _editing_workflow_id 之前）
        self._flush_autosave()
        self._save_session()
        self._session = self._new_session_obj()
        self._chat_view.clear_messages()
        self._agent_panel.clear_steps()
        self._agent_panel.set_context(show=False)  # 隐藏目标任务/屏幕状态
        self._current_stream_bubble = None
        self._current_code_bubble = None
        self._last_code_bubble = None
        self._pending_code_auto_fix_bubble = None
        self._current_thinking_bubble = None
        self._in_thinking_block = False
        self._thinking_shown_during_streaming = False
        self._stream_state = "normal"
        self._has_code_bubble = False
        self._parsed_up_to = 0
        self._code_start_pos = 0
        self._programming_buffer = ""
        self._ui_state = "idle"
        self._editing_workflow_id = None
        self._goal_text = ""
        self._reset_model_status()
        self._pro_console_token_lbl.setText("上下文 --")
        self.newConversationRequested.emit()
        self._apply_view_for_mode()

    def _save_session(self) -> None:
        if not self._session.get("messages"):
            return
        self._session["steps"] = self._session.get("steps", [])
        # 保存 Pro 编程 controller 的持久化 state（history + schedule + 控制变量）
        if self._mode == "programming":
            worker = getattr(self, '_worker', None)
            if worker is not None:
                pro_state = worker.getProState()
                if pro_state:
                    self._session["pro_state"] = pro_state
                else:
                    self._session.pop("pro_state", None)
        app_config.save_session(self._session)

    # ------------------------------------------------------------------
    # 全局停止热键（keyboard 库全局钩子）
    # ------------------------------------------------------------------
    def _register_hotkey_from_settings(self):
        """从设置读取停止快捷键并用 keyboard 库注册全局钩子。

        注册在后台线程进行（keyboard 首次初始化可能读取注册表较慢），
        回调触发 _HotkeySignal → 线程安全投递到主线程执行暂停。
        仅组合变化时才重新注册。
        """
        shortcut_str = self._settings.get("stop_shortcut", "Ctrl+.")
        hk = self._qt_shortcut_to_keyboard(shortcut_str)
        if hk == getattr(self, "_current_hk", None):
            return  # 组合未变化
        old = getattr(self, "_current_hk", None)
        self._current_hk = hk
        emit = self._hotkey_signal.triggered.emit
        threading.Thread(target=self._async_reg, args=(hk, old, emit), daemon=True).start()

    @staticmethod
    def _async_reg(hk: Optional[str], old: Optional[str], emit) -> None:
        """后台线程：移除旧热键，注册新热键。回调经 emit 线程安全投递到主线程。"""
        try:
            import keyboard
            if old:
                try:
                    keyboard.remove_hotkey(old)
                except Exception:
                    pass
            if hk:
                keyboard.add_hotkey(hk, emit)
                print(f"[热键] 全局热键注册 OK: keyboard '{hk}'")
        except BaseException as e:
            sys.stderr.write(f"[热键] keyboard 注册失败：{e}\n")

    @staticmethod
    def _qt_shortcut_to_keyboard(shortcut_str: str) -> Optional[str]:
        """把 Qt 快捷字符串（如 "Ctrl+."）转成 keyboard 库格式（如 "ctrl+."）。"""
        from PySide6.QtGui import QKeySequence
        ks = QKeySequence(shortcut_str)
        if ks.isEmpty():
            return None
        text = ks.toString(QKeySequence.SequenceFormat.PortableText)
        parts = text.split("+")
        mod_map = {"Ctrl": "ctrl", "Alt": "alt", "Shift": "shift", "Meta": "meta"}
        mods = [mod_map[p] for p in parts[:-1] if p in mod_map]
        key = (parts[-1] or "").strip()
        if not mods or not key:
            return None
        return "+".join(mods + [key])

    def _init_global_hotkey(self):
        """初始化全局热键（在 __init__ 最后调用）。"""
        if getattr(self, "_hotkey_signal", None) is None:
            self._hotkey_signal = _HotkeySignal(self)
            self._hotkey_signal.triggered.connect(self._on_stop_shortcut)
        self._current_hk = None
        self._register_hotkey_from_settings()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._video_bg is not None:
            self._video_bg.stop()
            self._video_bg = None
        # 移除停止热键
        hk = getattr(self, "_current_hk", None)
        if hk:
            try:
                import keyboard
                keyboard.remove_hotkey(hk)
            except Exception:
                pass
            self._current_hk = None
        # 关闭窗口前：flush 工作流编辑 + 保存会话
        self._flush_autosave()
        self._save_session()
        self._purge_cache()  # 清理未发送的缓存附件，避免残留占用磁盘
        self.stopRequested.emit()
        super().closeEvent(event)

    def hideEvent(self, event) -> None:
        # 窗口隐藏时暂停视频解码以节省 CPU
        if self._video_bg is not None:
            self._video_bg.pause()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        # 窗口重新显示时恢复视频
        if self._video_bg is not None:
            self._video_bg.resume()
        # 窗口重新显示时重新注册全局热键，避免隐藏/恢复过程中句柄变化导致失效
        try:
            self._register_hotkey_from_settings()
        except Exception:
            pass
        super().showEvent(event)

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------
    @staticmethod
    def _btn_style(color: str, pad: bool = False) -> str:
        p = "padding:6px 12px;" if pad else "padding:5px 8px;"
        # hover 用同色系亮一档（Qt CSS 不支持 filter，改用 background 覆盖）
        hover = _lighten_hex(color, 0.12)
        return (
            f"QPushButton,QToolButton{{background:{color};color:white;border:none;"
            f"border-radius:5px;{p}font-weight:bold;}}"
            f"QPushButton:hover,QToolButton:hover{{background:{hover};}}"
            "QPushButton:pressed,QToolButton:pressed{padding-top:6px;padding-left:9px;}"
        )
