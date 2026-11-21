# -*- coding: utf-8 -*-
import sys, os
def log(m):
    with open(os.path.join(os.environ.get("TEMP", "/tmp"), "alpha_test", "step.log"), "a", encoding="utf-8") as f:
        f.write(m + "\n")
    print(m, flush=True)

log("step0 import ok; platform=" + os.environ.get("QT_QPA_PLATFORM", ""))

from PySide6.QtWidgets import QApplication, QWidget
log("step1 imports ok")
app = QApplication(sys.argv)
log("step2 QApplication ok")
w = QWidget()
log("step3 QWidget ok")
w.setGeometry(60, 60, 200, 150)
log("step4 geometry ok")
w.show()
log("step5 show ok")
log("step6 winId=" + str(w.winId()))
from PySide6.QtCore import QTimer
QTimer.singleShot(500, app.quit)
rc = app.exec()
log("step7 exec rc=" + str(rc))
