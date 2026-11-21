# -*- coding: utf-8 -*-
"""聊天视图：微信式左右气泡。user 右(浅蓝)，assistant 左(浅灰)。

assistant 气泡渲染 Markdown（GFM 语法，支持表格、代码块、列表、标题等）。
"""

from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import Qt, QSize, Signal, QTimer
from PySide6.QtGui import (
    QTextDocument, QTextOption, QFont, QColor, QPalette, QTextCursor,
)
from PySide6.QtWidgets import (
    QScrollArea, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QSizePolicy, QTextBrowser, QTextEdit, QPushButton, QPlainTextEdit,
    QProgressBar,
)

from step_model import step_display_summary
from ui.video_background import load_image_pixmap


# assistant 气泡内 Markdown 渲染的样式表（注入到 QTextDocument）
_MD_CSS = """
body { color: #222; font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
       font-size: 14px; line-height: 1.5; }
h1, h2, h3, h4 { color: #1a1a1a; font-weight: bold; margin-top: 8px; }
h1 { font-size: 18px; } h2 { font-size: 16px; } h3 { font-size: 15px; }
p { margin: 4px 0; }
code { background: #f5f5f5; color: #c7254e; padding: 1px 4px;
       border-radius: 3px; font-family: Consolas, "Courier New", monospace;
       font-size: 13px; }
pre { background: #2d2d2d; color: #f8f8f2; padding: 8px;
      border-radius: 5px; font-family: Consolas, "Courier New", monospace;
      font-size: 13px; white-space: pre-wrap; }
pre code { background: transparent; color: inherit; padding: 0; }
table { border-collapse: collapse; margin: 6px 0; }
th, td { border: 1px solid #ddd; padding: 4px 8px; }
th { background: #f0f0f0; font-weight: bold; }
tr:nth-child(even) { background: #fafafa; }
ul, ol { margin: 4px 0; padding-left: 20px; }
blockquote { border-left: 3px solid #ccc; color: #666;
             margin: 4px 0; padding-left: 10px; }
a { color: #3498db; }
"""

# 消息行与窗口边缘的留白（px）。
# - 外侧：容器左右边距(2) + _MESSAGE_SIDE_GAP = 10，即气泡贴窗口边缘一侧留 10px
# - 内侧：气泡(含 padding)对侧边缘到窗口对侧边缘 = 48px
# 用户消息靠右、模型消息靠左，两侧按此收拢以区分消息源。
# 注意：BubbleWidget 自身左右 padding(12*2=24) 会撑大气泡宽度，
# 因此内容可用宽 = 窗口宽 - 4(容器左右边距) - 8(侧留白) - 70(内侧+padding 修正)。
_MESSAGE_SIDE_GAP = 8    # 与容器左右边距 2 合计 = 10（外侧留白）
_MESSAGE_INNER_GAP = 70  # 46(目标内侧48相关) + 24(BubbleWidget padding 12*2)

# 消息气泡底色（RGB，不含透明度），配合「消息泡泡透明度」设置渲染为 rgba：
# user=浅蓝 #d6eaff，assistant=浅灰 #f0f0f0。
_USER_BUBBLE_BG = (214, 234, 255)
_ASSISTANT_BUBBLE_BG = (240, 240, 240)
_CODE_BUBBLE_BG = (234, 231, 247)   # 代码框浅紫底 #eae7f7（区别于消息泡泡浅蓝/浅灰）
_THINKING_BUBBLE_BG = (252, 243, 224)  # 深度思考框浅琥珀底 #fcf3e0
_RESULT_OK_BG = (232, 246, 236)     # 运行成功浅绿底 #e8f6ec
_RESULT_ERR_BG = (253, 236, 234)    # 运行错误浅红底 #fdecea


class MarkdownBubble(QTextBrowser):
    """支持 Markdown 渲染的只读气泡内容（assistant 用）。

    使用 QTextBrowser + setMarkdown(MarkdownDialectGitHub) 渲染表格/代码块等。
    高度自适应内容，宽度跟随气泡容器。
    """

    # 流式阶段 sizeHint/geometry 更新节流间隔（毫秒）
    _STREAM_GEOMETRY_INTERVAL_MS = 150
    # 气泡宽度下限：保证短消息不至于过窄难看。
    # 上限不再固定：长消息宽度由 sizeHint 跟随容器可用宽度(avail)，
    # 以保证气泡内侧边缘到窗口对侧保持 48px 的目标留白。
    _MIN_WIDTH = 120
    _MAX_WIDTH = 4096

    def __init__(self, text: str, markdown: bool = True, parent=None):
        super().__init__(parent)
        self._text = text
        self._markdown = markdown  # True=Markdown 渲染(assistant)；False=纯文本(user)
        self._ideal_width = 0  # 内容理想宽度（自然不换行所需宽度，cap 在 max）
        self._streaming_mode = False  # 是否处于流式追加阶段
        self._streaming_locked_width: int = 0  # 流式阶段锁定的固定宽度（0=未锁定）
        self._stream_geometry_timer: Optional[QTimer] = None
        # 只读 + 透明背景 + 无边框
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewportMargins(2, 2, 2, 2)
        # 透明背景
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(0, 0, 0, 0))
        pal.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0, 0))
        self.setPalette(pal)
        # 文字交互：可选中复制
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        # 禁用 QTextBrowser 自带的原生右键菜单（黑底英文字体），
        # 让右键事件上抛给父泡泡 BubbleWidget，统一使用中文菜单。
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        # 尺寸策略：水平可伸缩，高度跟随内容
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        # 最大宽度设为一个合理上限（避免窗口全屏时气泡横拉过长），
        # 但不会小于主窗口默认宽度的 85%；实际可用宽度由 sizeHint 跟随容器。
        self.setMaximumWidth(self._MAX_WIDTH)
        self.setMinimumWidth(self._MIN_WIDTH)
        # 渲染初始内容
        self.set_markdown(text)

    def set_markdown(self, text: str) -> None:
        """渲染文本：markdown=True 时按 Markdown 渲染，否则按纯文本渲染。

        纯文本模式（user 消息）复用完全相同的宽度/换行/高度自适应逻辑，
        但避免用户输入中的 * # 等字符被误解析为 Markdown。
        """
        self._text = text
        doc = self.document()
        doc.setDefaultStyleSheet(_MD_CSS)
        if self._markdown:
            doc.setMarkdown(text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
        else:
            doc.setPlainText(text)
        # 文档左右边距设为 0：换行边界直接由 textWidth 决定，
        # 避免文档自带 margin 让换行跑到文字可显示区域之外
        doc.setDocumentMargin(0)
        # 设置默认字体
        font = QFont("Microsoft YaHei", 10)
        doc.setDefaultFont(font)
        # 文本换行：既设置文档选项，也设置控件级换行模式（setMarkdown 重新解析后仍生效）
        txt_opt = QTextOption()
        txt_opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(txt_opt)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setLineWrapMode(QTextEdit.LineWrapMode.FixedPixelWidth)
        # 计算理想宽度：不换行时文档自然宽度，cap 在最大宽度
        # 用一个很大的 textWidth 让文档不换行，测量 idealWidth
        doc.setTextWidth(10000)
        ideal = int(doc.idealWidth()) + 12  # 留余量给 padding/scrollbar
        self._ideal_width = min(ideal, self.maximumWidth())
        # 测量完立即恢复文档宽度为“视口内容区宽度”（视口宽 - 左右边距 6px）：
        # 若残留 10000 的超宽宽度，文字会变成可横向滚动、行尾字符被视口裁掉半个字
        vw = self.viewport().width()
        if vw > 0:
            self._set_wrap_width(vw - 6)
        else:
            self._set_wrap_width(400)  # 未布局前先用合理默认，布局后 resizeEvent 会修正
        # 触发尺寸更新
        self.updateGeometry()
        # 重置滚动位置到顶部，防止文字被推离视口
        self.verticalScrollBar().setValue(0)

    def start_streaming(self) -> None:
        """进入流式追加模式：锁定气泡宽度（避免宽度跳动）并节流 geometry 更新。"""
        self._streaming_mode = True
        # 读取当前容器可用宽度，把整个流式阶段固定到该宽度，后续只增长高度
        parent_widget = self.parentWidget()
        if parent_widget is not None:
            avail = parent_widget.width()
        else:
            avail = 0
        # 如果还没有 ideal（空气泡先建再流式），用合理默认值
        base_width = self._ideal_width if self._ideal_width > 0 else 420
        # 锁定宽度 = 夹在 [min, max] 之间，优先用容器可用宽度减去 padding
        if avail > 0:
            locked = max(self.minimumWidth(), min(avail - 12, self.maximumWidth()))
        else:
            locked = max(self.minimumWidth(), min(base_width, self.maximumWidth()))
        # 流式阶段把 maximumWidth 设为锁定值，且用固定宽度避免跳动
        self.setMaximumWidth(locked)
        self.setMinimumWidth(locked)
        self._streaming_locked_width = locked
        if self._stream_geometry_timer is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(self._STREAM_GEOMETRY_INTERVAL_MS)
            t.timeout.connect(self._on_stream_geometry_tick)
            self._stream_geometry_timer = t

    def finish_streaming(self) -> None:
        """结束流式追加模式：用完整 Markdown 重渲染积累文本 + 解除宽度锁定。"""
        self._streaming_mode = False
        if self._stream_geometry_timer is not None:
            self._stream_geometry_timer.stop()
            self._stream_geometry_timer = None
        # 解除流式阶段的宽度锁定，恢复 [minW, maxW] 自由范围
        if self._streaming_locked_width > 0:
            self.setMinimumWidth(self._MIN_WIDTH)
            self.setMaximumWidth(self._MAX_WIDTH)
            self._streaming_locked_width = 0
        # 流式阶段只插入了纯文本，结束时用 Markdown 重渲染完整文本
        self.set_markdown(self._text)
        self.updateGeometry()

    def _on_stream_geometry_tick(self) -> None:
        """流式阶段节流：定时重渲染 Markdown + 更新 geometry。"""
        if self._streaming_mode:
            # 流式阶段每 150ms 用 Markdown 重渲染完整文本，实现实时渲染
            self.set_markdown(self._text)
        self.updateGeometry()

    def append_markdown(self, chunk: str) -> None:
        """流式追加：先插入纯文本保持即时显示，再由节流定时器（150ms）重渲染 Markdown。

        流式阶段先插入纯文本给用户即时反馈，同时 150ms 节流定时器触发
        set_markdown() 重渲染完整文本，实现实时 Markdown 渲染而不会每 chunk 卡顿。
        set_markdown() 本身会触发 geometry 更新，无需额外调用。
        """
        if not chunk:
            return
        self._text += chunk
        # 用 cursor 直接追加纯文本（不触发 setMarkdown 全量重解析）
        # textCursor() 是 QTextBrowser 的方法，不是 QTextDocument 的
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(chunk)
        # 不要调用 setTextCursor(cursor)，否则 QTextBrowser 会自动滚动到末尾
        # 导致顶部文字被推离视口而无法滚动回来
        # 流式阶段：节流 geometry 更新（150ms 一次），避免卡顿
        if self._streaming_mode and self._stream_geometry_timer is not None:
            if not self._stream_geometry_timer.isActive():
                self._stream_geometry_timer.start()
        else:
            self.updateGeometry()

    @property
    def text(self) -> str:
        return self._text

    def sizeHint(self) -> QSize:
        """根据文档内容计算合适尺寸。

        宽度策略：
         - 流式锁定阶段：直接用锁定宽度（流式全程不跳动）
         - 短文本（理想宽度 < 容器可用宽度）：贴 ideal_width，无右侧空白
         - 长文本（理想宽度 >= 容器可用宽度）：跟随容器宽度，自动换行避免被截断
         - 最终夹在 [minimumWidth, maximumWidth] 之间
        高度：按该宽度换行后的文档高度
        """
        doc = self.document()
        # 流式阶段：宽度锁死不变化，只更新高度
        if self._streaming_locked_width > 0:
            w = self._streaming_locked_width
            # 换行宽度由 resizeEvent 按实际视口宽度统一控制，这里只按
            # “控件宽 - 左右边距 6px”计算文档高度，避免 sizeHint 与
            # resizeEvent 竞争导致换行宽度在窗口变宽时残留过大的理想值
            doc.setTextWidth(max(1, w - 4 - 6))
            h = int(doc.size().height()) + 20
            return QSize(w, h)
        # 可用宽度取对话区可视宽度（减去左右内边距），而非当前控件宽度。
        # 用当前控件宽度会让气泡在流式结束/窗口缩放后一直贴着旧宽度，
        # 短消息右侧留下大片空白；这里始终按“文字 vs 容器”重算。
        avail = self._chat_avail_width()
        if self._ideal_width >= avail:
            # 长文本：贴容器宽度，保证不被挤压过窄
            w = avail
        else:
            # 短文本：贴文字自然宽度，不留右侧空白
            w = self._ideal_width if self._ideal_width > 0 else avail
        # 夹在最小/最大之间
        w = max(self.minimumWidth(), min(w, self.maximumWidth()))
        # 换行宽度由 resizeEvent 按实际视口宽度统一控制，这里只按
        # “控件宽 - 左右边距 6px”计算文档高度，避免 sizeHint 与
        # resizeEvent 竞争导致换行宽度在窗口变宽时残留过大的理想值
        doc.setTextWidth(max(1, w - 4 - 6))
        h = int(doc.size().height()) + 20  # 上下边距（viewport margin 2+2 + 文档 margin 4+4 + 安全余量）
        return QSize(w, h)

    def _chat_avail_width(self) -> int:
        """向上找到 ChatView，返回气泡可用的最大宽度（可视宽度 - 左右内边距）。

        拿不到（独立使用 / 尚未布局）时回退到最大宽度。
        """
        p = self.parentWidget()
        while p is not None:
            if isinstance(p, ChatView):
                vw = p.viewport().width()
                if vw > 0:
                    # 内容可用宽 = 窗口宽 - 容器左右边距(4) - 侧留白(8) - 内侧+padding(70)
                    # 使可见气泡(BubbleWidget)宽 = vw - 58，内侧边缘距窗口对侧 = 48
                    return vw - 4 - _MESSAGE_SIDE_GAP - _MESSAGE_INNER_GAP
                return self.maximumWidth()
            p = p.parentWidget()
        return self.maximumWidth()

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def wheelEvent(self, event):
        """忽略滚轮事件，防止文字在气泡内随滚轮滚动而被遮挡。

        事件会自然传播到父级 QScrollArea 处理整体滚动。
        """
        event.ignore()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        """禁止横向滚动：文字不能左右挪动，只允许纵向。

        QTextBrowser 即使隐藏了滚动条，文档宽度略超视口时仍可能被
        拖动/触摸板等触发横向位移；这里把横向位移强制为 0。
        """
        super().scrollContentsBy(0, dy)

    def resizeEvent(self, event):
        """宽度变化时重算文档布局并通知布局系统更新高度。"""
        super().resizeEvent(event)
        doc = self.document()
        # 文档宽度 = 视口内容区宽度（视口宽 - 左右边距 6px），
        # 与 sizeHint/set_markdown 口径一致：换行边界贴着文字可显示区域，
        # 不会延伸到泡泡 padding 区域或被边缘遮住
        w = self.viewport().width()
        if w > 0:
            self._set_wrap_width(w - 6)
        # 通知布局系统：文档宽度变化后高度可能也变了，需重新计算气泡高度
        self.updateGeometry()

    def _set_wrap_width(self, width: int) -> None:
        """以固定像素宽度设定换行边界（视口内容区宽度，再留 6px 安全边距）。

        FixedPixelWidth 模式下 textWidth 完全由 lineWrapColumnOrWidth 决定，
        不会像 WidgetWidth 那样被 Qt 自动加上光标宽度(+2)而超出视口，
        从而保证换行边界严格在可视区域内：行尾不被裁剪、不出现横向滚动。
        """
        w = max(1, width)
        self.setLineWrapColumnOrWidth(w)
        self.document().setTextWidth(w)


class BubbleWidget(QFrame):
    """单条消息气泡。

    assistant 用 MarkdownBubble（渲染 Markdown + 表格）。
    user 用 QLabel（纯文本，避免用户输入被误解析为 Markdown）。
    """

    deleteRequested = Signal()
    recallRequested = Signal()
    undoRequested = Signal()

    def __init__(self, text: str, role: str, parent=None, bg_opacity: int = 0):
        super().__init__(parent)
        self.role = role
        self._text = text
        self._bg_opacity = 0
        # 气泡外框样式（背景色随透明度设置渲染为 rgba）
        self.set_background_opacity(bg_opacity)

        # assistant：Markdown 渲染；user：纯文本模式。
        # 两者用同一个 MarkdownBubble，宽度/换行/高度自适应逻辑完全一致：
        # 短消息贴内容宽度、长消息贴容器宽度自动换行、上限避免横拉过大。
        self._content = MarkdownBubble(text, markdown=(role == "assistant"))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._content)
        # 水平 Preferred：可跟随内容/容器宽度（短消息贴边、长消息贴容器换行）
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        # 上限放宽：长消息宽度由内容/容器可用宽度决定，保证内侧留白恒定
        self.setMaximumWidth(MarkdownBubble._MAX_WIDTH + 24)
        # 下限=内容最小宽+左右 padding(12*2)
        self.setMinimumWidth(MarkdownBubble._MIN_WIDTH + 24)

    def set_background_opacity(self, opacity: int) -> None:
        """设置气泡背景透明度：0=不透明，100=完全透明（仅背景淡化，文字保持清晰）。"""
        self._bg_opacity = max(0, min(100, int(opacity)))
        alpha = int(round(255 * (100 - self._bg_opacity) / 100))
        if self.role == "user":
            r, g, b = _USER_BUBBLE_BG
        else:
            r, g, b = _ASSISTANT_BUBBLE_BG
        self.setStyleSheet(
            f"BubbleWidget{{background:rgba({r},{g},{b},{alpha});"
            f"border-radius:10px;padding:8px 12px;}}"
        )

    def append_text(self, chunk: str) -> None:
        self._text += chunk
        if isinstance(self._content, MarkdownBubble):
            self._content.append_markdown(chunk)
        else:
            self._content.setText(self._text)

    def set_text(self, text: str) -> None:
        self._text = text
        if isinstance(self._content, MarkdownBubble):
            self._content.set_markdown(text)
        else:
            self._content.setText(text)

    @property
    def text(self) -> str:
        return self._text

    def contextMenuEvent(self, event) -> None:
        from PySide6.QtWidgets import QMenu, QApplication
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {background-color: white; color: black; border: 1px solid #ccc;}
            QMenu::item {padding: 4px 20px;}
            QMenu::item:selected {background-color: #0078d7; color: white;}
        """)
        copy_action = menu.addAction("📋 复制")
        if self.role == "user":
            recall_action = menu.addAction("↩ 撤回")
            if getattr(self, "_pro_undo", False):
                undo_action = menu.addAction("↺ 撤销（还原项目）")
        delete_action = menu.addAction("🗑 删除")
        action = menu.exec(event.globalPos())
        if action == copy_action:
            QApplication.clipboard().setText(self._text)
        elif self.role == "user" and action == recall_action:
            self.recallRequested.emit()
        elif self.role == "user" and getattr(self, "_pro_undo", False) and action == undo_action:
            self.undoRequested.emit()
        elif action == delete_action:
            self.deleteRequested.emit()


_USER_BUBBLE_BG = (249, 250, 251)


def _make_pill_palette(pal, r, g, b, a):
    """设置 QFrame/QWidget 的半透明背景（用 QPalette，不设样式表，
    避免遮蔽容器声明的白底圆角 QToolTip）。"""
    pal.setColor(QPalette.ColorRole.Window, QColor(r, g, b, a))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#333333"))
    return pal


class _AttachmentViewItem(QFrame):
    """消息泡泡中的附件项：文件→小图标+文件名；图片→缩略图。双击触发 activated(path)。"""

    activated = Signal(str)

    def __init__(self, path: str, is_image: bool, parent=None):
        super().__init__(parent)
        self._path = path
        name = os.path.basename(path)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # 纯文本 tooltip（样式由容器 AttachmentMessageBubble 声明的白底圆角 QToolTip 继承）
        tip = f"{name}\n双击可查看"
        self.setToolTip(tip)
        # 不设局部样式表，外观用 QPalette 实现，避免遮蔽容器的 QToolTip 白底圆角
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self.setPalette(_make_pill_palette(self.palette(), 255, 255, 255, 160))
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 4, 6, 4)
        h.setSpacing(6)
        if is_image:
            # 缩略图：等宽正方形，双击查看原图
            thumb = load_image_pixmap(path)
            if not thumb.isNull():
                thumb = thumb.scaled(
                    78, 78, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                lbl = QLabel()
                lbl.setPixmap(thumb)
                lbl.setFixedSize(78, 78)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                pal = lbl.palette()
                pal.setColor(QPalette.ColorRole.Window, QColor("#1a1a1a"))
                lbl.setPalette(pal)
                lbl.setAutoFillBackground(True)
                lbl.setToolTip(tip)
            else:
                lbl = QLabel("🖼")
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setToolTip(tip)
        else:
            ic = QLabel("📄")
            ic.setToolTip(tip)
            name = QLabel(name)
            name.setToolTip(tip)
            h.addWidget(ic)
            h.addWidget(name, 1)
            self.setToolTip(tip)
            return
        # 图片分支：把缩略图按原图大小比例放入，并让项可点击
        h.addWidget(lbl)

    def mouseDoubleClickEvent(self, event) -> None:
        self.activated.emit(self._path)
        super().mouseDoubleClickEvent(event)


class AttachmentMessageBubble(QFrame):
    """用户侧消息泡泡：纯文本 + 附件（文件小图标 / 图片缩略图），双击查看。"""

    activated = Signal(str)

    def __init__(
        self,
        text: str,
        image_paths: List[str],
        file_paths: List[str],
        parent=None,
        bg_opacity: int = 0,
    ):
        super().__init__(parent)
        self.role = "user"
        self._bg_opacity = 0
        self.set_background_opacity(bg_opacity)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        if text:
            t = QLabel(text)
            t.setWordWrap(True)
            t.setStyleSheet(
                "background:transparent;color:#222;font-size:14px;"
            )
            lay.addWidget(t)
        for p in image_paths:
            it = _AttachmentViewItem(p, True)
            it.activated.connect(self.activated.emit)
            lay.addWidget(it)
        for p in file_paths:
            it = _AttachmentViewItem(p, False)
            it.activated.connect(self.activated.emit)
            lay.addWidget(it)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setMaximumWidth(MarkdownBubble._MAX_WIDTH + 24)
        self.setMinimumWidth(MarkdownBubble._MIN_WIDTH + 24)

    def set_background_opacity(self, opacity: int) -> None:
        """设置消息泡泡背景透明度（0=不透明，100=完全透明）。"""
        self._bg_opacity = max(0, min(100, int(opacity)))
        alpha = int(round(255 * (100 - self._bg_opacity) / 100))
        r, g, b = _USER_BUBBLE_BG
        self.setStyleSheet(
            f"AttachmentMessageBubble{{background:rgba({r},{g},{b},{alpha});"
            f"border-radius:10px;padding:8px 10px;}}"
            "QToolTip{background-color:#ffffff;color:#000000;"
            "border:1px solid #b0b0b0;border-radius:4px;padding:4px 8px;}"
        )


class CollapsibleStepsWidget(QFrame):
    """可折叠的执行步骤列表，用于在对话界面中展示已执行的步骤。

    默认折叠，点击标题栏展开/收起。展开后显示每步的状态标记和摘要；
    长文本变量步骤显示变量名按钮，点击可查看完整原文。

    show_actions=True 时在列表下方显示「导出」「执行」按钮。
    """

    exportRequested = Signal(list)   # steps
    executeRequested = Signal(list)  # steps

    # status -> (mark, color)
    _STATUS_MARKS = {
        "done": ("✓", "#2E8B57"),
        "failed": ("✗", "#e74c3c"),
        "skipped": ("–", "#B0B0B0"),
        "locating": ("…", "#f39c12"),
        "executing": ("→", "#3498db"),
        "pending": ("○", "#B0B0B0"),
    }

    _STYLE_VAR_BTN = (
        "QPushButton{background:#e74c3c;color:white;border-radius:4px;"
        "padding:2px 6px;font-size:11px;font-weight:bold;}"
        "QPushButton:hover{background:#ff5c41;}"
    )
    # 紫色=执行时由聊天模型流式生成正文的长输入步骤
    _STYLE_LONG_BTN = (
        "QPushButton{background:#8e44ad;color:white;border-radius:4px;"
        "padding:2px 6px;font-size:11px;font-weight:bold;}"
        "QPushButton:hover{background:#a569bd;}"
    )
    _STYLE_EXPORT_BTN = (
        "QPushButton{background:#3498db;color:white;border-radius:4px;"
        "padding:4px 12px;font-size:12px;font-weight:bold;}"
        "QPushButton:hover{background:#5dade2;}"
    )
    _STYLE_EXEC_BTN = (
        "QPushButton{background:#2e8b57;color:white;border-radius:4px;"
        "padding:4px 12px;font-size:12px;font-weight:bold;}"
        "QPushButton:hover{background:#3aa569;}"
    )

    def __init__(self, steps: List[dict], show_actions: bool = False, parent=None):
        super().__init__(parent)
        self._steps = steps
        self._expanded = False
        self._var_values: dict = {}  # var_name -> full_text

        self.setStyleSheet(
            "CollapsibleStepsWidget{background:#f0f0f0;border-radius:10px;}"
        )
        self.setMaximumWidth(660)
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(10, 8, 10, 8)
        main_lay.setSpacing(4)

        # 标题行（可点击切换展开/折叠）
        self._header = QPushButton()
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(
            "QPushButton{text-align:left;background:transparent;border:none;"
            "font-weight:bold;color:#2c3e50;font-size:13px;padding:0;}"
            "QPushButton:hover{color:#1a5276;}"
        )
        self._header.clicked.connect(self._toggle)
        main_lay.addWidget(self._header)

        # 步骤列表容器（展开时显示）
        self._steps_container = QWidget()
        self._steps_container.setStyleSheet("background:transparent;")
        self._steps_layout = QVBoxLayout(self._steps_container)
        self._steps_layout.setContentsMargins(4, 0, 4, 0)
        self._steps_layout.setSpacing(2)
        main_lay.addWidget(self._steps_container)

        self._build_step_rows()
        self._update_header()
        self._steps_container.setVisible(False)  # 默认折叠

        # 操作按钮（导出 / 执行）
        if show_actions:
            btn_row = QHBoxLayout()
            btn_row.setContentsMargins(4, 0, 4, 0)
            btn_row.setSpacing(6)
            btn_row.addStretch(1)
            self._btn_export = QPushButton("📤 导出")
            self._btn_export.setStyleSheet(self._STYLE_EXPORT_BTN)
            self._btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
            self._btn_export.clicked.connect(
                lambda: self.exportRequested.emit(self._steps)
            )
            self._btn_execute = QPushButton("▶ 执行")
            self._btn_execute.setStyleSheet(self._STYLE_EXEC_BTN)
            self._btn_execute.setCursor(Qt.CursorShape.PointingHandCursor)
            self._btn_execute.clicked.connect(
                lambda: self.executeRequested.emit(self._steps)
            )
            btn_row.addWidget(self._btn_export)
            btn_row.addWidget(self._btn_execute)
            main_lay.addLayout(btn_row)

    def _build_step_rows(self) -> None:
        """构建步骤行列表。"""
        for step in self._steps:
            row = self._build_step_row(step)
            self._steps_layout.addWidget(row)

    def _build_step_row(self, step: dict) -> QWidget:
        """构建单个步骤行：状态标记 + 摘要 + 变量按钮。"""
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        # 状态标记
        status = step.get("status", "pending")
        mark, color = self._STATUS_MARKS.get(status, ("○", "#B0B0B0"))
        mark_lbl = QLabel(mark)
        mark_lbl.setFixedWidth(18)
        mark_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark_lbl.setStyleSheet(f"color:{color};font-weight:bold;")
        h.addWidget(mark_lbl)

        # 步骤摘要（带变量化）
        display = step_display_summary(step)
        summary_lbl = QLabel(display)
        summary_lbl.setWordWrap(True)
        summary_lbl.setStyleSheet("color:#333;background:transparent;")
        summary_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        h.addWidget(summary_lbl, 1)

        # 变量按钮 / 长输入按钮
        var = step.get("text_var")
        op = step.get("op", "")
        var_type = step.get("var_type")
        if var:
            full = step.get("text_full") or step.get("text") or step.get("desc_full") or ""
            self._var_values[var] = full
            btn = QPushButton(var)
            btn.setStyleSheet(self._STYLE_VAR_BTN)
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip("点击查看完整文本")
            btn.clicked.connect(lambda checked, v=var: self._show_var_text(v))
            h.addWidget(btn)
        elif op == "长输入" and var_type == "long_input":
            # 执行时流式生成的长输入（var_type="long_input" 短描述未变量化）
            # 正文写在 text / text_full 里；若还未执行则回退到 description
            full = step.get("text_full") or step.get("text") or ""
            if full:
                key = f"__long_{id(step)}__"
                self._var_values[key] = full
                btn = QPushButton("长输入正文")
            else:
                key = f"__long_{id(step)}__"
                desc = step.get("desc_full") or step.get("description") or "（尚未生成）"
                self._var_values[key] = desc
                btn = QPushButton("待生成")
            btn.setStyleSheet(self._STYLE_LONG_BTN)
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip("点击查看长输入主题/生成正文")
            btn.clicked.connect(lambda checked, k=key: self._show_var_text(k))
            h.addWidget(btn)

        return row

    def _show_var_text(self, var: str) -> None:
        """点击变量按钮：弹出对话框编辑变量值，保存后刷新本地缓存。"""
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QPlainTextEdit, QPushButton, QHBoxLayout,
            QLabel,
        )
        full = self._var_values.get(var, "")
        if var.startswith("__long_"):
            title = "编辑长输入正文"
        else:
            title = f"编辑输入变量 {var}"
        dlg = QDialog(self.window())
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(500, 380)
        dlg.resize(600, 460)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        val_lbl = QLabel("变量值 / 正文：")
        val_lbl.setStyleSheet("color:#2c3e50;font-weight:bold;font-size:12px;")
        v.addWidget(val_lbl)
        edit = QPlainTextEdit()
        edit.setPlainText(full)
        edit.setStyleSheet(
            "QPlainTextEdit{background:white;border:1px solid #ccc;"
            "border-radius:6px;padding:8px;font-family:Consolas,'Microsoft YaHei';"
            "font-size:13px;line-height:1.6;}"
        )
        v.addWidget(edit, 1)
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
        v.addLayout(btn_row)

        def _do_save():
            new_full = edit.toPlainText()
            self._var_values[var] = new_full
            # 同步更新底层步骤 dict（让后续执行使用新值）
            for s in self._steps:
                if s.get("text_var") == var:
                    s["text_full"] = new_full
                    s["text"] = new_full
                elif var.startswith("__long_") and s.get("var_type") == "long_input":
                    s["text_full"] = new_full
                    s["text"] = new_full
            dlg.accept()

        btn_save.clicked.connect(_do_save)
        dlg.exec()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._steps_container.setVisible(self._expanded)
        self._update_header()

    def _update_header(self) -> None:
        arrow = "▼" if self._expanded else "▶"
        total = len(self._steps)
        done = sum(1 for s in self._steps if s.get("status") == "done")
        failed = sum(1 for s in self._steps if s.get("status") == "failed")

        var_count = sum(1 for s in self._steps if s.get("text_var"))
        long_count = sum(1 for s in self._steps if s.get("op") == "长输入")


        if failed:
            header = f"{arrow} 执行步骤（{done}/{total} 完成，{failed} 失败）"
        else:
            header = f"{arrow} 执行步骤（{done}/{total} 完成）"
        self._header.setText(header)


class CodeBubbleWidget(QFrame):
    """编程模式下的代码气泡：折叠/展开 + 导出 + 运行按钮。

    默认折叠状态，点击标题栏展开/收起代码内容。
    导出：保存到 AppData/code/ 目录。
    运行：在终端中执行导出的脚本文件。
    流式追加时自动展开代码区域。
    """

    exportRequested = Signal(str, str)  # code_text, fence_language
    runRequested = Signal(str)      # code_text
    deleteRequested = Signal()

    _STYLE_HEADER = (
        "QPushButton{text-align:left;background:#dcd5f2;color:#4a3d8a;"
        "border:none;border-radius:6px;padding:6px 10px;"
        "font-weight:bold;font-size:12px;}"
        "QPushButton:hover{background:#cec5ec;}"
    )
    _STYLE_CODE = (
        "QPlainTextEdit{background:#f5f3fc;color:#33333f;"
        "border:none;border-radius:0px;padding:8px 10px;"
        "font-family:Consolas,'Courier New',monospace;font-size:13px;}"
    )
    _STYLE_EXPORT_BTN = (
        "QPushButton{background:#3498db;color:white;border-radius:4px;"
        "padding:4px 12px;font-size:11px;font-weight:bold;}"
        "QPushButton:hover{background:#5dade2;}"
    )
    _STYLE_RUN_BTN = (
        "QPushButton{background:#2e8b57;color:white;border-radius:4px;"
        "padding:4px 12px;font-size:11px;font-weight:bold;}"
        "QPushButton:hover{background:#3aa569;}"
    )

    def set_background_opacity(self, opacity: int) -> None:
        """设置背景透明度：0=不透明，100=完全透明（仅背景淡化，文字保持清晰）。"""
        self._bg_opacity = max(0, min(100, int(opacity)))
        alpha = int(round(255 * (100 - self._bg_opacity) / 100))
        r, g, b = _CODE_BUBBLE_BG
        self.setStyleSheet(
            f"CodeBubbleWidget{{background:rgba({r},{g},{b},{alpha});"
            f"border-radius:8px;border:1px solid #c9c1e8;}}"
        )

    def __init__(self, parent=None, bg_opacity: int = 0):
        super().__init__(parent)
        self._code_text = ""
        self._language = ""  # 代码围栏声明的语言（如 python/html/css/javascript）
        self._expanded = False
        self._streaming_mode = False
        self._bg_opacity = 0
        # 背景色随「消息泡泡透明度」设置渲染为 rgba（与消息气泡共用参数）
        self.set_background_opacity(bg_opacity)
        self.setMaximumWidth(660)
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(6, 6, 6, 6)
        main_lay.setSpacing(4)

        # 标题行（折叠/展开切换 + 导出 + 运行）
        header_row = QHBoxLayout()
        header_row.setSpacing(6)
        self._header_btn = QPushButton("▶ 生成的代码（点击展开）")
        self._header_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header_btn.setStyleSheet(self._STYLE_HEADER)
        self._header_btn.clicked.connect(self._toggle)
        header_row.addWidget(self._header_btn, 1)

        self._btn_export = QPushButton("📤 导出")
        self._btn_export.setStyleSheet(self._STYLE_EXPORT_BTN)
        self._btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_export.clicked.connect(self._on_export)
        header_row.addWidget(self._btn_export)

        self._btn_run = QPushButton("▶ 运行")
        self._btn_run.setStyleSheet(self._STYLE_RUN_BTN)
        self._btn_run.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_run.clicked.connect(self._on_run)
        header_row.addWidget(self._btn_run)
        main_lay.addLayout(header_row)

        # 代码编辑区（默认隐藏）
        self._code_edit = QPlainTextEdit()
        self._code_edit.setStyleSheet(self._STYLE_CODE)
        self._code_edit.setReadOnly(True)
        self._code_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._code_edit.setVisible(False)
        main_lay.addWidget(self._code_edit)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._code_edit.setVisible(self._expanded)
        arrow = "▼" if self._expanded else "▶"
        line_count = self._code_text.count("\n") + 1 if self._code_text else 0
        self._header_btn.setText(f"{arrow} 生成的代码（{line_count} 行，点击{'收起' if self._expanded else '展开'}）")

    def set_code(self, text: str) -> None:
        """设置完整代码文本（流式结束后调用）。"""
        self._code_text = text or ""
        self._code_edit.setPlainText(self._code_text)
        if self._streaming_mode:
            self._finish_streaming()
        # 更新标题行
        self._update_header_text()

    def append_code(self, chunk: str) -> None:
        """流式追加代码片段。"""
        if not chunk:
            return
        self._code_text += chunk
        self._code_edit.setPlainText(self._code_text)
        # 流式阶段自动展开
        if not self._expanded and not self._streaming_mode:
            self._toggle()
        elif self._streaming_mode and not self._expanded:
            self._toggle()
        # 自动滚动到底部
        sb = self._code_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    def start_streaming(self) -> None:
        """进入流式模式：自动展开代码区。"""
        self._streaming_mode = True
        if not self._expanded:
            self._toggle()

    def _finish_streaming(self) -> None:
        """结束流式模式。"""
        self._streaming_mode = False
        self._update_header_text()

    def _update_header_text(self) -> None:
        arrow = "▼" if self._expanded else "▶"
        line_count = self._code_text.count("\n") + 1 if self._code_text else 0
        action = "收起" if self._expanded else "展开"
        self._header_btn.setText(f"{arrow} 生成的代码（{line_count} 行，点击{action}）")

    def _on_export(self) -> None:
        if self._code_text.strip():
            self.exportRequested.emit(self._code_text, self._language)

    def _on_run(self) -> None:
        if self._code_text.strip():
            self.runRequested.emit(self._code_text)

    @property
    def code_text(self) -> str:
        return self._code_text

    def set_language(self, language: str) -> None:
        """设置代码围栏声明的语言（如 python/html/css/javascript）。"""
        self._language = (language or "").strip()

    @property
    def language(self) -> str:
        """代码围栏声明的语言。"""
        return self._language

    def contextMenuEvent(self, event) -> None:
        from PySide6.QtWidgets import QMenu, QApplication
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {background-color: white; color: black; border: 1px solid #ccc;}
            QMenu::item {padding: 4px 20px;}
            QMenu::item:selected {background-color: #0078d7; color: white;}
        """)
        copy_action = menu.addAction("📋 复制")
        delete_action = menu.addAction("🗑 删除")
        action = menu.exec(event.globalPos())
        if action == copy_action:
            QApplication.clipboard().setText(self._code_text)
        elif action == delete_action:
            self.deleteRequested.emit()


class CodeResultWidget(QFrame):
    """运行结果气泡：可折叠的输出/报错 + （出错时）手动修复按钮。

    默认折叠，点击标题栏展开/收起输出内容。
    出错时显示「🔄 修复」按钮，点击手动发送给模型修复。
    """

    fixRequested = Signal(str)  # error_text
    deleteRequested = Signal()

    _STYLE_HEADER_OK = (
        "QPushButton{text-align:left;background:#d0ebd9;color:#1e6b38;"
        "border:none;border-radius:6px;padding:6px 10px;"
        "font-weight:bold;font-size:12px;}"
        "QPushButton:hover{background:#bfe2cc;}"
    )
    _STYLE_HEADER_ERR = (
        "QPushButton{text-align:left;background:#f6d8d4;color:#a3392f;"
        "border:none;border-radius:6px;padding:6px 10px;"
        "font-weight:bold;font-size:12px;}"
        "QPushButton:hover{background:#efc6c1;}"
    )
    _STYLE_OUTPUT_OK = (
        "QPlainTextEdit{background:transparent;color:#1e6b38;"
        "border:none;border-radius:0px;padding:8px 10px;"
        "font-family:Consolas,'Courier New',monospace;font-size:12px;}"
    )
    _STYLE_OUTPUT_ERR = (
        "QPlainTextEdit{background:transparent;color:#a3392f;"
        "border:none;border-radius:0px;padding:8px 10px;"
        "font-family:Consolas,'Courier New',monospace;font-size:12px;}"
    )
    _STYLE_FIX_BTN = (
        "QPushButton{background:#c0392b;color:white;border-radius:4px;"
        "padding:4px 12px;font-size:11px;font-weight:bold;}"
        "QPushButton:hover{background:#e74c3c;}"
    )

    def set_background_opacity(self, opacity: int) -> None:
        """设置背景透明度：0=不透明，100=完全透明（仅背景淡化，文字保持清晰）。"""
        self._bg_opacity = max(0, min(100, int(opacity)))
        alpha = int(round(255 * (100 - self._bg_opacity) / 100))
        if self._is_error:
            r, g, b = _RESULT_ERR_BG
            border = "#f2c3c1"
        else:
            r, g, b = _RESULT_OK_BG
            border = "#b9e0c4"
        self.setStyleSheet(
            f"CodeResultWidget{{background:rgba({r},{g},{b},{alpha});"
            f"border-radius:8px;border:1px solid {border};}}"
        )

    def __init__(self, output: str, is_error: bool,
                 error_text: str = "", parent=None, bg_opacity: int = 0):
        super().__init__(parent)
        self._output = output or ""
        self._is_error = is_error
        self._error_text = error_text or ""
        self._expanded = False
        self._bg_opacity = 0
        # 背景色随「消息泡泡透明度」设置渲染为 rgba（与消息气泡共用参数）
        self.set_background_opacity(bg_opacity)
        self.setMaximumWidth(660)
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(6, 6, 6, 6)
        main_lay.setSpacing(4)

        header_row = QHBoxLayout()
        header_row.setSpacing(6)
        title = "▶ 运行出错（点击展开）" if is_error else "▶ 运行成功（点击展开）"
        self._header_btn = QPushButton(title)
        self._header_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header_btn.setStyleSheet(self._STYLE_HEADER_ERR if is_error else self._STYLE_HEADER_OK)
        self._header_btn.clicked.connect(self._toggle)
        header_row.addWidget(self._header_btn, 1)

        if is_error:
            self._btn_fix = QPushButton("🔄 修复")
            self._btn_fix.setStyleSheet(self._STYLE_FIX_BTN)
            self._btn_fix.setCursor(Qt.CursorShape.PointingHandCursor)
            self._btn_fix.clicked.connect(self._on_fix)
            header_row.addWidget(self._btn_fix)
        main_lay.addLayout(header_row)

        self._output_edit = QPlainTextEdit()
        self._output_edit.setStyleSheet(self._STYLE_OUTPUT_ERR if is_error else self._STYLE_OUTPUT_OK)
        self._output_edit.setReadOnly(True)
        self._output_edit.setPlainText(self._output)
        self._output_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._output_edit.setVisible(False)
        main_lay.addWidget(self._output_edit)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._output_edit.setVisible(self._expanded)
        arrow = "▼" if self._expanded else "▶"
        kind = "运行出错" if self._is_error else "运行成功"
        self._header_btn.setText(
            f"{arrow} {kind}（点击{'收起' if self._expanded else '展开'}）")

    def _on_fix(self) -> None:
        if self._error_text.strip():
            self.fixRequested.emit(self._error_text)

    def contextMenuEvent(self, event) -> None:
        from PySide6.QtWidgets import QMenu, QApplication
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {background-color: white; color: black; border: 1px solid #ccc;}
            QMenu::item {padding: 4px 20px;}
            QMenu::item:selected {background-color: #0078d7; color: white;}
        """)
        copy_action = menu.addAction("📋 复制")
        delete_action = menu.addAction("🗑 删除")
        action = menu.exec(event.globalPos())
        if action == copy_action:
            QApplication.clipboard().setText(self._output)
        elif action == delete_action:
            self.deleteRequested.emit()


class ChatView(QScrollArea):
    """聊天区域，纵向排列气泡。"""

    # 某个气泡请求删除（widget 引用）
    messageDeleteRequested = Signal(object)
    # 用户消息请求撤回（widget 引用）
    messageRecallRequested = Signal(object)
    # Pro 编程：用户消息请求撤销（还原项目到本条消息发送前，widget 引用）
    messageUndoRequested = Signal(object)
    # 消息泡泡内的附件被双击（传入本地路径，用于打开/查看）
    attachmentActivated = Signal(str)

    # 流式 chunk 批量合并间隔（毫秒）：N ms 内收到的 chunk 攒一起追加
    _STREAM_FLUSH_INTERVAL_MS = 50
    # 滚动防抖间隔（毫秒）：只做最后一次滚动，避免每 chunk 触发一次
    _SCROLL_DEBOUNCE_MS = 80

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("QScrollArea{background:transparent;}")

        self._container = QWidget()
        self._container.setStyleSheet("background:transparent;")
        self._layout = QVBoxLayout(self._container)
        # 左右边距 2px 与 _MESSAGE_SIDE_GAP(8) 合计 = 外侧留白 10px
        self._layout.setContentsMargins(2, 12, 2, 12)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)
        self.setWidget(self._container)

        self._current_assistant: Optional[BubbleWidget] = None
        # 消息泡泡背景透明度（0=不透明，100=完全透明）
        self._bubble_opacity = 0
        # Pro 编程：是否允许用户消息泡泡显示「撤销（还原项目）」菜单项
        self._pro_undo_enabled = False

        # 流式输出 chunk 缓冲 + 刷新定时器
        self._stream_buffer: List[str] = []
        self._stream_bubble: Optional[BubbleWidget] = None
        self._stream_flush_timer = QTimer(self)
        self._stream_flush_timer.setSingleShot(True)
        self._stream_flush_timer.setInterval(self._STREAM_FLUSH_INTERVAL_MS)
        self._stream_flush_timer.timeout.connect(self._flush_stream_buffer)

        # 滚动防抖定时器
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(self._SCROLL_DEBOUNCE_MS)
        self._scroll_timer.timeout.connect(self._do_scroll_to_bottom)
        # 用户向上滚动时关闭自动滚动（滚回底部才恢复）
        self._auto_scroll_enabled: bool = True
        self.verticalScrollBar().valueChanged.connect(self._on_scrollbar_changed)

    def _adaptive_avail_width(self) -> int:
        """侧面固定泡泡（步骤/代码/思考/结果）可用的最大宽度 = 可视宽度 - 边距。

        与普通消息泡泡(MarkdownBubble)口径一致：容器左右边距(2*2) + 行左侧留白(8)。
        """
        vw = self.viewport().width()
        return max(1, vw - 4 - _MESSAGE_SIDE_GAP)

    def _apply_adaptive_max(self, widget) -> None:
        """把某个固定型泡泡的最大宽度设置为当前可用宽度，随窗口自适应缩放。"""
        avail = self._adaptive_avail_width()
        widget.setMaximumWidth(max(widget.minimumWidth(), avail))

    def _update_adaptive_widths(self) -> None:
        """窗口尺寸变化时，刷新所有泡泡以自适应。

        - 固定型泡泡（步骤/代码/思考/结果）：直接把最大宽度设为当前可用宽度。
        - 普通消息泡泡(MarkdownBubble)：其 sizeHint 每次都按当前可视宽度重算，
          但窗口缩放后布局若沿用旧宽度就不会重新请求 sizeHint，导致 resizeEvent
          不触发、气泡卡在旧宽度。因此这里主动调用 updateGeometry() 使几何失效，
          强制布局重算宽度，从而随窗口自适应。
        """
        for i in range(self._layout.count() - 1):
            item = self._layout.itemAt(i)
            if item is None or item.widget() is None:
                continue
            row = item.widget()
            # 普通消息泡泡：主动失效，触发 sizeHint 按当前可视宽度重算
            if row.findChild(BubbleWidget) is not None:
                row.updateGeometry()
                for b in row.findChildren(BubbleWidget):
                    if isinstance(b._content, MarkdownBubble):
                        b._content.updateGeometry()
            # 固定型泡泡：随当前可用宽度自适应
            for cls in (CollapsibleStepsWidget, CodeBubbleWidget,
                        CodeResultWidget, ThinkingBubbleWidget):
                for w in row.findChildren(cls):
                    self._apply_adaptive_max(w)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_adaptive_widths()

    def _on_scrollbar_changed(self, value: int) -> None:
        """用户手动滚动：离底部还有距离时禁用自动滚动，到底部才恢复。"""
        sb = self.verticalScrollBar()
        try:
            at_bottom = (sb.maximum() - value) <= 4
        except Exception:
            at_bottom = False
        self._auto_scroll_enabled = at_bottom

    def set_pro_undo(self, flag: bool) -> None:
        """开启/关闭用户消息泡泡的「撤销（还原项目）」菜单项。"""
        self._pro_undo_enabled = bool(flag)

    def add_message(self, text: str, role: str) -> BubbleWidget:
        """添加一条消息。返回气泡引用（便于流式追加）。"""
        bubble = BubbleWidget(text, role, bg_opacity=self._bubble_opacity)
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        h = QHBoxLayout(row)
        # 用户消息靠右：右侧留白加大，气泡更贴近右边缘，左边缘离窗口更远；
        # 模型消息靠左：左侧留白加大，气泡更贴近左边缘，右边缘离窗口更远。
        # 两者拉开中间空隙，便于区分消息来源。
        if role == "user":
            h.setContentsMargins(0, 0, _MESSAGE_SIDE_GAP, 0)
            h.addStretch(1)
            h.addWidget(bubble)
        else:
            h.setContentsMargins(_MESSAGE_SIDE_GAP, 0, 0, 0)
            h.addWidget(bubble)
            h.addStretch(1)
        # 插到 stretch 之前
        self._layout.insertWidget(self._layout.count() - 1, row)
        # 滚到底部
        self._scroll_to_bottom()
        self._current_assistant = bubble if role != "user" else None
        bubble.deleteRequested.connect(lambda: self.messageDeleteRequested.emit(bubble))
        bubble.recallRequested.connect(lambda: self.messageRecallRequested.emit(bubble))
        bubble._pro_undo = self._pro_undo_enabled
        bubble.undoRequested.connect(lambda: self.messageUndoRequested.emit(bubble))
        return bubble

    def add_attachment_message(
        self, text: str, image_paths: List[str], file_paths: List[str]
    ) -> AttachmentMessageBubble:
        """添加一条用户消息泡泡，内含文本 + 附件：图片显示缩略图、文件显示小图标。

        附件均支持双击（通过 attachmentActivated 信号发出路径，由界面层打开查看）。
        """
        bubble = AttachmentMessageBubble(
            text, image_paths, file_paths, bg_opacity=self._bubble_opacity
        )
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, _MESSAGE_SIDE_GAP, 0)
        h.addStretch(1)
        h.addWidget(bubble)
        self._layout.insertWidget(self._layout.count() - 1, row)
        self._scroll_to_bottom()
        bubble.activated.connect(self.attachmentActivated.emit)
        return bubble

    def set_bubble_opacity(self, opacity: int) -> None:
        """设置消息泡泡背景透明度并刷新所有已存在的气泡（0=不透明，100=完全透明）。

        同步应用到消息气泡（user/assistant）、代码气泡与深度思考气泡，共用同一透明度参数。
        """
        self._bubble_opacity = max(0, min(100, int(opacity)))
        for i in range(self._layout.count() - 1):
            item = self._layout.itemAt(i)
            if item is None or item.widget() is None:
                continue
            row = item.widget()
            for bubble in row.findChildren(BubbleWidget):
                bubble.set_background_opacity(self._bubble_opacity)
            for cb in row.findChildren(CodeBubbleWidget):
                cb.set_background_opacity(self._bubble_opacity)
            for tb in row.findChildren(ThinkingBubbleWidget):
                tb.set_background_opacity(self._bubble_opacity)
            for cr in row.findChildren(CodeResultWidget):
                cr.set_background_opacity(self._bubble_opacity)

    def remove_widget(self, widget: QWidget) -> None:
        """从对话视图中移除指定气泡所在的整行。"""
        if widget is None:
            return
        for i in range(self._layout.count() - 1):
            item = self._layout.itemAt(i)
            if item is None or item.widget() is None:
                continue
            if item.widget() is widget or item.widget() is widget.parent():
                taken = self._layout.takeAt(i)
                w = taken.widget()
                if w is not None:
                    w.deleteLater()
                break

    def recall_widget(self, widget: BubbleWidget) -> None:
        """撤回用户消息：移除该消息及其后的所有回复气泡（直到下一条用户消息）。"""
        if widget is None:
            return
        found = False
        indices_to_remove = []
        for i in range(self._layout.count() - 1):
            item = self._layout.itemAt(i)
            if item is None or item.widget() is None:
                continue
            if item.widget() is widget or item.widget() is widget.parent():
                found = True
                indices_to_remove.append(i)
                continue
            if found:
                from PySide6.QtWidgets import QWidget as _QW
                row_widget = item.widget()
                # 找到下一条用户消息则停止
                bubble = row_widget.findChild(BubbleWidget)
                if bubble is not None and bubble.role == "user":
                    break
                indices_to_remove.append(i)
        for i in reversed(indices_to_remove):
            taken = self._layout.takeAt(i)
            w = taken.widget()
            if w is not None:
                w.deleteLater()

    def add_steps_progress(self, steps: List[str]) -> StepsProgressWidget:
        """添加 [schedule] 计划的可视化步骤清单（左对齐），返回引用（调用方驱动进度）。"""
        widget = StepsProgressWidget(list(steps))
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(_MESSAGE_SIDE_GAP, 0, 0, 0)
        h.addWidget(widget)
        h.addStretch(1)
        self._layout.insertWidget(self._layout.count() - 1, row)
        self._scroll_to_bottom()
        return widget

    def add_steps_summary(self, steps: List[dict], show_actions: bool = False) -> None:
        """添加可折叠的执行步骤列表到对话区（左对齐，如 assistant 消息）。
        show_actions=True 时在列表下方显示导出/执行按钮。
        """
        widget = CollapsibleStepsWidget(steps, show_actions=show_actions)
        self._apply_adaptive_max(widget)
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(_MESSAGE_SIDE_GAP, 0, 0, 0)
        h.addWidget(widget)
        h.addStretch(1)
        self._layout.insertWidget(self._layout.count() - 1, row)
        self._scroll_to_bottom()

    def add_code_bubble(self) -> CodeBubbleWidget:
        """添加一个代码气泡到对话区（左对齐），返回引用。"""
        widget = CodeBubbleWidget(bg_opacity=self._bubble_opacity)
        self._apply_adaptive_max(widget)
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(_MESSAGE_SIDE_GAP, 0, 0, 0)
        h.addWidget(widget)
        h.addStretch(1)
        self._layout.insertWidget(self._layout.count() - 1, row)
        self._scroll_to_bottom()
        widget.deleteRequested.connect(lambda: self.messageDeleteRequested.emit(widget))
        return widget

    def add_code_result(self, output: str, is_error: bool,
                        error_text: str = "") -> CodeResultWidget:
        """添加一个运行结果气泡（可折叠 + 出错时修复按钮），返回引用。"""
        widget = CodeResultWidget(output, is_error, error_text, bg_opacity=self._bubble_opacity)
        self._apply_adaptive_max(widget)
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(_MESSAGE_SIDE_GAP, 0, 0, 0)
        h.addWidget(widget)
        h.addStretch(1)
        self._layout.insertWidget(self._layout.count() - 1, row)
        self._scroll_to_bottom()
        widget.deleteRequested.connect(lambda: self.messageDeleteRequested.emit(widget))
        return widget

    def add_thinking_bubble(self, text: str,
                            title: str = "深度思考",
                            window_height: int = 0) -> ThinkingBubbleWidget:
        """添加一个可折叠的智能体输出窗口（左对齐），返回引用。

        window_height > 0 时内容区固定高度，内部滚动；= 0 时自适应内容。
        """
        widget = ThinkingBubbleWidget(text, bg_opacity=self._bubble_opacity,
                                      title=title, window_height=window_height)
        self._apply_adaptive_max(widget)
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(_MESSAGE_SIDE_GAP, 0, 0, 0)
        h.addWidget(widget)
        h.addStretch(1)
        self._layout.insertWidget(self._layout.count() - 1, row)
        self._scroll_to_bottom()
        widget.deleteRequested.connect(lambda: self.messageDeleteRequested.emit(widget))
        return widget

    def remove_code_bubble(self, widget: QWidget) -> None:
        """从对话视图中移除指定代码气泡（及其容器行）。"""
        if widget is None:
            return
        for i in range(self._layout.count() - 1):  # 最后一个是 stretch
            item = self._layout.itemAt(i)
            if item is None or item.widget() is None:
                continue
            # widget 自身或其父容器行
            if item.widget() is widget or item.widget() is widget.parent():
                taken = self._layout.takeAt(i)
                w = taken.widget()
                if w is not None:
                    w.deleteLater()
                break

    def start_streaming_assistant(self) -> BubbleWidget:
        """开始一条空的 assistant 气泡用于流式追加。

        内部 MarkdownBubble 进入流式模式（geometry 更新节流）。
        """
        # 先 flush 掉前一次可能残留的缓冲（防止上一条还没 flush）
        self._flush_stream_buffer()
        bubble = self.add_message("", "assistant")
        # 标记内部 MarkdownBubble 进入流式阶段
        if isinstance(bubble._content, MarkdownBubble):
            bubble._content.start_streaming()
        self._stream_bubble = bubble
        return bubble

    def append_stream(self, bubble: BubbleWidget, chunk: str) -> None:
        """流式追加：把 chunk 放入缓冲，50ms 合并刷新 + 滚动防抖。"""
        if not chunk:
            return
        # 如果换了 bubble：先 flush 旧缓冲
        if self._stream_bubble is not bubble:
            self._flush_stream_buffer()
            self._stream_bubble = bubble
        self._stream_buffer.append(chunk)
        # 启动/重启 flush 定时器（50ms 后批量追加）
        if not self._stream_flush_timer.isActive():
            self._stream_flush_timer.start()
        # 滚动防抖（80ms 后做最后一次滚动）
        self._scroll_timer.start()

    def _flush_stream_buffer(self) -> None:
        """把流式缓冲中的 chunks 合并后一次性追加到 bubble。"""
        if not self._stream_buffer:
            return
        bubble = self._stream_bubble
        if bubble is None:
            self._stream_buffer.clear()
            return
        combined = "".join(self._stream_buffer)
        self._stream_buffer.clear()
        bubble.append_text(combined)

    def end_streaming_assistant(self, bubble: BubbleWidget, final_text: Optional[str] = None) -> None:
        """结束流式追加：flush 缓冲 + 关闭流式节流 + 最终滚动。"""
        # 1. 立即 flush 剩余 chunks
        if self._stream_bubble is bubble and self._stream_buffer:
            combined = "".join(self._stream_buffer)
            self._stream_buffer.clear()
            bubble.append_text(combined)
        # 2. 如果有最终文本，设置完整 Markdown 渲染
        if final_text is not None:
            bubble.set_text(final_text)
        # 3. 结束流式模式（强制 geometry 更新）
        if isinstance(bubble._content, MarkdownBubble):
            bubble._content.finish_streaming()
        # 4. 最终滚到底部
        self._do_scroll_to_bottom()
        if self._stream_bubble is bubble:
            self._stream_bubble = None

    def clear_messages(self) -> None:
        # 清空流式缓冲 + 停止定时器
        self._stream_buffer.clear()
        self._stream_flush_timer.stop()
        self._scroll_timer.stop()
        self._stream_bubble = None
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._current_assistant = None

    def _remove_bubble(self, bubble: BubbleWidget) -> None:
        """从对话视图中移除指定气泡（用于规划空气泡清理等场景）。"""
        if bubble is None:
            return
        for i in range(self._layout.count() - 1):  # 最后一个是 stretch
            item = self._layout.itemAt(i)
            if item is not None and item.widget() is bubble:
                taken = self._layout.takeAt(i)
                w = taken.widget()
                if w is not None:
                    w.deleteLater()
                break
        if self._stream_bubble is bubble:
            self._stream_bubble = None
        if self._current_assistant is bubble:
            self._current_assistant = None

    def _scroll_to_bottom(self) -> None:
        """旧接口（兼容）：转发到滚动防抖。"""
        self._scroll_timer.start()

    def _do_scroll_to_bottom(self) -> None:
        """真正执行滚动到底部（防抖定时器到期时调用）。

        仅当自动滚动未被用户手动暂停时才执行；用户如果手动向上滚动过，
        必须自己滚回底部（此时 _auto_scroll_enabled 会被恢复）。
        """
        if not self._auto_scroll_enabled:
            return
        try:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        except Exception:
            pass


class ThinkingBubbleWidget(QFrame):
    """Pro 编程模式的智能体流式输出窗口。

    可折叠（点击标题栏），支持固定高度（window_height > 0）或自适应（=0）。
    内部内容区是一个 QTextBrowser，混合渲染：
      - 模型人话正文（正常字体，深灰色，Markdown 格式渲染）
      - 工具调用日志行（灰色底块 + 等宽小号字 + ✦ 图标）

    鼠标悬停时滚轮事件只在框内滚动，不会冒泡到外层对话界面。
    """

    deleteRequested = Signal()

    _STYLE_HEADER = (
        "QPushButton{text-align:left;background:#f5e6c2;color:#7a5c2e;"
        "border:none;border-radius:6px;padding:6px 10px;"
        "font-weight:bold;font-size:12px;}"
        "QPushButton:hover{background:#efd8a8;}"
    )
    _STYLE_CONTENT = (
        "QTextBrowser{background:#fef9ec;color:#3a2d16;"
        "border:none;border-radius:0px;padding:6px 8px;"
        "font-family:'Microsoft YaHei','Segoe UI',sans-serif;font-size:13px;}"
        "QTextBrowser *{line-height:1.5;}"
    )

    # 工具调用行的 HTML 模板（灰色底块 + 等宽小号字 + 左竖线）
    _TOOL_ROW_HTML = (
        '<div style="margin:2px 0;padding:2px 8px;background:#eef0f4;'
        'border-left:3px solid #94a3b8;border-radius:0 4px 4px 0;'
        'font-family:Consolas,monospace;font-size:12px;color:#546e7a;">'
        '<span style="color:#94a3b8;">✦ </span>{text}</div>'
    )
    # 系统/错误行的 HTML 模板
    _SYSTEM_ROW_HTML = (
        '<div style="margin:2px 0;padding:2px 8px;background:#fde8e4;'
        'border-left:3px solid #e57373;border-radius:0 4px 4px 0;'
        'font-size:12px;color:#c0392b;">'
        '<span style="color:#e57373;">⚠ </span>{text}</div>'
    )

    def set_background_opacity(self, opacity: int) -> None:
        """设置背景透明度：0=不透明，100=完全透明（仅背景淡化，文字保持清晰）。"""
        self._bg_opacity = max(0, min(100, int(opacity)))
        alpha = int(round(255 * (100 - self._bg_opacity) / 100))
        r, g, b = _THINKING_BUBBLE_BG
        self.setStyleSheet(
            f"ThinkingBubbleWidget{{background:rgba({r},{g},{b},{alpha});"
            f"border-radius:8px;border:1px solid #ecd9ae;}}"
        )

    def __init__(self, text: str = "", parent=None, bg_opacity: int = 0,
                 title: str = "深度思考", window_height: int = 0):
        super().__init__(parent)
        self._text = text or ""                  # 累积的原始文本（正文部分）
        self._title = title or "深度思考"
        self._expanded = False
        self._bg_opacity = 0
        self._window_height = max(0, int(window_height))   # 固定高度（像素），0=自适应
        self._tool_call_count = 0               # 工具调用计数

        # 背景色随「消息泡泡透明度」设置渲染为 rgba（与消息气泡共用参数）
        self.set_background_opacity(bg_opacity)
        self.setMaximumWidth(660)
        self.setMinimumWidth(300)
        # 垂直用 MinimumExpanding 确保展开后能随内容拉伸
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(6, 6, 6, 6)
        main_lay.setSpacing(0)

        # 标题行
        self._header_btn = QPushButton(f"▶ {self._title}（点击展开）")
        self._header_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header_btn.setStyleSheet(self._STYLE_HEADER)
        self._header_btn.clicked.connect(self._toggle)
        main_lay.addWidget(self._header_btn)

        # 内容区（默认隐藏；设置了固定高度时给一个最小高度占位）
        self._content_edit = QTextBrowser()
        self._content_edit.setStyleSheet(self._STYLE_CONTENT)
        self._content_edit.setReadOnly(True)
        self._content_edit.setOpenExternalLinks(False)
        self._content_edit.setFrameShape(QFrame.Shape.NoFrame)
        self._content_edit.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self._content_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        # 透明背景 + 零文档边距（QTextDocument 默认有几 px 底边距造成大片空白）
        pal = self._content_edit.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(0, 0, 0, 0))
        pal.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0, 0))
        self._content_edit.setPalette(pal)
        self._content_edit.document().setDocumentMargin(0)

        # 装 eventFilter：鼠标悬停时滚轮事件只在框内滚动，不冒泡到外层对话界面
        self._content_edit.installEventFilter(self)

        if self._window_height > 0:
            # 用 setMaximumHeight 而非 setFixedHeight：内容少时自动收缩，
            # 内容超过上限时才撑到上限并出现滚动条，避免正文后大片空白
            self._content_edit.setMaximumHeight(self._window_height)
            self._content_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        else:
            self._content_edit.setMaximumHeight(16777215)   # 不限制上限
            self._content_edit.setMinimumHeight(28)
            self._content_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content_edit.setVisible(False)
        main_lay.addWidget(self._content_edit)

        # 流式渲染节流：累积分块、定时整段刷新，保证内容可靠呈现
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(80)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._flush_to_browser)

        # 缓冲：正文增量（list of str chunks）和新工具调用行（list of str HTML）
        self._pending_text_chunks: list = []
        self._pending_tool_rows: list = []
        # 工具行原始 HTML 累积（finish_stream 做最终 markdown 重渲染时用）
        self._tool_html_rows: list = []

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------

    def set_window_height(self, height: int) -> None:
        """动态调整窗口高度（设置变更后实时生效）。"""
        self._window_height = max(0, int(height))
        if self._window_height > 0:
            self._content_edit.setMaximumHeight(self._window_height)
            self._content_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        else:
            self._content_edit.setMaximumHeight(16777215)
            self._content_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 重新 flush 一次，让高度变化立即作用到当前内容
        if self._render_timer.isActive():
            self._render_timer.stop()
        self._flush_to_browser()

    def expand(self) -> None:
        """展开内容区（如流式输出开始后自动展开显示）。"""
        if not self._expanded:
            self._toggle()

    def clear_all(self) -> None:
        """清空所有内容（新会话开始时调用）。"""
        self._text = ""
        self._tool_call_count = 0
        self._pending_text_chunks.clear()
        self._pending_tool_rows.clear()
        self._tool_html_rows.clear()
        self._content_edit.clear()
        self._update_header()

    def append_agent_text(self, chunk: str) -> None:
        """流式追加 assistant 正文（正常文字样式）。"""
        if not chunk:
            return
        self._text += chunk
        self._pending_text_chunks.append(chunk)
        self._schedule_flush()

    # 向后兼容：自主代理模式还在调用 append_thinking
    def append_thinking(self, chunk: str) -> None:
        """流式追加思考内容（别名：append_agent_text）。"""
        self.append_agent_text(chunk)

    def append_tool_call(self, description: str) -> None:
        """追加一个工具调用日志行（灰色底块、等宽小号字）。"""
        if not description:
            return
        self._tool_call_count += 1
        html = self._TOOL_ROW_HTML.format(text=description)
        self._pending_tool_rows.append(html)
        self._tool_html_rows.append(html)  # 同步收集，供 finish_stream 最终重建
        self._schedule_flush()

    def append_system_line(self, text: str) -> None:
        """追加一条系统行（错误/警告样式，红色底）。"""
        if not text:
            return
        html = self._SYSTEM_ROW_HTML.format(text=text)
        self._pending_tool_rows.append(html)
        self._schedule_flush()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        """拦截 _content_edit 的滚轮事件 — 不让它冒泡到外层 QScrollArea。

        QTextBrowser 自身处理 wheelEvent 时，如果内容没到边界会消耗事件，
        到了边界可能让事件继续传播。拦截后手动处理：
        - 滚轮向上：文本到顶了 → 不做任何事（不让外层抢）
        - 滚轮向下：文本到底了 → 也不冒泡
        总之思考框接管所有鼠标滚轮事件。
        """
        from PySide6.QtCore import QEvent
        if obj is self._content_edit and event.type() == QEvent.Type.Wheel:
            # 交给 QTextBrowser 自己处理（它内部有滚动条），然后吃掉事件
            self._content_edit.wheelEvent(event)
            return True  # True = 事件已处理，停止传播
        return super().eventFilter(obj, event)

    def _schedule_flush(self) -> None:
        """启动/重启节流定时器（80ms），攒够一批后统一刷进 QTextBrowser。"""
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _trim_trailing_blank_blocks(self) -> None:
        """删除文档末尾的纯空块。

        模型正文在调用工具前常带多个换行（如「正文\\n\\n[run:xxx]」），
        这些尾部换行进入 QTextDocument 会变成一连串空段落，造成
        「正文后一大片空白、与工具调用行隔得太开」。插入工具调用行前
        先清掉这些空块，让工具行紧贴正文最后一行。
        """
        doc = self._content_edit.document()
        while True:
            block = doc.lastBlock()
            if block.blockNumber() <= 0 or block.text().strip() != "":
                break
            cursor = QTextCursor(doc)
            cursor.setPosition(block.position())
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()  # 删除段落分隔符，将空块并入上一块

    def _markdown_to_html(self, text: str) -> str:
        """把 markdown 文本转为 HTML（轻量 fallback — 转义后用 <br> 换行）。"""
        if not text:
            return ""
        try:
            import markdown as md
            # 只启用核心扩展（不要表格/代码高亮等重量级功能），流式场景追求速度
            html = md.markdown(text, extensions=["fenced_code", "nl2br"])
            # markdown 默认给 <p> 加了 margin，让它紧凑一点
            return html
        except Exception:
            # fallback：纯文本转义 + <br>
            import html as _h
            return _h.escape(text).replace("\n", "<br>")

    def _flush_to_browser(self) -> None:
        """增量追加：正文用 insertText（纯文本，流式稳定），工具行用 insertHtml。

        关键：流式阶段不主动 resize QTextBrowser 高度，让它随内容自然撑开到
        setMaximumHeight 上限。主动 resize 会触发 QTextDocument 重排 + 外层
        layout 重新计算 + 滚动条重绘，三者不同步就会乱跳遮挡。
        高度只在 finish_stream 收尾时精确调整一次。
        """
        doc = self._content_edit.document()

        # 正文增量追加（纯文本）— 流式阶段保持稳定不替换
        if self._pending_text_chunks:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.End)
            text = "".join(self._pending_text_chunks)
            self._pending_text_chunks.clear()
            cursor.insertText(text)

        # 工具调用行 HTML 追加
        if self._pending_tool_rows:
            self._trim_trailing_blank_blocks()
            for html_row in self._pending_tool_rows:
                cursor = QTextCursor(doc)
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertHtml("<br>")
                cursor.insertHtml(html_row)
                cursor.insertHtml("<br>")
            self._pending_tool_rows.clear()

        # 滚到底部（用 QTextBrowser 自带的 moveCursor 更稳定，避免滚动条最大值计算时机问题）
        self._content_edit.moveCursor(QTextCursor.MoveOperation.End)
        self._update_header()

    def finish_stream(self) -> None:
        """流式输出结束 — 把累积的完整正文重新用 markdown 渲染一遍。

        流式阶段用 insertText（纯文本）保证稳定不遮挡不滑顶；
        结束后 setHtml 整体 rebuild 一次，让最终效果有完整 markdown 格式。
        工具行 HTML 在流式阶段已稳定显示，这里和 markdown 正文一起重建。
        """
        if not self._text.strip() and self._tool_call_count == 0:
            return
        if self._render_timer.isActive():
            self._render_timer.stop()
        self._flush_to_browser()

        body_html = self._markdown_to_html(self._text)
        combined = body_html
        if self._tool_html_rows:
            combined += "<br>" + "<br>".join(self._tool_html_rows)

        self._content_edit.setHtml(combined)
        self._content_edit.document().setDocumentMargin(0)
        # 滚到底部 + 收缩高度
        if self._content_edit.verticalScrollBar().maximum() > 0:
            sb = self._content_edit.verticalScrollBar()
            sb.setValue(sb.maximum())
        doc_h = self._content_edit.document().size().height()
        target_h = max(int(doc_h), 28)
        if self._window_height > 0:
            target_h = min(target_h, self._window_height)
        self._content_edit.resize(self._content_edit.width(), target_h)
        self._update_header()

    def _update_header(self) -> None:
        """根据当前折叠状态、正文行数与工具调用数实时刷新标题栏。"""
        arrow = "▼" if self._expanded else "▶"
        tool_n = self._tool_call_count
        tool_info = f"{tool_n} 步工具" if tool_n > 0 else ""
        # 正文行数（排除换行计数的空串边界）
        text_len = len(self._text.strip())
        if text_len > 0:
            line_count = self._text.count("\n") + 1
            text_info = f"{line_count} 行文字"
        else:
            text_info = ""
        # 拼标题
        parts = [p for p in (text_info, tool_info) if p]
        info = (" · " + " | ".join(parts)) if parts else ""
        hint = "点击收起" if self._expanded else "点击展开"
        self._header_btn.setText(f"{arrow} {self._title}{info}（{hint}）")

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._content_edit.setVisible(self._expanded)
        if self._expanded:
            # 展开时重新 flush 一次，确保 QTextBrowser 高度精确匹配内容
            if self._render_timer.isActive():
                self._render_timer.stop()
            self._flush_to_browser()
        self._update_header()

    @property
    def text(self) -> str:
        return self._text

    def contextMenuEvent(self, event) -> None:
        from PySide6.QtWidgets import QMenu, QApplication
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {background-color: white; color: black; border: 1px solid #ccc;}
            QMenu::item {padding: 4px 20px;}
            QMenu::item:selected {background-color: #0078d7; color: white;}
        """)
        copy_action = menu.addAction("📋 复制")
        delete_action = menu.addAction("🗑 删除")
        action = menu.exec(event.globalPos())
        if action == copy_action:
            QApplication.clipboard().setText(self._text)
        elif action == delete_action:
            self.deleteRequested.emit()


class StepsProgressWidget(QFrame):
    """[schedule] 计划的可视化步骤清单（对话区）。

    每一行显示「序号 + 步骤文本 + 状态」：未开始为空、执行中为转圈(不确定进度条)、
    完成打钩并淡化。由 controller 驱动：advance() 把当前"执行中"置为完成并推进，all_done()
    把剩余全部置为完成。
    """

    _STYLE = (
        "StepsProgressWidget{background:#eef5f3;border:1px solid #cfe3dd;"
        "border-radius:8px;}"
        "QLabel#spTitle{font-weight:bold;color:#25695b;font-size:12px;}"
        "QLabel#spStep{color:#334;font-size:13px;}"
        "QLabel#spStep[state='done']{color:#8a9;text-decoration:line-through;}"
        "QLabel#spStep[state='running']{color:#25695b;font-weight:bold;}"
        "QLabel#spStep[state='skipped']{color:#d9534f;text-decoration:line-through;}"
        "QProgressBar{background:transparent;border:none;}"
        "QProgressBar::chunk{background:#2ba84a;border-radius:2px;}"
    )

    def __init__(self, steps: List[str], parent=None):
        super().__init__(parent)
        self.setObjectName("StepsProgressWidget")
        self.setStyleSheet(self._STYLE)
        self.setMaximumWidth(660)
        self.setMinimumWidth(340)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("执行计划")
        title.setObjectName("spTitle")
        header.addWidget(title)
        header.addStretch(1)
        self._count_lbl = QLabel(f"{len(steps)} 步")
        self._count_lbl.setObjectName("spTitle")
        self._count_lbl.setStyleSheet("color:#7aa08f;font-weight:normal;")
        header.addWidget(self._count_lbl)
        lay.addLayout(header)

        # 每步一行：[序号+文本] [状态图标/进度条]
        self._labels: List[QLabel] = []
        self._spinners: List[QProgressBar] = []
        self._checks: List[QLabel] = []
        self._cur = -1  # 当前执行中的步骤下标
        for idx, s in enumerate(steps):
            row = QHBoxLayout()
            row.setSpacing(6)
            check = QLabel("")
            check.setFixedWidth(18)
            check.setStyleSheet("color:#2ba84a;font-weight:bold;font-size:14px;")
            lbl = QLabel(f"{idx + 1}. {s}")
            lbl.setObjectName("spStep")
            lbl.setWordWrap(True)
            spinner = QProgressBar()
            spinner.setRange(0, 0)  # 转圈（不定模式）
            spinner.setFixedSize(54, 10)
            spinner.setVisible(False)
            self._checks.append(check)
            self._labels.append(lbl)
            self._spinners.append(spinner)
            row.addWidget(check)
            row.addWidget(lbl, 1)
            row.addWidget(spinner)
            lay.addLayout(row)
        lay.addStretch(1)

    def advance(self) -> None:
        """把当前执行中步骤置为完成，并把下一条未完成步骤置为执行中。"""
        if not self._labels:
            return
        if 0 <= self._cur < len(self._labels):
            self._labels[self._cur].setProperty("state", "done")
            self._labels[self._cur].style().unpolish(self._labels[self._cur])
            self._labels[self._cur].style().polish(self._labels[self._cur])
            self._checks[self._cur].setText("✓")
            self._spinners[self._cur].setVisible(False)
        self._cur += 1
        if self._cur < len(self._labels):
            self._labels[self._cur].setProperty("state", "running")
            self._labels[self._cur].style().unpolish(self._labels[self._cur])
            self._labels[self._cur].style().polish(self._labels[self._cur])
            self._spinners[self._cur].setVisible(True)

    def set_start(self) -> None:
        """计划创建时，把第 0 步置为执行中。"""
        if self._labels:
            self._cur = -1
            self.advance()

    def all_skip(self) -> None:
        """跳过整个剩余计划：把当前及后续全部步骤标记为跳过（✗），结束转圈。"""
        if not self._labels:
            return
        if 0 <= self._cur < len(self._labels):
            self._spinners[self._cur].setVisible(False)
        for i in range(self._cur, len(self._labels)):
            if not self._checks[i].text():
                self._checks[i].setText("✗")
                self._labels[i].setProperty("state", "skipped")
                self._labels[i].style().unpolish(self._labels[i])
                self._labels[i].style().polish(self._labels[i])
        self._cur = -1

    def all_done(self) -> None:
        """把剩余全部步骤置为完成并结束转圈。"""
        if not self._labels:
            return
        if 0 <= self._cur < len(self._labels):
            self._spinners[self._cur].setVisible(False)
        for i in range(len(self._labels)):
            if not self._checks[i].text():
                self._checks[i].setText("✓")
                self._labels[i].setProperty("state", "done")
                self._labels[i].style().unpolish(self._labels[i])
                self._labels[i].style().polish(self._labels[i])
        self._cur = -1
