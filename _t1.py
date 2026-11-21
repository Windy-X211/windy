# -*- coding: utf-8 -*-
import sys
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QApplication, QWidget

class T(QWidget):
    def __init__(self, fill):
        super().__init__()
        self._fill = fill
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), self._fill)

app = QApplication(sys.argv)
w1 = T(QColor(255, 0, 0, 128))
w1.setWindowFlags(Qt.WindowType.FramelessWindowHint)
w1.setGeometry(60, 60, 200, 150)
w1.show()
QTimer.singleShot(1000, app.quit)
rc = app.exec()
print("frameless translucent exec rc", rc)
