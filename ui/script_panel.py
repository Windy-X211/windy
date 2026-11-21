# -*- coding: utf-8 -*-
"""脚本库面板：列出已保存的脚本，支持查看/编辑/执行/删除/重命名。

脚本数据存储在 AppData/scripts/ 目录（独立 json 文件，见 app_config）。
管理逻辑和界面与工作流面板一致。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QDialog, QPlainTextEdit, QMessageBox,
    QInputDialog,
)

import app_config


class ScriptPanel(QDialog):
    """脚本库管理面板。"""

    editRequested = Signal(str)       # 编辑脚本（script_id）
    executeRequested = Signal(str)    # 执行脚本（script_id）
    deleteRequested = Signal(str)     # 删除脚本（script_id）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("脚本库")
        self.setWindowFlags(Qt.WindowType.Dialog)
        self.setMinimumSize(500, 520)
        self.setStyleSheet("background:#f5f6fa;")

        self._build_ui()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # 顶部标题行
        top_row = QHBoxLayout()
        title = QLabel("脚本库")
        title.setStyleSheet("font-weight:bold;font-size:14px;color:#333;")
        top_row.addWidget(title)
        top_row.addStretch(1)
        lay.addLayout(top_row)

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget{background:white;border:1px solid #ccc;border-radius:6px;}"
            "QListWidget::item{padding:8px;border-bottom:1px solid #eee;}"
            "QListWidget::item:selected{background:#e8d5f5;color:#222;}"
        )
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.itemDoubleClicked.connect(self._on_edit)
        lay.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        self._btn_view = QPushButton("📖 查看")
        self._btn_rename = QPushButton("✎ 重命名")
        self._btn_execute = QPushButton("▶ 执行")
        self._btn_delete = QPushButton("🗑 删除")
        self._btn_close = QPushButton("关闭")
        for b, c in (
            (self._btn_view, "#3498db"),
            (self._btn_rename, "#e67e22"),
            (self._btn_execute, "#2e8b57"),
            (self._btn_delete, "#e74c3c"),
            (self._btn_close, "#95a5a6"),
        ):
            b.setStyleSheet(
                f"QPushButton{{background:{c};color:white;border-radius:5px;padding:6px 14px;}}"
            )
        btn_row.addWidget(self._btn_view)
        btn_row.addWidget(self._btn_rename)
        btn_row.addWidget(self._btn_execute)
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_delete)
        btn_row.addWidget(self._btn_close)
        lay.addLayout(btn_row)

        self._btn_view.clicked.connect(self._on_view)
        self._btn_rename.clicked.connect(self._on_rename)
        self._btn_execute.clicked.connect(self._on_execute)
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_close.clicked.connect(self.close)

    def refresh(self) -> None:
        """从 app_config 加载脚本列表。"""
        scripts = app_config.list_scripts()
        self._list.clear()
        for s in scripts:
            label = s.get("label", "未命名")
            lang = s.get("language", "python")
            lang_display = "🌐 网页" if lang == "web" else "🐍 Python"
            text = f"📜 {label}  ({lang_display})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, s["id"])
            self._list.addItem(item)

    def _current_id(self) -> str:
        item = self._list.currentItem()
        if item is None:
            return ""
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_view(self) -> None:
        """查看脚本代码。"""
        sid = self._current_id()
        if not sid:
            return
        script = app_config.load_script(sid)
        if not script:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"查看脚本 - {script.get('label', '')}")
        dlg.setMinimumSize(600, 400)
        dlg_lay = QVBoxLayout(dlg)
        code_edit = QPlainTextEdit()
        code_edit.setPlainText(script.get("code", ""))
        code_edit.setReadOnly(True)
        code_edit.setStyleSheet(
            "QPlainTextEdit{background:#1e1e2e;color:#cdd6f4;border:none;"
            "font-family:Consolas,'Courier New',monospace;font-size:13px;}"
        )
        dlg_lay.addWidget(code_edit)
        btn_close = QPushButton("关闭")
        btn_close.setStyleSheet(
            "QPushButton{background:#95a5a6;color:white;border-radius:5px;padding:6px 14px;}"
        )
        btn_close.clicked.connect(dlg.accept)
        dlg_lay.addWidget(btn_close)
        dlg.exec()

    def _on_rename(self) -> None:
        """重命名脚本。"""
        sid = self._current_id()
        if not sid:
            return
        script = app_config.load_script(sid)
        if not script:
            return
        old_label = script.get("label", "")
        new_label, ok = QInputDialog.getText(
            self, "重命名脚本", "脚本名称：", text=old_label,
        )
        if ok and new_label.strip() and new_label.strip() != old_label:
            script["label"] = new_label.strip()
            app_config.save_script(script)
            self.refresh()

    def _on_execute(self) -> None:
        sid = self._current_id()
        if sid:
            self.executeRequested.emit(sid)

    def _on_delete(self) -> None:
        sid = self._current_id()
        if not sid:
            return
        ret = QMessageBox.question(
            self, "删除脚本", "确定删除该脚本？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            app_config.delete_script(sid)
            self.refresh()

    def _on_edit(self) -> None:
        sid = self._current_id()
        if sid:
            self.editRequested.emit(sid)

    def _on_context_menu(self, pos) -> None:
        """右键菜单：查看/重命名/执行/删除。"""
        from PySide6.QtWidgets import QMenu
        item = self._list.itemAt(pos)
        if item is None:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        if not sid:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;color:black;border:1px solid #ccc;padding:4px;}"
            "QMenu::item{padding:6px 24px;}"
            "QMenu::item:selected{background:#3498db;color:white;}"
        )
        act_view = menu.addAction("📖 查看")
        act_rename = menu.addAction("✎ 重命名")
        menu.addSeparator()
        act_exec = menu.addAction("▶ 执行")
        act_del = menu.addAction("🗑 删除")
        action = menu.exec(self._list.mapToGlobal(pos))
        if action == act_view:
            self._on_view()
        elif action == act_rename:
            self._on_rename()
        elif action == act_exec:
            self.executeRequested.emit(sid)
        elif action == act_del:
            self._on_delete()