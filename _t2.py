# -*- coding: utf-8 -*-
import sys
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QApplication, QWidget

class T(QWidget):
    def __init__(self, fill, trans):
        super().__init__()
        self._fill = fill
        if trans:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), self._fill)

app = QApplication(sys.argv)

tests = []
# (name, fill, flags, trans)
tests.append(("plain", QColor(0, 200, 0), Qt.WindowType.Widget, False))
tests.append(("frameless_opaque", QColor(0, 200, 0), Qt.WindowType.FramelessWindowHint, False))
tests.append(("tool_translucent", QColor(255, 0, 0, 128), Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint, True))

wins = []
for i, (name, fill, flags, trans) in enumerate(tests):
    w = T(fill, trans)
    w.setWindowFlags(flags)
    w.setGeometry(60 + i * 240, 60, 200, 150)
    w.setObjectName(name)
    w.show()
    wins.append((name, w))

def finish():
    for name, w in wins:
        print(name, "visible=", w.isVisible(), "hwnd=", w.winId())
    app.quit()

QTimer.singleShot(1200, finish)
rc = app.exec()
print("exec rc", rc)
