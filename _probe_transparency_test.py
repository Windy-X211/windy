# -*- coding: utf-8 -*-
"""临时验证：Windows 上 WA_TranslucentBackground 在有边框/无边框窗口上的透明效果。"""
import sys, os, traceback
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QImage
from PySide6.QtWidgets import QApplication, QWidget

OUT = r"C:\Users\yangq\AppData\Local\Temp\alpha_test"
os.makedirs(OUT, exist_ok=True)

class T(QWidget):
    def __init__(self, fill):
        super().__init__()
        self._fill = fill
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), self._fill)

def main():
    app = QApplication(sys.argv)
    scr = app.primaryScreen()
    # 桌面参照：截取窗口将覆盖的区域（该区域在窗口出现前是桌面）
    base = scr.grabWindow(0, 60, 60, 700, 300).toImage().convertToFormat(QImage.Format.Format_ARGB32)
    base.save(os.path.join(OUT, "base.png"))

    w1 = T(QColor(255, 0, 0, 128))
    w1.setWindowFlags(Qt.WindowType.FramelessWindowHint)
    w1.setGeometry(60, 60, 200, 150)
    w1.show()

    w2 = T(QColor(0, 0, 255, 128))
    w2.setGeometry(320, 60, 200, 150)
    w2.show()

    def finish():
        try:
            img1 = scr.grabWindow(0, 60, 60, 700, 300).toImage().convertToFormat(QImage.Format.Format_ARGB32)
            img1.save(os.path.join(OUT, "both.png"))
        except Exception:
            traceback.print_exc()
        app.quit()

    QTimer.singleShot(1000, finish)
    try:
        rc = app.exec()
        print("exec rc", rc)
    except Exception:
        traceback.print_exc()
        print("exec crashed")

if __name__ == "__main__":
    main()
