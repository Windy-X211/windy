# -*- coding: utf-8 -*-
"""自主代理经验库管理对话框。

- 顶部显示常用经验数、专用经验数、总数；
- 「常用 / 专用」滑动按钮切换显示两类经验；
- 经验以条为单位展示，左侧标记条数，可勾选后批量删除；
- 输入区按换行符解析为多条经验，一键添加（每行一条）。
"""

from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, QEasingCurve, QPropertyAnimation, QRectF, Signal, Property
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton, QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

import experience


class _SlideToggle(QAbstractButton):
    """两段滑动开关：0 = 常用，1 = 专用。"""

    stateChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = 0
        self._knob = 0.0  # 滑块位置 0..1
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(132, 28)
        self._anim.valueChanged.connect(lambda _v: self.update())

    def _get_knob(self) -> float:
        return self._knob

    def _set_knob(self, v: float) -> None:
        self._knob = v
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def state(self) -> int:
        return self._state

    def setState(self, st: int) -> None:
        st = 0 if st <= 0 else 1
        self._state = st
        self._anim.stop()
        self._knob = float(st)
        self.update()

    def mousePressEvent(self, e) -> None:
        self._state = 1 - self._state
        self.stateChanged.emit(self._state)
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(float(self._state))
        self._anim.start()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        half = w / 2
        # 轨道
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#8e44ad") if self._state == 0 else QColor("#1a5fb4"))
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        # 滑块（白色）
        x = self._knob * half
        knob_rect = QRectF(x + 3, 3, half - 6, h - 6)
        p.setBrush(QColor("white"))
        p.drawRoundedRect(knob_rect, (h - 6) / 2, (h - 6) / 2)
        # 文字：滑块上的用深色，轨道上的用白色，保证两侧始终可见
        font = p.font()
        font.setBold(True)
        font.setPointSize(9)
        p.setFont(font)
        p.setPen(QColor("#333333"))
        p.drawText(knob_rect, Qt.AlignmentFlag.AlignCenter,
                   "常用" if self._state == 0 else "专用")
        p.setPen(QColor("white"))
        if self._state == 0:
            p.drawText(QRectF(half, 0, half, h), Qt.AlignmentFlag.AlignCenter, "专用")
        else:
            p.drawText(QRectF(0, 0, half, h), Qt.AlignmentFlag.AlignCenter, "常用")


class ExperienceDialog(QDialog):
    """经验库管理窗口（非模态，可边执行任务边管理）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("经验库管理")
        self.resize(580, 540)
        self.setMinimumSize(480, 440)
        self._tab = 0  # 0=常用 1=专用
        self._checkboxes: List[QCheckBox] = []  # 行勾选框引用（随刷新重建）
        self._counts_text = ""

        root = QVBoxLayout(self)
        root.setSpacing(8)

        root.addWidget(self._build_header())
        root.addLayout(self._build_toggle_row())
        self._list_widget = QListWidget()
        self._list_widget.setStyleSheet(
            "QListWidget{background:white;border:1px solid #d0d4dc;border-radius:6px;"
            "font-size:12px;}"
            "QListWidget::item{border-bottom:1px solid #eef0f3;}"
        )
        root.addWidget(self._list_widget, 1)
        root.addLayout(self._build_input_row())
        root.addLayout(self._build_button_row())

        self._toggle.stateChanged.connect(self._on_tab_changed)
        self._refresh()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_header(self) -> QLabel:
        lbl = QLabel("")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            "background:#f0e6f6;color:#5b2c6f;padding:8px 12px;"
            "border-radius:6px;font-size:12px;font-weight:bold;"
        )
        self._counts_label = lbl
        return lbl

    def _build_toggle_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self._toggle = _SlideToggle()
        row.addWidget(self._toggle)
        hint = QLabel("常用经验会自动加入自主代理的系统提示词；专用经验仅在模型搜索时被检索。")
        hint.setStyleSheet("color:#7f8c8d;font-size:11px;")
        hint.setWordWrap(True)
        row.addWidget(hint, 1)
        return row

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("新增经验（每行一条）：")
        lbl.setStyleSheet("color:#555;font-size:12px;")
        lbl.setFixedWidth(110)
        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("输入一条或多条经验，每行一条（按换行符分隔）…")
        self._input.setFixedHeight(64)
        self._input.setStyleSheet(
            "QPlainTextEdit{background:white;border:1px solid #ccc;border-radius:6px;"
            "padding:4px;font-size:12px;}"
        )
        row.addWidget(lbl, alignment=Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._input, 1)
        return row

    def _build_button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self._btn_add = QPushButton("添加")
        self._btn_add.setStyleSheet(self._btn_style("#2e8b57"))
        self._btn_add.clicked.connect(self._on_add)

        self._btn_delete = QPushButton("删除选中")
        self._btn_delete.setStyleSheet(self._btn_style("#c0392b"))
        self._btn_delete.clicked.connect(self._on_delete_selected)

        self._btn_save = QPushButton("保存修改")
        self._btn_save.setStyleSheet(self._btn_style("#2471a3"))
        self._btn_save.clicked.connect(self._on_save_edits)
        self._btn_save.setToolTip("把已编辑的经验文本保存到经验库（也可直接点击经验条完成编辑后在别处失焦自动保存）")

        self._btn_clear = QPushButton("清空所有")
        self._btn_clear.setStyleSheet(self._btn_style("#7f8c8d"))
        self._btn_clear.clicked.connect(self._on_clear)

        row.addWidget(self._btn_add)
        row.addWidget(self._btn_delete)
        row.addWidget(self._btn_save)
        row.addWidget(self._btn_clear)
        row.addStretch(1)
        return row

    @staticmethod
    def _btn_style(color: str) -> str:
        return (
            "QPushButton{background:" + color + ";color:white;border:none;"
            "border-radius:5px;padding:5px 14px;font-weight:bold;font-size:12px;}"
            "QPushButton:hover{background:" + _lighten(color) + ";}"
        )

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------

    def refresh_all(self) -> None:
        """由外部（主窗口打开时）调用，确保数据与磁盘最新一致。"""
        self._refresh()

    def _current_texts(self) -> List[str]:
        return experience.get_common() if self._tab == 0 else experience.get_special()

    def _refresh(self) -> None:
        c, s, total = experience.counts()
        self._counts_text = f"经验库　常用 {c} 条　｜　专用 {s} 条　｜　总计 {total} 条"
        self._counts_label.setText(self._counts_text)
        texts = self._current_texts()
        self._list_widget.clear()
        self._checkboxes = []
        for i, text in enumerate(texts, 1):
            row = self._make_row(i, text)
            item = QListWidgetItem(self._list_widget)
            item.setSizeHint(row.sizeHint())
            self._list_widget.addItem(item)
            self._list_widget.setItemWidget(item, row)
            cb = row.findChild(QCheckBox)
            if cb is not None:
                self._checkboxes.append(cb)

    def _make_row(self, idx: int, text: str) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(8)
        cb = QCheckBox()
        cb.setToolTip("勾选后点击「删除选中」删除该条经验")
        num = QLabel(f"#{idx}")
        num.setFixedWidth(36)
        num.setStyleSheet("color:#7f8c8d;font-size:11px;font-weight:bold;")
        # 经验内容可原地编辑：失焦（或回车）即自动保存，也可点「保存修改」统一保存
        edit = QLineEdit(text)
        edit.setToolTip("点击即可编辑该条经验，回车或失焦自动保存")
        edit.setStyleSheet(
            "QLineEdit{background:transparent;border:1px solid transparent;"
            "border-radius:3px;color:#2c3e50;font-size:12px;padding:2px 4px;}"
            "QLineEdit:focus{background:white;border:1px solid #8e44ad;}"
        )
        edit.editingFinished.connect(lambda old=text, e=edit: self._save_row_edit(old, e))
        lay.addWidget(cb, alignment=Qt.AlignmentFlag.AlignTop)
        lay.addWidget(num, alignment=Qt.AlignmentFlag.AlignTop)
        lay.addWidget(edit, 1)
        return row

    def _save_row_edit(self, old_text: str, edit_widget) -> None:
        """单条经验编辑自动保存（回车/失焦触发）。"""
        new_text = edit_widget.text().strip()
        cat = "common" if self._tab == 0 else "special"
        if not new_text:
            # 清空视为删除该条
            if QMessageBox.question(
                self, "删除经验", "该条经验已清空，是否删除它？",
            ) == QMessageBox.StandardButton.Yes:
                experience.delete(cat, old_text)
                self._refresh()
            else:
                edit_widget.setText(old_text)
            return
        if new_text == old_text:
            return
        if experience.edit(cat, old_text, new_text):
            # 更新该行“原文”，使后续继续编辑以新文本为准；并刷新顶部计数
            c, s, total = experience.counts()
            self._counts_text = f"经验库　常用 {c} 条　｜　专用 {s} 条　｜　总计 {total} 条"
            self._counts_label.setText(self._counts_text)
        else:
            self._show_tip("保存失败：该条目可能已被修改或删除")
            edit_widget.setText(old_text)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def _on_tab_changed(self, state: int) -> None:
        self._tab = state
        self._refresh()

    def _on_add(self) -> None:
        texts = [
            line.strip() for line in self._input.toPlainText().splitlines()
            if line.strip()
        ]
        if not texts:
            QMessageBox.information(self, "提示", "请输入至少一条经验（每行一条）。")
            return
        cat = "common" if self._tab == 0 else "special"
        added = experience.extend(cat, texts)
        self._input.clear()
        if added > 0:
            self._show_tip(f"已添加 {added} 条{'常用' if self._tab == 0 else '专用'}经验")
        else:
            self._show_tip("未添加：输入的经验均已存在或为空")
        self._refresh()

    def _on_delete_selected(self) -> None:
        texts = self._current_texts()
        checked = [
            texts[i] for i, cb in enumerate(self._checkboxes)
            if i < len(texts) and cb.isChecked()
        ]
        if not checked:
            QMessageBox.information(self, "提示", "请先勾选要删除的经验条目。")
            return
        if QMessageBox.question(
            self, "确认删除",
            f"确定删除选中的 {len(checked)} 条经验吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        cat = "common" if self._tab == 0 else "special"
        removed = experience.delete_many(cat, checked)
        self._show_tip(f"已删除 {removed} 条经验" if removed else "没有需要删除的经验")
        self._refresh()

    def _on_save_edits(self) -> None:
        """统一保存当前分类下所有已编辑的经验文本。"""
        cat = "common" if self._tab == 0 else "special"
        saved = 0
        for i, item in enumerate(self._list_widget_rows()):
            row = self._list_widget.itemWidget(item)
            if row is None:
                continue
            edit = row.findChild(QLineEdit)
            if edit is None:
                continue
            new_text = edit.text().strip()
            cur = self._current_texts()
            if i < len(cur) and new_text and new_text != cur[i]:
                if experience.edit(cat, cur[i], new_text):
                    saved += 1
        self._show_tip(f"已保存 {saved} 条修改" if saved else "没有需要保存的修改")
        self._refresh()

    def _list_widget_rows(self):
        return [self._list_widget.item(row) for row in range(self._list_widget.count())]

    def _on_clear(self) -> None:
        if QMessageBox.question(
            self, "确认清空",
            f"确定清空全部{'常用' if self._tab == 0 else '专用'}经验吗？此操作不可撤销。",
        ) != QMessageBox.StandardButton.Yes:
            return
        if self._tab == 0:
            experience.set_common([])
        else:
            experience.set_special([])
        self._show_tip("已清空")
        self._refresh()

    def _show_tip(self, msg: str) -> None:
        self._counts_label.setText(msg + "　｜　" + self._counts_text)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh()


def _lighten(hex_color: str) -> str:
    """把 #RRGGBB 颜色提亮 15%，用于按钮 hover 态。"""
    try:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        r = min(255, int(r + (255 - r) * 0.15))
        g = min(255, int(g + (255 - g) * 0.15))
        b = min(255, int(b + (255 - b) * 0.15))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_color
