# -*- coding: utf-8 -*-
"""视觉模型：单轮定位元素归一化坐标。

输入截图 + 步骤描述，模型只返回归一化坐标 x.xx,x.xx 或 None。
单次对话，不记录上下文，提示词简练。
拖动操作由调用方分两次调用（起点+终点）。
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

import model_client
from step_model import ParamInvalid, parse_coord, validate_coord

LABEL = "vision"

# 简练提示词
LOCATE_PROMPT_TMPL = (
    "在截图中找到「{location}」的「{target}」，返回其中心点的归一化坐标，"
    "格式为 x.xx,y.xx（范围0到1，两位小数）。找不到该元素则返回 None。"
    "只返回坐标或 None，不要输出任何其他内容。"
)

# 解析/校验失败的引导（非重复原 prompt）
GUIDANCE = "格式错误。只输出 x.xx,y.xx（0~1两位小数）或 None，无其他文字。"


def locate(
    target: str,
    location: str,
    screenshot_bytes: bytes,
    max_retry: int = 2,
    cancel_check: Optional[Callable[[], bool]] = None,
    temperature: Optional[float] = 0.1,
) -> Optional[Tuple[float, float]]:
    """定位单个元素，返回 (x, y) 归一化坐标或 None。

    - 解析失败/坐标非法：追加引导重试（≤ max_retry 次）
    - 重试后仍非法视为 None
    - 接口不可用抛 InterfaceUnavailable
    """
    prompt = LOCATE_PROMPT_TMPL.format(location=location or "当前界面", target=target or "目标元素")
    messages = model_client.build_image_messages(prompt, screenshot_bytes)

    raw = model_client.call_model(
        LABEL, messages, stream=False, temperature=temperature,
        cancel_check=cancel_check,
    )

    coord = parse_coord(raw)
    if coord is not None and validate_coord(coord):
        return coord

    # 重试：追加引导
    for _ in range(max_retry):
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": GUIDANCE},
        ]
        raw = model_client.call_model(
            LABEL, messages, stream=False, temperature=temperature,
            cancel_check=cancel_check,
        )
        coord = parse_coord(raw)
        if coord is not None and validate_coord(coord):
            return coord

    # 解析出 None（合法）或仍非法
    parsed = parse_coord(raw)
    if parsed is None:
        # 模型明确返回 None，或无法解析任何数字
        return None
    # 有数字但非法 → 视为找不到
    return None


def locate_pair(
    start_target: str,
    start_location: str,
    end_target: str,
    end_location: str,
    screenshot_bytes: bytes,
    max_retry: int = 2,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
    """拖动用：两次定位起点和终点。任一为 None 则拖动无法执行。"""
    start = locate(start_target, start_location, screenshot_bytes,
                   max_retry=max_retry, cancel_check=cancel_check)
    # 起点找不到直接返回，避免浪费一次视觉调用
    if start is None:
        return None, None
    end = locate(end_target, end_location, screenshot_bytes,
                 max_retry=max_retry, cancel_check=cancel_check)
    return start, end
