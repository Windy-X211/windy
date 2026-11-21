# -*- coding: utf-8 -*-
"""Agent Mode：自主代理模式执行器。

使用多模态模型直接观察屏幕截图，自主决定下一步操作（点击、输入、按键等），
持续循环直到任务完成。支持聚焦（focus）——截取局部区域放大以提高视觉精度。

核心流程：
1. 接收用户目标
2. 循环：
   a. 截图（或聚焦局部截图）
   b. 发送截图+历史到多模态模型
   c. 模型返回 JSON 格式的动作指令
   d. 解析并执行动作
   e. 记录结果到历史
3. 模型输出 {"action": "完成", ...} 时结束

模型输出格式：
{"action": "工具名", "params": {...}, "reasoning": "思考过程"}
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import time
import copy
import threading
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

import capture
import executor
import model_client
import app_config
import experience

LABEL = "agent_mode"

# 控制台打印互斥锁：用同一把锁把所有模型的“输出块”串成原子段，防止多个线程
# （自主代理模型 / 聊天模型 / 规划模型等）的 print 互相插入，造成控制台输出交错。
# 用户 `_call_model` 与流式 on_chunk 在同一线程、同一临界区内执行（RLock 可重入）。
CONSOLE_LOCK = threading.RLock()

# 系统提示词统一存放在 app_config.DEFAULT_SYSTEM_PROMPTS['agent_mode']，此处不再重复定义。
# 需要修改默认提示词时，直接编辑 app_config.py 中的 DEFAULT_SYSTEM_PROMPTS 字典即可。

# 工具定义列表
TOOLS = [{'type': 'function', 'function': {'name': 'click', 'description': '在屏幕归一化坐标(x,y)处单击。x从左到右0~1，y从上到下0~1。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程：为什么要点击这里'}, 'x': {'type': 'number', 'description': '归一化横坐标 0~1'}, 'y': {'type': 'number', 'description': '归一化纵坐标 0~1'}}, 'required': ['x', 'y']}}}, {'type': 'function', 'function': {'name': 'double_click', 'description': '在屏幕归一化坐标(x,y)处双击。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'x': {'type': 'number', 'description': '归一化横坐标 0~1'}, 'y': {'type': 'number', 'description': '归一化纵坐标 0~1'}}, 'required': ['x', 'y']}}}, {'type': 'function', 'function': {'name': 'right_click', 'description': '在屏幕归一化坐标(x,y)处右键。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'x': {'type': 'number', 'description': '归一化横坐标 0~1'}, 'y': {'type': 'number', 'description': '归一化纵坐标 0~1'}}, 'required': ['x', 'y']}}}, {'type': 'function', 'function': {'name': 'move_mouse', 'description': '移动鼠标到归一化坐标位置，不点击。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'x': {'type': 'number', 'description': '归一化横坐标 0~1'}, 'y': {'type': 'number', 'description': '归一化纵坐标 0~1'}}, 'required': ['x', 'y']}}}, {'type': 'function', 'function': {'name': 'scroll', 'description': '滚动鼠标滚轮。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'direction': {'type': 'string', 'enum': ['上', '下'], 'description': '滚动方向'}, 'times': {'type': 'integer', 'description': '滚动次数，默认 1'}, 'x': {'type': 'number', 'description': '可选：滚动目标位置的归一化 x'}, 'y': {'type': 'number', 'description': '可选：滚动目标位置的归一化 y'}}, 'required': ['direction']}}}, {'type': 'function', 'function': {'name': 'type', 'description': '在当前焦点位置输入文本。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'text': {'type': 'string', 'description': '要输入的文本'}}, 'required': ['text']}}}, {'type': 'function', 'function': {'name': 'key', 'description': '按下键盘按键。支持：enter, tab, esc, backspace, delete, ctrl+c, ctrl+v, ctrl+a, alt+f4, win+d 等。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'keys': {'type': 'string', 'description': '按键名，如 enter, ctrl+c'}}, 'required': ['keys']}}}, {'type': 'function', 'function': {'name': 'run_cmd', 'description': '在 Windows cmd 中执行命令，返回命令输出结果。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'command': {'type': 'string', 'description': '要执行的命令'}}, 'required': ['command']}}}, {'type': 'function', 'function': {'name': 'run_python', 'description': '执行 Python 代码，返回执行结果。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'code': {'type': 'string', 'description': '要执行的 Python 代码'}}, 'required': ['code']}}}, {'type': 'function', 'function': {'name': 'focus', 'description': '以归一化坐标(x,y)为中心截取局部区域并放大查看细节。宽高也是归一化值。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'x': {'type': 'number', 'description': '中心归一化横坐标 0~1'}, 'y': {'type': 'number', 'description': '中心归一化纵坐标 0~1'}, 'width': {'type': 'number', 'description': '区域归一化宽度，默认 0.2'}, 'height': {'type': 'number', 'description': '区域归一化高度，默认 0.2'}}, 'required': ['x', 'y']}}}, {'type': 'function', 'function': {'name': 'wait', 'description': '等待指定秒数，让界面有时间响应。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'seconds': {'type': 'number', 'description': '等待秒数，默认 1.0'}}}}}, {'type': 'function', 'function': {'name': 'wait_change', 'description': '智能等待：以操作前的截图为参考，自动检测屏幕是否发生变化并趋于稳定。屏幕稳定后返回。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'max_seconds': {'type': 'integer', 'description': '最长等待秒数，默认 10'}}}}}, {'type': 'function', 'function': {'name': 'search_experience', 'description': '检索经验库：当反复尝试仍无法解决问题时，给出多个关键字检索相关经验。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'keywords': {'type': 'array', 'items': {'type': 'string'}, 'description': '搜索关键字列表，尽量给出多个相关关键字'}}, 'required': ['keywords']}}}, {'type': 'function', 'function': {'name': 'summarize_experience', 'description': '任务完成后沉淀高效做法到经验库。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'category': {'type': 'string', 'enum': ['common', 'special'], 'description': 'common=通用自动加入系统提示词; special=专用可被搜索'}, 'content': {'type': 'string', 'description': '经验正文'}}, 'required': ['category', 'content']}}}, {'type': 'function', 'function': {'name': 'set_region', 'description': '启用操作区/结果区方案：一次性识别所有按钮坐标并选定结果区。适用于按钮位置固定、需连续多次点击的场景。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'buttons': {'type': 'object', 'description': '按钮名到归一化坐标的映射', 'additionalProperties': {'type': 'object', 'properties': {'x': {'type': 'number'}, 'y': {'type': 'number'}}}}, 'result': {'type': 'object', 'description': '结果区的归一化范围', 'properties': {'x': {'type': 'number'}, 'y': {'type': 'number'}, 'width': {'type': 'number'}, 'height': {'type': 'number'}}, 'required': ['x', 'y']}}, 'required': ['buttons', 'result']}}}, {'type': 'function', 'function': {'name': 'op_click', 'description': '点击操作区已识别的按钮（复用识别时的坐标，无需再定位）。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'button': {'type': 'string', 'description': '按钮名称'}}, 'required': ['button']}}}, {'type': 'function', 'function': {'name': 'op_double_click', 'description': '双击操作区已识别的按钮。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'button': {'type': 'string', 'description': '按钮名称'}}, 'required': ['button']}}}, {'type': 'function', 'function': {'name': 'op_right_click', 'description': '右键操作区已识别的按钮。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'button': {'type': 'string', 'description': '按钮名称'}}, 'required': ['button']}}}, {'type': 'function', 'function': {'name': 'full_view', 'description': '重截全屏核对：当结果区显示内容与预期不符时，请求重截全屏以检查操作区是否被遮挡或变化。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}}}}}, {'type': 'function', 'function': {'name': 'update_button', 'description': '纠正操作区方案中某个按钮的坐标（仅纠正单个，无需全部重识别）。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'button': {'type': 'string', 'description': '要纠正的按钮名称'}, 'x': {'type': 'number', 'description': '新的归一化横坐标 0~1'}, 'y': {'type': 'number', 'description': '新的归一化纵坐标 0~1'}}, 'required': ['button', 'x', 'y']}}}, {'type': 'function', 'function': {'name': 'stop_region', 'description': '退出操作区/结果区方案，恢复全屏截图模式。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}}}}}, {'type': 'function', 'function': {'name': 'complete', 'description': '任务完成：所有操作已完成，可以结束任务。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '思考过程'}, 'result': {'type': 'string', 'description': '任务完成结果描述'}}, 'required': ['result']}}}, {'type': 'function', 'function': {'name': 'fail', 'description': '任务失败：遇到无法解决的问题，如实报告失败原因。', 'parameters': {'type': 'object', 'properties': {'reasoning': {'type': 'string', 'description': '失败原因说明'}}, 'required': ['reasoning']}}}]


def _extract_actions(text: str) -> Optional[List[Dict[str, Any]]]:
    """从模型输出中提取动作列表。

    支持：
    - 单个 JSON 对象：{"action": ...}
    - JSON 数组：["action": ...}
    - 被 Markdown 代码块或文字包围的 JSON。
    统一返回规范化后的动作 dict 列表；解析失败返回 None。
    """
    text = text.strip()
    # 去掉 markdown 代码块
    if text.startswith("```"):
        lines = text.splitlines()
        start = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("```"):
                start = i + 1
                break
        end = len(lines)
        for i in range(len(lines) - 1, start - 1, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        text = "\n".join(lines[start:end]).strip()

    def _norm(obj: Any) -> Optional[List[Dict[str, Any]]]:
        if isinstance(obj, dict):
            if "action" in obj:
                return [obj]
            if isinstance(obj.get("actions"), list):
                acts = [a for a in obj["actions"]
                        if isinstance(a, dict) and "action" in a]
                return acts or None
        elif isinstance(obj, list):
            acts = [a for a in obj if isinstance(a, dict) and "action" in a]
            if acts:
                return acts
        return None

    # 尝试直接解析
    try:
        obj = json.loads(text)
        got = _norm(obj)
        if got:
            return got
    except json.JSONDecodeError:
        pass

    # 尝试查找 JSON 块（可能被文字包围）：优先数组，取最长有效块
    best = None
    for ch, close in (("[", "]"), ("{", "}")):
        idx = 0
        while True:
            idx = text.find(ch, idx)
            if idx < 0:
                break
            depth = 0
            j = idx
            found = -1
            while j < len(text):
                if text[j] == ch:
                    depth += 1
                elif text[j] == close:
                    depth -= 1
                    if depth == 0:
                        found = j
                        break
                j += 1
            if found > idx:
                candidate = text[idx:found + 1]
                try:
                    got = _norm(json.loads(candidate))
                    if got and (best is None or len(candidate) > len(best[0])):
                        best = (candidate, got)
                except json.JSONDecodeError:
                    pass
            idx = j + 1
    if best:
        return best[1]

    # 兜底：模型复述“动作：x / 参数：{...} / 思考：...” 的标签文本时，从中恢复动作
    got = _extract_labeled_action(text)
    if got:
        return [got]

    return None


def _extract_labeled_action(text: str) -> Optional[Dict[str, Any]]:
    """从带标签的文本（动作：x / 参数：{json} / 思考：...）中恢复单个动作。

    用于模型没有输出纯 JSON、而是模仿了历史反馈格式时的纠正。
    """
    m = re.search(r"动作\s*[：:]\s*([A-Za-z0-9_]{1,40})", text)
    if not m:
        return None
    action = m.group(1).strip()
    params: Dict[str, Any] = {}
    p = re.search(r"参数\s*[：:]\s*(\{.*?\})", text, re.S)
    if p:
        try:
            obj = json.loads(p.group(1))
            if isinstance(obj, dict):
                params = obj
        except json.JSONDecodeError:
            pass
    reasoning = ""
    r = re.search(r"思考\s*[：:]\s*([^\n]*)", text)
    if r:
        reasoning = r.group(1).strip()
    return {"action": action, "params": params, "reasoning": reasoning}


_FAIL_KEYWORDS = ("错误", "失败", "无法", "未知", "未启用", "缺少", "不存在",
                  "找不到", "无效", "未识别", "未提供", "未看到", "已取消", "取消")


def _feedback_line(action: str, result: str) -> str:
    """构造精简的执行反馈：只告诉模型「成功 / 执行失败 + 原因」。

    成功结尾附加“，继续下一步”；失败保留原因。不再带参数与思考。
    """
    if any(k in (result or "") for k in _FAIL_KEYWORDS):
        return f"本次动作“{action}”执行失败：{result}"
    return f"本次动作“{action}”已成功执行：{result}，继续下一步"


def _dump_messages(messages: List[Dict[str, Any]]) -> None:
    """把一次模型调用发送的 messages 打印到控制台（供问题排查）。

    system 角色的系统提示词不打印；图片部分只显示占位与大小，避免 base64 刷屏；超长文本截断。
    """
    for idx, m in enumerate(messages):
        role = m.get("role", "?")
        if role == "system":
            continue
        content = m.get("content")
        if isinstance(content, str):
            text = content if len(content) <= 2000 else content[:2000] + f"...（截断，共{len(content)}字符）"
            print(f"  [{idx}] {role}: {text}")
        elif isinstance(content, list):
            print(f"  [{idx}] {role}: [多部分]")
            for part in content:
                ptype = part.get("type") if isinstance(part, dict) else type(part).__name__
                if ptype == "text":
                    t = part.get("text", "")
                    t = t if len(t) <= 2000 else t[:2000] + f"...（截断，共{len(t)}字符）"
                    print(f"        text: {t}")
                else:
                    print(f"        {ptype}: [内容 len={len(str(part))})]")
        else:
            print(f"  [{idx}] {role}: {type(content).__name__}")


def _dedup_actions(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去重：把响应内连续完全相同的动作折叠为一个，抑制模型“重复输出”。"""
    if not actions:
        return actions
    out: List[Dict[str, Any]] = []
    last = None
    for a in actions:
        sig = (a.get("action", ""), json.dumps(a.get("params", {}) or {}, sort_keys=True, ensure_ascii=False))
        if sig != last:
            out.append(a)
        last = sig
    return out


def _image_change_ratio(a_bytes: bytes, b_bytes: bytes) -> float:
    """计算两张截图的内容差异比例（0~1）。缩到 64x48 灰度后比较平均绝对差。"""
    a = Image.open(io.BytesIO(a_bytes)).convert("L").resize((64, 48))
    b = Image.open(io.BytesIO(b_bytes)).convert("L").resize((64, 48))
    pa = a.tobytes()
    pb = b.tobytes()
    total = 0
    for x, y in zip(pa, pb):
        if x != y:
            total += abs(x - y)
    return total / (len(pa) * 255.0)


# 智能等待的判定阈值
_CHANGE_THRESHOLD = 0.08   # 小于参考图差异超过该值视为“屏幕发生变化”
_STABLE_THRESHOLD = 0.02   # 相邻两帧差异小于该值视为“稳定”

# 会改变屏幕内容的操作：执行前捕获“操作前截图”作为 wait_change 的参考
_SCREEN_MUTATING_ACTIONS = (
    "click", "double_click", "right_click",
    "key", "key_combination", "type", "paste",
    "drag", "scroll", "scroll_up", "scroll_down",
    "op_click", "op_double_click", "op_right_click",
)

# 连续相同操作达到该次数后强制升级点击强度（单击→双击→右键）
_SAME_SCHEME_CAP = 3
_CLICK_ESCALATION = {
    "click": "double_click",
    "double_click": "right_click",
    "op_click": "op_double_click",
    "op_double_click": "op_right_click",
}

# 历史上下文压缩：超过则压缩，仅保留原始目标 + 最近 N 条
_HISTORY_KEEP = 26
_HISTORY_TAIL = 12


def _get_system_prompt() -> str:
    """获取系统提示词。支持用户自定义，并自动附加常用经验段落。"""
    preset = app_config.get_preset_by_label(LABEL)
    custom = app_config.get_system_prompt("agent_mode", preset)
    base = custom.strip() if custom.strip() else app_config.DEFAULT_SYSTEM_PROMPTS.get(LABEL, "")
    common = experience.common_prompt_section()
    if common:
        base = base.rstrip() + "\n\n" + common
    return base


def _capture_full() -> bytes:
    """截取全屏，返回 PNG 字节（自主代理模式：绘制红色十字鼠标标记）。"""
    return capture.capture_screen(draw_cursor=True)


def _capture_focus(x: float, y: float, width: float, height: float) -> bytes:
    """截取并放大局部区域。
    
    Args:
        x, y: 中心点的归一化坐标
        width, height: 区域的归一化宽高
    
    Returns:
        放大后的局部截图 PNG 字节
    """
    screen_w, screen_h = capture.get_real_screen_size()
    # 归一化 → 像素
    cx = int(x * screen_w)
    cy = int(y * screen_h)
    rw = int(width * screen_w)
    rh = int(height * screen_h)
    
    # 确保最小尺寸
    rw = max(rw, 50)
    rh = max(rh, 50)
    
    # 计算裁剪区域
    left = max(0, cx - rw // 2)
    top = max(0, cy - rh // 2)
    right = min(screen_w, left + rw)
    bottom = min(screen_h, top + rh)
    
    # 截取全屏
    full_bytes = _capture_full()
    full_img = Image.open(io.BytesIO(full_bytes))
    
    # 裁剪并放大
    crop = full_img.crop((left, top, right, bottom))
    # 放大到原始截图的50%尺寸，便于观察细节
    target_size = (screen_w // 2, screen_h // 2)
    crop = crop.resize(target_size, Image.LANCZOS)
    
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def _capture_region(region: Dict[str, Any]) -> bytes:
    """截取并放大结果区截图。

    用于操作区/结果区方案：每次操作结果后只截取结果区核对检验，不重截全屏。

    Args:
        region: 归一化中心坐标与宽高 {"x", "y", "width", "height"}

    Returns:
        放大后的结果区截图 PNG 字节
    """
    screen_w, screen_h = capture.get_real_screen_size()
    x = region.get("x", 0.5)
    y = region.get("y", 0.5)
    width = region.get("width", 0.4)
    height = region.get("height", 0.4)
    cx = int(x * screen_w)
    cy = int(y * screen_h)
    rw = max(int(width * screen_w), 50)
    rh = max(int(height * screen_h), 50)

    left = max(0, cx - rw // 2)
    top = max(0, cy - rh // 2)
    right = min(screen_w, left + rw)
    bottom = min(screen_h, top + rh)

    full_bytes = _capture_full()
    full_img = Image.open(io.BytesIO(full_bytes))
    crop = full_img.crop((left, top, right, bottom))
    # 放大到原始截图的一半尺寸，便于观察结果内容
    target_size = (screen_w // 2, screen_h // 2)
    crop = crop.resize(target_size, Image.LANCZOS)

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def _focus_to_full(x: float, y: float, focus_params: Dict[str, Any]) -> Tuple[float, float]:
    """把聚焦放大图上的归一化坐标转换为全屏归一化坐标。

    聚焦模式下发给模型的截图是从全屏裁剪出的局部区域并放大显示的，
    因此模型返回的归一化坐标（x, y）是相对该放大图的；此处根据聚焦参数
    （中心点 + 宽高）精确换算回全屏坐标系，几何关系与 _capture_focus 完全一致。
    """
    screen_w, screen_h = capture.get_real_screen_size()
    rw = max(int(float(focus_params.get("width", 0.2)) * screen_w), 50)
    rh = max(int(float(focus_params.get("height", 0.2)) * screen_h), 50)
    left = max(0, int(float(focus_params.get("x", 0.5)) * screen_w) - rw // 2)
    top = max(0, int(float(focus_params.get("y", 0.5)) * screen_h) - rh // 2)
    right = min(screen_w, left + rw)
    bottom = min(screen_h, top + rh)
    crop_w = max(1, right - left)
    crop_h = max(1, bottom - top)
    fx = min(1.0, max(0.0, (left + x * crop_w) / screen_w))
    fy = min(1.0, max(0.0, (top + y * crop_h) / screen_h))
    return fx, fy


# 需要把聚焦放大图坐标换算为全屏坐标的动作（带 x/y 归一化坐标）
_COORD_ACTIONS = frozenset({"click", "double_click", "right_click", "move_mouse", "scroll"})


def _run_command(command: str, cancel_check: Optional[Callable[[], bool]] = None,
                 timeout: float = 30.0) -> str:
    """执行 shell 命令并返回执行反馈；支持运行中取消/超时，不会卡住控制台。

    相比 subprocess.run 的同步阻塞（最长可能要阻塞 timeout 秒、期间无法响应
    停止/暂停）：改用后台读线程逐行收集输出，主线程每 0.1s 检查一次取消与超时，
    因此命令报错或长时间运行时用户点击停止/暂停能立即终止子进程，控制台不再卡死。
    命令的标准输出与标准错误统一合并采集。
    """
    if not command:
        return "错误：缺少命令参数"
    import collections
    proc = subprocess.Popen(
        ["cmd", "/c", command],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    lines = collections.deque(maxlen=300)
    _done = threading.Event()

    def _reader() -> None:
        try:
            for line in proc.stdout:
                lines.append(line)
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            _done.set()

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.monotonic() + max(0.0, float(timeout))
    while not _done.wait(0.1):
        if cancel_check and cancel_check():
            proc.terminate()
            return "命令执行已取消（用户停止/暂停）"
        if time.monotonic() > deadline:
            proc.kill()
            return f"命令执行超时（{int(timeout)}秒）"
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait()
        except Exception:
            pass
    output = "".join(lines).strip()
    output = (output if len(output) <= 500 else output[:500] + f"...（截断，共{len(output)}字符）") or "(无输出)"
    return f"命令执行完成（返回码 {proc.returncode}）：\n{output}"


def _execute_action(action_obj: Dict[str, Any], screen_size: Tuple[int, int],
                    move_duration: float = 0.0,
                    cancel_check: Optional[Callable[[], bool]] = None) -> str:
    """执行动作，返回执行结果描述。"""
    action = action_obj.get("action", "")
    params = action_obj.get("params", {}) or {}
    
    # 中英文动作名映射
    _ACTION_ALIAS = {
        "点击": "click", "单击": "click",
        "双击": "double_click", "doubleclick": "double_click",
        "右键": "right_click", "rightclick": "right_click",
        "移动鼠标": "move_mouse", "move": "move_mouse",
        "滚动": "scroll",
        "输入": "type",
        "按键": "key", "按键组合": "key", "快捷键": "key",
        "执行命令": "run_cmd", "cmd": "run_cmd", "命令": "run_cmd",
        "执行python": "run_python", "python": "run_python",
        "聚焦": "focus",
        "等待": "wait", "sleep": "wait",
        "完成": "完成", "完成!": "完成", "complete": "完成", "done": "完成",
        "失败": "失败", "fail": "失败", "failed": "失败",
        "截图": "screenshot", "观察": "screenshot",
    }
    action = _ACTION_ALIAS.get(action, action)
    
    # 截图/观察：无需执行，截图已自动提供
    if action in ("screenshot", "截图", "observe", "观察"):
        return "截图已自动提供，请直接观察并决定下一步操作"
    
    if action == "click":
        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            return "错误：缺少坐标参数"
        executor.click_coord((float(x), float(y)), screen_size, move_duration=move_duration)
        return f"已点击 ({x:.2f}, {y:.2f})"
    
    elif action == "double_click":
        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            return "错误：缺少坐标参数"
        px, py = executor.norm_to_pixel((float(x), float(y)), screen_size)
        import pyautogui
        pyautogui.doubleClick(px, py, duration=move_duration)
        return f"已双击 ({x:.2f}, {y:.2f})"
    
    elif action == "right_click":
        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            return "错误：缺少坐标参数"
        px, py = executor.norm_to_pixel((float(x), float(y)), screen_size)
        import pyautogui
        pyautogui.rightClick(px, py, duration=move_duration)
        return f"已右键 ({x:.2f}, {y:.2f})"
    
    elif action == "move_mouse":
        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            return "错误：缺少坐标参数"
        import pyautogui
        px, py = executor.norm_to_pixel((float(x), float(y)), screen_size)
        pyautogui.moveTo(px, py, duration=move_duration)
        return f"鼠标已移动到 ({x:.2f}, {y:.2f})"
    
    elif action == "scroll":
        direction = params.get("direction", "下")
        times = int(params.get("times", 1))
        import pyautogui
        amount = int(times * 5)  # 每次滚动5个单位
        scroll_amount = amount if direction == "上" else -amount
        if "x" in params and "y" in params:
            x = params.get("x")
            y = params.get("y")
            px, py = executor.norm_to_pixel((float(x), float(y)), screen_size)
            pyautogui.moveTo(px, py, duration=move_duration)
            pyautogui.scroll(scroll_amount)
        else:
            pyautogui.scroll(scroll_amount)
        return f"已向{direction}滚动 {times} 次"
    
    elif action == "type":
        text = params.get("text", "")
        if not text:
            return "错误：缺少文本参数"
        executor.paste_text_chunk(text)
        return f"已输入文本：{text[:50]}{'...' if len(text) > 50 else ''}"
    
    elif action == "key":
        keys = params.get("keys", "")
        if not keys:
            return "错误：缺少按键参数"
        # 构建 step dict 让 executor 处理
        step = {"op": "按键", "key": keys}
        executor.execute_step(step, screen_size, 5, move_duration)
        return f"已按键：{keys}"
    
    elif action == "run_cmd":
        return _run_command(params.get("command", ""), cancel_check=cancel_check, timeout=30)
    
    elif action == "run_python":
        code = params.get("code", "")
        if not code:
            return "错误：缺少代码参数"
        try:
            import io as _io
            import contextlib
            stdout_capture = _io.StringIO()
            stderr_capture = _io.StringIO()
            with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
                exec(code, {"__builtins__": __builtins__})
            out = stdout_capture.getvalue().strip()
            err = stderr_capture.getvalue().strip()
            output = out or err or "(无输出)"
            return f"Python执行完成：\n{output[:500]}"
        except Exception as e:
            return f"Python执行失败：{e}"
    
    elif action == "focus":
        x = params.get("x")
        y = params.get("y")
        w = params.get("width", 0.2)
        h = params.get("height", 0.2)
        if x is None or y is None:
            return "错误：缺少坐标参数"
        # focus 本身不执行键鼠操作，只返回截图
        # 由调用方处理截图切换
        return f"已聚焦区域 ({x:.2f}, {y:.2f}) 大小 ({w:.2f}, {h:.2f})"
    
    elif action == "wait":
        seconds = float(params.get("seconds", 1.0))
        import time
        time.sleep(min(seconds, 30.0))
        return f"已等待 {seconds} 秒"
    
    elif action == "search_experience":
        # 模型遇困时用多个关键字检索总经验库，指导自己解决问题
        kws = params.get("keywords", [])
        if isinstance(kws, str):
            kws = [kws]
        kws = [str(k).strip() for k in kws if str(k).strip()]
        if not kws:
            return "错误：缺少搜索关键字 keywords（请提供字符串列表，尽量给出多个相关关键字）"
        results = experience.search(kws, top_k=5)
        if not results:
            return "经验库中未检索到与这些关键字相关的经验，请继续按自己的思路尝试"
        lines = []
        for i, r in enumerate(results, 1):
            tag = "常用" if r["category"] == "common" else "专用"
            lines.append(f"{i}. [{tag}] {r['text']}")
        return ("检索到以下相关经验（可参考其做法，但需结合当前屏幕实际情况执行）：\n"
                + "\n".join(lines))

    elif action == "summarize_experience":
        # 任务完成后模型选择性沉淀经验：common（常用，自动加入系统提示词）/ special（专用，可被搜索）
        cat = str(params.get("category", "")).strip().lower()
        content = str(params.get("content", "")).strip()
        if cat not in ("common", "special"):
            return "错误：category 只能是 common（常用）或 special（专用）"
        if not content:
            return "错误：缺少经验内容 content"
        if experience.add(cat, content):
            if cat == "common":
                return "经验已写入常用经验库（将自动加入之后任务的系统提示词）"
            return "经验已写入专用经验库（遇到相似任务时可被搜索到）"
        return "该经验已存在，未重复添加"
    
    elif action == "完成":
        return action_obj.get("result", "任务已完成")
    
    elif action == "失败":
        return action_obj.get("reasoning", "任务执行失败")
    
    else:
        return f"未知动作：{action}"


class AgentModeExecutor:
    """自主代理模式执行器。
    
    工作流程：
    - 接收用户目标
    - 循环：截图 → 模型决策 → 执行 → 记录
    - 直到模型输出"完成"或达到最大循环次数
    """
    
    MAX_ITERATIONS = 100
    
    def __init__(
        self,
        on_chunk: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        on_screenshot: Optional[Callable[[bytes], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_action: Optional[Callable[[str, Dict[str, Any], str], None]] = None,
        on_model_time: Optional[Callable[[float], None]] = None,
        on_task_time: Optional[Callable[[int], None]] = None,
    ):
        self._on_chunk = on_chunk
        self._cancel_check = cancel_check
        self._on_screenshot = on_screenshot
        self._on_status = on_status
        self._on_action = on_action  # action, params, result
        self._on_model_time = on_model_time  # 单次模型调用耗时(s)实时回调
        self._on_task_time = on_task_time  # 任务总用时(s, 整数)实时回调
        self._task_start = 0.0
        self._task_end_event = threading.Event()
        self._screen_size = capture.get_real_screen_size()
        self._messages: List[Dict[str, Any]] = []
        self._focus_mode = False  # 是否处于聚焦模式
        self._region_mode = False  # 是否启用操作区/结果区方案
        self._op_buttons: Dict[str, Dict[str, float]] = {}  # 操作区按钮: 名称 -> 归一化坐标
        self._result_region: Dict[str, float] = {}  # 结果区: x/y/width/height（归一化）
        self._pending_full_shot = False  # 是否下一轮截全屏（用于核对/纠正按钮坐标）
        self._seeing_focus = False  # 当前轮观察是否聚焦放大图（其坐标需换算回全屏）
        self._injected_feedback: List[str] = []  # 已注入的执行反馈，供硬过滤模型复述
        self._executed_actions: List[Tuple[str, str]] = []  # 已执行动作（action, 参数签名）
        self._disp_buf = ""  # 流式清洗的 keep-tail 缓冲
    
    def _emit_chunk(self, text: str) -> None:
        if self._on_chunk:
            self._on_chunk(text)
    
    def _emit_status(self, text: str) -> None:
        if self._on_status:
            self._on_status(text)
    
    def _check_cancel(self) -> bool:
        if self._cancel_check and self._cancel_check():
            return True
        return False

    def _cancellable_sleep(self, seconds: float) -> bool:
        """可取消的睡眠，每隔 50ms 检查一次取消状态。
        
        Returns:
            True = 已被取消（停止/暂停），False = 正常睡完
        """
        if seconds <= 0:
            return self._check_cancel()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._check_cancel():
                return True
            remaining = min(0.05, end - time.monotonic())
            if remaining > 0:
                time.sleep(remaining)
        return False
    
    def _build_observation(self, screenshot_bytes: bytes, kind: str = "full") -> List[Dict[str, Any]]:
        """构建带截图和上下文的消息。

        kind:
            - "full": 全屏截图（含坐标纠正的全屏复核）
            - "region": 操作区/结果区方案的结果区放大截图
        """
        if kind == "region":
            prompt = ("这是【结果区】的放大截图，请核对其中显示的结果内容是否与预期一致。"
                      "坐标系仍为整屏归一化坐标(0~1)。若结果正确，继续用 op_click 操作下一步；"
                      "若结果出错（不符预期），用 full_view 重截全屏，先检查操作区是否被遮挡/变化，"
                      "再决定是 stop_region 恢复界面重启用方案，还是仅 update_button 纠正个别坐标。")
        elif kind == "full" and self._region_mode:
            prompt = ("这是全屏截图，请先检查【操作区】：\n"
                      "1. 如果操作区按钮被遮挡、覆盖（弹窗、悬浮层）或者界面布局已经变化：先 stop_region 退出当前方案，"
                      "   恢复界面到正常状态后，再重新 set_region 识别按钮和结果区；\n"
                      "2. 如果操作区按钮没有变化，仅个别按钮坐标不对：用 update_button 纠正该单个按钮；\n"
                      "3. 如果多个按钮都错，确有必要才用 set_region 全部重新识别。")
        elif self._focus_mode:
            prompt = ("这是聚焦放大后的局部截图，请仔细观察细节。坐标仍然是相对于整个屏幕的归一化坐标(0~1)。")
        else:
            prompt = "请观察当前屏幕截图，决定下一步操作。"
        return model_client.build_image_messages(prompt, screenshot_bytes)
    
    def _call_model(self, messages: List[Dict[str, Any]], on_chunk: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """调用多模态模型（使用 tools 协议），返回 dict: {content, tool_calls, finish_reason}。

        附带重复/频率惩罚参数，抑制模型重复自续（可由设置调整）。
        每次调用都会把输入与输出打印到控制台，便于排查。
        """
        with CONSOLE_LOCK:
            print("--- 自主代理模型 ---")
            print("输入：")
            if messages:
                _text_idx, _text_content = -1, ""
                for _ti in range(len(messages) - 1, -1, -1):
                    _tm = messages[_ti]
                    if _tm.get("role") == "user" and isinstance(_tm.get("content"), str):
                        _text_idx, _text_content = _ti, _tm["content"]
                        break
                if _text_idx >= 0:
                    print(f"  [{_text_idx}] user: {_text_content}", flush=True)
                else:
                    _dump_messages(messages[-1:])
            print("------")
            print("输出：")
            _st = app_config.load_settings()
            _freq = float(_st.get("agent_frequency_penalty", 0.3))
            _pres = float(_st.get("agent_presence_penalty", 0.0))
            result = model_client.call_model(
                LABEL, messages, stream=on_chunk is not None,
                temperature=0.1,
                frequency_penalty=_freq,
                presence_penalty=_pres,
                cancel_check=self._cancel_check,
                on_chunk=on_chunk,
                tools=TOOLS,
            )
        return result

    def _do_op_click(self, action_obj: Dict[str, Any], click_type: str,
                     move_duration: float) -> str:
        """点击操作区已识别的按钮（复用归一化坐标，无需再次定位）。"""
        if not self._region_mode:
            return "错误：未启用操作区/结果区方案，请先用 set_region 识别按钮坐标"
        params = action_obj.get("params", {}) or {}
        name = params.get("button")
        if not name:
            return "错误：缺少按钮名称（button）"
        coord = self._op_buttons.get(str(name))
        if coord is None or "x" not in coord or "y" not in coord:
            return f"错误：未知按钮“{name}”，请先用 set_region 识别其坐标"
        px, py = executor.norm_to_pixel((float(coord["x"]), float(coord["y"])), self._screen_size)
        import pyautogui
        if click_type == "double_click":
            pyautogui.doubleClick(px, py, duration=move_duration)
            verb = "双击"
        elif click_type == "right_click":
            pyautogui.rightClick(px, py, duration=move_duration)
            verb = "右键"
        else:
            pyautogui.click(px, py, duration=move_duration)
            verb = "点击"
        return f"已{verb}按钮“{name}”（坐标为识别的固定坐标）"

    def _do_update_button(self, action_obj: Dict[str, Any]) -> str:
        """纠正某个操作区按钮的坐标（单按钮精准修改，无需全屏重截全识别）。"""
        if not self._region_mode:
            return "错误：未启用操作区/结果区方案，请先用 set_region"
        params = action_obj.get("params", {}) or {}
        name = params.get("button")
        x = params.get("x")
        y = params.get("y")
        if not name:
            return "错误：缺少按钮名称（button）"
        if x is None or y is None:
            return "错误：缺少新的 x/y 坐标"
        if str(name) not in self._op_buttons:
            return f"错误：未知按钮“{name}”，请先用 set_region 识别"
        self._op_buttons[str(name)] = {"x": float(x), "y": float(y)}
        return f"已纠正按钮“{name}”坐标为 ({float(x):.2f}, {float(y):.2f})"

    def _capture_now(self) -> bytes:
        """按当前模式截取一张用于【屏幕变化检测】的参考帧。

        优先级：结果区方案 > 聚焦 > 全屏（与每轮观察截图一致）。
        """
        if self._region_mode and self._result_region:
            return _capture_region(self._result_region)
        if self._focus_mode:
            lp = self._last_focus_params
            if lp:
                return _capture_focus(
                    float(lp.get("x", 0.5)), float(lp.get("y", 0.5)),
                    float(lp.get("width", 0.2)), float(lp.get("height", 0.2)),
                )
        return _capture_full()

    def _run_wait_change(self, ref_bytes: bytes, params: Optional[Dict[str, Any]] = None) -> str:
        """智能等待：以【操作前截图】ref_bytes 为参考，检测屏幕发生变化且趋于稳定。

        返回：
            正常 → 描述结果字符串
            取消 → 返回 "__CANCELLED__"（由调用方转成“任务已取消”）
        """
        params = params or {}
        try:
            max_seconds = float(params.get("max_seconds", 10) or 10)
        except (TypeError, ValueError):
            max_seconds = 10.0
        if max_seconds <= 0:
            max_seconds = 10.0
        try:
            interval = float(params.get("interval", 0.5) or 0.5)
        except (TypeError, ValueError):
            interval = 0.5
        if interval <= 0:
            interval = 0.5
        deadline = time.monotonic() + max_seconds
        changed = False
        stable_frames = 0
        prev = None
        self._emit_status("智能等待屏幕变化并稳定...")
        while time.monotonic() < deadline:
            if self._check_cancel():
                return "__CANCELLED__"
            try:
                cur = self._capture_now()
            except Exception:
                cur = None
            if cur is not None:
                if changed:
                    if prev is not None:
                        d = _image_change_ratio(prev, cur)
                        if d < _STABLE_THRESHOLD:
                            stable_frames += 1
                            if stable_frames >= 3:
                                return "屏幕已发生变化并保持稳定，可以继续操作"
                        else:
                            stable_frames = 0
                    prev = cur
                else:
                    d = _image_change_ratio(ref_bytes, cur)
                    if d > _CHANGE_THRESHOLD:
                        changed = True
                        stable_frames = 0
                        prev = cur
            if self._cancellable_sleep(interval):
                return "__CANCELLED__"
        if changed:
            return "屏幕已发生变化，但等待时间内未完全稳定（可适当加大 max_seconds）"
        return "等待超时：等待时间内屏幕未发生明显变化"

    def _strip_feedback(self, raw: str) -> str:
        """硬过滤：剔除模型输出中复述的[执行反馈]原文，防止其进入解析/上下文。"""
        if not raw or not self._injected_feedback:
            return raw
        out = raw
        for fb in self._injected_feedback:
            out = out.replace(fb, "")
        return out

    def _compress_history(self) -> None:
        """上下文腐烂控制：历史过长时压缩。

        保留开头的系统提示 + 原始任务目标（锚定到上下文头部），并保留最近
        _HISTORY_TAIL 条记录，把被省略的中间反馈折叠成一条紧凑提示，防止
        上下文无限膨胀拉低聚焦。
        """
        msgs = self._messages
        if len(msgs) <= _HISTORY_KEEP:
            return
        head = 1
        if len(msgs) > 1 and msgs[1].get("role") == "user":
            head = 2  # 保留系统提示(0) + 原始目标(1)
        tail = msgs[-_HISTORY_TAIL:]
        dropped = len(msgs) - head - len(tail)
        if dropped <= 0:
            return
        note = {
            "role": "user",
            "content": f"（上下文已压缩：省略较早的 {dropped} 条动作反馈，目标仍以开头消息为准。最近 {_HISTORY_TAIL} 条操作如下，请在它们基础上继续推进。）",
        }
        self._messages = msgs[:head] + [note] + tail

    def _stream_clean_for_display(self, chunk: str) -> str:
        """对控制台/浮窗的实时流做清洗，剔除模型复述的[执行反馈]。

        一旦检测到输出尾部开始出现反馈前缀（keep-tail+head 检测）即暂扣其后内容，
        待完整反馈到达后由 _strip_feedback 整段剔除，用户侧不显示复述内容。
        """
        self._disp_buf += chunk
        stripped = self._strip_feedback(self._disp_buf)
        if len(stripped) > 300:
            safe, carry = stripped, ""
        else:
            hold = self._fb_hold_start(stripped)
            if hold is not None:
                safe, carry = stripped[:hold], stripped[hold:]
            else:
                split = max(0, len(stripped) - (len("[执行反馈]") - 1))
                safe, carry = stripped[:split], stripped[split:]
        self._disp_buf = carry
        return safe

    def _fb_hold_start(self, s: str) -> Optional[int]:
        """返回 s 中从哪个位置起开始疑似一段反馈前缀（应为最早的一个）。"""
        if not self._injected_feedback:
            return None
        best: Optional[int] = None
        for fb in self._injected_feedback:
            lb = len(fb)
            for i in range(len(s)):
                tail = s[i:]
                if 0 < len(tail) <= lb and fb.startswith(tail):
                    if best is None or i < best:
                        best = i
        return best

    def get_state(self) -> Dict[str, Any]:
        """获取当前完整状态（消息历史 + 聚焦状态 + 操作区/结果区方案状态）。"""
        return {
            "messages": copy.deepcopy(self._messages),
            "focus_mode": self._focus_mode,
            "last_focus_params": copy.deepcopy(self._last_focus_params),
            "region_mode": self._region_mode,
            "op_buttons": copy.deepcopy(self._op_buttons),
            "result_region": copy.deepcopy(self._result_region),
            "pending_full_shot": self._pending_full_shot,
        }
    
    def get_messages(self) -> List[Dict[str, Any]]:
        """获取当前消息历史（深拷贝）。"""
        return copy.deepcopy(self._messages)
    
    def run(self, goal: str, initial_messages: Optional[List[Dict[str, Any]]] = None,
            initial_state: Optional[Dict[str, Any]] = None) -> str:
        """运行代理模式主循环。返回最终结果描述。

        自带任务总用时计时（整数秒，实时回调 on_task_time），并在 run 结束时可靠停止计时线程。
        """
        self._task_start = time.monotonic()
        self._task_end_event.clear()
        threading.Thread(target=self._task_tick_loop, daemon=True).start()
        try:
            return self._run_inner(goal, initial_messages, initial_state)
        finally:
            self._task_end_event.set()
            # 结束时补发最后一次总用时
            if self._on_task_time:
                self._on_task_time(int(time.monotonic() - self._task_start))

    def _task_tick_loop(self) -> None:
        """任务总用时实时回调：每秒上报一次整数秒。"""
        while not self._task_end_event.wait(1.0):
            if self._on_task_time:
                self._on_task_time(int(time.monotonic() - self._task_start))

    def _run_inner(self, goal: str, initial_messages: Optional[List[Dict[str, Any]]] = None,
                   initial_state: Optional[Dict[str, Any]] = None) -> str:
        self._messages = []
        self._focus_mode = False
        self._last_focus_params = {}
        self._region_mode = False
        self._op_buttons = {}
        self._result_region = {}
        self._pending_full_shot = False
        self._executed_actions: List[Tuple[str, str]] = []  # 本轮任务已执行动作（action, 参数签名），用于重复检测
        
        # 如果有完整状态（暂停后恢复），恢复所有状态
        if initial_state is not None:
            self._messages = copy.deepcopy(initial_state.get("messages", []))
            self._focus_mode = initial_state.get("focus_mode", False)
            self._last_focus_params = copy.deepcopy(initial_state.get("last_focus_params", {}))
            self._region_mode = initial_state.get("region_mode", False)
            self._op_buttons = copy.deepcopy(initial_state.get("op_buttons", {}))
            self._result_region = copy.deepcopy(initial_state.get("result_region", {}))
            self._pending_full_shot = initial_state.get("pending_full_shot", False)
        # 如果有初始消息（旧版兼容），直接使用
        elif initial_messages is not None and len(initial_messages) > 0:
            self._messages = copy.deepcopy(initial_messages)
            # 确保最后一轮截图已经完成，模型可以继续决策
        else:
            # 添加系统提示词
            system_prompt = _get_system_prompt()
            self._messages.append({"role": "system", "content": system_prompt})
            # 添加用户目标
            self._messages.append({"role": "user", "content": f"用户目标：{goal}\n\n请先截图观察当前屏幕状态，然后逐步完成任务。"})
        
        self._emit_status(f"开始执行：{goal}")
        
        # 读取配置
        settings = app_config.load_settings()
        agent_post_wait = float(settings.get("agent_post_wait", 0.5))
        move_duration = float(settings.get("mouse_move_duration", 0.0))
        self._injected_feedback: List[str] = []  # 本轮已注入的执行反馈，用于硬过滤模型复述
        self._disp_buf = ""
        
        # 等待主窗口完全隐藏后再截图（可取消）
        if self._cancellable_sleep(1):
            return "任务已取消"
        
        for iteration in range(self.MAX_ITERATIONS):
            if self._check_cancel():
                return "任务已取消"
            
            # 0. 贪腐控制：过长时压缩历史（保留原始目标 + 最近若干条），锚定目标到上下文
            self._compress_history()

            # 1. 截图
            try:
                self._seeing_focus = False  # 每轮重新计算：本轮观察是否聚焦放大图
                # 截图方式优先级：全屏复核 > 操作区/结果区方案 > 聚焦 > 全屏
                if self._pending_full_shot:
                    # 全屏复核：用于模型核对/纠正按钮坐标（一次性，之后回到默认截图方式）
                    self._pending_full_shot = False
                    shot = _capture_full()
                    capture_kind = "full"
                elif self._region_mode and self._result_region:
                    # 操作区/结果区方案：每次只截取结果区核对结果
                    shot = _capture_region(self._result_region)
                    capture_kind = "region"
                elif self._focus_mode:
                    # 聚焦模式：从上一条消息中提取聚焦参数；本轮观察为放大图，坐标需换算回全屏
                    last_focus = self._last_focus_params
                    self._seeing_focus = True
                    if last_focus:
                        shot = _capture_focus(
                            last_focus.get("x", 0.5),
                            last_focus.get("y", 0.5),
                            last_focus.get("width", 0.2),
                            last_focus.get("height", 0.2),
                        )
                    else:
                        shot = _capture_full()
                        self._focus_mode = False
                        self._seeing_focus = False
                    capture_kind = "full"
                else:
                    shot = _capture_full()
                    capture_kind = "full"
                
                if self._on_screenshot:
                    self._on_screenshot(shot)
            except Exception as e:
                self._emit_status(f"截图失败：{e}")
                return f"截图失败：{e}"
            
            # 2. 构建带截图的消息
            obs = self._build_observation(shot, kind=capture_kind)
            
            # 发送给模型（不含历史截图，只发当前截图）
            current_messages = list(self._messages)
            current_messages.extend(obs)
            
            # 3. 调用模型（流式输出到控制台和浮窗）
            self._emit_status(f"第 {iteration + 1} 轮：模型思考中...")
            raw_parts: List[str] = []
            _cancel_detected = False
            def _on_chunk(chunk: str) -> None:
                nonlocal _cancel_detected
                raw_parts.append(chunk)
                _safe = self._stream_clean_for_display(chunk)
                if _safe:
                    self._emit_chunk(_safe)
                # 每次 chunk 回调后检查取消，提高暂停响应速度
                if self._check_cancel():
                    _cancel_detected = True
            _t_start = time.monotonic()
            _stop_timer = threading.Event()
            def _tick() -> None:
                # 模型调用期间每 0.2s 实时上报一次耗时（保留一位小数）
                while not _stop_timer.wait(0.2):
                    if self._on_model_time:
                        self._on_model_time(round(time.monotonic() - _t_start, 1))
            threading.Thread(target=_tick, daemon=True).start()
            try:
                try:
                    result = self._call_model(current_messages, on_chunk=_on_chunk)
                finally:
                    _stop_timer.set()
            except Exception as e:
                self._emit_status(f"模型调用失败：{e}")
                return f"模型调用失败：{e}"
            # 调用结束后补发一次最终耗时（保留一位小数）
            _elapsed = time.monotonic() - _t_start
            if self._on_model_time:
                self._on_model_time(round(_elapsed, 1))
            
            self._emit_chunk("\n\n")
            # 冲刷流式缓冲中残留的安全文本（再次剔除完整反馈并切掉疑似反馈头）
            if self._disp_buf:
                _rest = self._strip_feedback(self._disp_buf)
                _h = self._fb_hold_start(_rest)
                if _h is not None:
                    _rest = _rest[:_h]
                self._disp_buf = ""
                if _rest:
                    self._emit_chunk(_rest)

            # 模型输出（含残留冲刷）全部打印完毕后再打印结束分割线，确保正文都在分割线之前
            with CONSOLE_LOCK:
                print("\n------------", flush=True)
            
            if _cancel_detected:
                return "任务已取消"
            
            # 从 tool_calls 解析动作列表
            _content_text = result.get("content") or ""
            _tool_calls = result.get("tool_calls") or []
            _content_text = self._strip_feedback(_content_text)

            # 记录 assistant 消息（包含 content 和 tool_calls）
            _assistant_msg: Dict[str, Any] = {"role": "assistant", "content": _content_text or None}
            if _tool_calls:
                _assistant_msg["tool_calls"] = _tool_calls
            self._messages.append(_assistant_msg)

            # 4. 从 tool_calls 解析动作
            actions: List[Dict[str, Any]] = []
            for _tc in _tool_calls:
                _fn = _tc.get("function", {})
                _name = _fn.get("name", "")
                try:
                    _args = json.loads(_fn.get("arguments", "{}") or "{}")
                except (json.JSONDecodeError, TypeError):
                    _args = {}
                # 转换为统一的 action obj 格式
                actions.append({
                    "action": _name,
                    "params": {k: v for k, v in _args.items() if k != "reasoning"},
                    "reasoning": _args.get("reasoning", ""),
                    "_tool_call_id": _tc.get("id"),
                })
            # 纯文本完成标记：模型可能直接输出"完成"而不调用工具
            if not actions and _content_text.strip():
                actions.append({"action": "complete", "params": {}, "reasoning": _content_text.strip()})

            if not actions:
                self._emit_status("模型未返回工具调用，尝试重试")
                self._messages.append({
                    "role": "user",
                    "content": "请通过调用工具来执行操作，例如 click、type、key 等，或调用 complete 结束任务。",
                })
                continue

            self._emit_status(f"→ 本轮动作数：{len(actions)}")

            # 智能等待参考：以本轮观察截图（批内首个操作前的屏幕）为默认参考
            wait_ref: Optional[bytes] = None
            last_action = ""
            repeat_hint: Optional[str] = None  # 检测到重复相同操作时的提醒
            round_feedback: List[str] = []  # 本轮各动作的执行反馈，轮末合并为一条 user 消息注入

            for _obj in actions:
                action = _obj.get("action", "")
                reasoning = self._strip_feedback(_obj.get("reasoning", ""))
                last_action = action
                self._emit_status(f"→ {action}：{reasoning}")

                # 5a. 连续相同操作检测：达到上限后强制升级点击强度（单击→双击→右键）
                _escalated = False
                if action in _SCREEN_MUTATING_ACTIONS:
                    try:
                        _sig_p = _obj.get("params", {}) or {}
                        if not isinstance(_sig_p, dict):
                            _sig_p = {}
                        _sig_for_repeat = (action, json.dumps(_sig_p, sort_keys=True, ensure_ascii=False))
                    except Exception:
                        _sig_for_repeat = (action, "")
                    _consec = 1
                    for _x in reversed(self._executed_actions):
                        if _x == _sig_for_repeat:
                            _consec += 1
                        else:
                            break
                    if _consec >= _SAME_SCHEME_CAP and action in _CLICK_ESCALATION:
                        _new_action = _CLICK_ESCALATION[action]
                        _obj["action"] = _new_action
                        action = _new_action
                        last_action = _new_action
                        _escalated = True
                        self._emit_status(f"！连续 {_consec} 次执行 {_sig_for_repeat[0]} 无效，已强制升级为 {_new_action}")

                # 5. 检查是否完成/失败（遇到立即结束整轮）
                if action in ("完成", "complete"):
                    result = _obj.get("params", {}).get("result") or _obj.get("result", "任务已完成")
                    self._emit_status(f"✅ {result}")
                    if self._on_action:
                        self._on_action(action, _obj.get("params", {}) or {}, result)
                    # 添加 tool 消息
                    _tc_id = _obj.get("_tool_call_id")
                    if _tc_id:
                        self._messages.append({"role": "tool", "tool_call_id": _tc_id, "content": result})
                    return result
                if action in ("失败", "fail"):
                    reason = _obj.get("reasoning") or _obj.get("params", {}).get("reasoning") or "任务执行失败"
                    self._emit_status(f"❌ {reason}")
                    if self._on_action:
                        self._on_action(action, _obj.get("params", {}) or {}, reason)
                    # 添加 tool 消息
                    _tc_id = _obj.get("_tool_call_id")
                    if _tc_id:
                        self._messages.append({"role": "tool", "tool_call_id": _tc_id, "content": reason})
                    return reason

                # 智能等待：以该操作前的屏幕截图为参考，等待屏幕变化并稳定
                if action == "wait_change":
                    ref = wait_ref if wait_ref is not None else shot
                    result = self._run_wait_change(ref, _obj.get("params", {}) or {})
                    if result == "__CANCELLED__":
                        return "任务已取消"
                    self._emit_status(f"✅ 智能等待：{result}")
                    if self._on_action:
                        self._on_action(action, _obj.get("params", {}) or {}, result)
                    _content = _feedback_line(action, result)
                    self._injected_feedback.append(_content)
                    round_feedback.append(_content)
                    _tc_id = _obj.get("_tool_call_id")
                    if _tc_id:
                        self._messages.append({"role": "tool", "tool_call_id": _tc_id, "content": _content})
                    continue

                # 操作前参考：对会改变屏幕内容的操作，在执行前捕获【该操作前】截图供 wait_change 使用
                if action in _SCREEN_MUTATING_ACTIONS:
                    try:
                        wait_ref = self._capture_now()
                    except Exception:
                        wait_ref = shot

                # 6. 执行动作
                try:
                    # 聚焦模式：本轮观察为放大图，模型返回的坐标基于放大图，需换算回全屏
                    if self._seeing_focus and action in _COORD_ACTIONS:
                        _p = _obj.get("params")
                        if isinstance(_p, dict) and "x" in _p and "y" in _p:
                            _fx, _fy = _focus_to_full(float(_p["x"]), float(_p["y"]),
                                                      self._last_focus_params)
                            _p["x"], _p["y"] = _fx, _fy

                    if action == "focus":
                        # 保存聚焦参数供下次截图使用；焦点模式与操作区方案互斥
                        self._last_focus_params = _obj.get("params", {})
                        self._focus_mode = True
                        self._region_mode = False
                        result = _execute_action(_obj, self._screen_size, move_duration, cancel_check=self._cancel_check)
                    elif action == "set_region":
                        # 启用操作区/结果区方案：一次性识别所有按钮坐标并选定结果区
                        params = _obj.get("params", {}) or {}
                        buttons = params.get("buttons", {}) or {}
                        result_region = params.get("result", {}) or {}
                        if not isinstance(buttons, dict) or not buttons:
                            result = "错误：缺少有效的 buttons（待识别的按钮名->归一化坐标）"
                        elif not isinstance(result_region, dict) or "x" not in result_region or "y" not in result_region:
                            result = "错误：缺少有效的结果区 result（需含归一化 x/y）"
                        else:
                            # 统一为 {按钮名: {"x":.., "y":..}}
                            cleaned = {}
                            for name, coord in buttons.items():
                                if isinstance(coord, dict) and "x" in coord and "y" in coord:
                                    cleaned[str(name)] = {
                                        "x": float(coord["x"]), "y": float(coord["y"]),
                                    }
                            if not cleaned:
                                result = "错误：buttons 中各按钮缺少归一化 x/y 坐标"
                            else:
                                self._op_buttons = cleaned
                                self._result_region = {
                                    "x": float(result_region.get("x", 0.5)),
                                    "y": float(result_region.get("y", 0.5)),
                                    "width": float(result_region.get("width", 0.4)),
                                    "height": float(result_region.get("height", 0.4)),
                                }
                                self._region_mode = True
                                self._focus_mode = False
                                names = "、".join(sorted(cleaned.keys()))
                                result = (f"已启用操作区/结果区方案；识别按钮【{names}】，"
                                          f"结果区范围已设定。此后每轮只截取结果区核对结果，op_* 点击复用按钮坐标。")
                    elif action == "op_click":
                        result = self._do_op_click(_obj, "click", move_duration)
                    elif action == "op_double_click":
                        result = self._do_op_click(_obj, "double_click", move_duration)
                    elif action == "op_right_click":
                        result = self._do_op_click(_obj, "right_click", move_duration)
                    elif action == "full_view":
                        # 重截全屏复核：下一轮截全屏供模型核对/纠正按钮坐标（不退出方案）
                        if not self._region_mode:
                            result = "当前未启用操作区/结果区方案，无需全屏复核"
                        else:
                            self._pending_full_shot = True
                            result = "已请求重截全屏，请核对按钮坐标是否正确并纠正"
                    elif action == "update_button":
                        result = self._do_update_button(_obj)
                    elif action == "stop_region":
                        # 退出操作区/结果区方案，恢复全屏截图
                        was_active = self._region_mode
                        self._region_mode = False
                        self._op_buttons = {}
                        self._result_region = {}
                        self._pending_full_shot = False
                        result = "已退出操作区/结果区方案，恢复全屏模式" if was_active else "当前未启用操作区/结果区方案"
                    else:
                        if action == "screenshot":
                            # 截图动作：清除聚焦模式（不退出操作区方案）
                            self._focus_mode = False
                        result = _execute_action(_obj, self._screen_size, move_duration, cancel_check=self._cancel_check)
                except Exception as e:
                    result = f"执行失败：{e}"
                    self._emit_status(f"⚠ {result}")

                # 执行工具后等待指定时间，让界面有时间响应（可取消）
                if agent_post_wait > 0:
                    if self._cancellable_sleep(agent_post_wait):
                        return "任务已取消"

                # 通知 UI 动作结果（用于浮窗可视化）
                if self._on_action:
                    self._on_action(action, _obj.get("params", {}) or {}, result)

                # 记录本轮动作反馈：同时注入 tool 消息（供 tools 协议）和 user 消息（保持兼容）
                _content = _feedback_line(action, result)
                self._injected_feedback.append(_content)
                round_feedback.append(_content)
                # 添加 tool 角色消息（每个 tool_call 对应一条结果）
                _tc_id = _obj.get("_tool_call_id")
                if _tc_id:
                    self._messages.append({"role": "tool", "tool_call_id": _tc_id, "content": _content})

                # 重复动作检测：完全相同的操作反复执行仍无不同结果时，提醒模型改变策略
                try:
                    _p = _obj.get("params", {}) or {}
                    if not isinstance(_p, dict):
                        _p = {}
                    _sig = (action, json.dumps(_p, sort_keys=True, ensure_ascii=False))
                except Exception:
                    _sig = (action, "")
                self._executed_actions.append(_sig)
                _cnt = sum(1 for _x in self._executed_actions if _x == _sig)
                if _cnt >= 2 and action == "full_view":
                    repeat_hint = (
                        "【系统引导】你已连续多次输出 full_view 反复核对，结果区仍不符预期。"
                        "请停止无意义的反复核对，真正采取下一步操作：若操作区正常就用 op_click 点击按钮并核对结果区；"
                        "若操作区被遮挡或布局变化，就 stop_region 退出方案、恢复界面后重建方案。"
                    )
                elif action in _SCREEN_MUTATING_ACTIONS and _cnt >= 2:
                    if _escalated:
                        repeat_hint = (
                            f"【系统引导】同一操作已连续尝试 {_SAME_SCHEME_CAP} 次仍无效，系统已【强制】把操作升级为“{action}”。"
                            f"请基于此次升级后的结果继续观察，若仍无效应改用完全不同的方法或目标，禁止再按原方式重试。"
                        )
                    else:
                        repeat_hint = (
                            f"【系统引导】你刚刚重复执行了与历史完全相同的操作“{action}”，"
                            f"该操作已执行 {_cnt} 次仍未产生不同结果，说明此方式无效。"
                            f"请改变操作方式：例如单击打不开就改用双击，或调整参数、改用其他步骤。"
                        )

            # 8. 追加下一轮引导：把本轮所有执行反馈、引导、重复提醒与进度合并为【一条】user 消息，
            #    避免多个 user 消息连续进入上下文导致模型输出重复或混乱。
            action = last_action

            # focus 单次生效：本轮已展示放大图并据此操作后，下一轮恢复全屏（除非本轮是重新聚焦）
            if self._seeing_focus and action != "focus":
                self._focus_mode = False

            if action == "screenshot":
                guidance = "请观察截图，决定下一步操作。"
            elif action == "focus":
                guidance = ("我已聚焦到指定区域并放大截图，请在这个放大图内观察细节并返回点击/操作的坐标。"
                            "注意：你本次返回的归一化坐标(x, y)是相对这张放大图的，程序会自动换算成全屏坐标后执行，"
                            "因此你只需按放大图内的实际位置给坐标即可。")
            elif action == "wait_change":
                guidance = "智能等待已完成，屏幕已进入稳定状态。请观察当前截图，决定下一步操作。"
            elif action == "full_view":
                guidance = ("已重截全屏，请先检查操作区：若按钮被遮挡/覆盖或界面布局已变化，先 stop_region 退出方案"
                            "并恢复界面，再重新 set_region 启用；若仅个别按钮坐标不对，用 update_button 纠正单个；"
                            "多个都错确有必要才用 set_region 全部重新识别。")
            elif action in ("set_region", "op_click", "op_double_click", "op_right_click",
                            "update_button", "stop_region"):
                if self._region_mode:
                    guidance = ("已处于操作区/结果区方案：下一轮将直接截取结果区供你核对。"
                                "结果正确则继续 op_click；结果出错可 full_view 重截全屏核对并纠正按钮坐标。")
                else:
                    guidance = "已退出该方案，恢复全屏模式。请观察截图决定下一步操作。"
            else:
                guidance = ("请基于本轮执行反馈和当前截图，直接判断并执行下一步操作，无需重复观察；"
                            "如果任务已完成，请直接输出完成动作。")

            _parts: List[str] = []
            if round_feedback:
                _parts.append("本轮动作执行结果：\n" + "\n".join(f"- {fb}" for fb in round_feedback))
            _parts.append(guidance)
            if repeat_hint:
                _parts.append(repeat_hint)
            if self._executed_actions:
                _recent = "、".join(_a for _a, _ in self._executed_actions[-8:])
                _parts.append(f"[进度] 累计已完成 {len(self._executed_actions)} 个操作，最近操作：{_recent}。请据此持续推进，若无进展就改用新策略。")
            self._messages.append({
                "role": "user",
                "content": "\n\n".join(_parts),
            })
        
        self._emit_status("⚠ 已达到最大迭代次数")
        return "已达到最大执行步数，任务未完成"