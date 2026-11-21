# -*- coding: utf-8 -*-
"""代理面板：两列表格式执行步骤（可编辑）+ 确认/修改按钮 + 进度文本与进度条。

- 第一列：操作指令下拉框（单击/双击/右键/上滑/下滑/拖动/输入/长输入/按键/等待/咨询用户）
- 第二列：参数编辑器，右键可添加输入变量 / 上下插入空行 / 删除
"""

from __future__ import annotations

import io
import os
from typing import List, Optional, Dict

from PIL import Image

from PySide6.QtCore import Qt, Signal, QPoint, QEvent
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QPushButton, QProgressBar, QScrollArea, QFrame, QSizePolicy,
    QMenu, QComboBox, QHeaderView, QDialog,
)

import ctypes

import app_config
import capture
import manual_positioner
from step_model import (
    parse_steps, step_summary, step_display_summary, serialize_step,
    OP_LONG_INPUT, OP_CLICK, OP_DBLCLICK, OP_RIGHT, OP_SCROLL_UP,
    OP_SCROLL_DOWN, OP_DRAG, OP_INPUT, OP_KEY, OP_WAIT, OP_CONSULT,
    OPS_NEED_VISION,
)


# 指令 → 参数说明提示（placeholder）
_OP_PARAM_HINT: Dict[str, str] = {
    OP_CLICK:       "位置-目标元素，如 桌面-微信图标",
    OP_DBLCLICK:    "位置-目标元素，如 桌面-微信图标",
    OP_RIGHT:       "位置-目标元素，如 任务栏-文件夹",
    OP_SCROLL_UP:   "位置-目标元素，如 浏览器-滚动区（可选）",
    OP_SCROLL_DOWN: "位置-目标元素，如 浏览器-滚动区（可选）",
    OP_DRAG:        "起点位置-起点元素-到-终点位置-终点元素",
    OP_INPUT:       "直接填写的短文本",
    OP_LONG_INPUT:  "主题或要求（执行时由写作模型生成正文）",
    OP_KEY:         "按键序列，如 ctrl+c 或 enter",
    OP_WAIT:        "等待秒数，如 3 或 0.5",
    OP_CONSULT:     "咨询用户的问题说明",
}

# 下拉框显示的全部指令顺序
_ALL_OP_ORDER = [
    OP_CLICK, OP_DBLCLICK, OP_RIGHT, OP_SCROLL_UP, OP_SCROLL_DOWN,
    OP_DRAG, OP_INPUT, OP_LONG_INPUT, OP_KEY, OP_WAIT, OP_CONSULT,
]


def _op_and_param_from_step(step: dict) -> tuple:
    """从 step dict 还原 (op, param_text) 两列显示值。

    param_text 为 serialize_step 去掉 op- 前缀的剩余部分：
    如 单击-桌面-微信图标 → (单击, 桌面-微信图标)
    如 长输入-{输入1}     → (长输入, {输入1})
    """
    op = step.get("op", OP_CLICK)
    raw = step.get("raw") or serialize_step(step)
    # raw 形如 "op-xxx-yyy"，去掉 "op-" 前缀
    prefix = f"{op}-"
    if raw.startswith(prefix):
        param = raw[len(prefix):]
    else:
        # 回退：基于字段拼接（只做基本显示，不涉及解析精度）
        if op in (OP_CLICK, OP_DBLCLICK, OP_RIGHT, OP_SCROLL_UP, OP_SCROLL_DOWN):
            loc = step.get("location", "")
            tgt = step.get("target", "")
            param = f"{loc}-{tgt}" if loc else tgt
        elif op == OP_DRAG:
            param = (f"{step.get('start_location', '')}-{step.get('start_target', '')}"
                     f"-到-{step.get('end_location', '')}-{step.get('end_target', '')}")
        elif op == OP_INPUT:
            param = step.get("text") or ""
        elif op == OP_LONG_INPUT:
            param = step.get("description") or ""
        elif op == OP_KEY:
            param = step.get("key") or ""
        elif op == OP_WAIT:
            param = str(step.get("seconds", 1))
        elif op == OP_CONSULT:
            param = step.get("question") or ""
        else:
            param = raw
    return op, param


class StepRow(QFrame):
    """表格单行：状态点 + [指令下拉框 | 参数编辑器 + 变量按钮]。"""

    # 右键菜单信号
    insertAboveRequested = Signal()
    insertBelowRequested = Signal()
    deleteRequested = Signal()
    # 变量相关信号
    variableUpdated = Signal()
    convertToVarRequested = Signal()
    insertVarAboveRequested = Signal()
    insertVarBelowRequested = Signal()
    # 指令或参数变更（供外部自动保存/感知变更）
    rowChanged = Signal()

    # 下拉框样式
    _STYLE_COMBO = (
        "QComboBox{background:rgba(255,255,255,200);border:1px solid #ccc;"
        "border-radius:4px;padding:3px 6px;min-width:86px;}"
        "QComboBox:hover{border:1px solid #3498db;}"
        "QComboBox QAbstractItemView{background:white;border:1px solid #ccc;"
        "selection-background:#3498db;selection-color:white;}"
    )
    _STYLE_COMBO_HL = (
        "QComboBox{background:#fffbe6;border:2px solid #e67e22;"
        "border-radius:5px;padding:2px 5px;min-width:86px;}"
        "QComboBox QAbstractItemView{background:white;border:1px solid #ccc;"
        "selection-background:#3498db;selection-color:white;}"
    )

    # 编辑态样式
    _STYLE_NORMAL = (
        "QLineEdit{background:rgba(255,255,255,200);border:1px solid #ccc;"
        "border-radius:4px;padding:3px 6px;}"
    )
    _STYLE_HIGHLIGHT = (
        "QLineEdit{background:#fffbe6;border:2px solid #e67e22;"
        "border-radius:5px;padding:2px 5px;}"
        "QLineEdit:focus{border:2px solid #d35400;background:#fff8e0;}"
    )
    _STYLE_VAR_BTN = (
        "QPushButton{background:#e74c3c;color:white;border-radius:4px;"
        "padding:2px 6px;font-size:11px;font-weight:bold;}"
        "QPushButton:hover{background:#ff5c41;}"
    )
    _STYLE_LONG_BTN = (
        "QPushButton{background:#8e44ad;color:white;border-radius:4px;"
        "padding:2px 6px;font-size:11px;font-weight:bold;}"
        "QPushButton:hover{background:#a569bd;}"
    )
    _STYLE_TPL_NORMAL = (
        "QPushButton{background:#7f8c8d;color:white;border-radius:4px;"
        "padding:2px 6px;font-size:10px;font-weight:bold;min-width:28px;max-width:28px;}"
        "QPushButton:hover{background:#95a5a6;}"
    )
    _STYLE_TPL_HIGHLIGHT = (
        "QPushButton{background:#27ae60;color:white;border:2px solid #1e8449;"
        "border-radius:4px;padding:1px 5px;font-size:10px;font-weight:bold;"
        "min-width:28px;max-width:28px;}"
        "QPushButton:hover{background:#2ecc71;}"
    )
    _MENU_STYLE = (
        "QMenu{background:white;color:black;border:1px solid #ccc;"
        "padding:4px;margin:0;}"
        "QMenu::item{padding:6px 24px;background:white;color:black;}"
        "QMenu::item:selected{background:#3498db;color:white;}"
        "QMenu::separator{height:1px;background:#ddd;margin:2px 6px;}"
    )

    def __init__(self, index: int, op: str, param: str, step: dict = None, parent=None,
                 screenshots_dir: Optional[str] = None):
        super().__init__(parent)
        # QToolTip 白底黑字：StepRow 自身设有 stylesheet 时可能阻断全局
        # QToolTip 规则继承，需在此再声明一次确保子控件 tooltip 可读。
        self.setStyleSheet(
            "StepRow{background:transparent;}"
            "QToolTip{background-color:#ffffff;color:#000000;"
            "border:1px solid #b0b0b0;border-radius:4px;padding:4px 8px;}"
        )
        self._step = step or {}
        self._var_values = {}  # var_name -> full_text（仅用于变量显示）
        self._index = index
        self._highlighted = False
        self._readonly = False
        self._user_skip = False  # 用户手动标记为跳过（✓=跳过，○=执行）
        self._screenshots_dir = screenshots_dir or app_config.templates_fallback_dir()

        h = QHBoxLayout(self)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(4)

        # 状态标识：可点击切换 ✓（跳过）/ ○（执行）
        self._mark = QLabel("○")
        self._mark.setFixedWidth(20)
        self._mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mark.setStyleSheet("color:#B0B0B0;font-weight:bold;")
        self._mark.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mark.mousePressEvent = lambda e: self._on_mark_clicked(e)
        h.addWidget(self._mark)

        # 第一列：指令下拉框
        self._combo = QComboBox()
        self._combo.wheelEvent = lambda e: None
        self._combo.addItems(_ALL_OP_ORDER)
        if op in _ALL_OP_ORDER:
            self._combo.setCurrentText(op)
        else:
            # 未知指令：插入并选中，保证一致性
            self._combo.addItem(op)
            self._combo.setCurrentText(op)
        self._combo.setStyleSheet(self._STYLE_COMBO)
        self._combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._combo.currentTextChanged.connect(self._on_op_changed)
        h.addWidget(self._combo)

        # 第二列：参数编辑器
        self._edit = QLineEdit(param)
        self._edit.setStyleSheet(self._STYLE_NORMAL)
        self._edit.setMinimumWidth(200)
        self._update_param_placeholder()
        self._edit.textChanged.connect(lambda: self.rowChanged.emit())
        h.addWidget(self._edit, 1)

        # 坐标显示区（仅对需要视觉定位的操作显示）
        self._coord_edit: Optional[QLineEdit] = None
        self._coord_edit2: Optional[QLineEdit] = None  # 拖动用第二个坐标
        # 模板按钮
        self._template_btn: Optional[QPushButton] = None
        self._template_path: str = ""
        self._template_highlighted: bool = False
        self._refresh_coord_display()

        # 变量按钮 / 长输入按钮
        self._var_btn: Optional[QPushButton] = None
        self._collect_var_values()
        self._refresh_var_button()

        # 记录初始值（用于判断修改）
        self._original_op = self._combo.currentText()
        self._original_param = param

        # 右键菜单：整行 + 参数编辑器 + 状态点 + 下拉框（下拉框默认会吞右键事件）
        for w in (self, self._edit, self._mark, self._combo):
            w.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            w.customContextMenuRequested.connect(self._on_context_menu)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _update_param_placeholder(self) -> None:
        hint = _OP_PARAM_HINT.get(self._combo.currentText(), "步骤参数（如 需要定位的元素）")
        self._edit.setPlaceholderText(hint)
        self._edit.setToolTip(hint)

    def _on_op_changed(self, _new_op: str) -> None:
        self._update_param_placeholder()
        if self._step:
            new_op = self._combo.currentText()
            old_op = self._step.get("op", "")
            self._step["op"] = new_op
            # 同步 raw 字段，确保与当前 op 和参数一致
            param_text = self._edit.text().strip()
            self._step["raw"] = f"{new_op}-{param_text}" if param_text else new_op
            # 根据目标操作类型添加/清理默认字段
            from step_model import OP_WAIT
            if new_op == OP_WAIT:
                # 确保等待步骤有 seconds 字段
                if "seconds" not in self._step or self._step["seconds"] is None:
                    from step_model import _parse_seconds
                    self._step["seconds"] = _parse_seconds(param_text)
            else:
                # 切换到非等待操作时清理无关字段
                self._step.pop("seconds", None)
            # 清理旧操作类型的特有字段
            if old_op != new_op:
                for _k in ("location", "target", "text", "description", "desc_full",
                           "key", "question", "start_location", "start_target",
                           "end_location", "end_target", "template_path",
                           "template_highlighted", "template_size", "template_threshold"):
                    if _k in self._step:
                        self._step[_k] = None
        self._refresh_coord_display()
        self.rowChanged.emit()

    # ------------------------------------------------------------------
    # 坐标/模板共用按钮
    # ------------------------------------------------------------------
    _BTN_STYLE_NORMAL = (
        "QPushButton{background:rgba(240,240,240,180);border:1px solid #ccc;"
        "border-radius:3px;padding:2px 4px;font-size:10px;color:#666;"
        "min-width:80px;max-width:95px;}"
        "QPushButton:hover{background:rgba(200,200,200,200);}"
    )
    _BTN_STYLE_HIGHLIGHT = (
        "QPushButton{background:rgba(46,204,113,60);border:2px solid #27ae60;"
        "border-radius:3px;padding:1px 3px;font-size:10px;color:#1a6b38;"
        "font-weight:bold;min-width:80px;max-width:95px;}"
        "QPushButton:hover{background:rgba(46,204,113,100);}"
    )

    def _fmt_coord(self, coord) -> str:
        """格式化坐标为显示字符串。"""
        if coord is None or not isinstance(coord, (tuple, list)) or len(coord) != 2:
            return ""
        try:
            return f"{float(coord[0]):.4f}, {float(coord[1]):.4f}"
        except (TypeError, ValueError):
            return ""

    def _refresh_coord_display(self) -> None:
        """根据当前操作类型，显示/隐藏坐标/模板共用按钮。"""
        # 清理旧按钮
        for attr in ("_coord_tpl_btn", "_coord_tpl_btn2"):
            btn = getattr(self, attr, None)
            if btn is not None:
                layout = btn.parentWidget().layout()
                if layout:
                    layout.removeWidget(btn)
                btn.deleteLater()
                setattr(self, attr, None)

        op = self._combo.currentText()
        if op not in OPS_NEED_VISION:
            return

        self._template_path = self._step.get("template_path", "") if self._step else ""
        self._template_size = self._step.get("template_size", 50) if self._step else 50
        self._template_threshold = self._step.get("template_threshold", 0.75) if self._step else 0.75

        if op == OP_DRAG:
            start_coord = self._step.get("start_coord") if self._step else None
            end_coord = self._step.get("end_coord") if self._step else None
            self._coord_tpl_btn = self._make_coord_tpl_btn(start_coord, "起点")
            self._coord_tpl_btn2 = self._make_coord_tpl_btn(end_coord, "终点")
            self.layout().addWidget(self._coord_tpl_btn)
            self.layout().addWidget(self._coord_tpl_btn2)
        else:
            coord = self._step.get("coord") if self._step else None
            self._coord_tpl_btn = self._make_coord_tpl_btn(coord, "坐标")
            self.layout().addWidget(self._coord_tpl_btn)

    def _make_coord_tpl_btn(self, coord, label: str) -> QPushButton:
        """创建坐标/模板共用按钮。"""
        btn = QPushButton()
        btn.setFixedHeight(22)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda pos, b=btn: self._on_btn_context_menu(b, pos))
        btn.clicked.connect(lambda: self._toggle_btn_highlight(btn))
        self._update_btn_display(btn, coord, label)
        return btn

    def _update_btn_display(self, btn: QPushButton, coord, label: str = "") -> None:
        """更新按钮的显示文本和样式。"""
        op = self._combo.currentText()
        if op not in OPS_NEED_VISION:
            return

        # 判断当前模式：有模板文件且step标记为模板模式
        has_template = bool(self._template_path) and os.path.isfile(self._template_path)
        tpl_hl = self._step.get("template_highlighted", False) if self._step else False
        coord_locked = self._step.get("coord_locked", False) if self._step else False

        # 模板模式：有模板文件且模板高亮激活
        if has_template and tpl_hl:
            btn.setText("📷")
            btn.setToolTip(f"模板模式·{os.path.basename(self._template_path)}\n单击取消高亮 | 右键菜单切换")
            btn.setStyleSheet(self._BTN_STYLE_HIGHLIGHT)
        elif coord_locked:
            btn.setText(self._fmt_coord(coord) if coord else "")
            btn.setToolTip(f"坐标锁定·{self._fmt_coord(coord) if coord else ''}\n单击取消高亮 | 右键菜单编辑")
            btn.setStyleSheet(self._BTN_STYLE_HIGHLIGHT)
        else:
            # 未高亮：显示坐标值（普通模式）
            btn.setText(self._fmt_coord(coord) if coord else "")
            btn.setToolTip(f"单击高亮锁定 | 右键菜单编辑坐标/转为模板")
            btn.setStyleSheet(self._BTN_STYLE_NORMAL)

    def _toggle_btn_highlight(self, btn: QPushButton) -> None:
        """单击切换按钮高亮状态。"""
        if self._step is None:
            return
        op = self._combo.currentText()
        if op not in OPS_NEED_VISION:
            return

        has_template = bool(self._template_path) and os.path.isfile(self._template_path)
        tpl_hl = self._step.get("template_highlighted", False) if self._step else False
        coord_locked = self._step.get("coord_locked", False) if self._step else False

        if has_template and tpl_hl:
            # 模板高亮 → 取消高亮
            self._step["template_highlighted"] = False
            self._step["coord_locked"] = False
        elif coord_locked:
            # 坐标锁定 → 取消高亮
            self._step["coord_locked"] = False
        else:
            # 未高亮 → 激活坐标锁定（高亮锁定坐标）
            coord = self._get_btn_coord(btn)
            if coord is not None:
                self._step["coord_locked"] = True
                if self._step.get("template_highlighted"):
                    self._step["template_highlighted"] = False

        self._refresh_coord_display()
        self.rowChanged.emit()

    def _get_btn_coord(self, btn: QPushButton):
        """获取按钮对应的坐标值。"""
        op = self._combo.currentText()
        if op not in OPS_NEED_VISION or self._step is None:
            return None
        if op == OP_DRAG:
            if btn is self._coord_tpl_btn:
                return self._step.get("start_coord")
            elif btn is self._coord_tpl_btn2:
                return self._step.get("end_coord")
        else:
            return self._step.get("coord")
        return None

    def _set_btn_coord(self, btn: QPushButton, x: float, y: float) -> None:
        """设置按钮对应的坐标值到 step。"""
        if self._step is None:
            return
        op = self._combo.currentText()
        if op == OP_DRAG:
            if btn is self._coord_tpl_btn:
                self._step["start_coord"] = (x, y)
            elif btn is self._coord_tpl_btn2:
                self._step["end_coord"] = (x, y)
        else:
            self._step["coord"] = (x, y)

    # ------------------------------------------------------------------
    # 右键菜单
    # ------------------------------------------------------------------
    def _on_btn_context_menu(self, btn: QPushButton, pos: QPoint) -> None:
        """坐标/模板按钮右键菜单。"""
        has_template = bool(self._template_path) and os.path.isfile(self._template_path)
        tpl_hl = self._step.get("template_highlighted", False) if self._step else False

        menu = QMenu(self)
        menu.setStyleSheet(self._MENU_STYLE)

        if has_template and tpl_hl:
            # 模板模式
            act_settings = menu.addAction("⚙ 模板设置")
            menu.addSeparator()
            act_switch = menu.addAction("📐 切换为坐标模式")
            menu.addSeparator()
            act_resave = menu.addAction("🔄 重新保存模板")
            action = menu.exec(btn.mapToGlobal(pos))
            if action == act_settings:
                self._show_template_dialog()
            elif action == act_switch:
                self._step["template_highlighted"] = False
                self._refresh_coord_display()
                self.rowChanged.emit()
            elif action == act_resave:
                self._save_as_template()
        else:
            # 坐标模式
            act_edit = menu.addAction("✏ 编辑坐标")
            menu.addSeparator()
            if has_template:
                act_switch = menu.addAction("📷 切换为模板模式")
                menu.addSeparator()
            act_save = menu.addAction("📷 转为模板")
            action = menu.exec(btn.mapToGlobal(pos))
            if action == act_edit:
                self._edit_coord_dialog(btn)
            elif has_template and action == act_switch:
                self._step["template_highlighted"] = True
                self._step["coord_locked"] = False
                self._refresh_coord_display()
                self.rowChanged.emit()
            elif action == act_save:
                self._save_as_template()

    def _edit_coord_dialog(self, btn: QPushButton) -> None:
        """编辑坐标对话框：支持手动输入或鼠标中键定位。"""
        coord = self._get_btn_coord(btn)
        default_text = self._fmt_coord(coord) if coord else ""

        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
            QPushButton, QDialogButtonBox,
        )

        dlg = QDialog(self.window())
        dlg.setWindowTitle("编辑坐标")
        dlg.setMinimumWidth(380)
        root = QVBoxLayout(dlg)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # 输入行
        input_row = QHBoxLayout()
        edit = QLineEdit(default_text)
        edit.setPlaceholderText("x, y（归一化 0~1）")
        edit.setStyleSheet(
            "QLineEdit{border:1px solid #bbb;border-radius:4px;"
            "padding:5px 8px;background:white;font-size:12px;}"
        )
        input_row.addWidget(edit, 1)

        btn_locate = QPushButton("🎯 手动定位")
        btn_locate.setStyleSheet(
            "QPushButton{background:#3498db;color:white;border-radius:4px;"
            "padding:5px 12px;font-weight:bold;font-size:11px;}"
            "QPushButton:hover{background:#2980b9;}"
        )
        btn_locate.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_locate.clicked.connect(lambda: _do_locate())
        input_row.addWidget(btn_locate)
        root.addLayout(input_row)

        # 按钮行
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        root.addWidget(btns)

        def _do_locate() -> None:
            """手动定位：透明化对话框，中键拾取坐标后自动保存。"""
            result = None
            try:
                result = manual_positioner.pick_coordinate()
                if result is not None:
                    x, y = result
                    edit.setText(f"{x:.4f}, {y:.4f}")
            finally:
                if result is not None:
                    # 定位成功，自动保存并关闭对话框
                    dlg.accept()
                else:
                    # 定位失败，恢复对话框让用户重试
                    dlg.setWindowOpacity(1.0)
                    dlg.raise_()
                    dlg.activateWindow()
                    try:
                        hwnd = int(dlg.winId())
                        ctypes.windll.user32.SetForegroundWindow(hwnd)
                    except Exception:
                        pass

        dlg.exec()
        if dlg.result() != QDialog.DialogCode.Accepted:
            return

        text = edit.text().strip()
        if not text:
            return
        import re
        nums = re.findall(r"\d+\.?\d*", text)
        if len(nums) < 2:
            return
        try:
            x, y = float(nums[0]), float(nums[1])
        except ValueError:
            return
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return
        self._set_btn_coord(btn, x, y)
        self._refresh_coord_display()
        self.rowChanged.emit()

    # ------------------------------------------------------------------
    # 模板保存/自动更新
    # ------------------------------------------------------------------
    def _get_or_create_screenshot_id(self) -> str:
        """获取步骤的截图 ID，不存在则生成。"""
        sid = self._step.get("screenshot_id", "") if self._step else ""
        if not sid:
            sid = app_config.new_screenshot_id()
            if self._step:
                self._step["screenshot_id"] = sid
        return sid

    def _save_screenshots(self, coord) -> Optional[str]:
        """截图并保存全图和模板，返回模板文件路径。"""
        if not coord or not isinstance(coord, (tuple, list)) or len(coord) != 2:
            return None
        try:
            x, y = float(coord[0]), float(coord[1])
        except (TypeError, ValueError):
            return None
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return None

        try:
            shot_bytes = capture.capture_screen()
            img = Image.open(io.BytesIO(shot_bytes))
            sw, sh = img.size
            px, py = int(x * sw), int(y * sh)
            size = self._template_size
            crop_w = crop_h = size

            # 确保截图目录存在
            app_config._ensure_dir(self._screenshots_dir)
            sid = self._get_or_create_screenshot_id()

            # 只保存裁剪后的模板，不保留全图
            left = max(0, px - crop_w // 2)
            top = max(0, py - crop_h // 2)
            right = min(sw, left + crop_w)
            bottom = min(sh, top + crop_h)
            cropped = img.crop((left, top, right, bottom))
            tpl_path = os.path.join(self._screenshots_dir, f"{sid}.png")
            cropped.save(tpl_path, "PNG")
            return tpl_path
        except Exception:
            return None

    def _get_template_filename(self) -> str:
        """生成模板文件基础名：使用截图 ID。"""
        return self._get_or_create_screenshot_id()

    def _save_as_template(self) -> None:
        """将当前坐标位置的元素截图保存为模板。"""
        op = self._combo.currentText()
        if op not in OPS_NEED_VISION:
            return
        coord = None
        if op == OP_DRAG:
            coord = self._step.get("start_coord") if self._step else None
        else:
            coord = self._step.get("coord") if self._step else None
        if not coord or not isinstance(coord, (tuple, list)) or len(coord) != 2:
            return

        filepath = self._save_screenshots(coord)
        if filepath is None:
            return

        self._template_path = filepath
        self._template_size = self._step.get("template_size", 50) if self._step else 50
        self._template_threshold = self._step.get("template_threshold", 0.75) if self._step else 0.75
        if self._step:
            self._step["template_path"] = filepath
            self._step["template_highlighted"] = True
            self._step["coord_locked"] = False
            self._step["template_threshold"] = self._template_threshold
        self._refresh_coord_display()
        self.rowChanged.emit()

    # ------------------------------------------------------------------
    # 模板对话框
    # ------------------------------------------------------------------
    def _show_template_dialog(self) -> None:
        """显示模板对话框：上方为可编辑文件路径+大小，下方为图片预览。"""
        dlg = QDialog(self.window())
        dlg.setWindowTitle("模板设置")
        dlg.setMinimumSize(400, 460)
        dlg.resize(500, 560)
        root = QVBoxLayout(dlg)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # 文件路径
        lbl = QLabel("模板文件路径：")
        lbl.setStyleSheet("color:#2c3e50;font-weight:bold;font-size:12px;")
        root.addWidget(lbl)
        path_edit = QLineEdit(self._template_path)
        path_edit.setStyleSheet(
            "QLineEdit{border:1px solid #bbb;border-radius:4px;"
            "padding:5px 8px;background:white;font-size:12px;}"
        )
        path_edit.setPlaceholderText("输入模板图片文件路径")
        root.addWidget(path_edit)

        # 裁剪大小
        size_row = QHBoxLayout()
        size_lbl = QLabel("裁剪大小(px)：")
        size_lbl.setStyleSheet("color:#2c3e50;font-weight:bold;font-size:12px;")
        size_row.addWidget(size_lbl)
        size_spin = type('', (), {})()  # placeholder
        from PySide6.QtWidgets import QSpinBox
        size_spin = QSpinBox()
        size_spin.setRange(10, 500)
        size_spin.setValue(self._template_size)
        size_spin.setStyleSheet(
            "QSpinBox{border:1px solid #bbb;border-radius:4px;"
            "padding:4px 6px;background:white;font-size:12px;min-width:80px;}"
        )
        size_spin.setSuffix(" px")
        size_row.addWidget(size_spin)
        size_row.addStretch(1)
        root.addLayout(size_row)

        # 匹配度（阈值）
        thresh_row = QHBoxLayout()
        thresh_lbl = QLabel("匹配度(%)：")
        thresh_lbl.setStyleSheet("color:#2c3e50;font-weight:bold;font-size:12px;")
        thresh_row.addWidget(thresh_lbl)
        thresh_spin = QSpinBox()
        thresh_spin.setRange(0, 100)
        thresh_spin.setValue(int(round(self._template_threshold * 100)))
        thresh_spin.setStyleSheet(
            "QSpinBox{border:1px solid #bbb;border-radius:4px;"
            "padding:4px 6px;background:white;font-size:12px;min-width:80px;}"
        )
        thresh_spin.setSuffix(" %")
        thresh_row.addWidget(thresh_spin)
        thresh_row.addStretch(1)
        root.addLayout(thresh_row)

        # 图片预览
        img_label = QLabel()
        img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_label.setStyleSheet(
            "background:rgba(240,240,240,100);border:1px solid #ccc;"
            "border-radius:6px;"
        )
        img_label.setMinimumHeight(200)
        root.addWidget(img_label, 1)
        self._update_template_preview(img_label)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_frame = QPushButton("📐 框出模板")
        btn_frame.setStyleSheet(
            "QPushButton{background:#e67e22;color:white;border-radius:5px;"
            "padding:6px 14px;font-weight:bold;font-size:11px;}"
            "QPushButton:hover{background:#d35400;}"
        )
        btn_frame.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_frame.clicked.connect(lambda: _do_frame_template())
        btn_row.addWidget(btn_frame)
        btn_row.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.setStyleSheet(
            "QPushButton{background:#bdc3c7;color:white;border-radius:5px;"
            "padding:6px 18px;font-weight:bold;}"
            "QPushButton:hover{background:#95a5a6;}"
        )
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        btn_save.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;border-radius:5px;"
            "padding:6px 20px;font-weight:bold;}"
            "QPushButton:hover{background:#2ecc71;}"
        )
        btn_save.setDefault(True)
        btn_row.addWidget(btn_save)
        root.addLayout(btn_row)

        # 框选暂存：确认保存前不覆盖正式模板文件
        pending = {"temp": None, "real": None, "cx": 0.0, "cy": 0.0, "size": 0}

        def _do_frame_template() -> None:
            """框出模板：透明化对话框，两次中键框选后恢复。

            仅生成临时预览文件暂存，点击“保存”后才写入正式模板文件。
            """
            dlg.setWindowOpacity(0.0)
            try:
                # 先获取或生成截图 ID，用作文件名确保唯一
                sid = self._get_or_create_screenshot_id()
                result = manual_positioner.pick_template_area(
                    save_dir=self._screenshots_dir, filename=sid, temp=True)
                if result is not None:
                    preview_path, cx_norm, cy_norm, size_px = result
                    real_path = os.path.join(self._screenshots_dir, f"{sid}.png")
                    pending.update({
                        "temp": preview_path,
                        "real": real_path,
                        "cx": cx_norm,
                        "cy": cy_norm,
                        "size": size_px,
                    })
                    # 展示正式路径（尚未写入），并用临时预览图展示框选结果
                    path_edit.setText(real_path)
                    size_spin.setValue(size_px)
                    self._update_template_preview(img_label, preview_path)
            finally:
                dlg.setWindowOpacity(1.0)
                dlg.raise_()
                dlg.activateWindow()
                try:
                    hwnd = int(dlg.winId())
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass

        def _do_save():
            new_path = path_edit.text().strip()
            new_size = size_spin.value()
            # 阈值：UI 显示 0-100，内部存 0.0-1.0
            new_threshold = thresh_spin.value() / 100.0

            if pending.get("temp"):
                # 已框选但尚未写入：只有在点击“保存”时才覆盖正式模板
                temp_path = pending["temp"]
                real_path = pending.get("real") or new_path
                target = new_path if new_path.strip() else real_path
                try:
                    import shutil
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                    if os.path.abspath(target) == os.path.abspath(temp_path):
                        target = real_path
                    shutil.copyfile(temp_path, target)
                except OSError:
                    pass
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                pending["temp"] = None

                self._template_path = target
                self._template_size = new_size
                self._template_threshold = new_threshold
                if self._step:
                    self._step["template_path"] = target
                    self._step["template_size"] = new_size
                    self._step["template_threshold"] = new_threshold
                    if self._combo.currentText() == OP_DRAG:
                        self._step["start_coord"] = (pending["cx"], pending["cy"])
                    else:
                        self._step["coord"] = (pending["cx"], pending["cy"])
            else:
                # 未框选：仅应用手动改动的路径/大小/阈值
                size_changed = new_size != self._template_size
                path_changed = new_path != self._template_path
                # 更新路径
                if path_changed:
                    self._template_path = new_path
                    if self._step:
                        self._step["template_path"] = new_path
                # 更新大小
                if size_changed:
                    self._template_size = new_size
                    if self._step:
                        self._step["template_size"] = new_size
                # 更新阈值
                self._template_threshold = new_threshold
                if self._step:
                    self._step["template_threshold"] = new_threshold
                # 大小改变时重新截图并按新大小裁剪（不保留原全图）
                if size_changed and self._template_path:
                    try:
                        shot_bytes = capture.capture_screen()
                        img = Image.open(io.BytesIO(shot_bytes))
                        # 从模板路径还原坐标（从 step 取）
                        op = self._combo.currentText()
                        coord = None
                        if op == OP_DRAG:
                            coord = self._step.get("start_coord") if self._step else None
                        else:
                            coord = self._step.get("coord") if self._step else None
                        if coord and isinstance(coord, (tuple, list)) and len(coord) == 2:
                            sw, sh = img.size
                            px, py = int(float(coord[0]) * sw), int(float(coord[1]) * sh)
                            half = new_size // 2
                            left = max(0, px - half)
                            top = max(0, py - half)
                            right = min(sw, left + new_size)
                            bottom = min(sh, top + new_size)
                            cropped = img.crop((left, top, right, bottom))
                            cropped.save(self._template_path, "PNG")
                    except Exception:
                        pass

            self._update_template_preview(img_label)
            self._refresh_coord_display()
            self.rowChanged.emit()
            dlg.accept()

        def _cleanup_pending_temp() -> None:
            """对话框关闭时清理未保存的临时预览文件。"""
            tp = pending.get("temp")
            if tp:
                try:
                    os.remove(tp)
                except OSError:
                    pass
                pending["temp"] = None

        dlg.finished.connect(lambda _: _cleanup_pending_temp())

        btn_save.clicked.connect(_do_save)
        path_edit.textChanged.connect(
            lambda: self._update_template_preview(img_label, path_edit.text().strip()))
        dlg.exec()

    def _update_template_preview(self, label: QLabel, path: str = None) -> None:
        """更新模板图片预览。"""
        p = path if path is not None else self._template_path
        if p and os.path.isfile(p):
            pixmap = QPixmap(p)
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    360, 280,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                label.setPixmap(scaled)
                label.setText("")
                return
        label.setPixmap(QPixmap())
        label.setText("（无图片预览）\n右键菜单 → 转为模板\n或输入已有模板文件路径")
        label.setStyleSheet(
            "background:rgba(240,240,240,100);border:1px solid #ccc;"
            "border-radius:6px;color:#888;font-size:12px;"
        )

    # ------------------------------------------------------------------
    # 外部调用：更新坐标显示
    # ------------------------------------------------------------------
    def update_coord_from_step(self) -> None:
        """从 step 更新坐标显示（由外部调用，如执行后记录坐标）。
        
        不重建控件，直接更新按钮文本和样式。
        如果模板未高亮，同步更新模板截图。
        """
        op = self._combo.currentText()
        if op not in OPS_NEED_VISION:
            return
        if op == OP_DRAG:
            if self._coord_tpl_btn is not None and self._step:
                start_coord = self._step.get("start_coord")
                self._update_btn_display(self._coord_tpl_btn, start_coord, "起点")
            if self._coord_tpl_btn2 is not None and self._step:
                end_coord = self._step.get("end_coord")
                self._update_btn_display(self._coord_tpl_btn2, end_coord, "终点")
        else:
            if self._coord_tpl_btn is not None and self._step:
                coord = self._step.get("coord")
                self._update_btn_display(self._coord_tpl_btn, coord, "坐标")

        # 模板未高亮时，同步更新模板截图
        tpl_hl = self._step.get("template_highlighted", False) if self._step else False
        if not tpl_hl and self._step:
            self._auto_update_template()

    def _auto_update_template(self) -> None:
        """执行时自动更新模板截图（模板未高亮时调用）。"""
        op = self._combo.currentText()
        if op not in OPS_NEED_VISION:
            return
        coord = None
        if op == OP_DRAG:
            coord = self._step.get("start_coord") if self._step else None
        else:
            coord = self._step.get("coord") if self._step else None
        if not coord or not isinstance(coord, (tuple, list)) or len(coord) != 2:
            return

        filepath = self._save_screenshots(coord)
        if filepath:
            self._template_path = filepath
            self._template_size = self._step.get("template_size", 50) if self._step else 50
            if self._step:
                self._step["template_path"] = filepath

    def _refresh_var_button(self) -> None:
        """根据 step 状态创建 / 更新变量按钮显示。"""
        # 移除旧按钮
        if self._var_btn is not None:
            layout = self._var_btn.parentWidget().layout()
            if layout:
                layout.removeWidget(self._var_btn)
            self._var_btn.deleteLater()
            self._var_btn = None

        op = self._step.get("op", "")
        var = self._step.get("text_var")
        var_type = self._step.get("var_type")

        if var:
            full = (self._step.get("text_full") or self._step.get("text")
                    or self._step.get("desc_full") or "")
            self._var_values[var] = full
            self._var_btn = QPushButton(var)
            self._var_btn.setStyleSheet(self._STYLE_VAR_BTN)
            self._var_btn.setFixedHeight(22)
            self._var_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._var_btn.setToolTip("点击查看完整文本")
            self._var_btn.clicked.connect(lambda _c, v=var: self._show_var_text(v))
            self.layout().addWidget(self._var_btn)
        elif op == "长输入" and var_type == "long_input":
            full_text = self._step.get("text_full") or self._step.get("text") or ""
            if full_text:
                btn_text = "长输入正文"
                self._var_values["__long_input__"] = full_text
            else:
                btn_text = "待生成"
                self._var_values["__long_input__"] = (
                    self._step.get("desc_full")
                    or self._step.get("description")
                    or "（尚未生成）"
                )
            self._var_btn = QPushButton(btn_text)
            self._var_btn.setStyleSheet(self._STYLE_LONG_BTN)
            self._var_btn.setFixedHeight(22)
            self._var_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._var_btn.setToolTip("点击查看长输入主题/生成正文")
            self._var_btn.clicked.connect(lambda _c: self._show_var_text("__long_input__"))
            self.layout().addWidget(self._var_btn)

    # ------------------------------------------------------------------
    # 右键菜单
    # ------------------------------------------------------------------
    def _on_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(self._MENU_STYLE)
        act_above = menu.addAction("⬆ 向上插入空行")
        act_below = menu.addAction("⬇ 向下插入空行")
        menu.addSeparator()
        act_add = menu.addAction("➕ 添加输入变量")
        menu.addSeparator()
        act_del = menu.addAction("❌ 删除")
        # 本行已是变量步骤时，禁用「添加输入变量」（避免叠加）
        has_var = bool(self._step and (self._step.get("text_var")
                                       or self._step.get("var_type") == "long_input"))
        act_add.setDisabled(has_var)
        source = self.sender() or self
        action = menu.exec(source.mapToGlobal(pos))
        if action == act_above:
            self.insertAboveRequested.emit()
        elif action == act_below:
            self.insertBelowRequested.emit()
        elif action == act_add:
            self.convertToVarRequested.emit()
        elif action == act_del:
            self.deleteRequested.emit()

    def _collect_var_values(self) -> None:
        """从 step 收集所有 text_var -> text_full 映射。"""
        if not self._step:
            return
        var = self._step.get("text_var")
        if var:
            full = (self._step.get("text_full") or self._step.get("text")
                    or self._step.get("desc_full") or "")
            self._var_values[var] = full

    # ------------------------------------------------------------------
    # 变量按钮弹窗：查看 / 编辑变量（同旧版）
    # ------------------------------------------------------------------
    def _show_var_text(self, var: str) -> None:
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QPlainTextEdit, QPushButton, QHBoxLayout,
            QLabel, QLineEdit,
        )
        is_long_input = var.startswith("__long")
        old_full = self._var_values.get(var, "")

        dlg = QDialog(self.window())
        if is_long_input:
            dlg.setWindowTitle("编辑长输入正文")
            dlg.setMinimumSize(520, 400)
            dlg.resize(620, 480)
        else:
            dlg.setWindowTitle(f"编辑输入变量 {var}")
            dlg.setMinimumSize(520, 420)
            dlg.resize(620, 500)

        root = QVBoxLayout(dlg)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        var_name_edit: Optional[QLineEdit] = None
        if not is_long_input:
            name_row = QHBoxLayout()
            name_lbl = QLabel("变量名：")
            name_lbl.setStyleSheet("color:#2c3e50;font-weight:bold;font-size:12px;")
            name_row.addWidget(name_lbl)
            var_name_edit = QLineEdit(var)
            var_name_edit.setStyleSheet(
                "QLineEdit{border:1px solid #bbb;border-radius:4px;"
                "padding:5px 8px;background:white;font-size:13px;}"
            )
            var_name_edit.setPlaceholderText("如 {输入1}，需保持 { } 包裹格式")
            name_row.addWidget(var_name_edit, 1)
            root.addLayout(name_row)

            tip = QLabel("💡 修改变量名后，后续步骤里如果引用了该变量名，请同步替换占位符。")
            tip.setStyleSheet("color:#7f8c8d;font-size:11px;")
            tip.setWordWrap(True)
            root.addWidget(tip)

        val_lbl = QLabel("变量值 / 正文：")
        val_lbl.setStyleSheet("color:#2c3e50;font-weight:bold;font-size:12px;")
        root.addWidget(val_lbl)
        edit = QPlainTextEdit()
        edit.setPlainText(old_full)
        edit.setStyleSheet(
            "QPlainTextEdit{background:white;border:1px solid #ccc;"
            "border-radius:6px;padding:8px;font-family:Consolas,'Microsoft YaHei';"
            "font-size:13px;line-height:1.6;}"
        )
        root.addWidget(edit, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.setStyleSheet(
            "QPushButton{background:#bdc3c7;color:white;border-radius:5px;"
            "padding:6px 18px;font-weight:bold;}"
            "QPushButton:hover{background:#95a5a6;}"
        )
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        btn_save.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;border-radius:5px;"
            "padding:6px 20px;font-weight:bold;}"
            "QPushButton:hover{background:#2ecc71;}"
        )
        btn_save.setDefault(True)
        btn_row.addWidget(btn_save)
        root.addLayout(btn_row)

        def _do_save():
            new_full = edit.toPlainText()
            new_var = var
            if not is_long_input and var_name_edit is not None:
                new_var = var_name_edit.text().strip() or var
                if not new_var.startswith("{"):
                    new_var = "{" + new_var
                if not new_var.endswith("}"):
                    new_var = new_var + "}"
            if new_var != var:
                self._var_values.pop(var, None)
            self._var_values[new_var] = new_full
            if self._step:
                self._step["text_full"] = new_full
                self._step["text"] = new_full
                if not is_long_input:
                    old_var_val = self._step.get("text_var")
                    self._step["text_var"] = new_var
                    desc = self._step.get("description", "")
                    if old_var_val and old_var_val in desc:
                        desc = desc.replace(old_var_val, new_var)
                        self._step["description"] = desc
                    # 同步按钮 + 参数编辑器
                    if self._var_btn is not None:
                        self._var_btn.setText(new_var)
                    self._edit.setText(new_var)
                    self._original_param = new_var
            self.variableUpdated.emit()
            dlg.accept()

        btn_save.clicked.connect(_do_save)
        dlg.exec()

    # ------------------------------------------------------------------
    # 对外 API（保持与旧版一致，text/set_text/set_readonly 等）
    # ------------------------------------------------------------------
    def text(self) -> str:
        """返回 parse_steps 兼容的 token：<op>-<param>。"""
        op = self._combo.currentText().strip()
        param = self._edit.text().strip()
        if not op:
            return param
        if not param:
            return op
        return f"{op}-{param}"

    def set_text(self, text: str) -> None:
        """兼容旧 API：接受 <op>-<param> 字符串并拆分到下拉框/参数编辑框。"""
        if "-" in text:
            op, rest = text.split("-", 1)
        else:
            op, rest = text, ""
        # 设置下拉框（若选项不存在则添加）
        if op not in [self._combo.itemText(i) for i in range(self._combo.count())]:
            self._combo.addItem(op)
        self._combo.setCurrentText(op)
        self._edit.setText(rest)
        self._original_op = op
        self._original_param = rest

    def is_modified(self) -> bool:
        return (self._combo.currentText() != self._original_op
                or self._edit.text().strip() != self._original_param)

    def set_readonly(self, on: bool) -> None:
        self._readonly = on
        self._edit.setReadOnly(on)
        self._combo.setEnabled(not on)
        if self._var_btn:
            self._var_btn.setEnabled(not on)

    def set_highlight(self, on: bool) -> None:
        self._highlighted = on
        self._edit.setStyleSheet(self._STYLE_HIGHLIGHT if on else self._STYLE_NORMAL)
        self._edit.setReadOnly(not on)
        self._combo.setStyleSheet(self._STYLE_COMBO_HL if on else self._STYLE_COMBO)
        self._combo.setEnabled(on or True)  # 高亮时可切换 op

    def is_highlighted(self) -> bool:
        return self._highlighted

    def focus_edit(self) -> None:
        self._edit.setFocus()
        self._edit.selectAll()

    def set_status(self, status: str, color: str = "#B0B0B0") -> None:
        if status == "pending":
            return  # pending 状态保持用户手动切换的标识，不覆盖
        marks = {"done": "✓", "failed": "✗", "skipped": "–", "locating": "…", "executing": "→"}
        # 用户手动跳过的步骤，显示 ✓ 而不是 –
        if status == "skipped" and self._user_skip:
            mark = "✓"
        else:
            mark = marks.get(status, "○")
        self._mark.setText(mark)
        self._mark.setStyleSheet(f"color:{color};font-weight:bold;")
        self._mark.setCursor(Qt.CursorShape.ArrowCursor)
        # 同步跳过状态：done/skipped 标记为跳过（✓），其余标记为执行（○）
        self._user_skip = (status in ("done", "skipped"))

    # ------------------------------------------------------------------
    # 状态标识点击切换 ✓/○
    # ------------------------------------------------------------------
    def _on_mark_clicked(self, event) -> None:
        """点击状态标识切换 ✓（跳过）/ ○（执行）。"""
        if self._readonly:
            return
        self._user_skip = not self._user_skip
        if self._user_skip:
            self._mark.setText("✓")
            self._mark.setStyleSheet("color:#2E8B57;font-weight:bold;")
        else:
            self._mark.setText("○")
            self._mark.setStyleSheet("color:#B0B0B0;font-weight:bold;")


class AgentPanel(QWidget):
    stepsConfirmed = Signal(list, bool)  # steps, proceed(True=执行/False=仅修改重渲染)
    resumeExecRequested = Signal()       # 暂停编辑后用户点「恢复执行」
    saveRequested = Signal()             # 用户点击「保存」按钮，请求立即保存到文件
    workflowDataChanged = Signal()       # 工作流数据有修改（目标/屏幕状态/步骤文本或结构），用于自动保存

    def __init__(self, parent=None):
        super().__init__(parent)
        # QToolTip 显式指定白底黑字：子控件（QLineEdit/QComboBox 等）各自
        # 设有 stylesheet 时，全局 app.setStyleSheet 的 QToolTip 规则可能不生效，
        # 需在面板根容器再声明一次确保级联覆盖。
        self.setStyleSheet(
            "background:transparent;"
            "QToolTip{background-color:#ffffff;color:#000000;"
            "border:1px solid #b0b0b0;border-radius:4px;padding:4px 8px;}"
        )
        self._paused_mode = False
        self._screenshots_dir = app_config.templates_fallback_dir()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        self._title = QLabel("执行步骤（可编辑）")
        self._title.setStyleSheet("color:#333;font-weight:bold;")
        lay.addWidget(self._title)

        # 目标任务 + 屏幕状态 + 截图按钮（同旧版）
        self._context_widget = QWidget()
        self._context_widget.setStyleSheet("background:transparent;")
        ctx_lay = QVBoxLayout(self._context_widget)
        ctx_lay.setContentsMargins(0, 0, 0, 0)
        ctx_lay.setSpacing(4)

        goal_row = QHBoxLayout()
        goal_lbl = QLabel("目标任务：")
        goal_lbl.setStyleSheet("color:#555;font-weight:bold;")
        goal_lbl.setFixedWidth(70)
        self._goal_edit = QLineEdit()
        self._goal_edit.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,200);border:1px solid #ccc;"
            "border-radius:4px;padding:3px 6px;}"
        )
        self._goal_edit.setPlaceholderText("目标任务")
        goal_row.addWidget(goal_lbl)
        goal_row.addWidget(self._goal_edit, 1)
        self._btn_screenshot = QPushButton()
        self._btn_screenshot.setCheckable(True)
        self._btn_screenshot.setChecked(True)
        self._btn_screenshot.setStyleSheet(
            "QPushButton{background:#95a5a6;color:white;border-radius:4px;"
            "padding:3px 8px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#7f8c8d;}"
            "QPushButton:checked{background:#3498db;}"
            "QPushButton:checked:hover{background:#5dade2;}"
        )
        self._btn_screenshot.setToolTip("重规划时是否截图给规划模型（蓝色=截图，灰色=仅用文字描述）")
        self._update_screenshot_btn_text()
        self._btn_screenshot.toggled.connect(self._update_screenshot_btn_text)
        goal_row.addWidget(self._btn_screenshot)
        ctx_lay.addLayout(goal_row)

        state_row = QHBoxLayout()
        state_lbl = QLabel("屏幕状态：")
        state_lbl.setStyleSheet("color:#555;font-weight:bold;")
        state_lbl.setFixedWidth(70)
        self._state_edit = QLineEdit()
        self._state_edit.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,200);border:1px solid #ccc;"
            "border-radius:4px;padding:3px 6px;}"
        )
        self._state_edit.setPlaceholderText("当前屏幕状态描述")
        state_row.addWidget(state_lbl)
        state_row.addWidget(self._state_edit, 1)
        ctx_lay.addLayout(state_row)

        self._context_widget.hide()
        lay.addWidget(self._context_widget)

        # ---- 列表表头（两列：指令 | 参数） ----
        self._header = QWidget()
        self._header.setStyleSheet("background:transparent;")
        hdr = QHBoxLayout(self._header)
        hdr.setContentsMargins(24, 0, 4, 2)
        hdr.setSpacing(4)
        col1 = QLabel("指令")
        col1.setStyleSheet("color:#555;font-weight:bold;font-size:12px;min-width:86px;")
        col2 = QLabel("参数（右键：添加输入变量 / 上下插入空行 / 删除）")
        col2.setStyleSheet("color:#555;font-weight:bold;font-size:12px;")
        hdr.addWidget(col1)
        hdr.addWidget(col2, 1)
        lay.addWidget(self._header)

        # ---- 步骤列表 ----
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background:transparent;")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        # 空状态占位：居中加号按钮
        self._empty_placeholder = QWidget()
        self._empty_placeholder.setStyleSheet("background:transparent;")
        ph_lay = QVBoxLayout(self._empty_placeholder)
        ph_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._btn_add_first = QPushButton("+")
        self._btn_add_first.setFixedSize(48, 48)
        self._btn_add_first.setStyleSheet(
            "QPushButton{background:#3498db;color:white;font-size:24px;font-weight:bold;"
            "border-radius:24px;border:none;}"
            "QPushButton:hover{background:#5dade2;}"
        )
        self._btn_add_first.setToolTip("添加第一个步骤")
        self._btn_add_first.clicked.connect(self._add_first_row)
        ph_lay.addWidget(self._btn_add_first)
        self._rows_layout.addWidget(self._empty_placeholder)
        self._rows_layout.addStretch(1)
        self._scroll.setWidget(self._rows_container)
        lay.addWidget(self._scroll, 1)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color:#c0392b;")
        self._hint.setWordWrap(True)
        self._hint.hide()
        lay.addWidget(self._hint)

        # 按钮区
        btn_row = QHBoxLayout()
        self._btn_confirm = QPushButton("确认执行")
        self._btn_save = QPushButton("保存")
        self._btn_confirm.setStyleSheet(
            "QPushButton{background:#2e8b57;color:white;border-radius:5px;padding:6px 16px;}"
            "QPushButton:hover{background:#3aa569;}"
        )
        self._btn_save.setStyleSheet(
            "QPushButton{background:#3498db;color:white;border-radius:5px;padding:6px 16px;}"
            "QPushButton:hover{background:#2980b9;}"
        )
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_save)
        btn_row.addWidget(self._btn_confirm)
        lay.addLayout(btn_row)

        # 进度条
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setStyleSheet(
            "QProgressBar{background:rgba(255,255,255,180);border:1px solid #bbb;"
            "border-radius:4px;text-align:center;} "
            "QProgressBar::chunk{background:#2e8b57;border-radius:3px;}"
        )
        self._progress.hide()
        lay.addWidget(self._progress)

        self._rows: List[StepRow] = []
        self._btn_confirm.clicked.connect(lambda: self._emit(True))
        self._btn_save.clicked.connect(self._on_save_clicked)

        self._settings = app_config.load_settings()

    # ------------------------------------------------------------------
    # 上下文显示
    # ------------------------------------------------------------------
    def set_context(self, goal: str = "", screen_state: str = "",
                    show: bool = False) -> None:
        if getattr(self, "_context_signals_connected", False):
            self._goal_edit.textChanged.disconnect(self.workflowDataChanged.emit)
            self._state_edit.textChanged.disconnect(self.workflowDataChanged.emit)
            self._context_signals_connected = False
        self._goal_edit.setText(goal)
        self._state_edit.setText(screen_state)
        self._context_widget.setVisible(show)
        if show:
            self._goal_edit.textChanged.connect(self.workflowDataChanged.emit)
            self._state_edit.textChanged.connect(self.workflowDataChanged.emit)
            self._context_signals_connected = True

    def get_goal(self) -> str:
        return self._goal_edit.text().strip()

    def get_screen_state(self) -> str:
        return self._state_edit.text().strip()

    def set_screenshots_dir(self, screenshots_dir: str) -> None:
        """设置截图保存目录。"""
        self._screenshots_dir = screenshots_dir

    def screenshot_enabled(self) -> bool:
        return self._btn_screenshot.isChecked()

    def _update_screenshot_btn_text(self) -> None:
        if self._btn_screenshot.isChecked():
            self._btn_screenshot.setText("📸 截图")
        else:
            self._btn_screenshot.setText("🚫 无截图")

    # ------------------------------------------------------------------
    # 步骤列表
    # ------------------------------------------------------------------
    def set_steps(self, steps: List[dict]) -> None:
        self._clear_rows()
        for s in steps:
            op, param = _op_and_param_from_step(s)
            row = StepRow(s.get("index", 0), op, param, step=s,
                          screenshots_dir=self._screenshots_dir)
            self._connect_row_signals(row)
            self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
            self._rows.append(row)
            row.set_highlight(True)
        # 不恢复 user_skip：每次打开都是初始化状态（○）

        var_count = sum(1 for s in steps if s.get("text_var"))
        long_input_count = sum(1 for s in steps if s.get("op") == "长输入")
        title = f"执行步骤（共 {len(steps)} 步，可直接编辑修改，或对话框提示模型修改）"
        hints = []
        if var_count:
            hints.append(f"{var_count} 处长文本已用变量表示（🔴 红色按钮可查看原文）")
        if long_input_count:
            hints.append(f"{long_input_count} 处「长输入」（🟣 紫色按钮：执行时由聊天模型流式生成正文）")
        if hints:
            title += "\n" + "；".join(hints)
        self._title.setText(title)
        self._hint.hide()
        self._update_empty_state()

    def _connect_row_signals(self, row: StepRow) -> None:
        row.insertAboveRequested.connect(lambda r=row: self._insert_above(r))
        row.insertBelowRequested.connect(lambda r=row: self._insert_below(r))
        row.deleteRequested.connect(lambda r=row: self._delete_row(r))
        row.variableUpdated.connect(self.workflowDataChanged.emit)
        row.rowChanged.connect(self.workflowDataChanged.emit)
        row.convertToVarRequested.connect(lambda r=row: self._add_input_variable_to_row(r))
        row.insertVarAboveRequested.connect(lambda r=row: self._insert_variable(r, above=True))
        row.insertVarBelowRequested.connect(lambda r=row: self._insert_variable(r, above=False))

    def _clear_rows(self) -> None:
        for r in self._rows:
            self._rows_layout.removeWidget(r)
            r.deleteLater()
        self._rows.clear()

    def _insert_above(self, row: StepRow) -> None:
        idx = self._rows.index(row)
        new_row = StepRow(idx, OP_CLICK, "", step={},
                          screenshots_dir=self._screenshots_dir)
        self._connect_row_signals(new_row)
        new_row.set_highlight(True)
        # +1 跳过 _empty_placeholder（布局索引 0）
        self._rows_layout.insertWidget(idx + 1, new_row)
        self._rows.insert(idx, new_row)
        self._reindex_rows()
        new_row.focus_edit()
        self.workflowDataChanged.emit()

    def _insert_below(self, row: StepRow) -> None:
        idx = self._rows.index(row) + 1
        new_row = StepRow(idx, OP_CLICK, "", step={},
                          screenshots_dir=self._screenshots_dir)
        self._connect_row_signals(new_row)
        new_row.set_highlight(True)
        # +1 跳过 _empty_placeholder（布局索引 0）
        self._rows_layout.insertWidget(idx + 1, new_row)
        self._rows.insert(idx, new_row)
        self._reindex_rows()
        new_row.focus_edit()
        self.workflowDataChanged.emit()

    def _delete_row(self, row: StepRow) -> None:
        idx = self._rows.index(row)
        self._rows_layout.removeWidget(row)
        # 步骤删除时连带清理该步骤占用的模板/全图（一处一张）
        app_config.delete_screenshot_files(
            row._screenshots_dir,
            (row._step or {}).get("screenshot_id"),
        )
        row.deleteLater()
        self._rows.pop(idx)
        self._reindex_rows()
        self._update_empty_state()
        self.workflowDataChanged.emit()

    def _reindex_rows(self) -> None:
        for i, r in enumerate(self._rows):
            r._index = i

    # ------------------------------------------------------------------
    # 空状态 + 加号按钮
    # ------------------------------------------------------------------
    def _update_empty_state(self) -> None:
        """步骤为空时显示加号按钮，有步骤时隐藏。"""
        self._empty_placeholder.setVisible(len(self._rows) == 0)

    def _add_first_row(self) -> None:
        """加号按钮回调：添加第一个空行。"""
        row = StepRow(0, OP_CLICK, "", step={},
                      screenshots_dir=self._screenshots_dir)
        self._connect_row_signals(row)
        row.set_highlight(True)
        # 插入到 placeholder 之后、stretch 之前
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self._rows.append(row)
        self._reindex_rows()
        self._update_empty_state()
        row.focus_edit()
        self.workflowDataChanged.emit()

    # ------------------------------------------------------------------
    # 手动输入变量
    # ------------------------------------------------------------------
    def _next_var_name(self) -> str:
        import re
        max_n = 0
        for r in self._rows:
            if not r._step:
                continue
            v = r._step.get("text_var") or ""
            m = re.fullmatch(r"\{输入(\d+)\}", v)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f"{{输入{max_n + 1}}}"

    def _make_input_step(self, var_name: str, var_value: str,
                         display_op: str = "输入") -> dict:
        """构造「输入变量」类型的 step dict。

        根据 op 类型区分：
        - 输入：text 存变量值全文，description 为变量名占位符
        - 长输入：description 为变量名占位符，desc_full 存原始主题，
          text 不设（正文执行时流式生成）
        """
        if display_op == "长输入":
            return {
                "index": 0,
                "op": "长输入",
                "description": var_name,
                "desc_full": var_value,
                "status": "pending",
                "note": None,
                "text": None,
                "text_var": var_name,
                "text_full": None,
                "var_type": "long_input",
            }
        return {
            "index": 0,
            "op": display_op or "输入",
            "description": var_name,
            "status": "pending",
            "note": None,
            "text": var_value,
            "text_var": var_name,
            "text_full": var_value,
            "var_type": "input",
        }

    def _edit_variable_dialog(self, parent_title: str, default_var: str = "",
                              default_value: str = "") -> Optional[tuple]:
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
            QPlainTextEdit, QPushButton,
        )
        dlg = QDialog(self.window())
        dlg.setWindowTitle(parent_title)
        dlg.setMinimumSize(500, 420)
        dlg.resize(580, 500)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)

        name_row = QHBoxLayout()
        name_lbl = QLabel("变量名：")
        name_lbl.setStyleSheet("color:#2c3e50;font-weight:bold;font-size:12px;min-width:72px;")
        name_row.addWidget(name_lbl)
        var_edit = QLineEdit(default_var or self._next_var_name())
        var_edit.setStyleSheet(
            "QLineEdit{border:1px solid #bbb;border-radius:4px;"
            "padding:6px 8px;background:white;font-size:13px;}"
        )
        var_edit.setPlaceholderText("如 {输入1}，自动补全 { } 包裹")
        name_row.addWidget(var_edit, 1)
        v.addLayout(name_row)

        val_lbl = QLabel("变量值（支持多行长文本）：")
        val_lbl.setStyleSheet("color:#2c3e50;font-weight:bold;font-size:12px;")
        v.addWidget(val_lbl)
        val_edit = QPlainTextEdit()
        val_edit.setPlainText(default_value)
        val_edit.setStyleSheet(
            "QPlainTextEdit{background:white;border:1px solid #ccc;"
            "border-radius:6px;padding:8px;font-family:Consolas,'Microsoft YaHei';"
            "font-size:13px;line-height:1.6;}"
        )
        v.addWidget(val_edit, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.setStyleSheet(
            "QPushButton{background:#bdc3c7;color:white;border-radius:5px;"
            "padding:6px 18px;font-weight:bold;}"
            "QPushButton:hover{background:#95a5a6;}"
        )
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_cancel)
        btn_save = QPushButton("确定")
        btn_save.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;border-radius:5px;"
            "padding:6px 20px;font-weight:bold;}"
            "QPushButton:hover{background:#2ecc71;}"
        )
        btn_save.setDefault(True)
        btn_row.addWidget(btn_save)
        v.addLayout(btn_row)

        result = {}

        def _do_accept():
            name = var_edit.text().strip() or self._next_var_name()
            if not name.startswith("{"):
                name = "{" + name
            if not name.endswith("}"):
                name = name + "}"
            result["name"] = name
            result["value"] = val_edit.toPlainText()
            dlg.accept()

        btn_save.clicked.connect(_do_accept)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return (result["name"], result["value"])
        return None

    def _convert_to_variable(self, row: StepRow) -> None:
        idx = self._rows.index(row)
        default_value = row.text()
        if row._step:
            default_value = (
                row._step.get("description")
                or row._step.get("text")
                or row.text()
            )
        result = self._edit_variable_dialog("转为输入变量", default_value=default_value)
        if result is None:
            return
        var_name, var_value = result
        op_name = row._combo.currentText() or "输入"
        new_step = self._make_input_step(var_name, var_value, op_name)
        self._rows_layout.removeWidget(row)
        row.deleteLater()
        self._rows.pop(idx)
        new_row = StepRow(idx, op_name, var_name, step=new_step,
                          screenshots_dir=self._screenshots_dir)
        self._connect_row_signals(new_row)
        # +1 跳过 _empty_placeholder（布局索引 0）
        self._rows_layout.insertWidget(idx + 1, new_row)
        self._rows.insert(idx, new_row)
        self._reindex_rows()
        self.workflowDataChanged.emit()

    def _insert_variable(self, row: StepRow, above: bool) -> None:
        idx = self._rows.index(row)
        result = self._edit_variable_dialog("插入输入变量")
        if result is None:
            return
        var_name, var_value = result
        new_step = self._make_input_step(var_name, var_value, "输入")
        insert_at = idx if above else idx + 1
        new_row = StepRow(insert_at, "输入", var_name, step=new_step,
                          screenshots_dir=self._screenshots_dir)
        self._connect_row_signals(new_row)
        new_row.set_highlight(True)
        # +1 跳过 _empty_placeholder（布局索引 0）
        self._rows_layout.insertWidget(insert_at + 1, new_row)
        self._rows.insert(insert_at, new_row)
        self._reindex_rows()
        new_row.focus_edit()
        self.workflowDataChanged.emit()

    def _add_input_variable_to_row(self, row: StepRow) -> None:
        """在当前行的第二列（参数编辑器）加入输入变量，第一列可自由选择指令。

        弹出变量编辑对话框 → 将变量名填入参数编辑框，变量值存入 step dict，
        第一列指令下拉框保持当前选择（用户可切换为"输入"或"长输入"等）。
        """
        # 始终用空值打开对话框，让用户从零输入
        result = self._edit_variable_dialog("添加输入变量")
        if result is None:
            return
        var_name, var_value = result
        op_name = row._combo.currentText() or "输入"
        # 更新 step dict
        new_step = self._make_input_step(var_name, var_value, op_name)
        row._step = new_step
        # 更新显示：参数编辑框填入变量名，下拉框保持当前选择
        row._edit.setText(var_name)
        # 同步 original 标记，确保后续 is_modified() 返回 False（不触发重复解析）
        row._original_param = var_name
        row._original_op = op_name
        # 刷新变量按钮
        row._refresh_var_button()
        self.workflowDataChanged.emit()

    # ------------------------------------------------------------------
    # 确认/恢复执行按钮
    # ------------------------------------------------------------------
    def _emit(self, proceed: bool) -> None:
        """确认/恢复执行按钮点击处理。

        - 暂停模式下：发射 resumeExecRequested 信号
        - 正常模式下：获取所有步骤，将 ✓ 标记为 user_skip 状态，保留所有步骤一起发射
        """
        if self._paused_mode:
            self.resumeExecRequested.emit()
            return

        # 正常模式：获取所有步骤
        steps = self.get_edited_steps()
        if steps is None:
            return

        # 将用户的 ✓/○ 标记写入 step dict，保留所有步骤（不过滤）
        skipped_count = 0
        for row, step in zip(self._rows, steps):
            if getattr(row, '_user_skip', False):
                step["user_skip"] = True
                skipped_count += 1
            else:
                step["user_skip"] = False

        if skipped_count == len(steps):
            self._hint.setText("所有步骤已标记为跳过（✓），没有可执行的步骤")
            self._hint.show()
            return

        if skipped_count > 0:
            self._hint.setText(f"已跳过 {skipped_count} 步，将执行剩余 {len(steps) - skipped_count} 步")
            self._hint.show()
        else:
            self._hint.hide()

        self.stepsConfirmed.emit(steps, proceed)

    def _on_save_clicked(self) -> None:
        """保存按钮点击：触发立即保存到文件。"""
        self.saveRequested.emit()

    def _show_hint(self, msg: str) -> None:
        self._hint.setText(msg)
        self._hint.show()

    def show_save_hint(self) -> None:
        """文件保存完成后显示"已保存"提示（由 main_window 在 I/O 完成后调用）。"""
        from PySide6.QtCore import QTimer
        self._hint.setStyleSheet("color:#27ae60;")
        self._hint.setText("已保存")
        self._hint.show()
        def _restore_and_hide():
            self._hint.setStyleSheet("color:#c0392b;")
            self._hint.hide()
        QTimer.singleShot(2000, _restore_and_hide)

    def show_save_error(self, msg: str) -> None:
        """保存失败时显示提示（由 main_window 调用）。"""
        from PySide6.QtCore import QTimer
        self._hint.setStyleSheet("color:#c0392b;")
        self._hint.setText(msg)
        self._hint.show()
        def _restore_and_hide():
            self._hint.setStyleSheet("color:#c0392b;")
            self._hint.hide()
        QTimer.singleShot(2000, _restore_and_hide)

    def get_edited_steps(self) -> Optional[List[dict]]:
        tokens = [r.text() for r in self._rows if r.text()]
        if not tokens:
            self._show_hint("没有可执行步骤")
            return None
        body = ">".join(tokens)
        raw = "当前屏幕状态：桌面\n目标：用户\n[" + body + "]"
        try:
            _, _, steps = parse_steps(raw)
        except Exception as e:
            self._show_hint(f"步骤解析失败：{e}")
            return None
        # 保留原始步骤中的自定义字段（坐标锁定、模板标记、跳过标记等）
        _PRESERVE_KEYS = {
            "coord_locked", "template_highlighted", "template_path",
            "template_size", "template_threshold",
            "coord", "start_coord", "end_coord",
        }
        for row, step in zip(self._rows, steps):
            if row._step:
                for k in _PRESERVE_KEYS:
                    if k in row._step and row._step[k] is not None and row._step[k] is not False:
                        step[k] = row._step[k]
        self._hint.hide()
        return steps

    def get_edited_steps_preserving(self) -> Optional[List[dict]]:
        steps: List[dict] = []
        _PRESERVE_KEYS = {
            "coord_locked", "template_highlighted", "template_path",
            "template_size", "template_threshold",
            "coord", "start_coord", "end_coord",
        }
        for r in self._rows:
            text = r.text()
            if not text:
                continue
            if not r.is_modified() and r._step:
                steps.append(r._step)
            else:
                raw = f"当前屏幕状态：桌面\n目标：用户\n[{text}]"
                try:
                    _, _, parsed = parse_steps(raw)
                    if r._step and parsed:
                        old = r._step
                        for p in parsed:
                            if (p.get("op") == "长输入"
                                    and old.get("op") == "长输入"
                                    and old.get("desc_full")):
                                p["desc_full"] = old["desc_full"]
                                p["text_var"] = old.get("text_var")
                                p["var_type"] = old.get("var_type")
                            # 保留原始步骤中的自定义字段（坐标锁定、模板标记等）
                            for k in _PRESERVE_KEYS:
                                if k in old and old[k] is not None and old[k] is not False:
                                    p[k] = old[k]
                    steps.extend(parsed)
                except Exception as e:
                    self._show_hint(f"步骤解析失败：{e}")
                    return None
        if not steps:
            self._show_hint("没有可执行步骤")
            return None
        self._hint.hide()
        return steps

    def request_model_modify(self) -> None:
        self._title.setText("正在让模型修改步骤…")
        self._hint.hide()

    # ---- 执行模式 ----
    def set_executing(self, on: bool) -> None:
        for r in self._rows:
            r.set_readonly(on)
        self._btn_save.setVisible(not on)
        self._btn_confirm.setVisible(not on)
        self._progress.setVisible(on)
        self._title.setText("执行中…" if on else "执行步骤（可编辑）")

    def set_paused_edit_mode(self, steps: List[dict]) -> int:
        self._paused_mode = True
        color_done = self._settings.get("progress_color_done", "#2E8B57")
        color_pending = self._settings.get("progress_color_pending", "#B0B0B0")
        color_failed = "#e74c3c"
        done_count = 0
        for idx, r in enumerate(self._rows):
            status = steps[idx].get("status", "pending") if idx < len(steps) else "pending"
            if status in ("done", "failed", "skipped", "executing"):
                r.set_readonly(True)
                r.set_highlight(False)
                if status == "done":
                    r.set_status(status, color_done)
                elif status == "failed":
                    r.set_status(status, color_failed)
                else:
                    r.set_status(status, color_pending)
                done_count += 1
            else:
                r.set_readonly(False)
                r.set_highlight(True)
                r.set_status("pending", color_pending)
        # 暂停模式下显示原有按钮：保存和确认执行
        self._btn_save.setVisible(True)
        self._btn_confirm.setVisible(True)
        self._btn_confirm.setText("确认执行")
        # 暂停时隐藏进度条：与执行前的可编辑界面保持一致（执行进度在 overlay 上显示）
        self._progress.setVisible(False)
        remaining = max(0, len(self._rows) - done_count)
        self._title.setText(
            f"已暂停：{done_count} 步已完成，剩余 {remaining} 步可编辑，"
            f"修改后点「确认执行」继续"
        )
        return done_count

    def exit_paused_edit_mode(self) -> None:
        self._paused_mode = False
        for r in self._rows:
            if r.is_highlighted():
                r.set_highlight(False)
                r.set_readonly(True)
        self._btn_confirm.setText("确认执行")
        self._btn_confirm.setVisible(False)
        self._btn_save.setVisible(False)
        self._progress.setVisible(True)
        self._title.setText("执行中…")

    def update_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self._progress.setValue(0)
            return
        self._progress.setRange(0, total)
        self._progress.setValue(done)
        self._progress.setFormat(f"{done}/{total}")

    def set_step_status(self, index: int, status: str, note: str = "") -> None:
        if 0 <= index < len(self._rows):
            color_done = self._settings.get("progress_color_done", "#2E8B57")
            color_pending = self._settings.get("progress_color_pending", "#B0B0B0")
            if status == "done":
                self._rows[index].set_status(status, color_done)
            else:
                self._rows[index].set_status(status, color_pending)
            # 视觉模型输出坐标后（执行中/完成），自动刷新坐标显示
            if status in ("executing", "done"):
                self._rows[index].update_coord_from_step()

    def refresh_step_coord(self, index: int) -> None:
        """外部（worker 视觉定位完成后）调用：刷新指定行的坐标显示。"""
        if 0 <= index < len(self._rows):
            self._rows[index].update_coord_from_step()

    def set_step_mark(self, index: int, mark: str, color: str) -> None:
        if 0 <= index < len(self._rows):
            self._rows[index]._mark.setText(mark)
            self._rows[index]._mark.setStyleSheet(f"color:{color};font-weight:bold;")

    def reload_settings(self) -> None:
        self._settings = app_config.load_settings()

    def clear_steps(self) -> None:
        self._clear_rows()
        self._progress.setValue(0)
