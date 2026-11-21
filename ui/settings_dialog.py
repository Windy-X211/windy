# -*- coding: utf-8 -*-
"""设置对话框：接口预设管理（tag 制）、背景图、用户数据保存位置、进度字体/颜色。"""

from __future__ import annotations

from typing import Dict, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QComboBox, QSpinBox, QDoubleSpinBox,
    QFileDialog, QColorDialog, QGroupBox, QMessageBox, QWidget, QPlainTextEdit,
    QScrollArea, QFrame, QKeySequenceEdit,
)

import app_config
from ui.toast_banner import ToastBanner


class NoWheelSpinBox(QSpinBox):
    """禁用滑轮事件的 QSpinBox，防止用户误滚修改数值。"""
    def wheelEvent(self, event):
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """禁用滑轮事件的 QDoubleSpinBox。"""
    def wheelEvent(self, event):
        event.ignore()


class SettingsDialog(QDialog):
    settingsChanged = Signal()
    interfacesChanged = Signal(str)  # active tag

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumSize(740, 680)
        self.setStyleSheet(self._stylesheet())

        self._interfaces = app_config.load_interfaces()
        self._settings = app_config.load_settings()
        self._current_tag: str = ""

        root = QVBoxLayout(self)
        root.setSpacing(10)

        # 外层滚动容器
        body_scroll = QScrollArea()
        body_scroll.setWidgetResizable(True)
        body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        body_layout.addWidget(self._build_interface_group())
        body_layout.addWidget(self._build_appearance_group())
        body_layout.addWidget(self._build_advanced_group())
        body_scroll.setWidget(body)
        root.addWidget(body_scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._btn_save = QPushButton("保存")
        self._btn_close = QPushButton("关闭")
        self._btn_save.setStyleSheet(
            "QPushButton{background:#2e8b57;color:white;border-radius:5px;padding:6px 24px;font-weight:bold;}"
            "QPushButton:hover{background:#27ae60;}"
        )
        self._btn_close.setStyleSheet(
            "QPushButton{background:#95a5a6;color:white;border-radius:5px;padding:6px 24px;}"
            "QPushButton:hover{background:#7f8c8d;}"
        )
        btn_row.addWidget(self._btn_save)
        btn_row.addWidget(self._btn_close)
        root.addLayout(btn_row)

        self._btn_save.clicked.connect(self._save_all)
        self._btn_close.clicked.connect(self.accept)

        self._refresh_preset_list()

    # ------------------------------------------------------------------
    # 接口预设组
    # ------------------------------------------------------------------
    def _build_interface_group(self) -> QGroupBox:
        grp = QGroupBox("模型接口")
        lay = QVBoxLayout(grp)
        lay.setSpacing(8)

        # 默认接口下拉
        top = QHBoxLayout()
        top.addWidget(QLabel("默认接口："))
        self._active_combo = QComboBox()
        self._active_combo.currentTextChanged.connect(self._on_active_changed)
        top.addWidget(self._active_combo, 1)
        lay.addLayout(top)

        # 预设列表在左，编辑表单在右
        body = QHBoxLayout()
        self._preset_list = QListWidget()
        self._preset_list.currentTextChanged.connect(self._on_preset_selected)
        self._preset_list.setFixedWidth(220)
        body.addWidget(self._preset_list)

        # 编辑表单
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        self._e_template = QComboBox()
        self._e_template.addItems(["自定义", "agent", "视觉定位", "写作", "聊天", "编程"])
        self._e_template.currentIndexChanged.connect(self._on_template_changed)

        self._e_tag = QLineEdit()
        self._e_base = QLineEdit()
        self._e_key = QLineEdit()
        self._e_model = QLineEdit()
        self._e_tokens = NoWheelSpinBox()
        self._e_tokens.setRange(64, 128000)
        self._e_temp = QLineEdit()
        self._e_top_p = QLineEdit()
        self._e_top_k = NoWheelSpinBox()
        self._e_top_k.setRange(0, 200)
        form.addRow("模板：", self._e_template)
        form.addRow("标签：", self._e_tag)
        form.addRow("接口地址：", self._e_base)
        form.addRow("API Key：", self._e_key)
        form.addRow("模型名：", self._e_model)
        form.addRow("最大tokens：", self._e_tokens)
        form.addRow("温度：", self._e_temp)
        form.addRow("top_p：", self._e_top_p)
        form.addRow("top_k：", self._e_top_k)
        body.addWidget(form_widget, 1)
        lay.addLayout(body)

        btn_row = QHBoxLayout()
        self._btn_new = QPushButton("新建")
        self._btn_apply = QPushButton("应用")
        self._btn_delete = QPushButton("删除")
        for b in (self._btn_new, self._btn_apply, self._btn_delete):
            b.setStyleSheet(
                "QPushButton{background:#3498db;color:white;border-radius:4px;padding:5px 14px;}"
                "QPushButton:hover{background:#2980b9;}"
            )
        self._btn_new.clicked.connect(self._new_preset)
        self._btn_apply.clicked.connect(self._apply_preset)
        self._btn_delete.clicked.connect(self._delete_preset)
        btn_row.addWidget(self._btn_new)
        btn_row.addWidget(self._btn_apply)
        btn_row.addWidget(self._btn_delete)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)
        return grp

    def _refresh_preset_list(self) -> None:
        presets = self._interfaces.get("presets", {})
        self._preset_list.blockSignals(True)
        self._preset_list.clear()
        for tag in presets:
            self._preset_list.addItem(tag)
        self._preset_list.blockSignals(False)
        self._active_combo.blockSignals(True)
        self._active_combo.clear()
        for tag in presets:
            self._active_combo.addItem(tag)
        active = self._interfaces.get("active", "")
        if active and active in presets:
            self._active_combo.setCurrentText(active)
        self._active_combo.blockSignals(False)

    def _on_preset_selected(self, tag: str) -> None:
        preset = self._interfaces.get("presets", {}).get(tag)
        if not preset:
            return
        self._e_template.setCurrentIndex(0)  # 加载预设时切回自定义
        self._current_tag = tag
        self._e_tag.setText(tag)
        self._e_base.setText(preset.get("base_url", ""))
        self._e_key.setText(preset.get("api_key", ""))
        self._e_model.setText(preset.get("model", ""))
        self._e_tokens.setValue(int(preset.get("max_tokens", 4096)))
        self._e_temp.setText(str(preset.get("temperature", 0.2)))
        self._e_top_p.setText(str(preset.get("top_p", 0.9)))
        self._e_top_k.setValue(int(preset.get("top_k", 50)))

    _TEMPLATE_PARAMS = {
        "agent": {"max_tokens": 8192, "temperature": 0.1, "top_p": 0.9, "top_k": 20},
        "视觉定位": {"max_tokens": 1024, "temperature": 0.05, "top_p": 0.9, "top_k": 10},
        "写作": {"max_tokens": 16384, "temperature": 0.7, "top_p": 0.95, "top_k": 60},
        "聊天": {"max_tokens": 4096, "temperature": 0.7, "top_p": 0.9, "top_k": 50},
        "编程": {"max_tokens": 16384, "temperature": 0.2, "top_p": 0.9, "top_k": 20},
    }

    def _on_active_changed(self, tag: str) -> None:
        if tag and tag in self._interfaces.get("presets", {}):
            self._interfaces["active"] = tag
            app_config.save_interfaces(self._interfaces)
            self.interfacesChanged.emit(tag)

    def _on_template_changed(self, index: int) -> None:
        """切换模板时更新参数，切换到自定义时保留当前参数不变。"""
        name = self._e_template.currentText()
        if name == "自定义":
            return
        params = self._TEMPLATE_PARAMS.get(name)
        if not params:
            return
        self._e_tokens.setValue(params["max_tokens"])
        self._e_temp.setText(str(params["temperature"]))
        self._e_top_p.setText(str(params["top_p"]))
        self._e_top_k.setValue(params["top_k"])

    def _new_preset(self) -> None:
        self._preset_list.clearSelection()
        self._current_tag = ""
        self._e_template.setCurrentIndex(0)  # 新建时切回自定义
        self._e_tag.clear()
        self._e_base.clear()
        self._e_key.clear()
        self._e_model.clear()
        self._e_tokens.setValue(4096)
        self._e_temp.setText("0.2")
        self._e_top_p.setText("0.9")
        self._e_top_k.setValue(50)
        self._e_tag.setFocus()

    def _apply_preset(self) -> None:
        tag = self._e_tag.text().strip() or self._e_base.text().strip()
        if not tag:
            QMessageBox.warning(self, "提示", "标签和接口地址不能同时为空")
            return
        preset: Dict[str, Any] = {
            "base_url": self._e_base.text().strip(),
            "api_key": self._e_key.text().strip(),
            "model": self._e_model.text().strip(),
            "max_tokens": self._e_tokens.value(),
            "temperature": self._parse_float(self._e_temp.text(), 0.2),
            "top_p": self._parse_float(self._e_top_p.text(), 0.9),
            "top_k": self._e_top_k.value(),
            "supports_vision": True,
        }
        if self._current_tag and self._current_tag != tag:
            self._interfaces["presets"].pop(self._current_tag, None)
            if self._interfaces.get("active") == self._current_tag:
                self._interfaces["active"] = tag
        self._interfaces["presets"][tag] = preset
        if not self._interfaces.get("active"):
            self._interfaces["active"] = tag
        app_config.save_interfaces(self._interfaces)
        self._current_tag = tag
        self._refresh_preset_list()
        self._preset_list.setCurrentRow(list(self._interfaces["presets"].keys()).index(tag))
        ToastBanner.show_message(self, f"接口预设「{tag}」已应用", "success")

    def _delete_preset(self) -> None:
        tag = self._current_tag or (self._preset_list.currentItem().text() if self._preset_list.currentItem() else "")
        if not tag:
            return
        presets = self._interfaces["presets"]
        if len(presets) <= 1:
            QMessageBox.warning(self, "提示", "至少需要保留一个接口预设")
            return
        presets.pop(tag, None)
        if self._interfaces.get("active") == tag:
            self._interfaces["active"] = next(iter(presets))
        app_config.save_interfaces(self._interfaces)
        self._current_tag = ""
        self._refresh_preset_list()
        self.interfacesChanged.emit(self._interfaces.get("active", ""))

    # ------------------------------------------------------------------
    # 界面与外观组
    # ------------------------------------------------------------------
    def _build_appearance_group(self) -> QGroupBox:
        grp = QGroupBox("界面与外观")
        lay = QVBoxLayout(grp)
        lay.setSpacing(8)

        # -- 子分组：主窗口 --
        sub1 = QGroupBox("主窗口")
        f1 = QFormLayout(sub1)
        f1.setContentsMargins(8, 12, 8, 8)
        f1.setVerticalSpacing(8)
        f1.setHorizontalSpacing(12)
        f1.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        f1.setLabelAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        self._e_window_title = QLineEdit(self._settings.get("window_title", ""))
        self._e_window_title.setPlaceholderText("留空则不显示标题")
        self._e_window_title.setMinimumHeight(28)
        f1.addRow("窗口标题：", self._e_window_title)

        self._e_bg = QLineEdit(self._settings.get("background_image", ""))
        self._e_bg.setMinimumHeight(28)
        self._btn_bg = QPushButton("选择…")
        self._btn_bg.setFixedHeight(28)
        self._btn_bg.setFixedWidth(60)
        self._btn_bg.clicked.connect(self._pick_bg)
        bg_row = QHBoxLayout()
        bg_row.addWidget(self._e_bg, 1)
        bg_row.addWidget(self._btn_bg)
        bg_w = QWidget(); bg_w.setLayout(bg_row)
        f1.addRow("背景图片：", bg_w)

        self._e_bubble_opacity = NoWheelSpinBox()
        self._e_bubble_opacity.setRange(0, 100)
        self._e_bubble_opacity.setSuffix(" %")
        self._e_bubble_opacity.setValue(int(self._settings.get("bubble_opacity", 0)))
        self._e_bubble_opacity.setFixedWidth(100)
        self._e_bubble_opacity.setMinimumHeight(28)
        self._e_bubble_opacity.setToolTip("0=不透明，100=完全透明；仅背景淡化，文字保持清晰")
        f1.addRow("消息泡泡透明度：", self._e_bubble_opacity)

        self._e_ide_theme = QComboBox()
        self._e_ide_theme.addItem("白色", "light")
        self._e_ide_theme.addItem("深色", "dark")
        idx = self._e_ide_theme.findData(self._settings.get("ide_theme", "light"))
        if idx >= 0:
            self._e_ide_theme.setCurrentIndex(idx)
        self._e_ide_theme.setFixedWidth(140)
        self._e_ide_theme.setMinimumHeight(28)
        self._e_ide_theme.setToolTip("IDE 窗口外观：白色（默认）或深色")
        f1.addRow("IDE 配色：", self._e_ide_theme)
        lay.addWidget(sub1)

        # -- 子分组：进度显示 --
        sub2 = QGroupBox("进度显示")
        f2 = QFormLayout(sub2)
        f2.setContentsMargins(8, 12, 8, 8)
        f2.setVerticalSpacing(8)
        f2.setHorizontalSpacing(12)
        f2.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        f2.setLabelAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        self._e_exec_opacity = NoWheelSpinBox()
        self._e_exec_opacity.setRange(0, 100)
        self._e_exec_opacity.setSuffix(" %")
        self._e_exec_opacity.setValue(int(self._settings.get("execution_opacity", 85)))
        self._e_exec_opacity.setFixedWidth(100)
        self._e_exec_opacity.setMinimumHeight(28)
        f2.addRow("执行窗口透明度：", self._e_exec_opacity)

        self._e_fontsize = NoWheelSpinBox()
        self._e_fontsize.setRange(8, 40)
        self._e_fontsize.setValue(int(self._settings.get("progress_font_size", 12)))
        self._e_fontsize.setFixedWidth(100)
        self._e_fontsize.setMinimumHeight(28)
        f2.addRow("进度字体大小：", self._e_fontsize)

        self._color_done = self._settings.get("progress_color_done", "#2E8B57")
        self._color_pending = self._settings.get("progress_color_pending", "#B0B0B0")
        self._btn_color_done = QPushButton(self._color_done)
        self._btn_color_pending = QPushButton(self._color_pending)
        self._btn_color_done.setFixedHeight(28)
        self._btn_color_pending.setFixedHeight(28)
        self._btn_color_done.setFixedWidth(100)
        self._btn_color_pending.setFixedWidth(100)
        self._btn_color_done.clicked.connect(lambda: self._pick_color("_color_done", self._btn_color_done))
        self._btn_color_pending.clicked.connect(lambda: self._pick_color("_color_pending", self._btn_color_pending))
        f2.addRow("已完成颜色：", self._btn_color_done)
        f2.addRow("未完成颜色：", self._btn_color_pending)
        lay.addWidget(sub2)

        # -- 子分组：任务清单 --
        sub3 = QGroupBox("任务清单")
        f3 = QFormLayout(sub3)
        f3.setContentsMargins(8, 12, 8, 8)
        f3.setVerticalSpacing(8)
        f3.setHorizontalSpacing(12)
        f3.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        f3.setLabelAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        self._e_tasklist_opacity = NoWheelSpinBox()
        self._e_tasklist_opacity.setRange(0, 100)
        self._e_tasklist_opacity.setSuffix(" %")
        self._e_tasklist_opacity.setValue(int(self._settings.get("task_list_opacity", 0)))
        self._e_tasklist_opacity.setFixedWidth(100)
        self._e_tasklist_opacity.setMinimumHeight(28)
        self._e_tasklist_opacity.setToolTip("0=不透明，100=完全透明；仅背景淡化，文字保持清晰")
        f3.addRow("背景透明度：", self._e_tasklist_opacity)
        lay.addWidget(sub3)

        return grp

    # ------------------------------------------------------------------
    # 高级组
    # ------------------------------------------------------------------
    def _build_advanced_group(self) -> QGroupBox:
        grp = QGroupBox("高级")
        form = QFormLayout(grp)
        form.setContentsMargins(10, 14, 10, 10)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        self._e_scroll = NoWheelSpinBox()
        self._e_scroll.setRange(1, 50)
        self._e_scroll.setValue(int(self._settings.get("scroll_amount", 5)))
        self._e_scroll.setFixedWidth(100)
        self._e_scroll.setMinimumHeight(28)
        form.addRow("滑动档量：", self._e_scroll)

        self._e_model_timeout = NoWheelSpinBox()
        self._e_model_timeout.setRange(5, 600)
        self._e_model_timeout.setSuffix(" 秒")
        self._e_model_timeout.setSingleStep(5)
        self._e_model_timeout.setValue(int(self._settings.get("model_timeout", 60)))
        self._e_model_timeout.setFixedWidth(100)
        self._e_model_timeout.setMinimumHeight(28)
        form.addRow("模型请求超时：", self._e_model_timeout)

        self._e_pro_max_iter = NoWheelSpinBox()
        self._e_pro_max_iter.setRange(1, 1000)
        self._e_pro_max_iter.setValue(int(self._settings.get("pro_max_iterations", 30)))
        self._e_pro_max_iter.setFixedWidth(100)
        self._e_pro_max_iter.setMinimumHeight(28)
        self._e_pro_max_iter.setToolTip("Pro 编程智能体在一个会话中最多的模型回合数，防止死循环；超限自动停止并总结")
        form.addRow("Pro 最大迭代：", self._e_pro_max_iter)

        self._e_pro_stream_h = NoWheelSpinBox()
        self._e_pro_stream_h.setRange(0, 800)
        self._e_pro_stream_h.setSuffix(" px")
        self._e_pro_stream_h.setSingleStep(20)
        self._e_pro_stream_h.setValue(int(self._settings.get("pro_stream_window_height", 300)))
        self._e_pro_stream_h.setFixedWidth(100)
        self._e_pro_stream_h.setMinimumHeight(28)
        self._e_pro_stream_h.setToolTip(
            "Pro 编程模式下智能体流式输出窗口的固定高度；"
            "0 表示自适应内容、不固定高度"
        )
        form.addRow("Pro 流式窗口高度：", self._e_pro_stream_h)

        self._e_agent_post_wait = NoWheelDoubleSpinBox()
        self._e_agent_post_wait.setRange(0.0, 10.0)
        self._e_agent_post_wait.setSuffix(" 秒")
        self._e_agent_post_wait.setSingleStep(0.1)
        self._e_agent_post_wait.setDecimals(1)
        self._e_agent_post_wait.setValue(self._settings.get("agent_post_wait", 0.5))
        self._e_agent_post_wait.setFixedWidth(100)
        self._e_agent_post_wait.setMinimumHeight(28)
        self._e_agent_post_wait.setToolTip("自主代理模式每执行完一个工具后等待的时长，让界面有时间响应后再截图")
        form.addRow("代理工具等待：", self._e_agent_post_wait)

        self._e_move_duration = NoWheelDoubleSpinBox()
        self._e_move_duration.setRange(0.0, 2.0)
        self._e_move_duration.setSuffix(" 秒")
        self._e_move_duration.setSingleStep(0.05)
        self._e_move_duration.setDecimals(2)
        self._e_move_duration.setValue(self._settings.get("mouse_move_duration", 0.0))
        self._e_move_duration.setFixedWidth(100)
        self._e_move_duration.setMinimumHeight(28)
        self._e_move_duration.setToolTip("鼠标从当前位置移动到目标位置的持续时长，0=瞬移，建议0.1~0.5 秒")
        form.addRow("鼠标移动时长：", self._e_move_duration)

        self._e_stop_shortcut = QKeySequenceEdit()
        self._e_stop_shortcut.setKeySequence(QKeySequence(self._settings.get("stop_shortcut", "Ctrl+.")))
        self._e_stop_shortcut.setFixedWidth(150)
        self._e_stop_shortcut.setMinimumHeight(28)
        self._e_stop_shortcut.setToolTip("自主代理模式的停止快捷键，点击后按下你想要绑定的键组合")
        form.addRow("停止快捷键：", self._e_stop_shortcut)

        self._e_data = QLineEdit(self._settings.get("user_data_dir", app_config.USER_DATA_DIR_DEFAULT))
        self._e_data.setPlaceholderText("历史、工作流、脚本、截图等所有用户数据存放位置")
        self._e_data.setMinimumHeight(28)
        self._btn_data = QPushButton("选择…")
        self._btn_data.setFixedHeight(28)
        self._btn_data.setFixedWidth(60)
        self._btn_data.clicked.connect(self._pick_data_dir)
        d_row = QHBoxLayout()
        d_row.addWidget(self._e_data, 1)
        d_row.addWidget(self._btn_data)
        d_w = QWidget(); d_w.setLayout(d_row)
        form.addRow("用户数据位置：", d_w)

        return grp

    def _pick_bg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择背景（图片或视频）", "",
            "背景文件 (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.mp4 *.mov *.avi *.mkv);;"
            "图片 (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;"
            "视频 (*.mp4 *.mov *.avi *.mkv)"
        )
        if path:
            self._e_bg.setText(path)

    def _pick_data_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择用户数据保存位置")
        if path:
            self._e_data.setText(path)

    def _pick_color(self, attr: str, btn: QPushButton) -> None:
        cur = getattr(self, attr, "#000000")
        color = QColorDialog.getColor(Qt.GlobalColor.white if False else self._qcolor(cur), self)
        if color.isValid():
            hexc = color.name()
            setattr(self, attr, hexc)
            btn.setText(hexc)

    def _qcolor(self, hexc: str):
        from PySide6.QtGui import QColor
        return QColor(hexc)

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------
    def _save_all(self) -> None:
        self._settings["background_image"] = self._e_bg.text().strip()
        self._settings["window_title"] = self._e_window_title.text().strip()
        self._settings["user_data_dir"] = self._e_data.text().strip() or app_config.USER_DATA_DIR_DEFAULT
        self._settings.pop("history_dir", None)
        self._settings["execution_opacity"] = self._e_exec_opacity.value()
        self._settings["task_list_opacity"] = self._e_tasklist_opacity.value()
        self._settings["bubble_opacity"] = self._e_bubble_opacity.value()
        self._settings["ide_theme"] = self._e_ide_theme.currentData() or "light"
        self._settings["progress_font_size"] = self._e_fontsize.value()
        self._settings["progress_color_done"] = self._color_done
        self._settings["progress_color_pending"] = self._color_pending
        self._settings["scroll_amount"] = self._e_scroll.value()
        self._settings["model_timeout"] = self._e_model_timeout.value()
        self._settings["pro_max_iterations"] = self._e_pro_max_iter.value()
        self._settings["pro_stream_window_height"] = self._e_pro_stream_h.value()
        self._settings["agent_post_wait"] = self._e_agent_post_wait.value()
        self._settings["mouse_move_duration"] = self._e_move_duration.value()
        self._settings["stop_shortcut"] = self._e_stop_shortcut.keySequence().toString()
        app_config.save_settings(self._settings)
        app_config.save_interfaces(self._interfaces)
        self.settingsChanged.emit()
        ToastBanner.show_message(self, "设置已保存", "success")

    @staticmethod
    def _parse_float(text: str, default: float) -> float:
        try:
            return float(text.strip())
        except (ValueError, AttributeError):
            return default

    @staticmethod
    def _stylesheet() -> str:
        return (
            "QDialog{background:#f5f6fa;}"
            "QGroupBox{font-weight:bold;border:1px solid #d0d4dc;border-radius:6px;"
            "margin-top:10px;padding:10px 8px 8px 8px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:12px;padding:0 6px;}"
            "QGroupBox>QGroupBox{font-weight:normal;background:#fafbfc;border:1px solid #e0e4ea;"
            "border-radius:5px;margin-top:8px;padding:8px 6px 6px 6px;}"
            "QGroupBox>QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;color:#555;}"
            "QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox{background:white;border:1px solid #ccc;"
            "border-radius:4px;padding:4px 6px;}"
            "QLineEdit:focus,QComboBox:focus,QSpinBox:focus,QDoubleSpinBox:focus{"
            "border-color:#3498db;}"
            "QListWidget{background:white;border:1px solid #ccc;border-radius:4px;}"
            "QPushButton{min-height:24px;}"
            "QScrollArea{border:none;background:transparent;}"
        )


class SystemPromptDialog(QDialog):
    """系统提示词编辑对话框，绑定到某个接口预设。"""

    _KEYS = [
        ("programming", "编程模式"),
        ("planning", "规划模式"),
        ("long_input", "长输入写作"),
    ]

    def __init__(self, prompts: Dict[str, str], initial_key: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("系统提示词")
        self.setMinimumSize(600, 400)
        self.setStyleSheet(self._stylesheet())

        self._prompts = dict(prompts)  # {key: text}

        root = QVBoxLayout(self)
        root.setSpacing(8)

        # 类型选择
        top = QHBoxLayout()
        top.addWidget(QLabel("提示词类型："))
        self._combo = QComboBox()
        for key, label in self._KEYS:
            self._combo.addItem(label, key)
        self._combo.currentIndexChanged.connect(self._on_type_changed)
        top.addWidget(self._combo, 1)

        self._reset_btn = QPushButton("恢复默认")
        self._reset_btn.setStyleSheet(
            "QPushButton{background:#e67e22;color:white;border-radius:4px;padding:4px 12px;}"
            "QPushButton:hover{background:#d35400;}"
        )
        self._reset_btn.clicked.connect(self._reset_current)
        top.addWidget(self._reset_btn)
        root.addLayout(top)

        # 提示词编辑器
        self._edit = QPlainTextEdit()
        self._edit.setStyleSheet(
            "QPlainTextEdit{background:white;border:1px solid #ccc;border-radius:4px;"
            "padding:6px;font-family:Consolas,'Microsoft YaHei',monospace;font-size:13px;}"
        )
        root.addWidget(self._edit, 1)

        # 操作按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._btn_save = QPushButton("保存")
        self._btn_cancel = QPushButton("取消")
        self._btn_save.setStyleSheet("QPushButton{background:#2e8b57;color:white;border-radius:5px;padding:6px 20px;}")
        self._btn_cancel.setStyleSheet("QPushButton{background:#95a5a6;color:white;border-radius:5px;padding:6px 20px;}")
        self._btn_save.clicked.connect(self.accept)
        self._btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_save)
        btn_row.addWidget(self._btn_cancel)
        root.addLayout(btn_row)

        # 加载当前选中类型
        if initial_key:
            idx = self._combo.findData(initial_key)
            if idx >= 0:
                self._combo.setCurrentIndex(idx)
            else:
                self._on_type_changed(0)
        else:
            self._on_type_changed(0)

    # ------------------------------------------------------------------
    # 默认值
    # ------------------------------------------------------------------
    @staticmethod
    def _get_default(key: str) -> str:
        """返回指定类型的默认提示词文本。"""
        return app_config.DEFAULT_SYSTEM_PROMPTS.get(key, "")

    # ------------------------------------------------------------------
    # 切换 / 恢复
    # ------------------------------------------------------------------
    def _on_type_changed(self, index: int) -> None:
        key = self._combo.itemData(index)
        text = self._prompts.get(key) or self._get_default(key)
        self._edit.setPlainText(text)

    def _reset_current(self) -> None:
        """恢复默认：填入默认文本，保存时如果与默认相同则自动移除（回退到默认）。"""
        key = self._combo.currentData()
        self._edit.setPlainText(self._get_default(key))

    def get_prompts(self) -> Dict[str, str]:
        """返回 {key: text} 字典，仅包含与默认不同的自定义项。"""
        key = self._combo.currentData()
        text = self._edit.toPlainText().strip()
        default = self._get_default(key)
        if text and text != default:
            self._prompts[key] = text
        else:
            self._prompts.pop(key, None)
        result = {}
        for k, v in self._prompts.items():
            if v.strip() and v.strip() != self._get_default(k):
                result[k] = v.strip()
        return result

    @staticmethod
    def _stylesheet() -> str:
        return (
            "QDialog{background:#f5f6fa;}"
            "QPlainTextEdit{background:white;border:1px solid #ccc;border-radius:4px;"
            "padding:6px;font-family:Consolas,'Microsoft YaHei',monospace;font-size:13px;}"
        )