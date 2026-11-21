# -*- coding: utf-8 -*-
"""模型配置对话框：为 6 种模型（自主代理/规划/视觉/聊天/写作/编程）分别选择不同接口预设。

点击「模型配置」按钮弹出，每个模型一个下拉框，可在预设列表中挑选，
或留空（= 使用默认接口）。保存后立即生效，模型客户端下次调用时
按新映射路由。
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QComboBox, QGroupBox, QWidget, QPlainTextEdit, QScrollArea,
)

import app_config
from ui.toast_banner import ToastBanner

# 模型按「四个模式」分组：分组名 → [ (label, 显示名称, 提示说明), ... ]
MODEL_GROUPS: Dict[str, list] = [
    ("自主代理模式", [
        ("agent_mode", "自主代理模型", "自主截图观察屏幕，决策并执行桌面操作（需支持视觉的多模态接口）"),
    ]),
    ("规划模式", [
        ("planning", "规划模型", "拆解用户目标为原子化执行步骤（单击/输入/长输入等）"),
        ("vision",   "视觉模型", "在截图中定位目标元素，返回归一化坐标（需支持视觉的接口）"),
        ("long_input", "写作模型", "执行「长输入」步骤时流式生成正文（邮件、段落、文章等）"),
    ]),
    ("聊天模式", [
        ("chat",     "聊天模型", "聊天模式 assistant 回复 & 执行时咨询用户的对话"),
        ("cos",      "角色模型", "cos 角色扮演聊天的单模型：既扮演角色又自控节奏，自主循环输出 [say]/[wait]/[end] 命令"),
        ("guide",    "引导模型", "Pro 真人聊天的导演：分析意图、指挥聊天模型，决定何时开口/停下/主动找话题"),
        ("persona",  "人设模型", "AI 一键生成 cos 角色人设内容（角色设定、性格、说话风格等）"),
    ]),
    ("编程模式", [
        ("programming", "编程模型", "标准编程模式：一次性生成代码（Python 脚本、函数、工具等）"),
        ("programmer", "高级编程模型", "Pro 编程智能体：自主设计/新建/改造项目，输出 [命令] 控制符操作文件与运行"),
        ("checker",  "检查模型", "Pro 编程智能体：只读检查项目文件语法/导入/逻辑错误，输出 相对路径:行号: 信息"),
    ]),
]

# 平铺后的模型元数据，供遍历使用
MODEL_META = [item for _group, items in MODEL_GROUPS for item in items]

# 模型 label → 对应的系统提示词 key（所有模型都有对应的系统提示词）
_LABEL_TO_SP_KEY: Dict[str, str] = {
    "agent_mode": "agent_mode",
    "planning": "planning",
    "vision": "vision",
    "chat": "chat",
    "cos": "cos",
    "guide": "guide",
    "persona": "persona",
    "long_input": "long_input",
    "programming": "programming",
    "programmer": "programmer",
    "checker": "checker",
}


class ModelConfigDialog(QDialog):
    """各模式模型→接口预设的映射配置对话框（按四个模式分组）。"""

    # 保存后发出：更新后的 interfaces（含新 model_map）
    modelsConfigChanged = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("模型配置")
        self.setMinimumWidth(600)
        self.setMinimumHeight(360)
        self.setStyleSheet(self._stylesheet())

        self._interfaces = app_config.load_interfaces()
        self._combos: Dict[str, QComboBox] = {}

        root = QVBoxLayout(self)
        root.setSpacing(12)

        root.addWidget(self._build_hint_box())

        # 模型列表包进滚动区，避免模型行过多时窗口高度超过屏幕导致 Qt 几何警告
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._build_models_group())
        root.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        self._btn_open_settings = QPushButton("🖉 编辑接口预设…")
        self._btn_open_settings.setStyleSheet(
            "QPushButton{background:#3498db;color:white;border-radius:5px;padding:6px 16px;font-weight:bold;}"
            "QPushButton:hover{background:#2980b9;}"
        )
        self._btn_open_settings.clicked.connect(self._open_settings_dialog)
        btn_row.addWidget(self._btn_open_settings)
        btn_row.addStretch(1)
        self._btn_cancel = QPushButton("取消")
        self._btn_save = QPushButton("保存")
        self._btn_cancel.setStyleSheet(
            "QPushButton{background:#95a5a6;color:white;border-radius:5px;padding:6px 20px;font-weight:bold;}"
            "QPushButton:hover{background:#7f8c8d;}"
        )
        self._btn_save.setStyleSheet(
            "QPushButton{background:#2e8b57;color:white;border-radius:5px;padding:6px 20px;font-weight:bold;}"
            "QPushButton:hover{background:#3aa569;}"
        )
        self._btn_save.setDefault(True)
        btn_row.addWidget(self._btn_cancel)
        btn_row.addWidget(self._btn_save)
        root.addLayout(btn_row)

        self._btn_cancel.clicked.connect(self.reject)
        self._btn_save.clicked.connect(self._save)

        self._reload_combos()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    @staticmethod
    def _build_hint_box() -> QLabel:
        lbl = QLabel(
            "为每种模型选择对应的接口预设。留空（使用默认接口）表示未单独配置，\n"
            "将回退到当前的默认接口（在「编辑接口预设」里可新增或修改预设）。"
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            "QLabel{background:#ecf0f1;color:#34495e;padding:8px 12px;"
            "border-radius:6px;border:1px solid #d0d4dc;font-size:12px;}"
        )
        return lbl

    def _build_models_group(self) -> QWidget:
        """按四个模式分组构建模型→接口预设映射。"""
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setSpacing(10)

        for group_name, models in MODEL_GROUPS:
            grp = QGroupBox(group_name)
            form = QFormLayout(grp)
            form.setSpacing(10)

            for label, display, hint in models:
                combo = QComboBox()
                combo.wheelEvent = lambda e: None  # 禁用滚轮切换，防止误触
                combo.setMinimumWidth(220)
                self._combos[label] = combo

                # 系统提示词按钮
                btn = QPushButton("系统提示词")
                btn.setStyleSheet(
                    "QPushButton{background:#8e44ad;color:white;border-radius:4px;"
                    "padding:3px 10px;font-size:12px;font-weight:bold;}"
                    "QPushButton:hover{background:#7d3c98;}"
                )
                btn.clicked.connect(lambda checked, lb=label: self._open_system_prompt(lb))

                # 组合框 + 按钮 水平排列
                row = QHBoxLayout()
                row.setSpacing(6)
                row.addWidget(combo, 1)
                row.addWidget(btn)

                hint_lbl = QLabel(hint)
                hint_lbl.setWordWrap(True)
                hint_lbl.setStyleSheet("color:#7f8c8d;font-size:11px;")

                wrapper = QVBoxLayout()
                wrapper.setSpacing(2)
                wrapper.addLayout(row)
                wrapper.addWidget(hint_lbl)
                wrap_w = QWidget()
                wrap_w.setLayout(wrapper)
                form.addRow(f"{display}：", wrap_w)

            outer.addWidget(grp)
        outer.addStretch(1)
        return wrap

    def _open_system_prompt(self, label: str) -> None:
        """打开指定模型 label 对应的系统提示词编辑对话框，仅显示该模型的提示词。"""
        # 获取当前选中的预设
        combo = self._combos.get(label)
        if not combo:
            return
        tag = combo.currentData() or ""
        if not tag:
            preset = app_config.get_active_preset(self._interfaces)
        else:
            preset = self._interfaces.get("presets", {}).get(tag, {})
        if not preset:
            ToastBanner.show_message(self, "请先为该模型选择接口预设", "warning")
            return

        sp_key = _LABEL_TO_SP_KEY.get(label)

        prompts = preset.get("system_prompts", {})
        current_text = prompts.get(sp_key) or ""

        # 简单单类型编辑器
        dlg = QDialog(self)
        dlg.setWindowTitle(f"系统提示词 - {self._get_display_name(label)}")
        dlg.setMinimumSize(600, 400)
        dlg.setStyleSheet(
            "QDialog{background:#f5f6fa;}"
            "QPlainTextEdit{background:white;border:1px solid #ccc;border-radius:4px;"
            "padding:6px;font-family:Consolas,'Microsoft YaHei',monospace;font-size:13px;}"
        )

        root = QVBoxLayout(dlg)
        root.setSpacing(8)

        root.addWidget(QLabel("编辑系统提示词（留空=使用默认提示词）："))

        edit = QPlainTextEdit()
        if current_text:
            edit.setPlainText(current_text)
        else:
            from app_config import DEFAULT_SYSTEM_PROMPTS
            edit.setPlainText(DEFAULT_SYSTEM_PROMPTS.get(sp_key, ""))
        root.addWidget(edit, 1)

        # 恢复默认按钮
        reset_row = QHBoxLayout()
        reset_btn = QPushButton("恢复默认")
        reset_btn.setStyleSheet(
            "QPushButton{background:#e67e22;color:white;border-radius:4px;padding:4px 12px;}"
            "QPushButton:hover{background:#d35400;}"
        )
        reset_btn.clicked.connect(lambda: edit.setPlainText(
            app_config.DEFAULT_SYSTEM_PROMPTS.get(sp_key, "")))
        reset_row.addWidget(reset_btn)
        reset_row.addStretch(1)
        root.addLayout(reset_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_save = QPushButton("保存")
        btn_cancel = QPushButton("取消")
        btn_save.setStyleSheet("QPushButton{background:#2e8b57;color:white;border-radius:5px;padding:6px 20px;}")
        btn_cancel.setStyleSheet("QPushButton{background:#95a5a6;color:white;border-radius:5px;padding:6px 20px;}")
        btn_save.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_cancel)
        root.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            text = edit.toPlainText().strip()
            default = app_config.DEFAULT_SYSTEM_PROMPTS.get(sp_key, "")
            if text and text != default:
                prompts[sp_key] = text
            else:
                prompts.pop(sp_key, None)
            if prompts:
                preset["system_prompts"] = prompts
            else:
                preset.pop("system_prompts", None)
            app_config.save_interfaces(self._interfaces)
            ToastBanner.show_message(self, "系统提示词已保存", "success")

    @staticmethod
    def _get_display_name(label: str) -> str:
        for lbl, display, _hint in MODEL_META:
            if lbl == label:
                return display
        return label

    def _reload_combos(self) -> None:
        """重新读取 interfaces 并填充下拉框选项。"""
        self._interfaces = app_config.load_interfaces()
        presets = self._interfaces.get("presets", {})
        model_map = app_config.get_model_map(self._interfaces)
        active = self._interfaces.get("active", "")

        preset_tags = list(presets.keys())

        for label, _display, _hint in MODEL_META:
            combo = self._combos[label]
            combo.blockSignals(True)
            combo.clear()
            # 首项：默认接口（回退项）
            active_display = f"【使用默认接口】" + (f" ({active})" if active else "")
            combo.addItem(active_display, "")
            for tag in preset_tags:
                # 标注当前是否是默认接口
                mark = "  ←默认" if tag == active else ""
                combo.addItem(f"{tag}{mark}", tag)
            # 选中当前已配置的项
            cur = model_map.get(label, "")
            if cur and cur in preset_tags:
                idx = combo.findData(cur)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    combo.setCurrentIndex(0)
            else:
                combo.setCurrentIndex(0)
            combo.blockSignals(False)

    # ------------------------------------------------------------------
    # 打开 SettingsDialog 编辑预设
    # ------------------------------------------------------------------
    def _open_settings_dialog(self) -> None:
        """打开设置对话框编辑接口预设；关闭后刷新当前下拉框。"""
        # 延迟导入，避免循环依赖（settings_dialog 也 import app_config）
        from ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self)
        # 保存成功后刷新：settingsChanged / interfacesChanged 任意一个都触发刷新
        dlg.settingsChanged.connect(self._reload_combos)
        dlg.interfacesChanged.connect(lambda _t: self._reload_combos())
        dlg.exec()
        # 无论用户是否点击了"保存"，最后再刷新一次（以防用户修改了预设但没触发信号）
        self._reload_combos()

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------
    def _save(self) -> None:
        model_map: Dict[str, str] = {}
        for label, _display, _hint in MODEL_META:
            combo = self._combos[label]
            tag = combo.currentData() or ""
            model_map[label] = tag
        self._interfaces = app_config.save_model_map(model_map, self._interfaces)
        # 让客户端缓存失效（切换接口后需重建 OpenAI 客户端）
        try:
            import model_client
            model_client.invalidate_cache()
        except Exception:
            pass
        self.modelsConfigChanged.emit(self._interfaces)
        ToastBanner.show_message(self, "模型配置已保存，下次调用模型时生效", "success")
        # 延迟关闭，让用户看到横幅提示
        from PySide6.QtCore import QTimer
        QTimer.singleShot(800, self.accept)

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------
    @staticmethod
    def _stylesheet() -> str:
        return (
            "QDialog{background:#f5f6fa;}"
            "QScrollArea{background:transparent;}"
            "QScrollArea>QWidget>QWidget{background:transparent;}"
            "QGroupBox{font-weight:bold;border:1px solid #d0d4dc;border-radius:6px;"
            "margin-top:10px;padding:10px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;}"
            "QComboBox{background:white;border:1px solid #ccc;border-radius:4px;padding:3px 8px;min-height:22px;}"
            "QComboBox:hover{border-color:#3498db;}"
        )
