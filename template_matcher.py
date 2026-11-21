# -*- coding: utf-8 -*-
"""OpenCV 模板匹配：在屏幕截图中查找模板图片位置。

提供 match_template_on_screen 进行单次匹配，以及
match_template_fallback 封装（失败时返回原始坐标）。

防误匹配改进：
- 阈值提升至 0.80，CCOEFF_NORMED 下 0.65 过低极易误锁
- 用 CLAHE 替代全局 equalizeHist，保留局部对比度，避免不同区域被拉平后变相似
- 多尺度采用"基准尺度优先 + 次优尺度需显著更优"策略，
  防止错误尺度的偶然高分盖过正确尺度
- 增加 top1-top2 margin 检查，最高分与次高分接近时拒绝（匹配不唯一）
- 模板最小尺寸提升至 20x20，过小模板无区分度
- 匹配后做边缘重合度二次验证，过滤纯亮度/颜色巧合
- 支持 ROI 搜索区域，缩小范围降低误匹配
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import cv2
import numpy as np

import capture

logger = logging.getLogger(__name__)

# 默认匹配阈值（归一化相关度，0~1）。
# TM_CCOEFF_NORMED 下 < 0.75 基本不可靠，0.80 是合理下限。
_DEFAULT_THRESHOLD = 0.80

# 多尺度搜索的范围（缩放比例列表），从小到大排列。
# 1.0 是基准，其余仅作为 DPI 偏差的补偿。
_SCALE_FACTORS = [0.8, 0.9, 1.0, 1.1, 1.2]

# 非基准尺度的结果必须比基准尺度高出这么多才允许胜出，
# 防止错误尺度的偶然噪声分盖过正确尺度。
_SCALE_DISCOUNT = 0.05

# top1 与 top2 的最小差距（margin），低于此值认为匹配不唯一，拒绝。
_MIN_MARGIN = 0.05

# 边缘重合度二次验证阈值（0~1），低于此值认为是亮度/颜色巧合，拒绝。
_EDGE_OVERLAP_THRESHOLD = 0.50

# 模板最小尺寸（像素），过小模板无纹理区分度。
_MIN_TEMPLATE_SIZE = 20

# 匹配方法：归一化相关系数 (TM_CCOEFF_NORMED)。
_MATCH_METHOD = cv2.TM_CCOEFF_NORMED

# CLAHE 限幅，避免过度增强噪声
_CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


def _pick_resize_interp(scale: float) -> int:
    """按缩放方向选择模板插值方式。"""
    return cv2.INTER_AREA if scale <= 1.0 else cv2.INTER_CUBIC


def _preprocess(img: np.ndarray) -> np.ndarray:
    """对灰度图做 CLAHE 限制对比度自适应直方图均衡化。

    全局 equalizeHist 会把整图直方图拉平，让不同区域纹理变相似，增加误匹配；
    CLAHE 只在局部块内均衡，保留全局对比度差异。
    """
    return _CLAHE.apply(img)


def _edge_overlap(img1: np.ndarray, img2: np.ndarray) -> float:
    """计算两张图的边缘重合度（0~1），用于二次验证。

    用 Canny 提取边缘后计算交并比（IoU）。
    纯亮度/颜色巧合的区域边缘结构通常不重合。
    """
    if img1.shape != img2.shape:
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
    e1 = cv2.Canny(img1, 50, 150)
    e2 = cv2.Canny(img2, 50, 150)
    intersection = np.logical_and(e1 > 0, e2 > 0).sum()
    union = np.logical_or(e1 > 0, e2 > 0).sum()
    if union == 0:
        return 0.0
    return float(intersection) / float(union)


def _match_single_scale(
    screen: np.ndarray,
    template: np.ndarray,
    scale: float,
) -> Optional[Tuple[float, Tuple[int, int], float]]:
    """单尺度匹配，返回 (max_val, max_loc, scale) 或 None。"""
    th, tw = template.shape[:2]
    sh, sw = screen.shape[:2]

    nw = max(_MIN_TEMPLATE_SIZE, int(tw * scale))
    nh = max(_MIN_TEMPLATE_SIZE, int(th * scale))
    if nw > sw or nh > sh:
        return None

    scaled_tpl = cv2.resize(
        template, (nw, nh), interpolation=_pick_resize_interp(scale)
    )

    try:
        result = cv2.matchTemplate(screen, scaled_tpl, _MATCH_METHOD)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
    except Exception:
        return None

    return (max_val, max_loc, scale)


def match_template_on_screen(
    template_path: str,
    threshold: float = _DEFAULT_THRESHOLD,
    roi: Optional[Tuple[float, float, float, float]] = None,
) -> Optional[Tuple[float, float, float]]:
    """在当前屏幕截图中匹配模板，返回 (x_norm, y_norm, confidence)。

    参数：
        template_path: 模板图片文件路径。
        threshold:     匹配置信度阈值（0~1），低于此值返回 None。
        roi:           搜索区域 (x_norm, y_norm, w_norm, h_norm)，
                       归一化坐标，缩小搜索范围可降低误匹配。None 表示全屏。

    返回：
        (归一化x, 归一化y, 置信度) 或 None（未找到）。
    """
    if not os.path.isfile(template_path):
        logger.warning("模板文件不存在: %s", template_path)
        return None

    # 截取当前屏幕
    try:
        shot_bytes = capture.capture_screen()
    except Exception as e:
        logger.error("截图失败: %s", e)
        return None

    # 加载模板（灰度）
    # cv2.imread 在 Windows 上不识别非 ASCII 路径（如中文目录），
    # 会静默失败并触发 findDecoder 警告。改用 np.fromfile + cv2.imdecode。
    try:
        with open(template_path, "rb") as _f:
            _buf = np.frombuffer(_f.read(), dtype=np.uint8)
        template = cv2.imdecode(_buf, cv2.IMREAD_GRAYSCALE)
    except OSError as _e:
        logger.warning("模板图片读取失败: %s (%s)", template_path, _e)
        return None
    if template is None:
        logger.warning("模板图片无法加载: %s", template_path)
        return None
    template = _preprocess(template)
    th, tw = template.shape[:2]
    if th < _MIN_TEMPLATE_SIZE or tw < _MIN_TEMPLATE_SIZE:
        logger.warning(
            "模板图片太小 (%dx%d)，最小需 %dx%d，无法可靠匹配",
            tw, th, _MIN_TEMPLATE_SIZE, _MIN_TEMPLATE_SIZE,
        )
        return None

    # 解码屏幕截图（灰度）
    arr = np.frombuffer(shot_bytes, dtype=np.uint8)
    screen = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if screen is None:
        logger.error("屏幕截图解码失败")
        return None
    screen = _preprocess(screen)
    full_sh, full_sw = screen.shape[:2]  # 保存原始全屏尺寸，供最终归一化用
    sh, sw = full_sh, full_sw

    # 裁剪 ROI（如果指定）
    roi_offset_x, roi_offset_y = 0, 0
    if roi is not None:
        rx, ry, rw, rh = roi
        x1 = max(0, int(rx * sw))
        y1 = max(0, int(ry * sh))
        x2 = min(sw, int((rx + rw) * sw))
        y2 = min(sh, int((ry + rh) * sh))
        if x2 - x1 < tw or y2 - y1 < th:
            logger.warning("ROI 区域比模板小，无法匹配")
            return None
        screen = screen[y1:y2, x1:x2]
        roi_offset_x, roi_offset_y = x1, y1
        sh, sw = screen.shape[:2]

    if sh < th or sw < tw:
        logger.warning("屏幕 (%dx%d) 比模板 (%dx%d) 小，不可能匹配", sw, sh, tw, th)
        return None

    # ---- 多尺度匹配 ----
    # 策略：非基准尺度的分数必须比基准尺度高 _SCALE_DISCOUNT 才允许胜出，
    # 防止错误尺度的偶然高分盖过正确尺度。
    best_val = float("-inf")
    best_loc = (0, 0)
    best_scale = 1.0
    matched = False

    # 收集所有尺度的 top1，用于 margin 检查
    all_top_vals = []

    for scale in _SCALE_FACTORS:
        res = _match_single_scale(screen, template, scale)
        if res is None:
            continue
        max_val, max_loc, _ = res
        all_top_vals.append(max_val)
        matched = True

        # 非基准尺度需要折扣后才能比较
        effective_val = max_val if scale == 1.0 else max_val - _SCALE_DISCOUNT
        if effective_val > best_val:
            best_val = max_val
            best_loc = max_loc
            best_scale = scale

    if not matched:
        logger.warning("模板匹配全部失败 (模板: %s)", template_path)
        return None

    # ---- 阈值检查 ----
    if best_val < threshold:
        logger.info(
            "模板匹配置信度 %.3f 低于阈值 %.2f (模板: %s, 缩放: %.2f)",
            best_val, threshold, template_path, best_scale,
        )
        return None

    # ---- margin 检查：top1 与 top2 差距过小则拒绝 ----
    if len(all_top_vals) >= 2:
        sorted_vals = sorted(all_top_vals, reverse=True)
        margin = sorted_vals[0] - sorted_vals[1]
        if margin < _MIN_MARGIN:
            logger.info(
                "模板匹配置信度不唯一: top1=%.3f top2=%.3f margin=%.3f < %.2f，拒绝 (模板: %s)",
                sorted_vals[0], sorted_vals[1], margin, _MIN_MARGIN, template_path,
            )
            return None

    # ---- 二次验证：边缘重合度 ----
    nw_scaled = max(_MIN_TEMPLATE_SIZE, int(tw * best_scale))
    nh_scaled = max(_MIN_TEMPLATE_SIZE, int(th * best_scale))
    x1, y1 = best_loc
    matched_region = screen[y1:y1 + nh_scaled, x1:x1 + nw_scaled]
    scaled_template = cv2.resize(
        template, (nw_scaled, nh_scaled),
        interpolation=_pick_resize_interp(best_scale),
    )
    edge_score = _edge_overlap(matched_region, scaled_template)
    if edge_score < _EDGE_OVERLAP_THRESHOLD:
        logger.info(
            "模板匹配边缘重合度 %.3f 低于阈值 %.2f，疑似亮度/颜色巧合，拒绝 (模板: %s)",
            edge_score, _EDGE_OVERLAP_THRESHOLD, template_path,
        )
        return None

    # 计算匹配区域中心（像素，加回 ROI 偏移）
    cx = roi_offset_x + best_loc[0] + nw_scaled // 2
    cy = roi_offset_y + best_loc[1] + nh_scaled // 2

    # 归一化（基于原始全屏尺寸）
    x_norm = cx / full_sw
    y_norm = cy / full_sh

    logger.info(
        "模板匹配成功: 置信度 %.3f (阈值 %.2f), 缩放 %.2f, 边缘重合 %.3f, 坐标 (%.4f, %.4f)",
        best_val, threshold, best_scale, edge_score, x_norm, y_norm,
    )
    return (x_norm, y_norm, float(best_val))


def match_template_fallback(
    template_path: str,
    fallback_coord: Optional[Tuple[float, float]],
    threshold: float = _DEFAULT_THRESHOLD,
) -> Optional[Tuple[float, float]]:
    """模板匹配，失败时回退到 fallback_coord。

    返回 (x, y) 归一化坐标，或 None（匹配失败且无回退坐标）。
    """
    result = match_template_on_screen(template_path, threshold)
    if result is not None:
        return (result[0], result[1])
    return fallback_coord