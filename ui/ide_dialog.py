# -*- coding: utf-8 -*-
"""IDE 窗口：项目管理与代码编辑。

- 左侧文件树：展示项目文件夹内全部文件（忽略常见垃圾目录），双击加载到编辑器；
- 中间编辑器：带行号的 QPlainTextEdit，支持新建/保存/插入行/删除行/查找替换；
- 底部控制台：选用 Python 解释器运行当前文件，实时输出，可停止。
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
from typing import List, Optional

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtGui import QFont, QTextCursor, QTextCharFormat, QColor, QBrush
from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QMessageBox, QInputDialog,
    QSplitter, QWidget, QComboBox, QAbstractItemView, QListWidget,
    QListWidgetItem, QTextEdit,
)

import app_config


_IGNORE_DIRS = {"__pycache__", ".git", ".hg", ".svn", "node_modules",
                "venv", ".venv", ".idea", "dist", "build", "__MACOSX"}

# 查找范围选项
_SCOPE_CURRENT = "当前文件"
_SCOPE_PROJECT = "整个项目"

# 高亮用统一的醒目橙色，避免灰色等不明显的底色
_FMT_ALL = QTextCharFormat()          # 全部匹配
_FMT_ALL.setBackground(QColor("#ff9100"))
_FMT_ALL.setForeground(QColor("#1a1a1a"))
_FMT_CUR = QTextCharFormat()          # 当前匹配（与全部匹配同色，视觉一致）
_FMT_CUR.setBackground(QColor("#ff9100"))
_FMT_CUR.setForeground(QColor("#1a1a1a"))


class _RunWorker(QThread):
    """后台线程运行程序并实时回传输出/退出码，可停止。"""

    output = Signal(str)   # 一行输出
    finished_run = Signal(int)  # 退出码

    def __init__(self, interp: str, script: str, cwd: str, parent=None):
        super().__init__(parent)
        self._cmd = [interp, script]
        self._cwd = cwd
        self._proc = None
        self._abort = threading.Event()

    def run(self) -> None:
        try:
            # 强制子进程以 UTF-8 输出，避免中文在 Windows 控制台编码（GBK）下乱码
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            self._proc = subprocess.Popen(
                self._cmd, cwd=self._cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env,
            )
            line_q: "queue.Queue[Optional[str]]" = queue.Queue()

            def reader() -> None:
                try:
                    for line in self._proc.stdout:
                        if self._abort.is_set():
                            break
                        line_q.put(line)
                finally:
                    line_q.put(None)

            threading.Thread(target=reader, daemon=True).start()
            while True:
                try:
                    line = line_q.get(timeout=0.1)
                except queue.Empty:
                    if self._abort.is_set():
                        self._kill()
                        break
                    continue
                if line is None:
                    break
                self.output.emit(line.rstrip("\n"))
            rc = self._proc.poll()
        except Exception as e:
            self.output.emit(f"启动失败：{e}")
            rc = -1
        finally:
            self.finished_run.emit(rc if rc is not None else 0)

    def stop(self) -> None:
        self._abort.set()
        self._kill()

    def _kill(self) -> None:
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
        except Exception:
            pass


class IdeDialog(QDialog):
    """项目管理 + 代码编辑 + 运行控制台的 IDE 窗口。"""

    _THEMES = {
        "light": (
            "QDialog{background:#ffffff;color:#333333;}"
            "QTreeWidget,QPlainTextEdit{background:#ffffff;color:#333333;"
            "border:1px solid #cfd8e3;font-family:Consolas,'Microsoft YaHei',monospace;font-size:13px;}"
            "QToolTip,QLabel{color:#333333;}"
            "QComboBox,QLineEdit{background:#ffffff;color:#333333;border:1px solid #cfd8e3;padding:3px;}"
            "QPushButton{background:#eef1f5;color:#2c3e50;border:1px solid #c5ccd6;border-radius:4px;padding:4px 10px;}"
            "QPushButton:hover{background:#dfe6ec;}"
        ),
        "dark": (
            "QDialog{background:#2b2b2b;color:#d8d8d8;}"
            "QTreeWidget,QPlainTextEdit{background:#1e1e1e;color:#d8d8d8;"
            "border:1px solid #3c3c3c;font-family:Consolas,'Microsoft YaHei',monospace;font-size:13px;}"
            "QToolTip,QLabel{color:#d8d8d8;}"
            "QComboBox,QLineEdit{background:#1e1e1e;color:#d8d8d8;border:1px solid #3c3c3c;padding:3px;}"
            "QPushButton{background:#3c3c3c;color:#e0e0e0;border:1px solid #555;border-radius:4px;padding:4px 10px;}"
            "QPushButton:hover{background:#4a4a4a;}"
        ),
    }

    def __init__(self, project_dir: str, parent=None):
        super().__init__(parent)
        self._project_dir = (project_dir or "").strip().rstrip("/\\")
        self._current_path: Optional[str] = None   # 当前编辑文件的绝对路径
        self._dirty = False
        self._worker: Optional[_RunWorker] = None
        # 查找相关状态
        self._file_matches: List[tuple] = []   # 当前文件内匹配 (start, length)
        self._cur_match: int = -1              # 当前文件当前匹配索引
        self._project_find_state = []          # 跨文件搜索结果 [(rel, abs, line)]
        self._project_find_idx = -1            # 跨文件当前结果索引
        self._current_tree_item: Optional[QTreeWidgetItem] = None  # 项目树中高亮的当前文件项
        # 主题：默认白色，可在设置中切换深色
        self._theme = app_config.load_settings().get("ide_theme", "light") or "light"
        if self._theme not in self._THEMES:
            self._theme = "light"
        self.setWindowTitle(f"IDE - {os.path.basename(self._project_dir) or self._project_dir}")
        self.resize(900, 620)
        self.setStyleSheet(self._THEMES[self._theme])

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # 左：文件树（支持多选）
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree.itemDoubleClicked.connect(self._on_tree_open)
        splitter.addWidget(self._tree)

        # 中：工具栏 + 编辑器 + 控制台
        editor_pane = QWidget()
        ecol = QVBoxLayout(editor_pane)
        ecol.setContentsMargins(0, 0, 0, 0)
        ecol.setSpacing(4)

        ecol.addWidget(self._build_toolbar())

        # 上下分隔条：上=编辑区(含查找结果)，下=控制台。通过拖动分隔条上边缘调整控制台高度
        self._body_splitter = QSplitter(Qt.Orientation.Vertical)
        self._body_splitter.setChildrenCollapsible(False)

        top_pane = QWidget()
        top_lay = QVBoxLayout(top_pane)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(4)
        self._editor_widget = self._build_editor()
        top_lay.addWidget(self._editor_widget, 1)
        self._results_wrap = self._build_results()
        self._results_wrap.setVisible(False)
        top_lay.addWidget(self._results_wrap, 2)
        self._body_splitter.addWidget(top_pane)

        self._console_widget = self._build_console()
        self._body_splitter.addWidget(self._console_widget)
        self._body_splitter.setStretchFactor(0, 3)
        self._body_splitter.setStretchFactor(1, 2)
        self._body_splitter.setSizes([400, 210])

        ecol.addWidget(self._body_splitter, 1)

        splitter.addWidget(editor_pane)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([260, 640])
        root.addWidget(splitter, 1)

        self._status = QLabel("")
        root.addWidget(self._status)

        self._load_tree()
        self._refresh_line_numbers()
        self._update_status()

    # ------------------------------------------------------------------
    # 工具栏 / 编辑器 / 控制台
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        for text, slot in (
            ("＋ 新建", self._new_file),
            ("💾 保存", self._save),
            ("↩ 撤销", self._undo),
            ("🗑 删除", self._delete_selected),
            ("＋行", self._insert_line),
            ("－行", self._delete_line),
        ):
            b = QPushButton(text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(slot)
            h.addWidget(b)
        h.addSpacing(8)
        h.addWidget(QLabel("查找"))
        self._find_input = QLineEdit()
        self._find_input.setPlaceholderText("输入关键字…")
        self._find_input.setFixedWidth(160)
        self._find_input.returnPressed.connect(self._find_next)
        self._find_input.textChanged.connect(self._on_find_text_changed)
        h.addWidget(self._find_input)
        self._scope_combo = QComboBox()
        self._scope_combo.setToolTip("查找范围（默认：当前打开的文件）")
        self._scope_combo.addItems([_SCOPE_CURRENT, _SCOPE_PROJECT])
        self._scope_combo.setFixedWidth(92)
        self._scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        h.addWidget(self._scope_combo)
        b_next = QPushButton("下一个")
        b_next.clicked.connect(self._find_next)
        h.addWidget(b_next)
        b_all = QPushButton("全部")
        b_all.clicked.connect(self._find_all)
        h.addWidget(b_all)
        h.addStretch(1)
        return bar

    def _build_results(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(6)
        self._results_label = QLabel("")
        head.addWidget(self._results_label)
        head.addStretch(1)
        b_clear = QPushButton("✕ 关闭")
        b_clear.clicked.connect(self._close_results)
        head.addWidget(b_clear)
        v.addLayout(head)
        self._results_list = QListWidget()
        self._results_list.setStyleSheet(
            "QListWidget{font-family:Consolas,'Microsoft YaHei',monospace;font-size:12px;}")
        self._results_list.itemClicked.connect(self._on_result_clicked)
        self._results_list.itemActivated.connect(self._on_result_clicked)
        v.addWidget(self._results_list)
        return wrap

    def _build_editor(self) -> QWidget:
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        self._line_numbers = QPlainTextEdit()
        self._line_numbers.setReadOnly(True)
        self._line_numbers.setFixedWidth(52)
        self._line_numbers.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._line_numbers.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        gutter_bg = "#ffffff" if self._theme == "light" else "#1e1e1e"
        gutter_fg = "#9aa7b4" if self._theme == "light" else "#6a859c"
        self._line_numbers.setStyleSheet(
            f"background:{gutter_bg};color:{gutter_fg};border:none;")
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPixelSize(13)
        self._line_numbers.setFont(font)

        self._editor = QPlainTextEdit()
        self._editor.setFont(font)
        self._editor.blockCountChanged.connect(self._refresh_line_numbers)
        self._editor.verticalScrollBar().valueChanged.connect(
            lambda v: self._line_numbers.verticalScrollBar().setValue(v))
        self._editor.textChanged.connect(self._on_text_changed)

        h.addWidget(self._line_numbers)
        h.addWidget(self._editor, 1)
        return wrap

    def _build_console(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        # 折叠条：始终显示，控制台正文可向下折叠
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._btn_collapse = QPushButton("▾ 控制台")
        self._btn_collapse.setFixedWidth(84)
        self._btn_collapse.setToolTip("折叠 / 展开控制台")
        self._btn_collapse.setCheckable(True)
        self._btn_collapse.clicked.connect(self._toggle_console)
        bar.addWidget(self._btn_collapse)
        bar.addStretch(1)

        # 控制台正文容器（折叠时整体隐藏）
        self._console_body = QWidget()
        bv = QVBoxLayout(self._console_body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(4)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        bv.addWidget(self._console, 1)

        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(QLabel("解释器"))
        self._interp_combo = QComboBox()
        self._interp_combo.setEditable(True)
        for cand in self._interpreter_candidates():
            self._interp_combo.addItem(cand)
        saved = (app_config.load_settings().get("pro_programming_interpreter") or "").strip()
        if saved:
            idx = self._interp_combo.findText(saved)
            self._interp_combo.setCurrentIndex(idx if idx >= 0 else self._interp_combo.count() - 1)
        row.addWidget(self._interp_combo, 1)
        self._btn_run = QPushButton("▶ 运行当前文件")
        self._btn_run.clicked.connect(self._run_current)
        row.addWidget(self._btn_run)
        self._btn_stop = QPushButton("■ 停止")
        self._btn_stop.setVisible(False)
        self._btn_stop.clicked.connect(self._stop_run)
        row.addWidget(self._btn_stop)
        bv.addLayout(row)

        v.addLayout(bar)
        v.addWidget(self._console_body, 1)
        return wrap

    def _toggle_console(self) -> None:
        collapsed = self._btn_collapse.isChecked()
        # 折叠时隐藏控制台正文（含解释器与运行/停止按钮）
        self._console_body.setVisible(not collapsed)
        self._btn_collapse.setText("▸ 控制台" if collapsed else "▾ 控制台")
        sp = getattr(self, "_body_splitter", None)
        if sp is None:
            return
        sizes = sp.sizes()
        total = max(1, sizes[0] + sizes[1])
        if collapsed:
            # 控制台仅保留折叠条高度，编辑区占满剩余空间
            bar_h = getattr(self, "_console_bar_h", 32)
            self._console_bar_h = bar_h
            sp.setSizes([max(0, total - bar_h), bar_h])
        else:
            # 展开：编辑区约 2/3、控制台约 1/3
            top = total * 2 // 3
            sp.setSizes([top, total - top])

    def _interpreter_candidates(self) -> List[str]:
        cands: List[str] = []
        if sys.executable:
            cands.append(sys.executable)
        for p in (r"C:\Python313\python.exe", r"F:\anaconda3\python.exe"):
            if os.path.isfile(p):
                cands.append(p)
        cands.append("python")
        # 去重
        seen = set()
        out = []
        for c in cands:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    # ------------------------------------------------------------------
    # 文件树
    # ------------------------------------------------------------------
    def _load_tree(self) -> None:
        self._tree.clear()
        self._current_tree_item = None
        if not self._project_dir or not os.path.isdir(self._project_dir):
            return
        root = QTreeWidgetItem([os.path.basename(self._project_dir) or self._project_dir])
        root.setData(0, Qt.ItemDataRole.UserRole, self._project_dir)
        self._tree.addTopLevelItem(root)
        self._populate(root, self._project_dir)
        root.setExpanded(True)

    def _populate(self, parent: QTreeWidgetItem, abspath: str) -> None:
        try:
            entries = sorted(os.listdir(abspath), key=lambda x: (os.path.isdir(os.path.join(abspath, x)), x.lower()))
        except OSError:
            return
        for name in entries:
            if name in _IGNORE_DIRS or name.startswith(".") or name.endswith(".pyc"):
                continue
            full = os.path.join(abspath, name)
            it = QTreeWidgetItem([name])
            it.setData(0, Qt.ItemDataRole.UserRole, full)
            parent.addChild(it)
            if os.path.isdir(full):
                it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                self._populate(it, full)

    def _on_tree_open(self, item: QTreeWidgetItem, _col: int) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path and os.path.isfile(path):
            self._open_file(path)

    def _select_tree_item(self, path: str) -> None:
        """在项目树中为当前打开的文件绘制持久的橙色高亮背景，并逐级展开使其可见。

        不依赖选中态（选中态失焦后会变灰），而是直接给该条目设置背景色，
        无论窗口/树是否获得焦点都保持醒目高亮。
        """
        real = os.path.realpath(path)

        def walk(item: QTreeWidgetItem) -> bool:
            for i in range(item.childCount()):
                child = item.child(i)
                cpath = child.data(0, Qt.ItemDataRole.UserRole)
                if cpath and os.path.realpath(cpath) == real:
                    # 先清除上个条目的高亮背景
                    if self._current_tree_item is not None:
                        self._current_tree_item.setBackground(0, QBrush())
                        self._current_tree_item.setForeground(0, QBrush())
                    child.setBackground(0, QBrush(QColor("#ff9100")))
                    child.setForeground(0, QBrush(QColor("#ffffff")))
                    self._current_tree_item = child
                    parent = child.parent()
                    while parent is not None:
                        parent.setExpanded(True)
                        parent = parent.parent()
                    # 清除选中态，避免选中高亮叠加/失焦变灰覆盖在橙色背景上；
                    # 不用 setCurrentItem，直接用背景色描绘高亮
                    self._tree.clearSelection()
                    self._tree.setCurrentItem(None)
                    self._tree.viewport().update()
                    self._tree.scrollToItem(child, QAbstractItemView.ScrollHint.PositionAtCenter)
                    return True
                if walk(child):
                    return True
            return False

        for i in range(self._tree.topLevelItemCount()):
            if walk(self._tree.topLevelItem(i)):
                break

    # ------------------------------------------------------------------
    # 文件打开 / 新建 / 保存
    # ------------------------------------------------------------------
    def _open_file(self, path: str) -> None:
        if self._dirty:
            ret = QMessageBox.question(self, "未保存", "当前文件有未保存修改，仍要切换吗？")
            if ret != QMessageBox.StandardButton.Yes:
                return
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.warning(self, "打开失败", str(e))
            return
        self._current_path = path
        self._editor.setPlainText(text)
        self._dirty = False
        self._update_title()
        self._select_tree_item(path)

    def _new_file(self) -> None:
        rel, ok = QInputDialog.getText(self, "新建文件", "文件相对路径（含扩展名，如 src/hello.py 或 README.md）:")
        if not ok or not rel.strip():
            return
        if self._dirty:
            if QMessageBox.question(self, "未保存", "当前文件有未保存修改，仍要新建吗？") != QMessageBox.StandardButton.Yes:
                return
        rel = rel.strip().lstrip("/\\")
        base = os.path.realpath(self._project_dir or ".")
        full = os.path.realpath(os.path.join(base, rel))
        if not (full == base or full.startswith(base + os.sep)):
            QMessageBox.warning(self, "路径越界", "不允许在项目目录外创建文件")
            return
        os.makedirs(os.path.dirname(full) or base, exist_ok=True)
        if not os.path.isfile(full):
            open(full, "w", encoding="utf-8").close()
        self._current_path = full
        self._editor.setPlainText("")
        self._dirty = False
        self._load_tree()
        self._update_title()
        self._select_tree_item(full)

    def _save(self) -> None:
        if not self._current_path:
            QMessageBox.information(self, "提示", "请先在左侧选择或新建一个文件")
            return
        try:
            os.makedirs(os.path.dirname(self._current_path) or ".", exist_ok=True)
            with open(self._current_path, "w", encoding="utf-8") as f:
                f.write(self._editor.toPlainText())
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self._dirty = False
        self._update_title()
        rel = os.path.relpath(self._current_path, self._project_dir or self._current_path)
        self._status.setText(f"已保存：{rel}  ✔")

    def _undo(self) -> None:
        """撤销编辑器上一次文本编辑（Ctrl+Z 同效）。"""
        self._editor.undo()
        self._editor.setFocus()

    def _delete_selected(self) -> None:
        """批量删除文件树中选中的文件/文件夹（Del 键同效）。"""
        items = self._tree.selectedItems()
        if not items:
            QMessageBox.information(
                self, "删除", "请先在左侧选择要删除的文件或文件夹（可按住 Ctrl/Shift 多选）")
            return
        # 收集绝对路径，剔除已被选中父目录涵盖的子项
        paths = []
        for it in items:
            p = it.data(0, Qt.ItemDataRole.UserRole)
            if p:
                paths.append(os.path.realpath(p))
        dedup = [p for p in paths
                 if not any(p != q and p.startswith(q + os.sep) for q in paths)]
        if not dedup:
            return
        names = "\n".join(f"· {os.path.basename(p) or p}" for p in dedup)
        if QMessageBox.question(
                self, "确认删除",
                f"确定删除这 {len(dedup)} 项吗？删除后不可恢复。\n\n{names}") \
                != QMessageBox.StandardButton.Yes:
            return
        failed = []
        for p in dedup:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
            except OSError as e:
                failed.append(f"{p}：{e}")
        if failed:
            QMessageBox.warning(self, "删除", "部分删除失败：\n" + "\n".join(failed))
        else:
            QMessageBox.information(self, "删除", f"已成功删除 {len(dedup)} 项")
        # 若删除了当前正在编辑的文件，则关闭编辑状态
        if self._current_path:
            cp = os.path.realpath(self._current_path)
            if any(cp == p or cp.startswith(p + os.sep) for p in dedup):
                self._current_path = None
                self._editor.clear()
                self._dirty = False
        self._load_tree()
        self._update_status()
        self._update_title()

    def _insert_line(self) -> None:
        cur = self._editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cur.insertText("\n")
        self._editor.setTextCursor(cur)

    def _delete_line(self) -> None:
        cur = self._editor.textCursor()
        if not cur.hasSelection():
            cur.select(QTextCursor.SelectionType.LineUnderCursor)
        cur.removeSelectedText()
        cur.deleteChar()
        self._editor.setTextCursor(cur)

    def _on_text_changed(self) -> None:
        if not self._dirty:
            self._dirty = True
        self._update_status()

    def _refresh_line_numbers(self) -> None:
        n = self._editor.blockCount()
        nums = "\n".join(str(i) for i in range(1, n + 1))
        if nums:
            self._line_numbers.setPlainText(nums)

    def _update_status(self) -> None:
        rel = "（尚未选择文件）"
        if self._current_path:
            rel = os.path.relpath(self._current_path, self._project_dir or self._current_path)
        lines = self._editor.blockCount()
        self._status.setText(f"{rel}  |  {lines} 行" + ("  *未保存" if self._dirty else ""))

    def _update_title(self) -> None:
        base = os.path.basename(self._project_dir) or self._project_dir
        self.setWindowTitle(f"IDE - {base}" + (" *" if self._dirty else ""))

    # ------------------------------------------------------------------
    # 查找
    # ------------------------------------------------------------------
    def _scope(self) -> str:
        return self._scope_combo.currentText() if hasattr(self, "_scope_combo") else _SCOPE_CURRENT

    def _on_scope_changed(self) -> None:
        # 切换范围时清掉上次的高亮与结果，避免误导
        self._file_matches = []
        self._cur_match = -1
        self._project_find_state = []
        self._project_find_idx = -1
        self._clear_editor_highlights()
        if self._scope() == _SCOPE_CURRENT:
            self._close_results()

    def _on_find_text_changed(self, _txt: str) -> None:
        # 关键字变化时，之前的位置/高亮/结果需作废
        self._file_matches = []
        self._cur_match = -1
        self._project_find_state = []
        self._project_find_idx = -1
        self._clear_editor_highlights()
        if self._scope() == _SCOPE_PROJECT:
            self._results_list.clear()
            self._results_label.setText("")

    def _apply_editor_selections(self, extra: list) -> None:
        """设置编辑器高亮选区并立即强制重绘，无需点击/聚焦也能显示。"""
        self._editor.setExtraSelections(extra)
        self._editor.viewport().update()
        QApplication.processEvents()

    def _clear_editor_highlights(self) -> None:
        self._apply_editor_selections([])

    def _focus_match(self, idx: int) -> None:
        """把编辑器当前匹配亮色套用到 idx 对应的匹配上。"""
        if not self._file_matches:
            return
        extra = []
        for i, (st, ln) in enumerate(self._file_matches):
            c = self._editor.textCursor()
            c.setPosition(st)
            c.setPosition(st + ln, QTextCursor.MoveMode.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = c
            sel.format = _FMT_CUR if i == idx else _FMT_ALL
            extra.append(sel)
        self._apply_editor_selections(extra)
        st, ln = self._file_matches[idx]
        self._editor._last_find_pos = st              # 供 _find_next 计算下一个起点
        self._editor._last_find_len = ln
        cur = self._editor.textCursor()
        cur.setPosition(st)
        self._editor.setTextCursor(cur)
        self._editor.centerCursor()
        self._editor.setFocus()

    def _find_next(self) -> None:
        kw = self._find_input.text()
        if not kw:
            return
        if self._scope() == _SCOPE_PROJECT:
            if not self._project_find_state:
                self._project_find(goto=True)
            else:
                nxt = (self._project_find_idx + 1) % len(self._project_find_state)
                self._jump_result(nxt)
            return
        self._build_file_matches(kw)
        if not self._file_matches:
            self._clear_editor_highlights()
            self._editor.moveCursor(QTextCursor.MoveOperation.Start)
            return
        # 从当前匹配向后推进
        cursor_pos = getattr(self._editor, "_last_find_pos", -1) + \
            getattr(self._editor, "_last_find_len", 0) if getattr(self, "_cur_match", -1) >= 0 else self._editor.textCursor().position()
        start_idx = 0
        for i, (st, _ln) in enumerate(self._file_matches):
            if st >= cursor_pos:
                start_idx = i
                break
        else:
            start_idx = 0
        self._cur_match = start_idx
        self._focus_match(start_idx)

    def _find_all(self) -> None:
        kw = self._find_input.text()
        if not kw:
            return
        if self._scope() == _SCOPE_PROJECT:
            self._project_find(goto=False)
            return
        self._build_file_matches(kw)
        self._cur_match = -1
        if not self._file_matches:
            self._clear_editor_highlights()
            self._editor.moveCursor(QTextCursor.MoveOperation.Start)
            return
        # 全部匹配都套用醒目底色
        extra = []
        for st, ln in self._file_matches:
            c = self._editor.textCursor()
            c.setPosition(st)
            c.setPosition(st + ln, QTextCursor.MoveMode.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = c
            sel.format = _FMT_ALL
            extra.append(sel)
        self._apply_editor_selections(extra)

    def _build_file_matches(self, kw: str) -> None:
        """扫描当前编辑器文档，重建匹配位置列表（按位置排序）。"""
        self._file_matches = []
        text = self._editor.toPlainText()
        pos = 0
        while True:
            idx = text.find(kw, pos)
            if idx < 0:
                break
            self._file_matches.append((idx, len(kw)))
            pos = idx + max(1, len(kw))
        self._cur_match = -1

    # ------------------------------------------------------------------
    # 跨文件查找（整个项目范围）
    # ------------------------------------------------------------------
    def _project_find(self, goto: bool) -> None:
        kw = self._find_input.text()
        results: List[tuple] = []
        base = os.path.realpath(self._project_dir or ".")
        if os.path.isdir(base):
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs
                           if d not in _IGNORE_DIRS and not d.startswith(".")]
                for name in files:
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, base)
                    try:
                        with open(full, "r", encoding="utf-8", errors="replace") as f:
                            text = f.read()
                    except OSError:
                        continue
                    for ln, line in enumerate(text.splitlines(), 1):
                        if kw in line:
                            # (相对路径, 绝对路径, 行号, 预览文本)
                            results.append((rel, full, ln, line.rstrip("\n")[:120]))
        self._project_find_state = results
        self._project_find_idx = -1
        self._results_list.clear()
        self._results_label.setText(
            "项目中匹配 %d 处（点击定位，'下一个'可逐个跳转）" % len(results))
        for rel, _full, ln, prev in results:
            it = QListWidgetItem(f"{rel}:{ln}:  {prev}")
            it.setData(Qt.ItemDataRole.UserRole, (rel, _full, ln))
            self._results_list.addItem(it)
        self._results_wrap.setVisible(True)
        if goto and results:
            self._jump_result(0)

    def _on_result_clicked(self, it: QListWidgetItem) -> None:
        data = it.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        self._project_find_idx = self._results_list.row(it)
        rel, full, ln = data
        self._jump_result_at(full, rel, ln)

    def _jump_result(self, idx: int) -> None:
        if not self._project_find_state:
            return
        rel, full, ln = self._project_find_state[idx][:3]
        self._project_find_idx = idx
        self._results_list.setCurrentRow(idx)
        self._jump_result_at(full, rel, ln)

    def _jump_result_at(self, full: str, rel: str, ln: int) -> None:
        kw = self._find_input.text()
        self._open_file(full)
        # 定位到第 ln 行
        doc = self._editor.document()
        block = doc.findBlockByNumber(max(0, ln - 1))
        if not block.isValid():
            return
        st = block.position()
        # 在该行内定位关键字并高亮
        line = block.text()
        col = line.find(kw)
        if col >= 0:
            st += col
            ln_len = len(kw)
        else:
            ln_len = 0
        cur = self._editor.textCursor()
        cur.setPosition(st)
        if ln_len:
            cur.setPosition(st + ln_len, QTextCursor.MoveMode.KeepAnchor)
        self._editor.setTextCursor(cur)
        self._editor.centerCursor()
        self._editor.setFocus()
        if ln_len:
            extra = []
            c = self._editor.textCursor()
            c.setPosition(st)
            c.setPosition(st + ln_len, QTextCursor.MoveMode.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = c
            sel.format = _FMT_CUR
            extra.append(sel)
            self._apply_editor_selections(extra)

    def _close_results(self) -> None:
        self._results_wrap.setVisible(False)
        self._results_list.clear()
        self._project_find_state = []
        self._project_find_idx = -1
        self._clear_editor_highlights()

    # ------------------------------------------------------------------
    # 运行 / 停止
    # ------------------------------------------------------------------
    def _run_current(self) -> None:
        if self._current_path and self._dirty:
            self._save()
        if not self._current_path or not os.path.isfile(self._current_path):
            QMessageBox.information(self, "提示", "请先选择要运行的文件")
            return
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "提示", "已有程序在运行，请先停止")
            return
        interp = self._interp_combo.currentText().strip() or sys.executable
        # 持久化所选解释器
        s = app_config.load_settings()
        s["pro_programming_interpreter"] = interp
        app_config.save_settings(s)

        self._console.appendPlainText(f"\n>>> python {os.path.basename(self._current_path)}")
        self._worker = _RunWorker(interp, self._current_path, self._project_dir or ".")
        self._worker.output.connect(self._console.appendPlainText)
        self._worker.finished_run.connect(self._on_run_done)
        self._btn_run.setVisible(False)
        self._btn_stop.setVisible(True)
        self._worker.start()

    def _stop_run(self) -> None:
        if self._worker:
            self._worker.stop()

    def _on_run_done(self, rc: int) -> None:
        self._console.appendPlainText(f"\n[进程结束，退出码 {rc}]")
        self._btn_run.setVisible(True)
        self._btn_stop.setVisible(False)
        self._worker = None

    def keyPressEvent(self, event) -> None:
        # 文件树聚焦时按 Del 删除选中项
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) \
                and self._tree.hasFocus() and self._tree.selectedItems():
            event.accept()
            self._delete_selected()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
        if self._dirty:
            ret = QMessageBox.question(self, "未保存", "有未保存修改，确要退出？",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                       QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()