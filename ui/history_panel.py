# -*- coding: utf-8 -*-
"""历史会话面板：左上角列出历史对话，可加载/删除。

交互：
- 双击条目：直接加载对应历史对话（与点「加载」等价）
- 右键菜单：加载 / 删除 / 全选 / 取消全选
- 多选：Ctrl 点选、Shift 范围选；「删除」按钮和右键「删除」都会批量删除所有选中项
"""

from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMenu, QAbstractItemView,
)

import app_config


class HistoryPanel(QWidget):
    loadRequested = Signal(str)        # session_id（单个加载）
    deleteRequested = Signal(list)     # [session_id, ...]（批量删除）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("历史对话")
        self.setWindowFlags(Qt.WindowType.Dialog)
        self.setMinimumSize(360, 480)
        self.setStyleSheet("background:#f5f6fa;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)

        title = QLabel("历史对话（可多选/双击加载/右键操作）")
        title.setStyleSheet("font-weight:bold;font-size:14px;color:#333;")
        lay.addWidget(title)

        self._list = QListWidget()
        # 启用多选：Ctrl 点选切换、Shift 范围选择；单/双击仍能获取 currentItem
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setStyleSheet(
            "QListWidget{background:white;border:1px solid #ccc;border-radius:6px;}"
            "QListWidget::item{padding:6px;border-bottom:1px solid #eee;}"
            "QListWidget::item:selected{background:#d6eaff;color:#222;}"
        )
        # 右键菜单：CustomContextMenu 模式
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        # 双击直接加载
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        lay.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        self._btn_load = QPushButton("加载")
        self._btn_delete = QPushButton("删除所选")
        self._btn_close = QPushButton("关闭")
        for b, c in ((self._btn_load, "#2e8b57"), (self._btn_delete, "#e74c3c"), (self._btn_close, "#95a5a6")):
            b.setStyleSheet(f"QPushButton{{background:{c};color:white;border-radius:5px;padding:6px 16px;}}")
        btn_row.addWidget(self._btn_load)
        btn_row.addWidget(self._btn_delete)
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_close)
        lay.addLayout(btn_row)

        self._btn_load.clicked.connect(self._on_load)
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_close.clicked.connect(self.close)

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        sessions = app_config.list_sessions()
        self._list.clear()
        for s in sessions:
            item = QListWidgetItem(f"{s['title']}\n{s.get('updated_at','')}")
            item.setData(Qt.ItemDataRole.UserRole, s["id"])
            self._list.addItem(item)

    # ------------------------------------------------------------------
    # 工具：获取选中 ID 列表 / 当前 ID
    # ------------------------------------------------------------------
    def _selected_ids(self) -> List[str]:
        """返回所有选中项的 session_id 列表（按列表顺序）。"""
        ids: List[str] = []
        for item in self._list.selectedItems():
            sid = item.data(Qt.ItemDataRole.UserRole)
            if sid:
                ids.append(sid)
        return ids

    def _current_id(self) -> str:
        item = self._list.currentItem()
        if item is None:
            return ""
        return item.data(Qt.ItemDataRole.UserRole) or ""

    # ------------------------------------------------------------------
    # 按钮 / 双击 动作
    # ------------------------------------------------------------------
    def _on_item_double_clicked(self, _item: QListWidgetItem) -> None:
        """双击条目：等价于「加载」按钮。"""
        self._on_load()

    def _on_load(self) -> None:
        """加载当前高亮项（currentItem，多选时以最后点击的为准）。"""
        sid = self._current_id()
        if sid:
            self.loadRequested.emit(sid)

    def _on_delete(self) -> None:
        """删除所有选中项（多选支持）。"""
        ids = self._selected_ids()
        if not ids:
            # 若没有显式选中项，回退到当前项
            sid = self._current_id()
            if not sid:
                return
            ids = [sid]
        self.deleteRequested.emit(ids)
        self.refresh()

    # ------------------------------------------------------------------
    # 右键菜单
    # ------------------------------------------------------------------
    def _on_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;border:1px solid #ccc;border-radius:4px;padding:4px;}"
            "QMenu::item{padding:6px 14px;border-radius:3px;}"
            "QMenu::item:selected{background:#d6eaff;}"
            "QMenu::separator{height:1px;background:#eee;margin:4px 0;}"
        )

        act_load = menu.addAction("📂 加载当前对话")
        menu.addSeparator()
        act_select_all = menu.addAction("✓ 全选")
        act_select_none = menu.addAction("✗ 取消全选")
        menu.addSeparator()
        act_delete = menu.addAction("🗑 删除所选对话")

        # 是否在空白处点击（无 item）
        item_at_pos = self._list.itemAt(pos)
        if item_at_pos is None:
            # 空白处右键："加载当前" 和 "删除所选" 置灰（如果没有选中）
            if not self._current_id():
                act_load.setEnabled(False)
            if not self._selected_ids():
                act_delete.setEnabled(False)
        else:
            # 点在某个 item 上：如果它未被选中，则先让它成为 current 并单独选中，
            # 这样右键菜单操作直观地针对这个 item
            if not item_at_pos.isSelected():
                self._list.setCurrentItem(item_at_pos)

        action = menu.exec(self._list.viewport().mapToGlobal(pos))
        if action is None:
            return

        if action is act_load:
            self._on_load()
        elif action is act_delete:
            self._on_delete()
        elif action is act_select_all:
            self._list.selectAll()
        elif action is act_select_none:
            self._list.clearSelection()
