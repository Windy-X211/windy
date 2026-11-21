# -*- coding: utf-8 -*-
import sys, os, time, faulthandler, threading, traceback

LOG = r"F:\PythonProjects\TraeProjects\规划性桌面代理\_probe_min.log"
STK = r"F:\PythonProjects\TraeProjects\规划性桌面代理\_probe_stack.log"

def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.time():.1f}] {msg}\n")

def dump():
    with open(STK, "a", encoding="utf-8") as f:
        for t in threading.enumerate():
            f.write(f"--- thread {t.name} ---\n")
            traceback.print_stack(sys._current_frames().get(t.ident), file=f)

log("start")
t = threading.Timer(20, dump)
t.daemon = True
t.start()
sys.path.insert(0, r"F:\PythonProjects\TraeProjects\规划性桌面代理")
try:
    log("importing PySide6...")
    from PySide6.QtWidgets import QApplication, QLabel
    from PySide6.QtCore import QTimer
    log("imported PySide6")
    log("creating QApplication...")
    app = QApplication(sys.argv)
    log("app created")
    w = QLabel("probe")
    w.show()
    log("label shown")
    QTimer.singleShot(2000, app.quit)
    app.exec()
    log("exec done")
except Exception as e:
    log(f"ERROR: {type(e).__name__}: {e}")
    traceback.print_exc()
log("exit")
os._exit(0)
