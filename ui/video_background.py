# -*- coding: utf-8 -*-
"""视频背景：用 OpenCV 解码 mp4/mov，循环播放作为窗口背景。

设计：
- VideoBackground 在后台线程解码帧，通过 Qt 信号投递 QImage 到主线程
- 主线程把 QImage 转 QPixmap 用于绘制
- 视频解码失败时回退到静态帧（取第一帧）
- 窗口隐藏/最小化时暂停解码以节省 CPU
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from PySide6.QtCore import QObject, Signal, QTimer, QSize
from PySide6.QtGui import QImage, QPixmap

try:
    import cv2  # type: ignore
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False


def is_video_file(path: str) -> bool:
    """判断路径是否为支持的视频文件（按扩展名）。"""
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext in {".mp4", ".mov", ".avi", ".mkv"}


def is_image_file(path: str) -> bool:
    """判断路径是否为支持的图片文件。"""
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


def load_image_pixmap(path: str) -> QPixmap:
    """加载图片为 QPixmap，兼容 Qt 插件缺失的格式（jpg/jpeg/webp）。

    策略：
    1. 先尝试 Qt 原生加载（QPixmap(path)）
    2. 若结果为 null 且 cv2 可用，用 OpenCV 解码再转 QPixmap
    3. 仍失败则返回空 QPixmap

    这解决了部分 Anaconda 环境下 Qt 的 qjpeg.dll 未加载导致 jpg/jpeg 无法显示的问题。
    """
    pm = QPixmap(path)
    if not pm.isNull():
        return pm
    # Qt 加载失败：尝试 cv2 解码（jpg/jpeg/webp 等常见缺失插件格式）
    if not _HAS_CV2:
        return QPixmap()
    try:
        import numpy as np  # type: ignore
        # cv2.imread 不支持中文路径，用 np.fromfile + imdecode 绕过
        with open(path, "rb") as f:
            buf = np.frombuffer(f.read(), dtype=np.uint8)
        arr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if arr is None:
            return QPixmap()
        qimg = _cv_frame_to_qimage(arr)
        if qimg is None:
            return QPixmap()
        return QPixmap.fromImage(qimg)
    except Exception:
        return QPixmap()


def _cv_frame_to_qimage(frame) -> Optional[QImage]:
    """OpenCV BGR numpy 数组 → QImage（RGB）。"""
    import numpy as np  # type: ignore
    if frame is None:
        return None
    try:
        # BGR → RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        img = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        # 必须深拷贝脱离 numpy 缓冲区
        return img.copy()
    except Exception:
        return None


class VideoBackground(QObject):
    """循环播放视频，逐帧通过 frameReady(QPixmap) 信号投递到主线程。

    使用独立解码线程 + QTimer 节流，避免阻塞 GUI。
    失败时回退到静态首帧（若可读），并把 failed 置位。
    """

    frameReady = Signal(QPixmap)
    failed = Signal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path
        self._cap = None
        self._fps = 25.0
        self._timer: Optional[QTimer] = None
        self._lock = threading.Lock()
        self._running = False
        self._stopped = False
        self._fallback_pixmap: Optional[QPixmap] = None

        if not _HAS_CV2:
            self.failed.emit("未安装 opencv-python，无法播放视频背景")
            return

        if not os.path.isfile(path):
            self.failed.emit(f"视频文件不存在：{path}")
            return

        # 打开视频
        try:
            self._cap = cv2.VideoCapture(path)
            if not self._cap.isOpened():
                self.failed.emit(f"无法打开视频：{path}")
                self._cap = None
                return
            fps = self._cap.get(cv2.CAP_PROP_FPS)
            if fps and fps > 0:
                self._fps = fps
            # 读取首帧作为 fallback（视频解码失败时仍能显示一帧）
            ok, first = self._cap.read()
            if ok and first is not None:
                qimg = _cv_frame_to_qimage(first)
                if qimg is not None:
                    self._fallback_pixmap = QPixmap.fromImage(qimg)
                    self.frameReady.emit(self._fallback_pixmap)
            # 回到开头以便循环
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        except Exception as e:
            self.failed.emit(f"视频初始化失败：{e}")
            self._cap = None
            return

        # 启动定时器（按视频 fps 推进，但限制 1~30 fps 避免过高）
        interval = int(1000 / max(1.0, min(self._fps, 30.0)))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(interval)
        self._running = True

    def _on_tick(self) -> None:
        """定时器回调：读下一帧并发送到主线程。"""
        if self._stopped or self._cap is None:
            return
        try:
            with self._lock:
                ok, frame = self._cap.read()
                if not ok or frame is None:
                    # 到末尾，循环
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = self._cap.read()
                    if not ok or frame is None:
                        return
            qimg = _cv_frame_to_qimage(frame)
            if qimg is not None:
                pm = QPixmap.fromImage(qimg)
                self.frameReady.emit(pm)
        except Exception:
            pass  # 偶发解码错误跳过

    def pause(self) -> None:
        """暂停解码（窗口隐藏时调用）。"""
        if self._timer:
            self._timer.stop()
        self._running = False

    def resume(self) -> None:
        """恢复解码。"""
        if self._timer and not self._running:
            self._timer.start()
            self._running = True

    def stop(self) -> None:
        """彻底停止并释放资源。"""
        self._stopped = True
        if self._timer:
            self._timer.stop()
            self._timer = None
        if self._cap is not None:
            try:
                with self._lock:
                    self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._running = False

    @property
    def fallback_pixmap(self) -> Optional[QPixmap]:
        return self._fallback_pixmap

    @property
    def is_valid(self) -> bool:
        return self._cap is not None or self._fallback_pixmap is not None
