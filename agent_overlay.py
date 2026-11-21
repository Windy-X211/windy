# -*- coding: utf-8 -*-
"""自主代理模式右下角思考过程浮窗。

- 屏幕右下角半透明浮窗，显示模型实时推理过程
- 使用 WDA_EXCLUDEFROMCAPTURE 彻底避免被截图捕获
- 解析模型输出，结构化展示思考、动作、结果
- 底部保留原始流式输出供查看详情
"""

from __future__ import annotations

import ctypes
import json
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QFont, QTextCursor, QPixmap, QTextCharFormat
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QFrame, QScrollArea,
)

# Win32
user32 = ctypes.windll.user32
WDA_EXCLUDEFROMCAPTURE = 0x00000011
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1
HTCLIENT = 1
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
SetWindowDisplayAffinity = user32.SetWindowDisplayAffinity
SetWindowDisplayAffinity.restype = ctypes.c_bool
SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]


def _exclude_from_capture(hwnd: int) -> None:
    if not hwnd:
        return
    try:
        root = user32.GetAncestor(hwnd, 2)  # GA_ROOT
        if root:
            hwnd = root
        SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except Exception:
        pass


def _enable_clickthrough(hwnd: int) -> None:
    """添加 WS_EX_TRANSPARENT 样式，使窗口 OS 级鼠标穿透。"""
    if not hwnd:
        return
    try:
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_TRANSPARENT)
    except Exception:
        pass


class AgentOverlay(QWidget):
    """右下角思考过程浮窗。"""

    WIDTH = 460
    HEIGHT = 300
    MARGIN = 16
    THUMB_WIDTH = 160
    THUMB_HEIGHT = 90

    def __init__(self, parent=None):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(self.WIDTH, self.HEIGHT)

        self._raw_buffer = ""  # 累积原始输出
        self._last_action = ""  # 最后识别的动作名
        self._last_reasoning = ""  # 最后识别的思考
        self._last_result = ""  # 最后执行结果
        self._stop_shortcut_str = "Ctrl+."  # 当前停止快捷键（用于按钮标签显示）

        # 流式解析状态
        self._reasoning_parse_pos = -1  # -1=未开始，>=0=当前解析位置
        self._reasoning_value_start = -1  # reasoning 值在 buffer 中的起始位置
        self._parsed_up_to = 0  # 已解析到的 buffer 位置（用于跳过已处理的 JSON）

        self._build_ui()

        QTimer.singleShot(100, self._apply_win32_setup)
        self._position_bottom_right()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _make_label(self, text: str, color: str, size: int = 10, bold: bool = False) -> QLabel:
        lbl = QLabel(text)
        weight = "bold" if bold else "normal"
        lbl.setStyleSheet(f"color:{color};font-size:{size}px;font-weight:{weight};")
        return lbl

    def _make_sep(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:rgba(255,255,255,25);")
        sep.setFixedHeight(1)
        return sep

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        container = QFrame()
        container.setStyleSheet(
            "QFrame{background:rgba(30,30,30,200);border-radius:8px;"
            "border:1px solid rgba(255,255,255,25);}"
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        # === 标题栏 ===
        title_row = QHBoxLayout()
        title_label = QLabel("🤖 自主代理")
        title_label.setStyleSheet("color:#8e44ad;font-weight:bold;font-size:11px;")
        title_row.addWidget(title_label)
        title_row.addStretch(1)
        self._stop_btn = QPushButton(f"■ 停止 ({self._stop_shortcut_str})")
        self._stop_btn.setFixedHeight(20)
        self._stop_btn.setStyleSheet(
            "QPushButton{background:#e74c3c;color:white;border:none;border-radius:3px;"
            "padding:1px 8px;font-size:10px;font-weight:bold;}"
            "QPushButton:hover{background:#c0392b;}"
        )
        title_row.addWidget(self._stop_btn)
        layout.addLayout(title_row)

        # === 目标 ===
        self._goal_label = QLabel("")
        self._goal_label.setStyleSheet("color:#bbb;font-size:10px;")
        self._goal_label.setWordWrap(True)
        self._goal_label.setMaximumHeight(28)
        layout.addWidget(self._goal_label)

        layout.addWidget(self._make_sep())

        # === 主内容：截图 + 结构化信息 ===
        content_row = QHBoxLayout()
        content_row.setSpacing(6)
        content_row.setContentsMargins(0, 2, 0, 0)

        # 左侧：截图
        self._thumb_label = QLabel()
        self._thumb_label.setFixedSize(self.THUMB_WIDTH, self.THUMB_HEIGHT)
        self._thumb_label.setStyleSheet(
            "color:#666;font-size:9px;background:rgba(0,0,0,80);"
            "border:1px solid rgba(255,255,255,30);border-radius:3px;"
        )
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setText("等待截图...")
        content_row.addWidget(self._thumb_label)

        # 右侧：结构化信息卡片
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        info_layout.setContentsMargins(0, 0, 0, 0)

        # 思考
        thinking_row = QHBoxLayout()
        thinking_row.setSpacing(2)
        thinking_label = self._make_label("💭", "#f39c12", 10)
        thinking_row.addWidget(thinking_label)
        self._reasoning_label = QLabel("等待模型输出...")
        self._reasoning_label.setStyleSheet("color:#ddd;font-size:10px;")
        self._reasoning_label.setWordWrap(True)
        self._reasoning_label.setMaximumHeight(40)
        thinking_row.addWidget(self._reasoning_label, 1)
        info_layout.addLayout(thinking_row)

        # 动作
        action_row = QHBoxLayout()
        action_row.setSpacing(2)
        action_label = self._make_label("🛠", "#3498db", 10)
        action_row.addWidget(action_label)
        self._action_label = QLabel("—")
        self._action_label.setStyleSheet("color:#3498db;font-size:10px;font-weight:bold;")
        action_row.addWidget(self._action_label)
        action_row.addStretch(1)
        info_layout.addLayout(action_row)

        # 结果
        result_row = QHBoxLayout()
        result_row.setSpacing(2)
        result_label = self._make_label("✅", "#27ae60", 10)
        result_row.addWidget(result_label)
        self._result_label = QLabel("")
        self._result_label.setStyleSheet("color:#27ae60;font-size:10px;")
        self._result_label.setWordWrap(True)
        self._result_label.setMaximumHeight(28)
        result_row.addWidget(self._result_label, 1)
        info_layout.addLayout(result_row)

        info_layout.addStretch(1)
        content_row.addLayout(info_layout, 1)
        layout.addLayout(content_row, 1)

        # === 原始输出（可折叠促狭区）===
        layout.addWidget(self._make_sep())
        raw_header = QHBoxLayout()
        raw_header.setSpacing(2)
        raw_icon = self._make_label("📄", "#7f8c8d", 9)
        raw_header.addWidget(raw_icon)
        raw_title = self._make_label("原始输出", "#7f8c8d", 9)
        raw_header.addWidget(raw_title)
        raw_header.addStretch(1)
        layout.addLayout(raw_header)

        self._raw_output = QPlainTextEdit()
        self._raw_output.setReadOnly(True)
        self._raw_output.setFixedHeight(44)
        self._raw_output.setStyleSheet(
            "QPlainTextEdit{background:rgba(0,0,0,80);color:#999;border:none;"
            "font-size:9px;font-family:Consolas,monospace;padding:1px;}"
        )
        self._raw_output.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self._raw_output)

        # === 底部状态 ===
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color:#7f8c8d;font-size:9px;")
        status_row.addWidget(self._status_label, 1)
        self._task_time_label = QLabel("")
        self._task_time_label.setStyleSheet("color:#e67e22;font-size:9px;font-weight:bold;")
        status_row.addWidget(self._task_time_label)
        self._model_time_label = QLabel("")
        self._model_time_label.setStyleSheet("color:#9b59b6;font-size:9px;font-weight:bold;")
        status_row.addWidget(self._model_time_label)
        layout.addLayout(status_row)

        root.addWidget(container)

    # ------------------------------------------------------------------
    # 窗口位置 & Win32 设置
    # ------------------------------------------------------------------

    def _apply_win32_setup(self) -> None:
        """延迟执行 Win32 窗口设置（排除截图 + 鼠标穿透）。"""
        hwnd = int(self.winId())
        if not hwnd:
            return
        _exclude_from_capture(hwnd)
        _enable_clickthrough(hwnd)

    def _position_bottom_right(self) -> None:
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.right() - self.WIDTH - self.MARGIN
        y = geo.bottom() - self.HEIGHT - self.MARGIN
        self.move(x, y)

    # ------------------------------------------------------------------
    # nativeEvent：处理 WM_NCHITTEST，仅停止按钮区域可点击
    # ------------------------------------------------------------------

    def nativeEvent(self, eventType, message):
        """拦截 WM_NCHITTEST，停止按钮外区域返回 HTTRANSPARENT 实现穿透。"""
        if eventType == b"windows_generic_MSG":
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == WM_NCHITTEST:
                    x = ctypes.c_short(msg.lParam & 0xFFFF).value
                    y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                    local_pos = self.mapFromGlobal(QPoint(x, y))
                    if self._stop_btn.geometry().contains(local_pos):
                        return True, HTCLIENT
                    return True, HTTRANSPARENT
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def set_goal(self, goal: str) -> None:
        self._goal_label.setText(f"🎯 {goal}")

    def update_thinking(self, chunk: str) -> None:
        """追加一段原始输出，流式解析思考内容，更新结构化信息。"""
        self._raw_buffer += chunk

        # 追加到原始输出区
        cursor = self._raw_output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(chunk)
        sb = self._raw_output.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

        # 流式提取 reasoning 值（实时更新，不等完整 JSON）
        self._update_reasoning_stream()

        # 尝试解析完整 JSON 更新结构化信息
        self._try_parse_structured()

    def _update_reasoning_stream(self) -> None:
        """从缓冲区中流式提取 reasoning 值并实时更新显示。"""
        text = self._raw_buffer
        if not text:
            return

        # 尚未找到 "reasoning" 字段 → 从 _parsed_up_to 开始搜索
        if self._reasoning_parse_pos < 0:
            idx = text.find('"reasoning"', self._parsed_up_to)
            if idx < 0:
                return
            idx += len('"reasoning"')
            # 跳过空白和冒号
            while idx < len(text) and text[idx] in ' \t\n\r:':
                idx += 1
            if idx >= len(text) or text[idx] != '"':
                return
            idx += 1  # 跳过开头的引号
            self._reasoning_parse_pos = idx
            self._reasoning_value_start = idx

        # 从上次位置继续扫描 reasoning 值，直到遇到未转义的结束引号
        pos = self._reasoning_parse_pos
        escaped = False
        while pos < len(text):
            ch = text[pos]
            if escaped:
                escaped = False
                pos += 1
                continue
            if ch == '\\':
                escaped = True
                pos += 1
                continue
            if ch == '"':
                # 结束引号，提取完整值
                value = text[self._reasoning_value_start:pos]
                value = value.replace('\\"', '"').replace('\\\\', '\\')
                self._last_reasoning = value
                self._reasoning_label.setText(value)
                # 更新已解析位置到结束引号之后
                self._parsed_up_to = pos + 1
                self._reasoning_parse_pos = -1
                self._reasoning_value_start = -1
                return
            pos += 1

        # 未找到结束引号，显示当前累积值
        self._reasoning_parse_pos = pos
        value = text[self._reasoning_value_start:pos]
        value = value.replace('\\"', '"').replace('\\\\', '\\')
        self._last_reasoning = value
        self._reasoning_label.setText(value)

    def _try_parse_structured(self) -> None:
        """从累积输出中尝试解析完整 JSON 动作并更新结构化显示。"""
        text = self._raw_buffer
        # 查找 "action": 的位置，然后向前找到最近的 { 开始解析
        idx = text.find('"action"')
        if idx < 0:
            return
        # 向前找到 JSON 对象的起始位置（最近的 { 在 idx 之前）
        start = -1
        for i in range(idx, -1, -1):
            if text[i] == '{':
                start = i
                break
        if start < 0:
            return
        # 尝试找到闭合括号
        depth = 0
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end <= start:
            return
        snippet = text[start:end]
        try:
            obj = json.loads(snippet)
        except json.JSONDecodeError:
            return
        
        action = obj.get("action", "")
        reasoning = obj.get("reasoning", "")
        params = obj.get("params", {})
        result = obj.get("result", "")

        # 更新结构化显示（reasoning 已在流式中更新，这里只更新 action/result）
        if action:
            self._last_action = action
            if action == "完成":
                self._action_label.setText("🎉 完成")
                self._action_label.setStyleSheet("color:#27ae60;font-size:10px;font-weight:bold;")
            elif action == "失败":
                self._action_label.setText("❌ 失败")
                self._action_label.setStyleSheet("color:#e74c3c;font-size:10px;font-weight:bold;")
            else:
                param_str = self._format_params(params)
                self._action_label.setText(f"{action} {param_str}")
                self._action_label.setStyleSheet("color:#3498db;font-size:10px;font-weight:bold;")
        if result:
            self._last_result = result
            self._result_label.setText(result[:50])

    def _format_params(self, params: dict) -> str:
        """格式化参数为简短字符串。"""
        if not params:
            return ""
        parts = []
        if "x" in params and "y" in params:
            parts.append(f"({params['x']:.2f}, {params['y']:.2f})")
        if "text" in params:
            t = params["text"]
            parts.append(f"「{t[:20]}」")
        if "keys" in params:
            parts.append(params["keys"])
        if "command" in params:
            parts.append(params["command"][:20])
        if "code" in params:
            parts.append("code...")
        if "direction" in params:
            parts.append(params["direction"])
        if "seconds" in params:
            parts.append(f"{params['seconds']}s")
        if "width" in params and "height" in params:
            if "x" not in params:
                parts.append(f"({params.get('x',0):.2f}, {params.get('y',0):.2f})")
            parts.append(f"[{params['width']:.2f}×{params['height']:.2f}]")
        return " ".join(parts)

    def set_action_result(self, action: str, params: dict, result: str) -> None:
        """由外部直接设置动作结果（避免解析依赖）。"""
        self._last_action = action
        self._last_result = result

        if action == "完成":
            self._action_label.setText("🎉 完成")
            self._action_label.setStyleSheet("color:#27ae60;font-size:10px;font-weight:bold;")
        elif action == "失败":
            self._action_label.setText("❌ 失败")
            self._action_label.setStyleSheet("color:#e74c3c;font-size:10px;font-weight:bold;")
        else:
            param_str = self._format_params(params)
            self._action_label.setText(f"{action} {param_str}")
            self._action_label.setStyleSheet("color:#3498db;font-size:10px;font-weight:bold;")

        if result:
            self._result_label.setText(result[:50])

    def clear(self) -> None:
        self._raw_buffer = ""
        self._last_action = ""
        self._last_reasoning = ""
        self._last_result = ""
        self._reasoning_parse_pos = -1
        self._reasoning_value_start = -1
        self._parsed_up_to = 0
        self._raw_output.clear()
        self._goal_label.setText("")
        self._status_label.setText("")
        self._task_time_label.setText("")
        self._model_time_label.setText("")
        self._reasoning_label.setText("等待模型输出...")
        self._action_label.setText("—")
        self._action_label.setStyleSheet("color:#3498db;font-size:10px;font-weight:bold;")
        self._result_label.setText("")
        self._thumb_label.clear()
        self._thumb_label.setText("等待截图...")

    def set_screenshot(self, png_bytes: bytes) -> None:
        from PIL import Image
        import io
        pil_img = Image.open(io.BytesIO(png_bytes))
        w, h = pil_img.size
        scale = min(self.THUMB_WIDTH / w, self.THUMB_HEIGHT / h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        pil_img = pil_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        data = io.BytesIO()
        pil_img.save(data, format="PNG")
        data.seek(0)
        qpix = QPixmap()
        qpix.loadFromData(data.getvalue())
        self._thumb_label.setPixmap(qpix)
        self._thumb_label.setText("")

    def set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def set_model_time(self, seconds: float) -> None:
        """实时显示单次模型调用耗时（秒，保留一位小数）。"""
        self._model_time_label.setText(f"⏱ {seconds:.1f}s")

    def set_task_time(self, seconds: int) -> None:
        """实时显示任务总用时（整数秒）。"""
        self._task_time_label.setText(f"任务 {seconds}s")

    def set_stop_shortcut(self, shortcut: str) -> None:
        """更新停止按钮上显示的快捷键文本（设置变更后同步）。"""
        if not shortcut:
            shortcut = "Ctrl+."
        self._stop_shortcut_str = shortcut
        self._stop_btn.setText(f"■ 停止 ({shortcut})")

    def set_stop_callback(self, callback) -> None:
        self._stop_btn.clicked.connect(callback)