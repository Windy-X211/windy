# -*- coding: utf-8 -*-
"""隔离测试：空块删除方法（日志文件版，避免管道缓冲/死循环挂起）"""
import os, sys, time

LOG = r"F:\PythonProjects\TraeProjects\规划性桌面代理\_probe_trim.log"

def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.time():.1f}] {msg}\n")

_cand = r"F:\anaconda3\Library\lib\qt6\plugins\platforms"
if os.path.isdir(_cand):
    os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", _cand)

sys.path.insert(0, r"F:\PythonProjects\TraeProjects\规划性桌面代理")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor, QTextDocument

app = QApplication(sys.argv)

def dump(d, tag):
    lines = [f"--- {tag} --- blocks={d.blockCount()} text={d.toPlainText()!r}"]
    b = d.begin()
    while b.isValid():
        lines.append(f"  block[{b.blockNumber()}] text={b.text()!r} pos={b.position()} len={b.length()}")
        b = b.next()
    log("\n".join(lines))

def try_method(name, fn, max_iter=20):
    d = QTextDocument()
    d.setPlainText("第一行\n第二行\n\n\n")
    dump(d, f"start[{name}]")
    try:
        fn(d, max_iter)
        dump(d, f"end[{name}]")
    except Exception as e:
        log(f"method[{name}] EXC: {e!r}")

def m_range(d, max_iter):
    for i in range(max_iter):
        b = d.lastBlock()
        if b.blockNumber() <= 0 or b.text().strip() != "":
            break
        c = QTextCursor(d)
        c.setPosition(b.position())
        c.setPosition(b.position() + b.length(), QTextCursor.MoveMode.KeepAnchor)
        c.removeSelectedText()
        dump(d, f"  range iter{i}")

def m_del_prev(d, max_iter):
    for i in range(max_iter):
        b = d.lastBlock()
        if b.blockNumber() <= 0 or b.text().strip() != "":
            break
        c = QTextCursor(d)
        c.setPosition(b.position())
        c.deletePreviousChar()
        dump(d, f"  del_prev iter{i}")

def m_del_char(d, max_iter):
    for i in range(max_iter):
        b = d.lastBlock()
        if b.blockNumber() <= 0 or b.text().strip() != "":
            break
        c = QTextCursor(d)
        c.setPosition(b.position())
        c.deleteChar()
        dump(d, f"  del_char iter{i}")

def m_cut(d, max_iter):
    for i in range(max_iter):
        b = d.lastBlock()
        if b.blockNumber() <= 0 or b.text().strip() != "":
            break
        c = QTextCursor(d)
        c.setPosition(b.position())
        c.select(QTextCursor.SelectionType.BlockUnderCursor)
        c.cut()
        dump(d, f"  cut iter{i}")

def m_anchor_next(d, max_iter):
    # 选中 空块开头 -> 下一块开头（即含空块+分隔符），删除
    for i in range(max_iter):
        b = d.lastBlock()
        if b.blockNumber() <= 0 or b.text().strip() != "":
            break
        nxt = b.next()
        end = nxt.position() if nxt.isValid() else b.position() + b.length()
        c = QTextCursor(d)
        c.setPosition(b.position())
        c.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        c.removeSelectedText()
        dump(d, f"  anchor_next iter{i}")

try_method("range", m_range)
try_method("del_prev", m_del_prev)
try_method("del_char", m_del_char)
try_method("cut", m_cut)
try_method("anchor_next", m_anchor_next)
log("done")
