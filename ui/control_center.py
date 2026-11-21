# -*- coding: utf-8 -*-
"""控制中心：工作流管理 + 脚本库管理 + 母工作流编排。

功能：
- 工作流：列出/新建/编辑/执行/删除工作流，以及母工作流编排
- 脚本库：列出/查看/重命名/执行/删除脚本
- 母工作流节点：支持工作流节点和脚本节点，使用变量传递输入和输出
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QDialog, QCheckBox, QScrollArea,
    QMessageBox, QInputDialog, QMenu,
)

import app_config
from ui.script_panel import ScriptPanel
from ui.workflow_panel import WorkflowPanel
from ui.master_workflow_editor import MasterWorkflowBlockEditor


# 样式常量
_STYLE_BTN_PRIMARY = (
    "QPushButton{background:#27ae60;color:white;border-radius:5px;"
    "padding:6px 16px;font-weight:bold;}"
    "QPushButton:hover{background:#2ecc71;}"
)
_STYLE_BTN_WARN = (
    "QPushButton{background:#e67e22;color:white;border-radius:5px;"
    "padding:6px 16px;font-weight:bold;}"
    "QPushButton:hover{background:#f39c12;}"
)
_STYLE_BTN_DANGER = (
    "QPushButton{background:#e74c3c;color:white;border-radius:5px;"
    "padding:6px 16px;font-weight:bold;}"
    "QPushButton:hover{background:#ff5c41;}"
)
_STYLE_BTN_NORMAL = (
    "QPushButton{background:#7f8c8d;color:white;border-radius:5px;"
    "padding:6px 16px;}"
    "QPushButton:hover{background:#95a5a6;}"
)
_STYLE_BTN_INFO = (
    "QPushButton{background:#3498db;color:white;border-radius:5px;"
    "padding:6px 16px;font-weight:bold;}"
    "QPushButton:hover{background:#2980b9;}"
)
_STYLE_LIST = (
    "QListWidget{background:white;border:1px solid #ddd;border-radius:6px;}"
    "QListWidget::item{padding:10px;border-bottom:1px solid #eee;}"
    "QListWidget::item:selected{background:#d5e8f7;color:#222;}"
    "QListWidget::item:hover{background:#f0f4ff;}"
)
_STYLE_GROUP = (
    "QGroupBox{font-weight:bold;border:1px solid #ddd;border-radius:8px;"
    "margin-top:10px;padding:12px 8px 8px 8px;}"
    "QGroupBox::title{subcontrol-origin:margin;left:12px;padding:0 6px;}"
)


class ControlCenterDialog(QDialog):
    """控制中心：母工作流编排 + 快捷入口（工作流列表 / 脚本库）。"""

    # 工作流相关信号
    workflowNewRequested = Signal()
    workflowEditRequested = Signal(str)
    workflowExecuteRequested = Signal(str)
    # 脚本相关信号
    scriptEditRequested = Signal(str)
    scriptExecuteRequested = Signal(str)
    # 母工作流相关信号
    masterWorkflowSaved = Signal()
    runRequested = Signal(str)
    stopRequested = Signal(str)
    inlineWorkflowRequested = Signal(list, str, bool)  # (steps, goal, replan_enabled) 工作流块"执行"按钮触发

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("控制中心")
        self.setWindowFlags(Qt.WindowType.Dialog)
        self.setMinimumSize(900, 620)
        self.resize(960, 680)
        self.setStyleSheet("background:#f5f6fa;")

        self._current_mwf: Optional[Dict[str, Any]] = None
        self._mwf_backup_label: Optional[str] = None  # 正在编辑且有备份的母工作流标签
        self._wf_panel: Optional[WorkflowPanel] = None
        self._script_panel: Optional[ScriptPanel] = None

        self._build_ui()
        self._refresh_mwf_list()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        # ======== 顶部导航栏：工作流 / 脚本库快捷入口 ========
        nav_row = QHBoxLayout()
        nav_row.setSpacing(6)

        self._btn_workflows = QPushButton("📋 工作流")
        self._btn_workflows.setStyleSheet(
            "QPushButton{background:#8e44ad;color:white;border-radius:5px;"
            "padding:6px 18px;font-weight:bold;font-size:13px;}"
            "QPushButton:hover{background:#9b59b6;}"
        )
        self._btn_workflows.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_workflows.clicked.connect(self._open_workflow_list)

        self._btn_scripts = QPushButton("📜 脚本库")
        self._btn_scripts.setStyleSheet(
            "QPushButton{background:#8e44ad;color:white;border-radius:5px;"
            "padding:6px 18px;font-weight:bold;font-size:13px;}"
            "QPushButton:hover{background:#9b59b6;}"
        )
        self._btn_scripts.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_scripts.clicked.connect(self._open_script_library)

        nav_row.addWidget(self._btn_workflows)
        nav_row.addWidget(self._btn_scripts)
        nav_row.addStretch(1)
        root.addLayout(nav_row)

        # ======== 母工作流编排区域（主内容） ========
        mwf_label = QLabel("母工作流编排")
        mwf_label.setStyleSheet("font-weight:bold;font-size:14px;color:#333;")
        root.addWidget(mwf_label)

        mwf_content = QWidget()
        mwf_lay = QHBoxLayout(mwf_content)
        mwf_lay.setContentsMargins(0, 0, 0, 0)
        mwf_lay.setSpacing(10)

        # 左侧：母工作流列表
        mwf_left = QWidget()
        mwf_left.setFixedWidth(200)
        mwf_left_lay = QVBoxLayout(mwf_left)
        mwf_left_lay.setContentsMargins(0, 0, 0, 0)
        mwf_left_lay.setSpacing(6)

        mwf_list_title = QLabel("母工作流列表")
        mwf_list_title.setStyleSheet("font-weight:bold;font-size:12px;color:#555;")
        mwf_left_lay.addWidget(mwf_list_title)

        self._mwf_list = QListWidget()
        self._mwf_list.setStyleSheet(_STYLE_LIST)
        self._mwf_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._mwf_list.customContextMenuRequested.connect(self._on_mwf_context_menu)
        self._mwf_list.currentItemChanged.connect(self._on_mwf_selection_changed)
        mwf_left_lay.addWidget(self._mwf_list, 1)

        mwf_btn_col = QVBoxLayout()
        self._btn_mwf_new = QPushButton("✚ 新建")
        self._btn_mwf_new.setStyleSheet(_STYLE_BTN_PRIMARY)
        self._btn_mwf_new.clicked.connect(self._on_mwf_new)
        mwf_btn_col.addWidget(self._btn_mwf_new)
        self._btn_mwf_del = QPushButton("🗑 删除")
        self._btn_mwf_del.setStyleSheet(_STYLE_BTN_DANGER)
        self._btn_mwf_del.clicked.connect(self._on_mwf_delete)
        mwf_btn_col.addWidget(self._btn_mwf_del)
        mwf_left_lay.addLayout(mwf_btn_col)

        mwf_lay.addWidget(mwf_left)

        # 右侧：母工作流编辑区
        mwf_right = QWidget()
        mwf_right_lay = QVBoxLayout(mwf_right)
        mwf_right_lay.setContentsMargins(0, 0, 0, 0)
        mwf_right_lay.setSpacing(6)

        self._mwf_edit_area = QScrollArea()
        self._mwf_edit_area.setWidgetResizable(True)
        self._mwf_edit_area.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._mwf_edit_content = QWidget()
        self._mwf_edit_content.setStyleSheet("background:transparent;")
        self._mwf_edit_lay = QVBoxLayout(self._mwf_edit_content)
        self._mwf_edit_lay.setContentsMargins(0, 0, 0, 0)
        self._mwf_edit_lay.setSpacing(8)
        self._mwf_edit_area.setWidget(self._mwf_edit_content)
        mwf_right_lay.addWidget(self._mwf_edit_area, 1)

        # 底部按钮
        mwf_bottom = QHBoxLayout()
        self._btn_mwf_save = QPushButton("💾 保存")
        self._btn_mwf_save.setStyleSheet(_STYLE_BTN_PRIMARY)
        self._btn_mwf_save.clicked.connect(self._on_mwf_save)
        self._btn_mwf_run = QPushButton("▶ 运行所有")
        self._btn_mwf_run.setStyleSheet(_STYLE_BTN_INFO)
        self._btn_mwf_run.clicked.connect(self._on_mwf_run_all)
        mwf_bottom.addWidget(self._btn_mwf_save)
        mwf_bottom.addWidget(self._btn_mwf_run)

        # 取消测试按钮：仅在存在待测试块时临时显示
        self._btn_mwf_cancel_test = QPushButton("◌ 取消测试")
        self._btn_mwf_cancel_test.setStyleSheet(
            "QPushButton{background:#f39c12;color:white;border-radius:5px;"
            "padding:6px 14px;font-weight:bold;font-size:12px;}"
            "QPushButton:hover{background:#e67e22;}"
        )
        self._btn_mwf_cancel_test.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mwf_cancel_test.clicked.connect(self._on_mwf_cancel_test)
        self._btn_mwf_cancel_test.setVisible(False)
        mwf_bottom.addWidget(self._btn_mwf_cancel_test)

        mwf_bottom.addStretch(1)
        mwf_right_lay.addLayout(mwf_bottom)

        # 状态提示
        self._mwf_status = QLabel("")
        self._mwf_status.setStyleSheet("color:#27ae60;font-size:12px;padding:2px 4px;")
        self._mwf_status.setVisible(False)
        mwf_right_lay.addWidget(self._mwf_status)

        mwf_lay.addWidget(mwf_right, 1)
        root.addWidget(mwf_content, 1)

    # ------------------------------------------------------------------
    # 快捷入口
    # ------------------------------------------------------------------

    def _open_workflow_list(self) -> None:
        """打开工作流列表面板（独立对话框）。"""
        if self._wf_panel is None:
            self._wf_panel = WorkflowPanel(self)
            self._wf_panel.newRequested.connect(self.workflowNewRequested.emit)
            self._wf_panel.editRequested.connect(self.workflowEditRequested.emit)
            self._wf_panel.executeRequested.connect(self.workflowExecuteRequested.emit)
            self._wf_panel.controlCenterRequested.connect(self.raise_)
        self._wf_panel.refresh()
        self._wf_panel.show()
        self._wf_panel.raise_()
        self._wf_panel.activateWindow()

    def _open_script_library(self) -> None:
        """打开脚本库面板（独立对话框）。"""
        if self._script_panel is None:
            self._script_panel = ScriptPanel(self)
            self._script_panel.editRequested.connect(self.scriptEditRequested.emit)
            self._script_panel.executeRequested.connect(self.scriptExecuteRequested.emit)
        self._script_panel.refresh()
        self._script_panel.show()
        self._script_panel.raise_()
        self._script_panel.activateWindow()

    def refresh_scripts(self) -> None:
        """脚本导出后刷新脚本库面板（若已打开）。"""
        if self._script_panel is not None:
            self._script_panel.refresh()

    def refresh_workflows(self) -> None:
        """工作流保存后刷新工作流面板（若已打开）。"""
        if self._wf_panel is not None:
            self._wf_panel.refresh()

    # ------------------------------------------------------------------
    # 母工作流管理
    # ------------------------------------------------------------------

    def _show_mwf_status(self, message: str, is_error: bool = False) -> None:
        self._mwf_status.setText(message)
        self._mwf_status.setStyleSheet(
            f"color:{'#e74c3c' if is_error else '#27ae60'};font-size:12px;padding:2px 4px;"
        )
        self._mwf_status.setVisible(True)
        QTimer.singleShot(3000, lambda: self._mwf_status.setVisible(False))

    def _refresh_mwf_list(self) -> None:
        """刷新母工作流列表。"""
        current_id = self._current_mwf.get("id", "") if self._current_mwf else ""
        self._mwf_list.blockSignals(True)
        self._mwf_list.clear()
        for mwf in app_config.list_master_workflows():
            label = mwf.get("label", "未命名")
            enabled = mwf.get("enabled", True)
            nodes_count = mwf.get("nodes_count", 0)
            updated = mwf.get("updated_at", "")
            icon = "🟢" if enabled else "🔴"
            text = f"{icon} {label}\n   {nodes_count} 个节点  |  {updated}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, mwf["id"])
            self._mwf_list.addItem(item)
        if current_id:
            for i in range(self._mwf_list.count()):
                item = self._mwf_list.item(i)
                if item and item.data(Qt.ItemDataRole.UserRole) == current_id:
                    self._mwf_list.setCurrentItem(item)
                    break
        self._mwf_list.blockSignals(False)

    def _on_mwf_selection_changed(self, current, _previous) -> None:
        if not current:
            self._finalize_mwf_edit(committed=False)  # 未保存退出上一个编辑
            self._clear_mwf_edit()
            self._current_mwf = None
            return
        # 切换到其他母工作流：先还原上一个未保存的编辑
        self._finalize_mwf_edit(committed=False)
        mwf_id = current.data(Qt.ItemDataRole.UserRole)
        if not mwf_id:
            return
        mwf = app_config.load_master_workflow(mwf_id)
        if mwf:
            self._current_mwf = mwf
            self._build_mwf_edit_panel(mwf)

    def _on_mwf_context_menu(self, pos) -> None:
        """母工作流右键菜单。"""
        item = self._mwf_list.itemAt(pos)
        if not item:
            return
        mwf_id = item.data(Qt.ItemDataRole.UserRole)
        if not mwf_id:
            return
        mwf = app_config.load_master_workflow(mwf_id)
        if not mwf:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;color:black;border:1px solid #ccc;padding:4px;}"
            "QMenu::item{padding:6px 24px;}"
            "QMenu::item:selected{background:#3498db;color:white;}"
        )
        enabled = mwf.get("enabled", True)
        if enabled:
            act_toggle = menu.addAction("⏸ 禁用")
        else:
            act_toggle = menu.addAction("▶ 启用")
        menu.addSeparator()
        act_rename = menu.addAction("✎ 重命名")
        menu.addSeparator()
        act_del = menu.addAction("🗑 删除")
        action = menu.exec(self._mwf_list.mapToGlobal(pos))
        if action == act_toggle:
            mwf["enabled"] = not enabled
            app_config.save_master_workflow(mwf)
            self.masterWorkflowSaved.emit()
            self._refresh_mwf_list()
            if self._current_mwf and self._current_mwf.get("id") == mwf_id:
                self._current_mwf = mwf
                self._build_mwf_edit_panel(mwf)
            if mwf.get("enabled", True):
                self.runRequested.emit(mwf_id)
            else:
                self.stopRequested.emit(mwf_id)
        elif action == act_rename:
            old_label = mwf.get("label", "")
            new_label, ok = QInputDialog.getText(
                self, "重命名", "母工作流名称：", text=old_label)
            if ok and new_label.strip() and new_label.strip() != old_label:
                mwf["label"] = new_label.strip()
                app_config.save_master_workflow(mwf)
                self._refresh_mwf_list()
        elif action == act_del:
            self._confirm_mwf_delete(mwf_id)

    def _on_mwf_new(self) -> None:
        label, ok = QInputDialog.getText(self, "新建母工作流", "请输入母工作流名称：")
        if not ok or not label.strip():
            return
        mwf = {
            "label": label.strip(),
            "enabled": True,
            "nodes": [],
            "connections": [],
        }
        mwf_id = app_config.save_master_workflow(mwf)
        self._refresh_mwf_list()
        self._show_mwf_status(f"已创建母工作流「{label.strip()}」")
        self.select_master_workflow(mwf_id)

    def select_master_workflow(self, mwf_id: str) -> None:
        """在列表中选中指定母工作流。"""
        for i in range(self._mwf_list.count()):
            item = self._mwf_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == mwf_id:
                self._mwf_list.setCurrentItem(item)
                break

    def refresh(self) -> None:
        """刷新母工作流状态显示（程序自动禁用后同步）。"""
        self._refresh_mwf_list()
        # 若正在编辑的母工作流被外部修改（如执行后自动禁用），重新加载并重建编辑面板
        if self._current_mwf and self._current_mwf.get("id"):
            cur_id = self._current_mwf.get("id")
            latest = app_config.load_master_workflow(cur_id)
            if latest:
                self._current_mwf = latest
                self._build_mwf_edit_panel(latest)

    def _on_mwf_delete(self) -> None:
        if not self._current_mwf:
            return
        self._confirm_mwf_delete(self._current_mwf.get("id", ""))

    def _confirm_mwf_delete(self, mwf_id: str) -> None:
        ret = QMessageBox.question(
            self, "删除母工作流", "确定删除该母工作流？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.stopRequested.emit(mwf_id)
            label = (self._current_mwf.get("label", "")
                     if self._current_mwf and self._current_mwf.get("id") == mwf_id
                     else "")
            # 删除正在编辑的母工作流：清除其备份，避免关闭时误还原已删除的文件夹
            if self._current_mwf and self._current_mwf.get("id") == mwf_id:
                self._finalize_mwf_edit(committed=True)
            app_config.delete_master_workflow(mwf_id)
            if self._current_mwf and self._current_mwf.get("id") == mwf_id:
                self._current_mwf = None
            self._refresh_mwf_list()
            self._clear_mwf_edit()
            self._show_mwf_status(f"已删除母工作流「{label}」" if label else "已删除母工作流")

    # ------------------------------------------------------------------
    # 母工作流编辑面板
    # ------------------------------------------------------------------

    def _clear_mwf_edit(self) -> None:
        while self._mwf_edit_lay.count():
            item = self._mwf_edit_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _build_mwf_edit_panel(self, mwf: Dict[str, Any]) -> None:
        """构建母工作流编辑面板（使用块式编辑器）。"""
        self._clear_mwf_edit()

        # 标题
        title = QLabel(f"📋 {mwf.get('label', '未命名')}")
        title.setStyleSheet("font-weight:bold;font-size:16px;color:#2c3e50;padding:4px 0;")
        self._mwf_edit_lay.addWidget(title)

        # 启用状态
        status_row = QHBoxLayout()
        self._mwf_chk_enabled = QCheckBox("启用")
        self._mwf_chk_enabled.setChecked(mwf.get("enabled", True))
        self._mwf_chk_enabled.setStyleSheet("font-size:13px;font-weight:bold;color:#333;")
        status_row.addWidget(self._mwf_chk_enabled)
        status_row.addStretch(1)
        self._mwf_edit_lay.addLayout(status_row)

        # 块编辑器（替换旧的节点列表）
        editor_label = QLabel("编排区域（右键菜单添加/管理块，左侧为上方插入，右侧为下方插入）")
        editor_label.setStyleSheet("font-weight:bold;font-size:12px;color:#555;padding:2px 0;")
        self._mwf_edit_lay.addWidget(editor_label)

        self._block_editor = MasterWorkflowBlockEditor()
        self._block_editor.changed.connect(self._on_blocks_changed)
        self._block_editor.executeRequested.connect(self._on_block_execute_requested)
        # 设置截图目录到母工作流目录
        mwf_label = mwf.get("label", "未命名")
        self._block_editor.set_screenshots_dir(
            app_config.get_master_workflow_screenshots_dir(mwf_label))
        self._mwf_edit_lay.addWidget(self._block_editor, 1)

        # 加载已有块（兼容旧格式 nodes）
        blocks = mwf.get("blocks", [])
        if not blocks and mwf.get("nodes"):
            # 旧格式兼容：将 nodes 转为 blocks
            blocks = self._convert_old_nodes_to_blocks(mwf.get("nodes", []))
        self._block_editor.set_blocks(blocks)

    def _convert_old_nodes_to_blocks(self, old_nodes: List[Dict]) -> List[Dict]:
        """将旧格式的 nodes 转换为新的 blocks 格式。"""
        from ui.master_workflow_editor import (
            new_block, BLOCK_TIMER, BLOCK_WORKFLOW, BLOCK_SCRIPT,
            BLOCK_CONDITION, BLOCK_LOOP, BLOCK_CONTINUE, BLOCK_BREAK,
        )
        blocks = []
        for node in old_nodes:
            node_type = node.get("node_type", "workflow")
            trigger_type = node.get("trigger_type", "timer")
            if node_type == "script":
                block = new_block(BLOCK_SCRIPT, 0)
                block["data"].update({
                    "script_id": node.get("script_id", ""),
                    "label": node.get("label", ""),
                    "input_vars": node.get("input_vars", {}),
                    "output_vars": node.get("output_vars", {}),
                })
                blocks.append(block)
            elif trigger_type == "conditional":
                block = new_block(BLOCK_CONDITION, 0)
                block["data"].update({
                    "branch": "if",
                    "condition_type": node.get("condition", "success"),
                    "template_path": node.get("template_path", ""),
                    "template_size": node.get("template_size", 50),
                    "template_threshold": node.get("template_threshold", 0.8),
                    "not_condition": node.get("not_condition", False),
                })
                blocks.append(block)
            elif node.get("loop_count", 0) > 0:
                block = new_block(BLOCK_LOOP, 0)
                block["data"].update({
                    "loop_type": "count",
                    "count": node.get("loop_count", 10),
                    "interval": node.get("loop_interval", 1),
                })
                blocks.append(block)
                # 将原节点作为子工作流块
                wf_block = new_block(BLOCK_WORKFLOW, 1)
                wf_block["data"].update({
                    "workflow_id": node.get("workflow_id", ""),
                    "goal": node.get("goal", ""),
                    "label": node.get("label", ""),
                })
                blocks.append(wf_block)
            else:
                block = new_block(BLOCK_WORKFLOW, 0)
                block["data"].update({
                    "workflow_id": node.get("workflow_id", ""),
                    "goal": node.get("goal", ""),
                    "label": node.get("label", ""),
                })
                blocks.append(block)

        return blocks

    def _on_blocks_changed(self) -> None:
        """块编辑器内容已变更：刷新取消测试按钮显示，并为当前母工作流创建备份。"""
        self._refresh_cancel_test_visibility()
        # 母工作流修改时，备份一份完整原文件夹；未保存退出则还原，保存则删除
        if self._current_mwf:
            self._ensure_mwf_backup(self._current_mwf.get("label", ""))

    def _ensure_mwf_backup(self, label: str) -> None:
        """为当前母工作流创建完整原文件夹备份（每个编辑会话仅首次变更时创建）。"""
        if not label:
            return
        if self._mwf_backup_label != label:
            app_config.backup_master_workflow_folder(label)
            self._mwf_backup_label = label

    def _finalize_mwf_edit(self, committed: bool) -> None:
        """结束当前母工作流的编辑会话。

        committed=True：已保存，保留当前文件夹并删除备份；
        committed=False：未保存退出，用备份还原原文件夹后删除备份。
        """
        label = self._mwf_backup_label
        if not label:
            return
        try:
            if committed:
                app_config.discard_master_workflow_backup(label)
            else:
                app_config.restore_master_workflow_folder(label)
        finally:
            # 无论保存与否，结束时都删除备份，避免重复占用空间
            app_config.discard_master_workflow_backup(label)
            self._mwf_backup_label = None

    def closeEvent(self, event) -> None:
        """关闭控制中心：若当前母工作流编辑未保存，还原其原文件夹。"""
        self._finalize_mwf_edit(committed=False)
        super().closeEvent(event)

    def _refresh_cancel_test_visibility(self) -> None:
        """根据是否存在待测试块，临时显示/隐藏取消测试按钮。"""
        has_test = False
        if hasattr(self, "_block_editor"):
            for b in self._block_editor.get_blocks():
                if b.get("data", {}).get("test_selected"):
                    has_test = True
                    break
        self._btn_mwf_cancel_test.setVisible(has_test)

    def _on_mwf_cancel_test(self) -> None:
        """取消所有块上的测试选中状态。"""
        if hasattr(self, "_block_editor"):
            for b in self._block_editor.get_blocks():
                if b.get("data", {}).get("test_selected"):
                    b.setdefault("data", {})["test_selected"] = False
            self._block_editor._rebuild_rows()
        self._refresh_cancel_test_visibility()

    def _on_mwf_save(self) -> None:
        if not self._current_mwf:
            return
        # 从块编辑器收集块数据
        if hasattr(self, "_block_editor"):
            self._current_mwf["blocks"] = self._block_editor.get_blocks()
        # 清理旧 nodes 字段
        self._current_mwf.pop("nodes", None)
        enabled_after = self._mwf_chk_enabled.isChecked()
        self._current_mwf["enabled"] = enabled_after
        mwf_id = app_config.save_master_workflow(self._current_mwf)
        label = self._current_mwf.get("label", "")
        # 已保存：删除备份（须在刷新列表前，否则列表重建触发选择信号会误还原）
        self._finalize_mwf_edit(committed=True)
        self._refresh_mwf_list()
        self.masterWorkflowSaved.emit()
        if enabled_after:
            self.stopRequested.emit(mwf_id)
            self.runRequested.emit(mwf_id)
        else:
            self.stopRequested.emit(mwf_id)
        self._show_mwf_status(f"已保存母工作流「{label}」{'（已启用）' if enabled_after else '（已禁用）'}")

    def _on_block_execute_requested(self, steps: list, goal: str, replan_enabled: bool = False) -> None:
        """工作流块的"执行"按钮触发，仅执行该块的步骤。"""
        if not steps:
            return
        self.inlineWorkflowRequested.emit(steps, goal, replan_enabled)

    def _on_mwf_run_all(self) -> None:
        if not self._current_mwf:
            return
        if hasattr(self, "_block_editor"):
            self._current_mwf["blocks"] = self._block_editor.get_blocks()
        self._current_mwf.pop("nodes", None)
        mwf_id = self._current_mwf.get("id", "")
        label = self._current_mwf.get("label", "")
        self._current_mwf["enabled"] = True
        self._mwf_chk_enabled.setChecked(True)
        app_config.save_master_workflow(self._current_mwf)
        # 已保存（运行前持久化）：删除备份（须在刷新列表前）
        self._finalize_mwf_edit(committed=True)
        self._refresh_mwf_list()
        self.masterWorkflowSaved.emit()
        self.stopRequested.emit(mwf_id)
        self.runRequested.emit(mwf_id)
        self._show_mwf_status(f"已启动母工作流「{label}」")