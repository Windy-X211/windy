# -*- coding: utf-8 -*-
r"""程序入口。

职责：
1. SetProcessDPIAware 高 DPI 修正
2. 创建 QApplication、MainWindow、AgentWorker、QThread、FloatingBall
3. worker.moveToThread(thread)；MainWindow.connect_worker 连接所有信号槽
4. 注册主窗口与悬浮球的 hide-on-capture 钩子
5. 悬浮球单击切换主窗口显示/隐藏
6. 启动事件循环

注意：搜狗输入法沙箱冲突可能导致崩溃，建议从终端直接运行：
    & 'F:\anaconda3\python.exe' f:\PythonProjects\TraeProjects\规划性桌面代理\main.py
或在 Trae 设置中将 C:\\Users\\yangq\\AppData\\LocalLow\\SogouPY 加入可写目录。
"""

from __future__ import annotations

import os
import sys

# DPI 修正：PySide6/Qt6 默认已启用 PER_MONITOR_AWARE_V2，并自行调用
# SetProcessDpiAwarenessContext。这里若再手动 SetProcessDPIAware 会让 Qt 的
# 后续调用报“拒绝访问”警告，故不再手动设置（坐标由 capture.get_real_screen_size
# 经 GetSystemMetrics 获取，已与 Qt 的 awareness 一致）。

# 显式定位 Qt 平台插件目录（Anaconda 环境下 Qt 有时找不到 qwindows.dll）
# 必须在 QApplication 构造之前设置，否则报 "Could not find the Qt platform plugin windows"
try:
    from PySide6.QtCore import QLibraryInfo
    _qt_plugin_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
    if _qt_plugin_path and os.path.isdir(os.path.join(_qt_plugin_path, "platforms")):
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH",
                              os.path.join(_qt_plugin_path, "platforms"))
        if hasattr(QLibraryInfo, "addLibraryPath"):
            QLibraryInfo.addLibraryPath(_qt_plugin_path)
except Exception:
    # 回退：按常见安装位置补丁（Anaconda）
    import PySide6 as _p6
    _cand = os.path.join(os.path.dirname(_p6.__file__), "plugins", "platforms")
    if not os.path.isdir(_cand):
        _cand = os.path.normpath(os.path.join(
            os.path.dirname(sys.executable), "..", "Library", "lib", "qt6", "plugins", "platforms"
        ))
    if os.path.isdir(_cand):
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", _cand)

# 确保程序目录在 sys.path，便于包内导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import app_config
import capture
import executor  # noqa: F401  触发 pyautogui 初始化
from agent_worker import AgentWorker
from ui.main_window import MainWindow
from ui.floating_ball import FloatingBall


def _install_message_handler() -> None:
    """安装 Qt 消息处理器，过滤已知的无害警告。

    - "SetProcessDpiAwarenessContext() failed" — Qt6 自调 DPI awareness 时
      因已被系统/其它调用设置而失败，不影响功能。
    - "Unknown property" — 某些样式表属性 Qt CSS 不支持（已在代码中规避，
      此处兜底防止第三方主题/QSS 引入的噪声）。
    """
    from PySide6.QtCore import qInstallMessageHandler, QtMsgType, QMessageLogContext

    _suppress_markers = (
        "SetProcessDpiAwarenessContext",
        "Unknown property",
    )

    def _handler(msg_type, ctx: QMessageLogContext, message: str):
        if msg_type == QtMsgType.QtWarningMsg:
            if any(m in message for m in _suppress_markers):
                return  # 丢弃
        # 默认行为：输出到 stderr
        import sys
        if msg_type in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            sys.stderr.write(f"Qt CRITICAL: {message}\n")
        elif msg_type == QtMsgType.QtWarningMsg:
            sys.stderr.write(f"Qt warning: {message}\n")
        else:
            sys.stderr.write(f"Qt: {message}\n")

    qInstallMessageHandler(_handler)


def _minimize_console() -> None:
    """最小化当前控制台窗口（启动后自动隐藏黑框）。"""
    try:
        import ctypes
        SW_MINIMIZE = 6
        ctypes.windll.user32.ShowWindow(
            ctypes.windll.kernel32.GetConsoleWindow(), SW_MINIMIZE
        )
    except Exception:
        pass


def main() -> int:
    app_config.ensure_appdata()
    _minimize_console()

    # 必须在 QApplication 构造之前安装消息处理器：
    # DPI awareness 调用发生在 QApplication() 构造函数内部，
    # 若处理器在之后安装，DPI 警告会先泄漏出去。
    _install_message_handler()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 关闭主窗口仍由悬浮球保留
    # 全局 tooltip 样式：白底黑字。setPalette 在 Windows 上常被系统主题覆盖，
    # 改用 stylesheet 确保可靠生效（白底黑字、细边框、不遮挡其他 UI 交互）。
    app.setStyleSheet(
        "QToolTip {"
        "background-color: #ffffff;"
        "color: #000000;"
        "border: 1px solid #b0b0b0;"
        "border-radius: 4px;"
        "padding: 4px 8px;"
        "}"
    )

    # 主窗口
    window = MainWindow()
    # 居中显示
    screen = app.primaryScreen().availableGeometry()
    window.move(
        (screen.width() - window.width()) // 2,
        (screen.height() - window.height()) // 2,
    )

    # Worker 线程
    from PySide6.QtCore import QThread
    worker = AgentWorker()
    thread = QThread()
    worker.moveToThread(thread)
    window.connect_worker(worker)
    thread.start()

    # 悬浮球
    ball = FloatingBall()
    ball.move(screen.width() - 80, 120)
    ball_visible = {"main": True}

    def toggle_main():
        if window.isVisible() and not window.isMinimized():
            window.hide()
            ball_visible["main"] = False
        else:
            # 使用 showNormal 确保最小化窗口能正确恢复
            window.showNormal()
            window.raise_()
            window.activateWindow()
            ball_visible["main"] = True

    ball.toggled.connect(toggle_main)

    # 右键菜单「置顶」
    def _set_stay_on_top(on_top: bool):
        flags = window.windowFlags()
        if on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        # 切换置顶标志需要 hide/show 才能生效
        was_visible = window.isVisible() and not window.isMinimized()
        window.hide()
        window.setWindowFlags(flags)
        if was_visible:
            window.showNormal()
            window.raise_()
            window.activateWindow()
        else:
            window.show()
            window.hide()

    ball.stay_on_top_toggled.connect(_set_stay_on_top)

    # 右键菜单「控制中心」
    ball.controlCenterRequested.connect(window._open_control_center)

    # 右键菜单「退出程序」
    ball.exit_requested.connect(app.quit)

    # 悬浮球颜色同步：母工作流启用/出错状态
    window.ballColorChanged.connect(ball.set_color)
    window.ballErrorLabelChanged.connect(ball.set_error_label)

    # 悬浮球打开出错母工作流
    ball.openErroredMasterWorkflow.connect(window._open_errored_master_workflow)

    # 右键菜单「任务清单」开关
    def _toggle_task_list(shown: bool):
        window._toggle_task_list(shown)
        ball.set_task_list_shown(shown)

    ball.taskListToggleRequested.connect(_toggle_task_list)

    # 注册 hide-on-capture 钩子（必须在窗口创建/显示后取句柄）
    window.show()
    ball.show()

    _register_capture_hooks(window, ball)

    try:
        code = app.exec()
    except KeyboardInterrupt:
        code = 0
    finally:
        # 确保线程被清理（避免 "QThread: Destroyed while still running"）
        try:
            worker.stop()
        except Exception:
            pass
        thread.quit()
        thread.wait(2000)
    return code


def _register_capture_hooks(window: MainWindow, ball: FloatingBall) -> None:
    try:
        whwnd = int(window.winId())
        bhwnd = int(ball.winId())
        w_hide, w_show = capture.make_hide_show_hooks(whwnd)
        b_hide, b_show = capture.make_hide_show_hooks(bhwnd)
        capture.register_capture_window(w_hide, w_show)
        capture.register_capture_window(b_hide, b_show)
    except Exception as e:
        sys.stderr.write(f"注册截图隐藏钩子失败：{e}\n")


if __name__ == "__main__":
    sys.exit(main())
