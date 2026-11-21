# -*- coding: utf-8 -*-
"""复现 Pro 模式思考框「正文后大片空白」问题（临时脚本，运行后删除）"""
import os, sys, traceback

# ---- Qt 插件路径补丁（Anaconda 下 PySide6 无内置插件目录）----
_cand = r"F:\anaconda3\Library\lib\qt6\plugins\platforms"
if os.path.isdir(_cand):
    os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", _cand)

sys.path.insert(0, r"F:\PythonProjects\TraeProjects\规划性桌面代理")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from ui.chat_view import ThinkingBubbleWidget

app = QApplication(sys.argv)

out_dir = r"F:\PythonProjects\TraeProjects\规划性桌面代理\_probe_out"
os.makedirs(out_dir, exist_ok=True)

def build(window_height, text_chunks, tool_rows, extra_text_chunks=None):
    w = ThinkingBubbleWidget("", title="智能体", window_height=window_height)
    w.expand()
    w.resize(660, 400)
    for c in text_chunks:
        w.append_agent_text(c)
    for t in tool_rows:
        w.append_tool_call(t)
    if extra_text_chunks:
        for c in extra_text_chunks:
            w.append_agent_text(c)
    w._render_timer.stop()
    w._flush_to_browser()
    return w

cases = [
    ("A_normal_auto", 0, ["这是第一段正文。\n第二行正文。"], ["[run] 执行 python main.py"], None),
    ("B_normal_fixed300", 300, ["这是第一段正文。\n第二行正文。"], ["[run] 执行 python main.py"], None),
    ("C_trailing_newlines_fixed300", 300, ["这是第一段正文。\n第二行正文。\n\n\n"], ["[run] 执行 python main.py"], None),
    ("D_trailing_newlines_auto", 0, ["这是第一段正文。\n第二行正文。\n\n\n"], ["[run] 执行 python main.py"], None),
    ("E_text_tool_text_fixed300", 300, ["前一段正文。\n\n"], ["[run] 执行 python main.py"], ["\n后一段正文。"]),
]

for name, h, chunks, tools, extra in cases:
    try:
        w = build(h, chunks, tools, extra)
        w.show()
        app.processEvents()
        doc = w._content_edit.document()
        print(f"=== {name} === widget_h={w.height()} content_h={w._content_edit.height()} "
              f"doc_h={doc.size().height()} blocks={doc.blockCount()}", flush=True)
        b = doc.begin()
        while b.isValid():
            print(f"  block[{b.blockNumber()}] text={b.text()!r} h={b.layout().boundingRect().height():.1f}", flush=True)
            b = b.next()
        img = w.grab()
        img.save(os.path.join(out_dir, f"{name}.png"))
        w.close()
        w.deleteLater()
        app.processEvents()
    except Exception:
        traceback.print_exc()

print("done", flush=True)
