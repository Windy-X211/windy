# -*- coding: utf-8 -*-
"""角色人设（cos 模式）的切换 / 编辑对话框。

- 左侧列出全部人设，点击切换并编辑当前项；
- 支持新建人设（标签 + 内容），内容可手动编辑，也可 AI 一键生成；
- 「设为当前角色」将当前项设为活动人设并关闭，主窗口人设按钮随即显示其标签。
"""

from __future__ import annotations

import uuid
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QPlainTextEdit, QListWidget, QListWidgetItem, QMessageBox, QWidget,
    QSplitter, QApplication,
)

import app_config
import model_client
from ui.toast_banner import ToastBanner


class _GenerateWorker(QThread):
    """后台线程执行 AI 人设生成，避免阻塞对话框的事件循环。"""

    finished = Signal(str)  # 成功：生成的人设内容
    failed = Signal(str)    # 失败：错误信息

    def __init__(self, tag: str, parent=None):
        super().__init__(parent)
        self._tag = tag

    def run(self) -> None:
        try:
            prompt = (
                f"为角色「{self._tag}」创作一份用于聊天角色扮演的人设内容。\n"
                "请用中文、第二人称（你…）描述，涵盖：性格特点、说话语气与常用口吻、"
                "背景与经历、兴趣喜好、与用户互动的习惯。\n"
                "并务必写明：\n"
                "- 活跃度：你是偏外向（更主动、话更多、能坦然开启新话题）还是偏内向"
                "（更含蓄、话精炼、能接受较长的沉默与等待）；\n"
                "- 情绪感知与应对：当用户开心/低落/烦躁时你通常如何回应；\n"
                "- 对沉默的处理：当对话冷场或用户不回复时你倾向于主动开口还是安静等待。\n"
                "篇幅约 200~300 字，具体、鲜活、口语化，便于角色模型直接照此扮演，"
                "不要输出任何开场白。"
            )
            messages = [
                {"role": "system", "content": "你是角色人设创作助手，只输出人设正文。"},
                {"role": "user", "content": prompt},
            ]
            content = model_client.call_model("persona", messages, stream=False, temperature=0.9)
            content = (content or "").strip()
            if content:
                self.finished.emit(content)
            else:
                self.failed.emit("生成内容为空，请重试或手动编写")
        except Exception as e:
            self.failed.emit(f"AI 生成人设失败：{e}")


class PersonaDialog(QDialog):
    """角色人设的切换与编辑窗口。"""

    # 选中某个人设后发出（主窗口据此刷新人设按钮标签）
    personaSelected = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("角色人设（cos）")
        self.resize(680, 480)
        self.setStyleSheet(self._stylesheet())

        self._data: Dict[str, object] = app_config.load_personas()
        self._personas: List[Dict[str, str]] = self._data.get("personas", [])
        self._active_id: str = self._data.get("active_persona_id", "")
        self._current_id: Optional[str] = None  # 当前正在编辑的人设 id
        self._generate_worker: Optional[_GenerateWorker] = None

        root = QVBoxLayout(self)
        root.setSpacing(8)

        root.addWidget(self._build_top_label())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_list_panel())
        splitter.addWidget(self._build_edit_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        root.addLayout(self._build_button_row())

        self._refresh_list()
        # 默认选中活动人设（若有）
        if self._personas:
            target = self._active_id or self._personas[0].get("id", "")
            self._select_id(target)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_top_label(self) -> QLabel:
        lbl = QLabel("当前使用角色：<b>（未设置）</b>")
        lbl.setObjectName("topInfo")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            "QLabel#topInfo{background:#f0e6f6;color:#6c3483;padding:8px 12px;"
            "border-radius:6px;font-size:12px;}"
        )
        self._top_label = lbl
        return lbl

    def _build_list_panel(self) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(4)
        lay.addWidget(QLabel("人设列表"))
        self._list = QListWidget()
        self._list.setMinimumWidth(150)
        self._list.currentItemChanged.connect(self._on_item_changed)
        lay.addWidget(self._list, 1)
        return wrap

    def _build_edit_panel(self) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(6, 0, 0, 0)
        lay.setSpacing(6)

        tag_row = QHBoxLayout()
        tag_row.addWidget(QLabel("标签："))
        self._tag_edit = QLineEdit()
        self._tag_edit.setPlaceholderText("角色名/标签，例如：温柔学姐、冷酷侦探、热心医生…")
        tag_row.addWidget(self._tag_edit, 1)
        lay.addLayout(tag_row)

        lay.addWidget(QLabel("人设内容（性格、说话风格、背景等，将注入角色模型的系统提示词）："))
        self._content_edit = QPlainTextEdit()
        self._content_edit.setPlaceholderText(
            "在这里编写该角色的设定，或点击「AI 生成」一键生成。\n"
            "示例：你是性格温柔细腻的学姐，说话轻声细语、善解人意，"
            "喜欢关心他人，常用“你最近怎么样呀？”这样亲切的语气开启话题。"
        )
        lay.addWidget(self._content_edit, 1)
        return wrap

    def _build_button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)

        self._btn_new = QPushButton("➕ 新建人设")
        self._btn_del = QPushButton("🗑 删除")
        self._btn_ai = QPushButton("✨ AI 生成")
        self._btn_save = QPushButton("💾 保存")
        self._btn_use = QPushButton("设为当前角色")
        self._btn_cancel = QPushButton("取消")

        for b in (self._btn_new, self._btn_del, self._btn_ai, self._btn_save):
            b.setStyleSheet(
                "QPushButton{background:#95a5a6;color:white;border-radius:5px;"
                "padding:6px 14px;font-weight:bold;}"
                "QPushButton:hover{background:#7f8c8d;}"
            )
        self._btn_use.setStyleSheet(
            "QPushButton{background:#8e44ad;color:white;border-radius:5px;"
            "padding:6px 18px;font-weight:bold;}"
            "QPushButton:hover{background:#7d3c98;}"
        )
        self._btn_cancel.setStyleSheet(
            "QPushButton{background:#e74c3c;color:white;border-radius:5px;"
            "padding:6px 18px;font-weight:bold;}"
            "QPushButton:hover{background:#c0392b;}"
        )

        self._btn_new.clicked.connect(self._on_new)
        self._btn_del.clicked.connect(self._on_delete)
        self._btn_ai.clicked.connect(self._on_ai_generate)
        self._btn_save.clicked.connect(self._on_save)
        self._btn_use.clicked.connect(self._on_use)
        self._btn_cancel.clicked.connect(self.reject)

        row.addWidget(self._btn_new)
        row.addWidget(self._btn_del)
        row.addWidget(self._btn_ai)
        row.addWidget(self._btn_save)
        row.addStretch(1)
        row.addWidget(self._btn_use)
        row.addWidget(self._btn_cancel)
        return row

    # ------------------------------------------------------------------
    # 数据与列表
    # ------------------------------------------------------------------
    def _current_persona(self) -> Optional[Dict[str, str]]:
        for p in self._personas:
            if p.get("id") == self._current_id:
                return p
        return None

    def _refresh_list(self) -> None:
        """按当前数据重建列表，并尽量保持当前选中项。"""
        prev = self._current_id
        self._list.blockSignals(True)
        self._list.clear()
        for p in self._personas:
            item = QListWidgetItem(p.get("tag") or "（无标签）")
            item.setData(Qt.ItemDataRole.UserRole, p.get("id", ""))
            if p.get("id") == self._active_id:
                item.setText((p.get("tag") or "（无标签）") + "  ●当前")
            self._list.addItem(item)
        self._list.blockSignals(False)
        if prev is not None:
            self._select_id(prev)
        self._update_top_label()
        self._refresh_buttons()

    def _select_id(self, pid: str) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == pid:
                self._list.setCurrentItem(item)
                return

    def _on_item_changed(self, cur, _prev) -> None:
        if cur is None:
            self._current_id = None
            self._tag_edit.clear()
            self._content_edit.clear()
            self._refresh_buttons()
            return
        self._current_id = cur.data(Qt.ItemDataRole.UserRole)
        p = self._current_persona()
        if p:
            self._tag_edit.setText(p.get("tag", ""))
            self._content_edit.setPlainText(p.get("content", ""))
        self._refresh_buttons()

    def _update_top_label(self) -> None:
        active = None
        for p in self._personas:
            if p.get("id") == self._active_id:
                active = p
                break
        if active:
            self._top_label.setText(
                "当前使用角色：<b>%s</b>" % (active.get("tag") or "（无标签）")
            )
        else:
            self._top_label.setText("当前使用角色：<b>（未设置）</b>")

    def _refresh_buttons(self) -> None:
        has = self._current_persona() is not None
        for b in (self._btn_del, self._btn_ai, self._btn_save, self._btn_use):
            b.setEnabled(has)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _on_new(self) -> None:
        p: Dict[str, str] = {"id": "p" + uuid.uuid4().hex[:12], "tag": "新角色", "content": ""}
        self._personas.append(p)
        self._refresh_list()
        self._select_id(p["id"])
        self._tag_edit.setFocus()
        self._tag_edit.selectAll()

    def _on_delete(self) -> None:
        p = self._current_persona()
        if not p:
            return
        if QMessageBox.question(self, "删除人设", f"确定删除人设「{p.get('tag','')}」吗？") \
                != QMessageBox.StandardButton.Yes:
            return
        self._personas.remove(p)
        if self._active_id == p["id"]:
            self._active_id = ""
            self._data["active_persona_id"] = ""
        self._current_id = None
        self._persist()
        self._refresh_list()
        if self._personas:
            self._select_id(self._personas[0].get("id", ""))

    def _on_save(self) -> None:
        p = self._current_persona()
        if not p:
            return
        p["tag"] = self._tag_edit.text().strip()
        p["content"] = self._content_edit.toPlainText().strip()
        self._persist()
        self._refresh_list()
        ToastBanner.show_message(self, "人设已保存", "success")

    def _on_use(self) -> None:
        p = self._current_persona()
        if not p:
            return
        p["tag"] = self._tag_edit.text().strip()
        p["content"] = self._content_edit.toPlainText().strip()
        self._active_id = p["id"]
        self._data["active_persona_id"] = p["id"]
        self._persist()
        self._refresh_list()
        self.personaSelected.emit()
        ToastBanner.show_message(self, f"已使用角色「{p['tag'] or '无标签'}」", "success")
        self.accept()

    def _on_ai_generate(self) -> None:
        p = self._current_persona()
        if not p:
            return
        tag = self._tag_edit.text().strip()
        if not tag:
            QMessageBox.information(self, "提示", "请先填写人设标签，再使用 AI 生成内容")
            self._tag_edit.setFocus()
            return
        self._btn_ai.setEnabled(False)
        self._btn_ai.setText("✨ 生成中…")

        self._generate_worker = _GenerateWorker(tag, self)
        self._generate_worker.finished.connect(self._on_generate_finished)
        self._generate_worker.failed.connect(self._on_generate_failed)
        self._generate_worker.finished.connect(self._generate_worker.deleteLater)
        self._generate_worker.failed.connect(self._generate_worker.deleteLater)
        self._generate_worker.start()

    def _on_generate_finished(self, content: str) -> None:
        p = self._current_persona()
        if p:
            p["content"] = content
            self._content_edit.setPlainText(content)
            ToastBanner.show_message(self, "人设已生成，可继续编辑或保存", "success")
        self._end_generate_state()

    def _on_generate_failed(self, message: str) -> None:
        ToastBanner.show_message(self, message, "warning")
        self._end_generate_state()

    def _end_generate_state(self) -> None:
        self._btn_ai.setEnabled(self._current_persona() is not None)
        self._btn_ai.setText("✨ AI 生成")
        self._generate_worker = None

    def closeEvent(self, event) -> None:
        if self._generate_worker is not None and self._generate_worker.isRunning():
            self._generate_worker.terminate()
            self._generate_worker.wait(500)
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _persist(self) -> None:
        self._data["personas"] = self._personas
        self._data["active_persona_id"] = self._active_id
        app_config.save_personas(self._data)

    @staticmethod
    def _stylesheet() -> str:
        return (
            "QDialog{background:#f5f6fa;}"
            "QListWidget{background:white;border:1px solid #d0d4dc;border-radius:6px;}"
            "QListWidget::item{height:28px;padding:2px 8px;}"
            "QListWidget::item:selected{background:#8e44ad;color:white;}"
            "QLineEdit{background:white;border:1px solid #ccc;border-radius:4px;padding:5px 8px;}"
            "QPlainTextEdit{background:white;border:1px solid #ccc;border-radius:4px;"
            "padding:6px;font-family:Consolas,'Microsoft YaHei',monospace;font-size:13px;}"
        )
