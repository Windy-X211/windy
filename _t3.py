# -*- coding: utf-8 -*-
import sys
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication(sys.argv)
w = QWidget()
w.setGeometry(60, 60, 200, 150)
w.show()
print("shown, hwnd=", w.winId(), flush=True)
from PySide6.QtCore import QTimer
QTimer.singleShot(800, app.quit)
rc = app.exec()
print("exec rc", rc, flush=True)
