import os, sys
sys.path.insert(0, r"F:\PythonProjects\TraeProjects\规划性桌面代理")
try:
    from PySide6.QtCore import QLibraryInfo
    _qt_plugin_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
    if _qt_plugin_path and os.path.isdir(os.path.join(_qt_plugin_path, "platforms")):
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", os.path.join(_qt_plugin_path, "platforms"))
except Exception:
    pass

from PySide6.QtWidgets import QApplication
from ui.chat_view import ChatView

app = QApplication([])
cv = ChatView()
cv.show()

long_as = ("这是一条非常非常长的模型消息用来测试消息气泡的宽度行为是否按照预期，"
           "需要包含足够多的文字才能超过容器可用宽度从而触发贴满容器宽度的自动换行逻辑，"
           "继续补充大量文字确保理想宽度大于可用宽度，从而验证窗口缩放时换行是否正确。"
           "还要再加更多文字，让这段内容在极宽窗口下也能产生多行，避免一行排满后看起来不换行。")

def setup(w):
    cv.resize(w, 600)
    app.processEvents()
    b = cv.add_message(long_as, "assistant")
    app.processEvents()
    return b

def measure(name, b):
    mb = b._content
    doc = mb.document()
    lines = round(doc.size().height() / 15, 1)
    print(f"{name}: 气泡宽={b.width()} viewport={mb.viewport().width()} "
          f"wrap={mb.lineWrapColumnOrWidth()} doc.w={round(doc.size().width(),1)} "
          f"行数≈{lines} hScroll={mb.horizontalScrollBar().maximum()}")

b = setup(600)
measure("窗口600(初始)", b)

for w in (1200, 2000, 400, 800, 1400):
    cv.resize(w, 600)
    app.processEvents()
    b.updateGeometry()
    app.processEvents()
    measure(f"窗口{w}", b)

app.processEvents()
print("DONE")
