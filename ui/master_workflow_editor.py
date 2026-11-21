# -*- coding: utf-8 -*-
"""母工作流块式编辑器：采用类似 Python 代码缩进的方式编排节点。

支持块类型：
- 定时器（timer）：等待指定时间后继续
- 工作流（workflow）：选择已有工作流或创建新的
- 脚本（script）：选择脚本库中的脚本
- 条件判断（condition）：if/elif/else，支持模板匹配等条件
- 循环（loop）：while 循环，支持模板匹配或次数+间隔
- continue：跳过当前循环剩余部分
- break：退出当前循环

编排界面通过右键菜单添加/管理块，层次分明。
"""

from __future__ import annotations

import copy
import os
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtGui import QPixmap, QAction, QCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QPushButton, QLabel, QFrame, QMenu, QMessageBox,
    QInputDialog, QDialog, QFormLayout, QSpinBox, QCheckBox,
    QLineEdit, QComboBox, QPlainTextEdit, QFileDialog,
    QTimeEdit, QListWidget, QDialogButtonBox, QWidgetAction,
    QToolButton, QTextEdit, QSizePolicy,
)

import app_config
import manual_positioner

# 块类型常量
BLOCK_TIMER = "timer"
BLOCK_WORKFLOW = "workflow"
BLOCK_SCRIPT = "script"
BLOCK_CONDITION = "condition"
BLOCK_LOOP = "loop"
BLOCK_CONTINUE = "continue"
BLOCK_BREAK = "break"

# 块类型显示名称
BLOCK_LABELS = {
    BLOCK_TIMER: "定时器",
    BLOCK_WORKFLOW: "工作流",
    BLOCK_SCRIPT: "脚本",
    BLOCK_CONDITION: "条件判断",
    BLOCK_LOOP: "循环",
    BLOCK_CONTINUE: "continue",
    BLOCK_BREAK: "break",
}

# 块类型颜色
BLOCK_COLORS = {
    BLOCK_TIMER: "#e67e22",
    BLOCK_WORKFLOW: "#3498db",
    BLOCK_SCRIPT: "#8e44ad",
    BLOCK_CONDITION: "#27ae60",
    BLOCK_LOOP: "#2c3e50",
    BLOCK_CONTINUE: "#7f8c8d",
    BLOCK_BREAK: "#e74c3c",
}

# 每层缩进像素
INDENT_PX = 32


def new_block(block_type: str, indent: int = 0) -> Dict[str, Any]:
    """创建一个新的块 dict。"""
    block: Dict[str, Any] = {
        "type": block_type,
        "indent": indent,
        "data": {},
    }
    if block_type == BLOCK_TIMER:
        block["data"] = {
            "wait_type": "duration",  # "duration" | "until"
            "duration": 10,
            "time": "09:00",
        }
    elif block_type == BLOCK_WORKFLOW:
        block["data"] = {
            "workflow_id": "",
            "goal": "",
            "label": "",
            "inline_steps": [],
            "inline_goal": "",
        }
    elif block_type == BLOCK_SCRIPT:
        block["data"] = {
            "script_id": "",
            "label": "",
            "input_vars": {},
            "output_vars": {},
        }
    elif block_type == BLOCK_CONDITION:
        block["data"] = {
            "branch": "if",  # "if" | "elif" | "else"
            "condition_type": "template_match",  # "template_match" | "always" | "success" | "failure"
            "template_path": "",
            "template_size": 50,
            "template_threshold": 0.8,
            "not_condition": False,
            "template_retry_count": 0,
            "template_retry_interval": 5,
        }
    elif block_type == BLOCK_LOOP:
        block["data"] = {
            "loop_type": "count",  # "count" | "template_match"
            "count": 10,
            "interval": 1,
            "template_path": "",
            "template_size": 50,
            "template_threshold": 0.8,
            "not_condition": False,
            "template_retry_count": 0,
            "template_retry_interval": 5,
        }
    elif block_type == BLOCK_CONTINUE:
        block["data"] = {}
    elif block_type == BLOCK_BREAK:
        block["data"] = {}
    return block


def get_child_blocks(blocks: List[Dict], parent_indent: int) -> List[Dict]:
    """获取指定缩进级别的所有子块（下一级缩进的块）。"""
    children = []
    for b in blocks:
        if b.get("indent", 0) == parent_indent + 1:
            children.append(b)
        elif b.get("indent", 0) <= parent_indent:
            break
    return children


def get_blocks_at_indent(blocks: List[Dict], indent: int, start_idx: int = 0) -> List[Dict]:
    """从指定位置获取指定缩进级别的所有连续块。"""
    result = []
    for i in range(start_idx, len(blocks)):
        if blocks[i].get("indent", 0) == indent:
            result.append(blocks[i])
        elif blocks[i].get("indent", 0) < indent and result:
            break
    return result


# ---------------------------------------------------------------------------
# 块编辑器控件
# ---------------------------------------------------------------------------


def _collect_screenshot_ids(obj, out: set) -> None:
    """递归收集 dict/list 结构中所有 screenshot_id。"""
    if isinstance(obj, dict):
        sid = obj.get("screenshot_id")
        if sid:
            out.add(sid)
        for v in obj.values():
            _collect_screenshot_ids(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_screenshot_ids(item, out)


class _BlockRow(QFrame):
    """单个块的行控件。"""

    changed = Signal()  # 内容变更（不重建控件树）
    structureChanged = Signal()  # 结构变更（缩进等，需重建控件树）
    deleteRequested = Signal(object)  # self
    insertAboveRequested = Signal(object)  # (block, ref_row, above)
    insertBelowRequested = Signal(object)  # (block, ref_row, above)
    executeRequested = Signal(object, list, str, bool)  # (self, steps, goal, replan_enabled)

    def __init__(self, block: Dict[str, Any], parent=None,
                 screenshots_dir: Optional[str] = None,
                 row_number: int = 0):
        super().__init__(parent)
        self._block = block
        self._row_number = row_number
        self._editing = False
        self._screenshots_dir = screenshots_dir or app_config.templates_fallback_dir()
        self._build_ui()

    def _build_ui(self) -> None:
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("""
            _BlockRow{background:white;border:1px solid #ddd;border-radius:6px;}
            _BlockRow:hover{border:1px solid #3498db;}
            QToolTip{background-color:#ffffff;color:#000000;
                     border:1px solid #b0b0b0;border-radius:4px;padding:4px 8px;}
        """)

        main_lay = QHBoxLayout(self)
        main_lay.setContentsMargins(8, 6, 8, 6)
        main_lay.setSpacing(8)

        # 序号（可点击切换测试选中状态），选中时画黄圈表示测试
        test_selected = self._block.get("data", {}).get("test_selected", False)
        self._num_btn = QPushButton(str(self._row_number))
        self._num_btn.setFixedSize(22, 22)
        self._num_btn.setFlat(True)
        self._num_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_num_style(test_selected)
        self._num_btn.clicked.connect(self._toggle_test_selected)
        main_lay.addWidget(self._num_btn)

        # 缩进占位
        indent = self._block.get("indent", 0)
        if indent > 0:
            indent_spacer = QWidget()
            indent_spacer.setFixedWidth(indent * INDENT_PX)
            indent_spacer.setStyleSheet("background:transparent;")
            main_lay.addWidget(indent_spacer)

        # 类型标签
        btype = self._block.get("type", BLOCK_WORKFLOW)
        label = BLOCK_LABELS.get(btype, btype)
        color = BLOCK_COLORS.get(btype, "#3498db")
        type_label = QLabel(label)
        type_label.setFixedWidth(70)
        type_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        type_label.setStyleSheet(
            f"background:{color};color:white;border-radius:4px;"
            f"padding:3px 6px;font-weight:bold;font-size:11px;"
        )
        main_lay.addWidget(type_label)

        # 内容区域
        self._content_widget = QWidget()
        self._content_lay = QHBoxLayout(self._content_widget)
        self._content_lay.setContentsMargins(0, 0, 0, 0)
        self._content_lay.setSpacing(4)
        self._build_content()
        main_lay.addWidget(self._content_widget, 1)

        # 删除按钮
        btn_del = QPushButton("✕")
        btn_del.setFixedSize(22, 22)
        btn_del.setStyleSheet(
            "QPushButton{background:#e74c3c;color:white;border-radius:11px;"
            "font-weight:bold;font-size:10px;}"
            "QPushButton:hover{background:#ff5c41;}"
        )
        btn_del.clicked.connect(lambda: self.deleteRequested.emit(self))
        main_lay.addWidget(btn_del)

        # 右键菜单
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def _update_num_style(self, test_selected: bool) -> None:
        """设置序号按钮样式：测试选中时画黄色圆圈。"""
        if test_selected:
            self._num_btn.setStyleSheet(
                "QPushButton{color:#ffffff;font-size:11px;font-weight:bold;"
                "background:#f1c40f;border-radius:11px;}"
                "QPushButton:hover{background:#e67e22;}"
            )
        else:
            self._num_btn.setStyleSheet(
                "QPushButton{color:#999;font-size:11px;font-weight:bold;"
                "background:transparent;border-radius:11px;}"
                "QPushButton:hover{color:#e67e22;}"
            )

    def _toggle_test_selected(self) -> None:
        """切换测试选中状态。"""
        data = self._block.setdefault("data", {})
        current = data.get("test_selected", False)
        data["test_selected"] = not current
        self._update_num_style(not current)
        self.changed.emit()

    def _build_content(self) -> None:
        """根据块类型构建内容显示。"""
        btype = self._block.get("type", "")
        data = self._block.get("data", {})

        # 清空已有内容
        while self._content_lay.count():
            item = self._content_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if btype == BLOCK_TIMER:
            self._build_timer_content(data)
        elif btype == BLOCK_WORKFLOW:
            self._build_workflow_content(data)
        elif btype == BLOCK_SCRIPT:
            self._build_script_content(data)
        elif btype == BLOCK_CONDITION:
            self._build_condition_content(data)
        elif btype == BLOCK_LOOP:
            self._build_loop_content(data)
        elif btype in (BLOCK_CONTINUE, BLOCK_BREAK):
            info = QLabel("")
            self._content_lay.addWidget(info)

    def _build_timer_content(self, data: Dict) -> None:
        wait_type = data.get("wait_type", "duration")
        combo = QComboBox()
        combo.wheelEvent = lambda e: None
        combo.addItems(["等待时长", "等待到指定时间"])
        combo.setCurrentIndex(0 if wait_type == "duration" else 1)
        combo.setStyleSheet(
            "QComboBox{background:white;border:1px solid #ccc;border-radius:4px;"
            "padding:2px 4px;font-size:12px;}"
        )

        def _on_wait_type_changed(idx: int):
            data["wait_type"] = "duration" if idx == 0 else "until"
            self._rebuild_timer_content()

        combo.currentIndexChanged.connect(_on_wait_type_changed)
        self._content_lay.addWidget(combo)

        self._timer_params = QWidget()
        self._timer_params_lay = QHBoxLayout(self._timer_params)
        self._timer_params_lay.setContentsMargins(0, 0, 0, 0)
        self._timer_params_lay.setSpacing(4)
        self._rebuild_timer_content()
        self._content_lay.addWidget(self._timer_params, 1)

    def _rebuild_timer_content(self) -> None:
        while self._timer_params_lay.count():
            item = self._timer_params_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        data = self._block.get("data", {})
        wait_type = data.get("wait_type", "duration")

        if wait_type == "duration":
            spin = QSpinBox()
            spin.setRange(1, 86400)
            spin.setValue(data.get("duration", 10))
            spin.setSuffix(" 秒")
            spin.setStyleSheet(
                "QSpinBox{border:1px solid #ccc;border-radius:4px;"
                "padding:2px 4px;font-size:12px;min-width:80px;}"
            )
            spin.valueChanged.connect(lambda v: data.update({"duration": v}))
            self._timer_params_lay.addWidget(QLabel("等待"))
            self._timer_params_lay.addWidget(spin)
            self._timer_params_lay.addStretch(1)
        else:
            te = QTimeEdit()
            te.setDisplayFormat("HH:mm")
            saved = data.get("time", "09:00")
            try:
                parts = saved.split(":")
                h, m = int(parts[0]), int(parts[1])
                from PySide6.QtCore import QTime
                te.setTime(QTime(h, m))
            except (ValueError, IndexError):
                from PySide6.QtCore import QTime
                te.setTime(QTime(9, 0))
            te.setStyleSheet(
                "QTimeEdit{border:1px solid #ccc;border-radius:4px;"
                "padding:2px 4px;font-size:12px;}"
            )
            te.timeChanged.connect(lambda t: data.update({"time": t.toString("HH:mm")}))
            self._timer_params_lay.addWidget(QLabel("等待到"))
            self._timer_params_lay.addWidget(te)
            self._timer_params_lay.addStretch(1)

    def _build_workflow_content(self, data: Dict) -> None:
        """构建可折叠的步骤列表，类似代理面板的编辑逻辑。"""
        from ui.agent_panel import StepRow, _op_and_param_from_step

        container = QWidget()
        vl = QVBoxLayout(container)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(4)

        # ---- 头部：块名称 + 折叠按钮 + 重规划 + 导入工作流 + 步数 ----
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)

        # 块名称输入框
        self._wf_name_edit = QLineEdit()
        self._wf_name_edit.setPlaceholderText("代码块名称...")
        self._wf_name_edit.setText(data.get("block_name", ""))
        self._wf_name_edit.setStyleSheet(
            "QLineEdit{background:white;border:1px solid #ccc;border-radius:4px;"
            "padding:2px 6px;font-size:12px;font-weight:bold;min-width:80px;max-width:150px;}"
        )
        self._wf_name_edit.textChanged.connect(self._on_wf_name_changed)
        hl.addWidget(self._wf_name_edit)

        self._wf_collapse_btn = QToolButton()
        self._wf_collapse_btn.setArrowType(Qt.ArrowType.DownArrow)
        self._wf_collapse_btn.setCheckable(True)
        self._wf_collapse_btn.setChecked(True)
        self._wf_collapse_btn.setStyleSheet(
            "QToolButton{border:none;background:transparent;}"
        )
        self._wf_collapse_btn.toggled.connect(self._on_wf_collapse_toggled)
        hl.addWidget(self._wf_collapse_btn)

        # 重规划按钮
        self._wf_replan_btn = QPushButton("重规划")
        replan_enabled = data.get("replan_enabled", False)
        self._wf_replan_btn.setCheckable(True)
        self._wf_replan_btn.setChecked(replan_enabled)
        self._update_replan_btn_style()
        self._wf_replan_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._wf_replan_btn.customContextMenuRequested.connect(self._on_replan_right_click)
        self._wf_replan_btn.clicked.connect(self._on_replan_toggled)
        hl.addWidget(self._wf_replan_btn)

        import_btn = QPushButton("导入工作流 ▼")
        import_btn.setStyleSheet(
            "QPushButton{background:#3498db;color:white;border-radius:3px;"
            "padding:2px 10px;font-size:11px;}"
            "QPushButton:hover{background:#2980b9;}"
        )
        import_btn.clicked.connect(self._on_wf_import)
        hl.addWidget(import_btn)

        steps = data.get("inline_steps", [])
        self._wf_step_count = QLabel(f"共 {len(steps)} 步")
        self._wf_step_count.setStyleSheet("color:#888;font-size:11px;")
        hl.addWidget(self._wf_step_count)
        hl.addStretch(1)

        vl.addWidget(header)

        # ---- 步骤列表区域（可折叠） ----
        self._wf_step_area = QWidget()
        self._wf_step_lay = QVBoxLayout(self._wf_step_area)
        self._wf_step_lay.setContentsMargins(0, 0, 0, 0)
        self._wf_step_lay.setSpacing(2)

        self._wf_step_rows = []
        self._rebuild_wf_steps()

        vl.addWidget(self._wf_step_area)

        self._content_lay.addWidget(container, 1)

    def _on_wf_collapse_toggled(self, checked: bool) -> None:
        """折叠/展开步骤列表。"""
        self._wf_step_area.setVisible(checked)
        self._wf_collapse_btn.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )

    def _on_wf_name_changed(self, text: str) -> None:
        """块名称变更。"""
        self._block.setdefault("data", {})["block_name"] = text.strip()
        self.changed.emit()

    def _update_replan_btn_style(self) -> None:
        """更新重规划按钮样式。"""
        enabled = self._wf_replan_btn.isChecked()
        if enabled:
            self._wf_replan_btn.setStyleSheet(
                "QPushButton{background:#27ae60;color:white;border-radius:3px;"
                "padding:2px 10px;font-size:11px;font-weight:bold;}"
                "QPushButton:hover{background:#2ecc71;}"
            )
        else:
            self._wf_replan_btn.setStyleSheet(
                "QPushButton{background:#95a5a6;color:white;border-radius:3px;"
                "padding:2px 10px;font-size:11px;}"
                "QPushButton:hover{background:#b0b0b0;}"
            )

    def _show_white_text_dialog(self, title: str, label: str,
                                 current_text: str = "") -> tuple:
        """显示白底文本输入对话框，替换 QInputDialog 的默认样式。

        Returns:
            (text, accepted) — accepted 为 True 表示用户点击了 OK。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(480, 260)
        dlg.setStyleSheet(
            "QDialog{background:white;} "
            "QLabel{color:#333;font-size:13px;} "
            "QTextEdit{background:white;border:1px solid #ccc;"
            "border-radius:4px;padding:6px;color:#333;font-size:12px;} "
            "QPushButton{background:#3498db;color:white;border:none;"
            "border-radius:4px;padding:6px 20px;font-size:12px;}"
            "QPushButton:hover{background:#2980b9;}"
            "QPushButton#btn_cancel{background:#95a5a6;}"
            "QPushButton#btn_cancel:hover{background:#7f8c8d;}"
        )

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        lbl = QLabel(label)
        layout.addWidget(lbl)

        editor = QTextEdit()
        editor.setPlainText(current_text)
        layout.addWidget(editor, 1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("btn_cancel")
        btn_cancel.clicked.connect(dlg.reject)
        btn_layout.addWidget(btn_cancel)
        btn_ok = QPushButton("确定")
        btn_ok.clicked.connect(dlg.accept)
        btn_layout.addWidget(btn_ok)
        layout.addLayout(btn_layout)

        result = dlg.exec()
        if result == QDialog.DialogCode.Accepted:
            return editor.toPlainText(), True
        return "", False

    def _on_replan_toggled(self, checked: bool) -> None:
        """重规划按钮点击：首次高亮并显示任务描述窗口，再次点击变灰。"""
        data = self._block.setdefault("data", {})
        if checked:
            # 首次点击：高亮，弹出任务描述窗口
            data["replan_enabled"] = True
            self._update_replan_btn_style()
            # 弹出白底任务描述窗口
            current_goal = data.get("replan_goal", "")
            new_goal, ok = self._show_white_text_dialog(
                "工作流任务描述",
                "请输入工作流任务描述（规划模型将据此生成步骤）：",
                current_goal
            )
            if ok and new_goal.strip():
                data["replan_goal"] = new_goal.strip()
            elif ok and not new_goal.strip():
                # 用户确认但未输入内容，取消启用
                data["replan_enabled"] = False
                self._wf_replan_btn.setChecked(False)
                self._update_replan_btn_style()
                return
        else:
            # 再次点击：变灰
            data["replan_enabled"] = False
            self._update_replan_btn_style()
        self.changed.emit()

    def _on_replan_right_click(self, pos) -> None:
        """重规划按钮右键：修改任务描述。"""
        data = self._block.setdefault("data", {})
        if not data.get("replan_enabled", False):
            return
        current_goal = data.get("replan_goal", "")
        new_goal, ok = self._show_white_text_dialog(
            "修改任务描述",
            "修改工作流任务描述：",
            current_goal
        )
        if ok and new_goal.strip():
            data["replan_goal"] = new_goal.strip()
            self.changed.emit()

    def _rebuild_wf_steps(self) -> None:
        """重建步骤列表中的行。"""
        from ui.agent_panel import StepRow, _op_and_param_from_step

        # 清除旧行
        for r in getattr(self, '_wf_step_rows', []):
            self._wf_step_lay.removeWidget(r)
            r.deleteLater()
        self._wf_step_rows = []

        # 移除旧按钮栏容器及内部按钮
        old_bar = getattr(self, '_wf_btn_bar', None)
        if old_bar:
            self._wf_step_lay.removeWidget(old_bar)
            old_bar.deleteLater()
        for attr in ('_wf_add_btn', '_wf_exec_btn'):
            btn = getattr(self, attr, None)
            if btn and btn.parent() is self._wf_step_area:
                self._wf_step_lay.removeWidget(btn)
                btn.deleteLater()

        data = self._block.get("data", {})
        steps = data.get("inline_steps", [])

        # 步骤行
        for s in steps:
            op, param = _op_and_param_from_step(s)
            row = StepRow(len(self._wf_step_rows), op, param, step=s,
                          screenshots_dir=self._screenshots_dir)
            self._connect_wf_step_row(row)
            self._wf_step_lay.addWidget(row)
            self._wf_step_rows.append(row)

        # 底部按钮栏（右侧，小按钮）
        self._wf_btn_bar = btn_bar = QWidget()
        btn_lay = QHBoxLayout(btn_bar)
        btn_lay.setContentsMargins(0, 2, 0, 0)
        btn_lay.setSpacing(4)
        btn_lay.addStretch(1)

        self._wf_add_btn = QPushButton("+ 添加步骤")
        self._wf_add_btn.setFixedHeight(22)
        self._wf_add_btn.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;border-radius:3px;"
            "padding:2px 10px;font-size:11px;}"
            "QPushButton:hover{background:#2ecc71;}"
        )
        self._wf_add_btn.clicked.connect(self._on_wf_add_step)
        btn_lay.addWidget(self._wf_add_btn)

        self._wf_exec_btn = QPushButton("执行")
        self._wf_exec_btn.setFixedHeight(22)
        self._wf_exec_btn.setStyleSheet(
            "QPushButton{background:#e67e22;color:white;border-radius:3px;"
            "padding:2px 10px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#f39c12;}"
        )
        self._wf_exec_btn.clicked.connect(self._on_wf_execute)
        btn_lay.addWidget(self._wf_exec_btn)

        self._wf_step_lay.addWidget(btn_bar)

        # 更新步数
        self._wf_step_count.setText(f"共 {len(steps)} 步")

    def _connect_wf_step_row(self, row) -> None:
        """连接步骤行的信号。"""
        row.insertAboveRequested.connect(lambda: self._on_wf_insert_above(row))
        row.insertBelowRequested.connect(lambda: self._on_wf_insert_below(row))
        row.deleteRequested.connect(lambda: self._on_wf_delete_row(row))
        row.rowChanged.connect(self._on_wf_step_changed)

    def _on_wf_insert_above(self, row) -> None:
        from step_model import new_step
        idx = self._wf_step_rows.index(row)
        data = self._block.setdefault("data", {})
        steps = data.setdefault("inline_steps", [])
        steps.insert(idx, new_step(idx, "单击", "单击-"))
        self._rebuild_wf_steps()
        self.changed.emit()

    def _on_wf_insert_below(self, row) -> None:
        from step_model import new_step
        idx = self._wf_step_rows.index(row) + 1
        data = self._block.setdefault("data", {})
        steps = data.setdefault("inline_steps", [])
        steps.insert(idx, new_step(idx, "单击", "单击-"))
        self._rebuild_wf_steps()
        self.changed.emit()

    def _on_wf_delete_row(self, row) -> None:
        idx = self._wf_step_rows.index(row)
        data = self._block.setdefault("data", {})
        steps = data.get("inline_steps", [])
        if 0 <= idx < len(steps):
            removed = steps.pop(idx)
            # 步骤删除时连带清理占用的模板/全图
            app_config.delete_screenshot_files(
                self._screenshots_dir, removed.get("screenshot_id"))
        self._rebuild_wf_steps()
        self.changed.emit()

    def _on_wf_step_changed(self) -> None:
        self.changed.emit()

    def _on_wf_add_step(self) -> None:
        from step_model import new_step
        data = self._block.setdefault("data", {})
        steps = data.setdefault("inline_steps", [])
        steps.append(new_step(len(steps), "单击", "单击-"))
        self._rebuild_wf_steps()
        self.changed.emit()

    def _on_wf_execute(self) -> None:
        """收集当前步骤 UI 状态保存到 block data，并触发母工作流执行。"""
        from step_model import parse_steps

        data = self._block.setdefault("data", {})
        replan_enabled = data.get("replan_enabled", False)
        tokens = [r.text() for r in self._wf_step_rows if r.text()]
        if not tokens:
            data["inline_steps"] = []
            self._rebuild_wf_steps()
            self.changed.emit()
            self.executeRequested.emit(self, [], data.get("goal", "") or "内联工作流",
                                       replan_enabled)
            return

        body = ">".join(tokens)
        raw = "当前屏幕状态：桌面\n目标：内联工作流\n[" + body + "]"
        try:
            _, _, steps = parse_steps(raw)
        except Exception as e:
            from PySide6.QtWidgets import QToolTip
            QToolTip.showText(
                self._wf_exec_btn.mapToGlobal(self._wf_exec_btn.rect().center()),
                f"步骤解析失败：{e}", self._wf_exec_btn, msec=3000
            )
            return

        # 保留自定义字段（坐标、模板、跳过标记等）
        _PRESERVE_KEYS = {
            "coord_locked", "template_highlighted", "template_path",
            "template_size", "template_threshold",
            "coord", "start_coord", "end_coord", "user_skip",
        }
        for row, step in zip(self._wf_step_rows, steps):
            if row._step:
                for k in _PRESERVE_KEYS:
                    if k in row._step and row._step[k] is not None and row._step[k] is not False:
                        step[k] = row._step[k]
            if row._user_skip:
                step["user_skip"] = True

        data["inline_steps"] = steps
        self._rebuild_wf_steps()
        self.changed.emit()
        # 仅执行该工作流块的步骤，不执行整个母工作流
        goal = data.get("goal", "") or "内联工作流"
        replan_enabled = data.get("replan_enabled", False)
        self.executeRequested.emit(self, steps, goal, replan_enabled)

    def _on_wf_import(self) -> None:
        """从已有工作流导入步骤，追加到当前列表末尾。"""
        from step_model import parse_steps

        workflows = app_config.list_workflows()
        if not workflows:
            QMessageBox.information(self, "提示", "没有任何工作流可供导入。")
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;color:black;border:1px solid #ccc;padding:4px;}"
            "QMenu::item{padding:6px 24px;}"
            "QMenu::item:selected{background:#3498db;color:white;}"
        )
        for wf in workflows:
            label = wf.get("label", "未命名")
            act = menu.addAction(f"  {label}")
            act.setData(wf.get("id", ""))

        action = menu.exec(QCursor.pos())
        if action is None:
            return

        wf_id = action.data()
        wf = app_config.load_workflow(wf_id)
        if not wf:
            return

        steps_text = wf.get("steps", "")
        if not steps_text:
            QMessageBox.information(self, "提示", "该工作流没有步骤。")
            return

        # steps 可能是列表（dict 格式）或字符串（文本格式）
        if isinstance(steps_text, list):
            imported_steps = list(steps_text)
        else:
            try:
                _, _, imported_steps = parse_steps(str(steps_text))
            except Exception as e:
                QMessageBox.warning(self, "解析失败", f"步骤解析失败：{e}")
                return

        if not imported_steps:
            QMessageBox.information(self, "提示", "未解析到有效步骤。")
            return

        data = self._block.setdefault("data", {})
        existing = data.setdefault("inline_steps", [])
        existing.extend(imported_steps)
        self._rebuild_wf_steps()
        self.changed.emit()

    def _build_script_content(self, data: Dict) -> None:
        combo = QComboBox()
        combo.wheelEvent = lambda e: None
        combo.setStyleSheet(
            "QComboBox{background:white;border:1px solid #ccc;border-radius:4px;"
            "padding:2px 4px;font-size:12px;}"
        )
        scripts = app_config.list_scripts()
        for s in scripts:
            combo.addItem(f"{s.get('label', '未命名')}", s.get("id", ""))
        current_id = data.get("script_id", "")
        if current_id:
            for i in range(combo.count()):
                if combo.itemData(i) == current_id:
                    combo.setCurrentIndex(i)
                    break
        combo.currentIndexChanged.connect(
            lambda idx: self._on_script_selected(combo.itemData(idx))
        )
        self._content_lay.addWidget(combo, 1)

        label_text = data.get("label", "")
        if not label_text and data.get("script_id"):
            s = app_config.load_script(data["script_id"])
            if s:
                label_text = s.get("label", "")
        if label_text:
            info = QLabel(label_text)
            info.setStyleSheet("color:#555;font-size:12px;")
            self._content_lay.addWidget(info)

    def _on_script_selected(self, script_id: Optional[str]) -> None:
        data = self._block.setdefault("data", {})
        data["script_id"] = script_id or ""
        if script_id:
            s = app_config.load_script(script_id)
            if s:
                data["label"] = s.get("label", "未命名")
        self._build_content()

    def _build_condition_content(self, data: Dict) -> None:
        branch = data.get("branch", "if")
        branch_combo = QComboBox()
        branch_combo.wheelEvent = lambda e: None
        branch_combo.addItems(["if", "elif", "else"])
        branch_combo.setCurrentIndex(["if", "elif", "else"].index(branch)
                                     if branch in ("if", "elif", "else") else 0)
        branch_combo.setStyleSheet(
            "QComboBox{background:white;border:1px solid #ccc;border-radius:4px;"
            "padding:2px 4px;font-size:12px;font-weight:bold;}"
        )
        branch_combo.currentIndexChanged.connect(
            lambda idx: data.update({"branch": ["if", "elif", "else"][idx]})
        )
        self._content_lay.addWidget(branch_combo)

        if branch != "else":
            cond_type = data.get("condition_type", "template_match")
            cond_combo = QComboBox()
            cond_combo.wheelEvent = lambda e: None
            cond_combo.addItems(["模板匹配", "始终", "成功", "失败"])
            cond_map = {"template_match": 0, "always": 1, "success": 2, "failure": 3}
            cond_combo.setCurrentIndex(cond_map.get(cond_type, 0))
            cond_combo.setStyleSheet(
                "QComboBox{background:white;border:1px solid #ccc;border-radius:4px;"
                "padding:2px 4px;font-size:12px;}"
            )

            def _on_cond_changed(idx: int):
                types = ["template_match", "always", "success", "failure"]
                data["condition_type"] = types[idx]
                self._rebuild_condition_template_ui()

            cond_combo.currentIndexChanged.connect(_on_cond_changed)
            self._content_lay.addWidget(cond_combo)

            # 取反
            not_chk = QCheckBox("取反")
            not_chk.setChecked(data.get("not_condition", False))
            not_chk.setStyleSheet("font-size:11px;color:#555;")
            not_chk.toggled.connect(lambda c: data.update({"not_condition": c}))
            self._content_lay.addWidget(not_chk)

            # 模板路径（仅模板匹配时显示）
            self._cond_tpl_widget = QWidget()
            self._cond_tpl_lay = QVBoxLayout(self._cond_tpl_widget)
            self._cond_tpl_lay.setContentsMargins(0, 0, 0, 0)
            self._cond_tpl_lay.setSpacing(4)
            self._cond_preview_highlighted = False
            self._cond_preview_label = None
            self._rebuild_condition_template_ui()
            self._content_lay.addWidget(self._cond_tpl_widget, 1)
        else:
            # else 分支没有条件
            info = QLabel("（否则执行）")
            info.setStyleSheet("color:#888;font-size:12px;font-style:italic;")
            self._content_lay.addWidget(info)
            self._content_lay.addStretch(1)

    def _rebuild_condition_template_ui(self) -> None:
        while self._cond_tpl_lay.count():
            item = self._cond_tpl_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        data = self._block.get("data", {})
        cond_type = data.get("condition_type", "template_match")
        if cond_type == "template_match":
            # 第一行：控件行
            ctrl_widget = QWidget()
            ctrl_lay = QHBoxLayout(ctrl_widget)
            ctrl_lay.setContentsMargins(0, 0, 0, 0)
            ctrl_lay.setSpacing(4)

            edit = QLineEdit()
            edit.setText(data.get("template_path", ""))
            edit.setPlaceholderText("模板图片路径...")
            edit.setStyleSheet(
                "QLineEdit{border:1px solid #ccc;border-radius:4px;"
                "padding:2px 4px;font-size:12px;}"
            )
            edit.textChanged.connect(lambda t: data.update({"template_path": t}))
            ctrl_lay.addWidget(edit, 1)

            btn_browse = QPushButton("选择")
            btn_browse.setStyleSheet(
                "QPushButton{background:#3498db;color:white;border-radius:3px;"
                "padding:2px 8px;font-size:11px;}"
            )
            btn_browse.clicked.connect(lambda: self._browse_template(edit))
            ctrl_lay.addWidget(btn_browse)

            btn_frame = QPushButton("框出")
            btn_frame.setStyleSheet(
                "QPushButton{background:#e67e22;color:white;border-radius:3px;"
                "padding:2px 8px;font-size:11px;}"
            )
            btn_frame.clicked.connect(lambda: self._frame_template(edit))
            ctrl_lay.addWidget(btn_frame)

            # 预览按钮（灰色，点击后高亮）
            btn_preview = QPushButton("预览")
            btn_preview.setCheckable(True)
            btn_preview.setChecked(self._cond_preview_highlighted)
            self._update_preview_btn_style(btn_preview)
            btn_preview.clicked.connect(lambda: self._toggle_cond_preview())
            ctrl_lay.addWidget(btn_preview)

            # 匹配度
            thresh = QSpinBox()
            thresh.setRange(0, 100)
            thresh.setValue(int(round(data.get("template_threshold", 0.8) * 100)))
            thresh.setSuffix(" %")
            thresh.setStyleSheet(
                "QSpinBox{border:1px solid #ccc;border-radius:4px;"
                "padding:2px 4px;font-size:11px;min-width:60px;}"
            )
            thresh.valueChanged.connect(lambda v: data.update({"template_threshold": v / 100.0}))
            ctrl_lay.addWidget(QLabel("匹配度"))
            ctrl_lay.addWidget(thresh)

            self._cond_tpl_lay.addWidget(ctrl_widget)

            # 第二行：预览图（仅高亮时显示）
            self._cond_preview_label = QLabel()
            self._cond_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._cond_preview_label.setStyleSheet(
                "QLabel{background:#f5f5f5;border:1px solid #ddd;border-radius:4px;"
                "padding:4px;}"
            )
            self._cond_preview_label.setMinimumHeight(160)
            self._cond_preview_label.setSizePolicy(
                QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding))
            if self._cond_preview_highlighted:
                tpl_path = data.get("template_path", "")
                if tpl_path and os.path.isfile(tpl_path):
                    pixmap = QPixmap(tpl_path)
                    if not pixmap.isNull():
                        scaled = pixmap.scaled(200, 150, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation)
                        self._cond_preview_label.setPixmap(scaled)
                        self._cond_preview_label.setToolTip("双击放大")
                        # 双击放大
                        self._cond_preview_label.mouseDoubleClickEvent = (
                            lambda e, p=tpl_path: self._show_enlarged_preview(p))
            self._cond_preview_label.setVisible(self._cond_preview_highlighted)
            self._cond_tpl_lay.addWidget(self._cond_preview_label)
        else:
            self._cond_tpl_lay.addStretch(1)

    def _update_preview_btn_style(self, btn: QPushButton) -> None:
        if btn.isChecked():
            btn.setStyleSheet(
                "QPushButton{background:#27ae60;color:white;border-radius:3px;"
                "padding:2px 8px;font-size:11px;font-weight:bold;}"
                "QPushButton:hover{background:#2ecc71;}"
            )
        else:
            btn.setStyleSheet(
                "QPushButton{background:#95a5a6;color:white;border-radius:3px;"
                "padding:2px 8px;font-size:11px;}"
                "QPushButton:hover{background:#b0b0b0;}"
            )

    def _toggle_cond_preview(self) -> None:
        self._cond_preview_highlighted = not self._cond_preview_highlighted
        self._rebuild_condition_template_ui()

    def _show_enlarged_preview(self, tpl_path: str) -> None:
        if not tpl_path or not os.path.isfile(tpl_path):
            return
        pixmap = QPixmap(tpl_path)
        if pixmap.isNull():
            return
        dlg = QDialog(self.window())
        dlg.setWindowTitle("模板预览")
        dlg.setMinimumSize(400, 300)
        # 放大到合适尺寸，不超过屏幕
        screen = dlg.screen().availableSize()
        max_w = int(screen.width() * 0.8)
        max_h = int(screen.height() * 0.8)
        scaled = pixmap.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        label = QLabel()
        label.setPixmap(scaled)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay = QVBoxLayout(dlg)
        lay.addWidget(label)
        # 显示原图尺寸信息
        info = QLabel(f"原图尺寸: {pixmap.width()} × {pixmap.height()} px")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info.setStyleSheet("color:#888;font-size:11px;")
        lay.addWidget(info)
        dlg.exec()

    def _build_loop_content(self, data: Dict) -> None:
        loop_type = data.get("loop_type", "count")
        type_combo = QComboBox()
        type_combo.addItems(["循环次数", "模板匹配循环"])
        type_combo.setCurrentIndex(0 if loop_type == "count" else 1)
        type_combo.setStyleSheet(
            "QComboBox{background:white;border:1px solid #ccc;border-radius:4px;"
            "padding:2px 4px;font-size:12px;}"
        )

        def _on_loop_type_changed(idx: int):
            data["loop_type"] = "count" if idx == 0 else "template_match"
            self._rebuild_loop_content()

        type_combo.currentIndexChanged.connect(_on_loop_type_changed)
        self._content_lay.addWidget(type_combo)

        self._loop_params = QWidget()
        self._loop_params_lay = QHBoxLayout(self._loop_params)
        self._loop_params_lay.setContentsMargins(0, 0, 0, 0)
        self._loop_params_lay.setSpacing(4)
        self._rebuild_loop_content()
        self._content_lay.addWidget(self._loop_params, 1)

    def _rebuild_loop_content(self) -> None:
        while self._loop_params_lay.count():
            item = self._loop_params_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        data = self._block.get("data", {})
        loop_type = data.get("loop_type", "count")

        if loop_type == "count":
            spin = QSpinBox()
            spin.setRange(1, 9999)
            spin.setValue(data.get("count", 10))
            spin.setStyleSheet(
                "QSpinBox{border:1px solid #ccc;border-radius:4px;"
                "padding:2px 4px;font-size:12px;min-width:60px;}"
            )
            spin.valueChanged.connect(lambda v: data.update({"count": v}))
            self._loop_params_lay.addWidget(QLabel("循环"))
            self._loop_params_lay.addWidget(spin)
            self._loop_params_lay.addWidget(QLabel("次"))

            interval_spin = QSpinBox()
            interval_spin.setRange(0, 86400)
            interval_spin.setValue(data.get("interval", 1))
            interval_spin.setSuffix(" 秒")
            interval_spin.setStyleSheet(
                "QSpinBox{border:1px solid #ccc;border-radius:4px;"
                "padding:2px 4px;font-size:12px;min-width:70px;}"
            )
            interval_spin.valueChanged.connect(lambda v: data.update({"interval": v}))
            self._loop_params_lay.addWidget(QLabel("间隔"))
            self._loop_params_lay.addWidget(interval_spin)
            self._loop_params_lay.addStretch(1)
        else:
            edit = QLineEdit()
            edit.setText(data.get("template_path", ""))
            edit.setPlaceholderText("模板图片路径...")
            edit.setStyleSheet(
                "QLineEdit{border:1px solid #ccc;border-radius:4px;"
                "padding:2px 4px;font-size:12px;}"
            )
            edit.textChanged.connect(lambda t: data.update({"template_path": t}))
            self._loop_params_lay.addWidget(QLabel("匹配到"))
            self._loop_params_lay.addWidget(edit, 1)

            btn_browse = QPushButton("浏览")
            btn_browse.setStyleSheet(
                "QPushButton{background:#3498db;color:white;border-radius:3px;"
                "padding:2px 8px;font-size:11px;}"
            )
            btn_browse.clicked.connect(lambda: self._browse_template(edit))
            self._loop_params_lay.addWidget(btn_browse)

            btn_frame = QPushButton("框出")
            btn_frame.setStyleSheet(
                "QPushButton{background:#e67e22;color:white;border-radius:3px;"
                "padding:2px 8px;font-size:11px;}"
            )
            btn_frame.clicked.connect(lambda: self._frame_template(edit))
            self._loop_params_lay.addWidget(btn_frame)

            # 取反
            not_chk = QCheckBox("消失时退出")
            not_chk.setChecked(data.get("not_condition", False))
            not_chk.setStyleSheet("font-size:11px;color:#555;")
            not_chk.toggled.connect(lambda c: data.update({"not_condition": c}))
            self._loop_params_lay.addWidget(not_chk)

            self._loop_params_lay.addStretch(1)

    def _browse_template(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择模板图片", "", "图片文件 (*.png *.jpg *.jpeg *.bmp)"
        )
        if path:
            edit.setText(path)

    def _frame_template(self, edit: QLineEdit) -> None:
        """框出模板。"""
        try:
            top = self.window() if self.window() else self
            top.setWindowOpacity(0.0)
        except Exception:
            pass
        try:
            data = self._block.setdefault("data", {})
            # 先获取或生成截图 ID，用作文件名确保唯一
            sid = data.setdefault("screenshot_id", app_config.new_screenshot_id())
            result = manual_positioner.pick_template_area(
                save_dir=self._screenshots_dir, filename=sid)
            if result is not None:
                tpl_path, _, _, size_px = result
                edit.setText(tpl_path)
                data["template_path"] = tpl_path
                data["template_size"] = size_px
        finally:
            try:
                top = self.window() if self.window() else self
                top.setWindowOpacity(1.0)
                top.raise_()
                top.activateWindow()
            except Exception:
                pass

    def _on_context_menu(self, pos) -> None:
        """右键菜单：根据鼠标位置决定插入方向。"""
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;color:black;border:1px solid #ccc;padding:4px;}"
            "QMenu::item{padding:6px 24px;}"
            "QMenu::item:selected{background:#3498db;color:white;}"
        )

        # 判断鼠标在左侧还是右侧
        widget_width = self.width()
        insert_above = pos.x() < widget_width // 2

        # 插入块菜单
        add_menu = menu.addMenu("➕ 插入块")
        add_menu.setStyleSheet(
            "QMenu{background:white;color:black;border:1px solid #ccc;padding:4px;}"
            "QMenu::item{padding:6px 24px;}"
            "QMenu::item:selected{background:#3498db;color:white;}"
        )

        acts = {}
        for bt, bl in BLOCK_LABELS.items():
            color = BLOCK_COLORS.get(bt, "#3498db")
            act = add_menu.addAction(f"  {bl}")
            act.setData(bt)
            acts[bt] = act

        menu.addSeparator()

        indent_right = menu.addAction("  → 向右缩进")
        indent_left = menu.addAction("  ← 向左缩进")
        menu.addSeparator()
        act_delete = menu.addAction("  🗑 删除")

        action = menu.exec(self.mapToGlobal(pos))

        if action is None:
            return

        # 插入块
        if action in acts.values():
            bt = action.data()
            block = new_block(bt, self._block.get("indent", 0))
            if insert_above:
                self.insertAboveRequested.emit((block, self, True))
            else:
                self.insertBelowRequested.emit((block, self, False))
            return

        if action == indent_right:
            data = self._block.setdefault("data", {})
            self._block["indent"] = self._block.get("indent", 0) + 1
            self.structureChanged.emit()
            return

        if action == indent_left:
            new_indent = max(0, self._block.get("indent", 0) - 1)
            self._block["indent"] = new_indent
            self.structureChanged.emit()
            return

        if action == act_delete:
            self.deleteRequested.emit(self)
            return


# ---------------------------------------------------------------------------
# 母工作流块编辑器主控件
# ---------------------------------------------------------------------------


class MasterWorkflowBlockEditor(QWidget):
    """母工作流块编辑器：采用类似 Python 代码缩进的方式编排节点。"""

    changed = Signal()
    executeRequested = Signal(list, str, bool)  # (steps, goal, replan_enabled) 由工作流块触发，仅执行该块

    def __init__(self, parent=None):
        super().__init__(parent)
        self._blocks: List[Dict[str, Any]] = []
        self._rows: List[_BlockRow] = []
        self._screenshots_dir = app_config.templates_fallback_dir()
        self._build_ui()

    def set_screenshots_dir(self, screenshots_dir: str) -> None:
        """设置截图保存目录。"""
        self._screenshots_dir = screenshots_dir

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # 块列表区域
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "QScrollArea{border:1px solid #ddd;border-radius:6px;background:#fafafa;}"
        )
        self._scroll_content = QWidget()
        self._scroll_lay = QVBoxLayout(self._scroll_content)
        self._scroll_lay.setContentsMargins(8, 8, 8, 8)
        self._scroll_lay.setSpacing(4)
        # 注意：不要在此处添加 stretch，_rebuild_rows 末尾会添加
        self._scroll.setWidget(self._scroll_content)
        lay.addWidget(self._scroll, 1)

        # 底部工具栏
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self._btn_add = QPushButton("➕ 添加块")
        self._btn_add.setStyleSheet(
            "QPushButton{background:#3498db;color:white;border-radius:5px;"
            "padding:6px 16px;font-weight:bold;font-size:13px;}"
            "QPushButton:hover{background:#2980b9;}"
        )
        self._btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add.clicked.connect(self._on_add_block)
        toolbar.addWidget(self._btn_add)

        self._btn_add_condition = QPushButton("➕ 条件分支")
        self._btn_add_condition.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;border-radius:5px;"
            "padding:6px 16px;font-weight:bold;font-size:13px;}"
            "QPushButton:hover{background:#2ecc71;}"
        )
        self._btn_add_condition.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_condition.clicked.connect(self._on_add_condition_branch)
        toolbar.addWidget(self._btn_add_condition)

        toolbar.addStretch(1)
        lay.addLayout(toolbar)

    def set_blocks(self, blocks: List[Dict[str, Any]]) -> None:
        """设置块列表。

        深拷贝加载，编辑仅作用于编辑器内部副本，不会改动原始母工作流数据；
        未点击「保存」直接退出控制中心时，原有编排保持不变。
        """
        self._blocks = copy.deepcopy(blocks)
        self._rebuild_rows()

    def get_blocks(self) -> List[Dict[str, Any]]:
        """获取块列表。"""
        return list(self._blocks)

    def _rebuild_rows(self) -> None:
        """重建所有行控件。"""
        # 清除所有子项（包括 stretch 等 spacer 项）
        while self._scroll_lay.count():
            item = self._scroll_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._rows = []
        for i, block in enumerate(self._blocks):
            row = _BlockRow(block, screenshots_dir=self._screenshots_dir,
                            row_number=i + 1)
            row.changed.connect(self._on_content_changed)
            row.structureChanged.connect(self._on_structure_changed)
            row.deleteRequested.connect(self._on_delete_row)
            row.insertAboveRequested.connect(self._on_insert_block)
            row.insertBelowRequested.connect(self._on_insert_block)
            row.executeRequested.connect(self._on_block_execute_requested)
            self._scroll_lay.addWidget(row)
            self._rows.append(row)

        self._scroll_lay.addStretch(1)
        self.changed.emit()

    def _on_content_changed(self) -> None:
        """内容变更（如步骤文本、块名称等），仅通知数据变更，不重建控件树。"""
        self.changed.emit()

    def _on_structure_changed(self) -> None:
        """结构变更（如缩进调整），需重建控件树。"""
        self._rebuild_rows()

    def _on_delete_row(self, row: _BlockRow) -> None:
        idx = self._rows.index(row)
        block = self._blocks.pop(idx)
        # 块删除时连带清理其中所有步骤占用的模板/全图
        ids: set = set()
        _collect_screenshot_ids(block, ids)
        for sid in ids:
            app_config.delete_screenshot_files(row._screenshots_dir, sid)
        self._rebuild_rows()
        self.changed.emit()

    def _on_insert_block(self, data) -> None:
        """插入块。data 可以是 (block, ref_row, above) 元组或直接 block。"""
        if isinstance(data, tuple):
            block, ref_row, above = data
            idx = self._rows.index(ref_row)
            if not above:
                idx += 1
            self._blocks.insert(idx, block)
            self._rebuild_rows()
            self.changed.emit()
        elif isinstance(data, _BlockRow):
            # 单纯的 insertAbove 信号
            pass

    def _on_block_execute_requested(self, row: _BlockRow, steps: list, goal: str,
                                    replan_enabled: bool = False) -> None:
        """工作流块请求执行其内联步骤。"""
        self.executeRequested.emit(steps, goal, replan_enabled)

    def _on_add_block(self) -> None:
        """添加新块。"""
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;color:black;border:1px solid #ccc;padding:4px;}"
            "QMenu::item{padding:8px 32px;}"
            "QMenu::item:selected{background:#3498db;color:white;}"
        )

        for bt, bl in BLOCK_LABELS.items():
            color = BLOCK_COLORS.get(bt, "#3498db")
            act = menu.addAction(f"  {bl}")
            act.setData(bt)

        action = menu.exec(self._btn_add.mapToGlobal(
            self._btn_add.rect().bottomLeft()))
        if action is None:
            return

        bt = action.data()
        block = new_block(bt, 0)
        self._blocks.append(block)
        self._rebuild_rows()
        self.changed.emit()

    def _on_add_condition_branch(self) -> None:
        """添加条件分支块（if/elif/else）。"""
        # 寻找最后一个条件块来确定分支类型
        last_cond = None
        last_cond_idx = -1
        for i, b in enumerate(self._blocks):
            if b.get("type") == BLOCK_CONDITION:
                last_cond = b
                last_cond_idx = i

        if last_cond is None:
            # 没有条件块，添加 if
            branch = "if"
        elif last_cond.get("data", {}).get("branch") == "else":
            QMessageBox.information(self, "提示", "else 分支后不能再添加条件分支。")
            return
        else:
            # 后续添加 elif 或 else
            # 检查是否已存在 else
            has_else = any(
                b.get("type") == BLOCK_CONDITION
                and b.get("data", {}).get("branch") == "else"
                for b in self._blocks
            )
            if has_else:
                QMessageBox.information(self, "提示", "已在 else 分支后，不能继续添加条件分支。")
                return

            # 弹出选择 elif 还是 else
            dlg = QDialog(self)
            dlg.setWindowTitle("添加条件分支")
            dlg.setMinimumWidth(300)
            dl = QVBoxLayout(dlg)
            from PySide6.QtWidgets import QLabel as QL, QListWidget as QLW, QDialogButtonBox as QDB
            dl.addWidget(QL("选择分支类型："))
            lst = QLW()
            lst.addItem("elif")
            lst.addItem("else")
            lst.setStyleSheet(
                "QListWidget{background:white;border:1px solid #ccc;border-radius:4px;}"
                "QListWidget::item{padding:8px;}"
                "QListWidget::item:selected{background:#3498db;color:white;}"
            )
            dl.addWidget(lst)
            btns = QDB(QDB.StandardButton.Ok | QDB.StandardButton.Cancel)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            dl.addWidget(btns)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            sel = lst.currentRow()
            if sel < 0:
                return
            branch = ["elif", "else"][sel]

        block = new_block(BLOCK_CONDITION, 0)
        block["data"]["branch"] = branch
        # 插入到最后一个条件块之后
        if last_cond_idx >= 0:
            self._blocks.insert(last_cond_idx + 1, block)
        else:
            self._blocks.append(block)
        self._rebuild_rows()
        self.changed.emit()