# -*- coding: utf-8 -*-
"""规划模型：从当前屏幕状态 + 目标，产出原子化执行步骤。

固定 prompt，直接使用案例格式（不泛化）。输出形如：
    当前屏幕状态：桌面
    目标：搜索李白是谁
    [单击-桌面上-浏览器图标>单击-浏览器界面中-搜索栏>输入-李白是谁>按键-enter>单击-搜索结果页中-第一个条目]

解析失败时追加引导消息重新请求（绝不重复相同 prompt），超过重试上限抛 ParseError。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

import app_config
import model_client
from step_model import ParseError, parse_steps

LABEL = "planning"


def get_plan_prompt() -> str:
    """获取规划模式的系统提示词（用户可自定义）。"""
    preset = app_config.get_preset_by_label(LABEL)
    return app_config.get_system_prompt("planning", preset)


# 解析失败时的引导消息（不重复原 prompt）
GUIDANCE_PROMPT = "你的输出格式有误。必须只输出「当前屏幕状态/目标/[步骤]」三行，步骤用 > 连接、整体用 [] 包裹。请重新输出，不要输出多余解释。"


def _strip_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从消息列表中移除图片内容，仅保留文本（重试时节省 token）。

    首次规划已把截图发给模型，重试只需纠正格式，无需重复发送图片。
    将 image_url 项替换为一行文字提示，保留文本部分。
    """
    out: List[Dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            new_parts: List[Dict[str, Any]] = []
            had_image = False
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    had_image = True
                else:
                    new_parts.append(part)
            if had_image:
                new_parts.append({"type": "text", "text": "(截图已在上文展示，无需重复发送)"})
            out.append({**m, "content": new_parts})
        else:
            out.append(m)
    return out


def build_messages(goal: str, screenshot_bytes: Optional[bytes] = None,
                   extra_context: Optional[str] = None) -> List[Dict[str, Any]]:
    """构造规划请求消息。有截图且支持视觉时附带图片。"""
    user_text = f"用户目标：{goal}\n\n请观察当前屏幕截图，按上述格式输出执行步骤。"
    if extra_context:
        user_text = f"补充信息：{extra_context}\n\n{user_text}"

    plan_prompt = get_plan_prompt()
    if screenshot_bytes and model_client.preset_supports_vision(LABEL):
        return model_client.build_image_messages(plan_prompt + "\n\n" + user_text, screenshot_bytes)
    # 无截图或不支持视觉：纯文本
    return [
        {"role": "system", "content": plan_prompt},
        {"role": "user", "content": user_text},
    ]


def plan(
    goal: str,
    screenshot_bytes: Optional[bytes] = None,
    extra_context: Optional[str] = None,
    max_retry: int = 2,
    on_chunk: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    temperature: Optional[float] = None,
) -> Tuple[str, str, List[Dict[str, Any]], str]:
    """运行规划模型。

    返回 (screen_state, goal_text, steps, raw_output)。
    解析失败超过 max_retry 次抛 ParseError。
    接口不可用抛 InterfaceUnavailable。
    """
    messages = build_messages(goal, screenshot_bytes, extra_context)

    # 首次：流式输出供 UI 展示
    raw = model_client.call_model(
        LABEL, messages, stream=True, temperature=temperature,
        on_chunk=on_chunk, cancel_check=cancel_check,
    )

    last_error = None
    for attempt in range(max_retry):
        try:
            screen_state, goal_text, steps = parse_steps(raw)
            return screen_state, goal_text, steps, raw
        except ParseError as e:
            last_error = e
            # 追加引导消息重试（非重复原 prompt）
            # 重试时移除图片：首次已发截图，重试只需纠正格式，避免重复占用 token
            messages = _strip_images(messages) + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": GUIDANCE_PROMPT},
            ]
            raw = model_client.call_model(
                LABEL, messages, stream=False, temperature=temperature,
                cancel_check=cancel_check,
            )

    # 最后一次尝试
    try:
        screen_state, goal_text, steps = parse_steps(raw)
        return screen_state, goal_text, steps, raw
    except ParseError:
        raise ParseError(f"规划模型输出无法解析（已重试 {max_retry} 次）：{last_error}")


def replan(
    goal: str,
    feedback: str,
    screenshot_bytes: Optional[bytes] = None,
    preserved_inputs: Optional[List[Dict[str, Any]]] = None,
    max_retry: int = 2,
    on_chunk: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    temperature: Optional[float] = None,
) -> Tuple[str, str, List[Dict[str, Any]], str]:
    """根据执行失败反馈重新规划或插入意外处理步骤。

    feedback 例如：「在「搜索栏」未找到「浏览器图标」，请插入意外处理步骤或重新规划。」

    preserved_inputs: 已完成步骤中已经输入的内容（短/长输入）列表，每项为
        {op, index, target, var_name, content, description}。重规划时若反馈
        未明确要求修改这些内容，则应尽量保留原变量名和内容不变。
    """
    extra_hint = ""
    if preserved_inputs:
        lines = ["以下步骤中已经输入的内容（变量名→内容），除非反馈明确要求修改，请尽量沿用原变量名和内容，不要重新生成长输入文本："]
        for item in preserved_inputs:
            idx = item.get("index", "?")
            op = item.get("op", "输入")
            tgt = item.get("target", "")
            var = item.get("var_name") or "(无变量名)"
            desc = item.get("description")
            content = item.get("content") or "(空)"
            # 内容超长做摘要，避免 prompt 膨胀
            preview = content
            if isinstance(preview, str) and len(preview) > 240:
                preview = preview[:240] + f"…（共{len(content)}字）"
            line = f"  - 步骤{idx} [{op}] 目标={tgt} 变量名={var}"
            if desc:
                line += f" 长输入说明={desc}"
            line += f" 已输入内容：{preview}"
            lines.append(line)
        extra_hint = "\n" + "\n".join(lines) + "\n"
    plan_prompt = get_plan_prompt()
    replan_prompt = (
        plan_prompt
        + extra_hint
        + f"\n\n用户目标：{goal}\n"
        + f"执行反馈：{feedback}\n"
        + "请根据反馈插入意外处理步骤，或重新规划剩余步骤，仍按上述格式输出。"
    )
    if screenshot_bytes and model_client.preset_supports_vision(LABEL):
        messages = model_client.build_image_messages(replan_prompt, screenshot_bytes)
    else:
        messages = [
            {"role": "system", "content": plan_prompt + extra_hint},
            {"role": "user", "content": f"用户目标：{goal}\n执行反馈：{feedback}\n请重新规划。"},
        ]

    raw = model_client.call_model(
        LABEL, messages, stream=True, temperature=temperature,
        on_chunk=on_chunk, cancel_check=cancel_check,
    )
    last_error = None
    for attempt in range(max_retry):
        try:
            screen_state, goal_text, steps = parse_steps(raw)
            return screen_state, goal_text, steps, raw
        except ParseError as e:
            last_error = e
            # 重试时移除图片：首次已发截图，重试只需纠正格式，避免重复占用 token
            messages = _strip_images(messages) + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": GUIDANCE_PROMPT},
            ]
            raw = model_client.call_model(
                LABEL, messages, stream=False, temperature=temperature,
                cancel_check=cancel_check,
            )
    try:
        screen_state, goal_text, steps = parse_steps(raw)
        return screen_state, goal_text, steps, raw
    except ParseError:
        raise ParseError(f"重规划输出无法解析（已重试 {max_retry} 次）：{last_error}")
