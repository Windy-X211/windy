# -*- coding: utf-8 -*-
"""Step 数据模型：原子化步骤的规范、解析、校验。

规划模型输出形如：
    当前屏幕状态：桌面 目标：搜索李白是谁
    [单击-桌面上-浏览器图标>单击-浏览器界面中-搜索栏>输入-李白是谁>按键-enter>单击-搜索结果页中-第一个条目]

parse_steps 从中提取 [] 内的步骤序列，按 > 切分，再按 op 类型解析为 Step dict 列表。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# 操作类型常量
OP_CLICK = "单击"
OP_DBLCLICK = "双击"
OP_RIGHT = "右键"
OP_SCROLL_UP = "上滑"
OP_SCROLL_DOWN = "下滑"
OP_DRAG = "拖动"
OP_INPUT = "输入"          # 短文本输入（<=LONG_TEXT_THRESHOLD 字），步骤内直接携带文本
OP_LONG_INPUT = "长输入"    # 长文本/生成型输入，步骤内只带主题/描述，执行时由聊天模型流式生成
OP_KEY = "按键"
OP_WAIT = "等待"
OP_CONSULT = "咨询用户"  # 规划模型请求用户裁决的特殊步骤

# 需要视觉定位的操作（鼠标类）；长输入无需视觉定位（在前序步骤中已聚焦输入框）
OPS_NEED_VISION = {OP_CLICK, OP_DBLCLICK, OP_RIGHT, OP_SCROLL_UP, OP_SCROLL_DOWN, OP_DRAG}
# 拖动需要两次定位（起点+终点）
OP_VISION_CALLS: Dict[str, int] = {
    OP_CLICK: 1, OP_DBLCLICK: 1, OP_RIGHT: 1,
    OP_SCROLL_UP: 1, OP_SCROLL_DOWN: 1, OP_DRAG: 2,
    OP_LONG_INPUT: 0,
    OP_INPUT: 0, OP_KEY: 0, OP_WAIT: 0, OP_CONSULT: 0,
}

ALL_OPS = set(OP_VISION_CALLS.keys())


class ParseError(Exception):
    """规划/视觉模型输出无法解析。"""


class ParamInvalid(Exception):
    """模型输出参数校验失败（如坐标越界/非两值）。"""


class InterfaceUnavailable(Exception):
    """模型接口不可用（连接拒绝/401/超时等）。"""


# ---------------------------------------------------------------------------
# Step 规范
# ---------------------------------------------------------------------------

def new_step(index: int, op: str, raw: str, **extra: Any) -> Dict[str, Any]:
    """构造一个空 Step dict。"""
    step: Dict[str, Any] = {
        "index": index,
        "op": op,
        "raw": raw,
        "location": None,
        "target": None,
        "coord": None,            # (x, y) 归一化
        "coord_locked": False,    # 坐标是否锁定（锁定后执行时跳过视觉模型，直接使用已记录坐标）
        "text": None,             # 输入全文（短输入直接填；长输入执行时由模型流式填充）
        "description": None,      # 长输入主题/要求（由规划模型写入，执行时作为生成提示）
        "desc_full": None,        # 长输入原始主题（变量化后 description 可能被替换为变量名，此字段保留原文）
        "key": None,              # 按键
        "seconds": None,          # 等待
        "start_location": None,
        "start_target": None,
        "start_coord": None,
        "end_location": None,
        "end_target": None,
        "end_coord": None,
        "question": None,         # 咨询用户
        "status": "pending",      # pending|locating|executing|done|failed|skipped
        "note": None,
    }
    step.update(extra)
    return step


# ---------------------------------------------------------------------------
# 解析规划模型输出
# ---------------------------------------------------------------------------

# 匹配首个 [ ... ] 块（非贪婪，跨行）
_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]", re.S)
# 提取目标行
_GOAL_RE = re.compile(r"目标[：:]\s*(.+?)(?:\s*\[|\n|$)", re.S)
# 提取屏幕状态行
_STATE_RE = re.compile(r"当前屏幕状态[：:]\s*(.+?)(?:\n|$)", re.S)


def extract_goal(raw_text: str) -> str:
    m = _GOAL_RE.search(raw_text)
    return m.group(1).strip() if m else ""


def extract_screen_state(raw_text: str) -> str:
    m = _STATE_RE.search(raw_text)
    return m.group(1).strip() if m else ""


def parse_steps(raw_text: str) -> Tuple[str, str, List[Dict[str, Any]]]:
    """解析规划模型输出。

    返回 (screen_state, goal, steps)。
    解析失败抛 ParseError。
    """
    if not raw_text or not raw_text.strip():
        raise ParseError("规划模型输出为空")

    screen_state = extract_screen_state(raw_text)
    goal = extract_goal(raw_text)

    m = _BRACKET_RE.search(raw_text)
    if not m:
        raise ParseError("规划模型输出中未找到 [...] 步骤块")

    body = m.group(1).strip()
    if not body:
        raise ParseError("步骤块为空")

    tokens = [t.strip() for t in body.split(">") if t.strip()]
    if not tokens:
        raise ParseError("步骤块切分后为空")

    steps: List[Dict[str, Any]] = []
    for i, tok in enumerate(tokens):
        steps.append(_parse_one(i, tok))

    return screen_state, goal, steps


def _parse_one(index: int, token: str) -> Dict[str, Any]:
    """解析单个步骤 token，如 '单击-桌面上-浏览器图标'。

    容错策略：
    - 对空间操作（单击/双击/右键/上滑/下滑），rest 按第一个 '-' 分为 location
      和 target；target 保留后续所有 '-' 连接的内容（如 URL 中的 '-' 不会被截断）。
    - 仅 1 段时（无 '-'）视为纯目标元素，location 留空。
    """
    # 完全不含 '-' 的 token（纯 op，无参数）
    if "-" not in token:
        op = token.strip()
        if op not in ALL_OPS:
            raise ParseError(f"未知操作类型：{token}")
        if op == OP_WAIT:
            return new_step(index, op, token, seconds=1.0)
        return new_step(index, op, token)

    head, rest = token.split("-", 1)
    op = head.strip()
    if op not in ALL_OPS:
        raise ParseError(f"未知操作类型：{token}")

    if op == OP_INPUT:
        return new_step(index, op, token, text=rest)
    if op == OP_KEY:
        return new_step(index, op, token, key=rest.strip())
    if op == OP_WAIT:
        return new_step(index, op, token, seconds=_parse_seconds(rest))
    if op == OP_CONSULT:
        return new_step(index, op, token, question=rest)
    if op == OP_DRAG:
        return _parse_drag(index, token, rest)
    if op == OP_LONG_INPUT:
        # 长输入：格式为「长输入-主题或要求」，rest 全部作为 description。
        # 无需位置/目标元素（不进行视觉定位和点击聚焦），
        # 执行时直接在当前焦点处由聊天模型流式生成并粘贴。
        description = rest.strip()
        if not description:
            raise ParseError(f"长输入步骤缺少主题或要求：{token}")
        return new_step(
            index, OP_LONG_INPUT, token,
            # description 给聊天模型作为生成要求
            description=description,
            # text 在执行时由模型流式填充（保存全文，UI 显示用）
            text=None,
        )

    # —— 空间操作（单击/双击/右键/上滑/下滑）——
    parts = rest.split("-")
    if not parts or not parts[0].strip():
        raise ParseError(f"步骤缺少位置信息：{token}")

    if len(parts) == 1:
        # 仅目标元素，无位置（如 "单击-桌面图标"）
        location = ""
        target = parts[0].strip()
    else:
        location = parts[0].strip()
        target = "-".join(parts[1:]).strip()

    if not target:
        raise ParseError(f"步骤缺少目标元素：{token}")
    return new_step(index, op, token, location=location, target=target)


def _parse_drag(index: int, token: str, rest: str) -> Dict[str, Any]:
    """解析拖动：'位置A-元素A-到-位置B-元素B'。"""
    if "-到-" not in rest:
        raise ParseError(f"拖动步骤缺少 '-到-' 分隔：{token}")
    left, right = rest.split("-到-", 1)
    left = left.strip()
    right = right.strip()
    lp = left.split("-")
    rp = right.split("-")
    start_location = lp[0].strip() if lp else ""
    start_target = "-".join(lp[1:]).strip() if len(lp) > 1 else ""
    end_location = rp[0].strip() if rp else ""
    end_target = "-".join(rp[1:]).strip() if len(rp) > 1 else ""
    if not start_target or not end_target:
        raise ParseError(f"拖动步骤缺少起止目标：{token}")
    return new_step(
        index, OP_DRAG, token,
        start_location=start_location, start_target=start_target,
        end_location=end_location, end_target=end_target,
        location=start_location, target=start_target,
    )


def _parse_seconds(text: str) -> float:
    text = text.strip()
    # 取数字部分
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return 1.0
    try:
        return float(m.group(1))
    except ValueError:
        return 1.0


# ---------------------------------------------------------------------------
# 坐标校验
# ---------------------------------------------------------------------------

def parse_coord(text: str) -> Optional[Tuple[float, float]]:
    """从视觉模型返回文本解析归一化坐标。

    接受 'x.xx,y.xx' 或 'x.xx y.xx'。返回 (x, y) 或 None。
    无法解析返回 None（交由调用方走重试或反馈）。
    """
    if not text:
        return None
    t = text.strip()
    if t.lower() == "none":
        return None
    # 提取所有浮点
    nums = re.findall(r"\d+\.?\d*", t)
    if len(nums) < 2:
        return None
    try:
        x, y = float(nums[0]), float(nums[1])
    except ValueError:
        return None
    return (x, y)


def validate_coord(coord: Any) -> bool:
    """校验坐标合法：长度 2、均 float、0<=v<=1。"""
    if not isinstance(coord, (tuple, list)) or len(coord) != 2:
        return False
    try:
        x, y = float(coord[0]), float(coord[1])
    except (TypeError, ValueError):
        return False
    return 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


def step_summary(step: Dict[str, Any]) -> str:
    """生成步骤的人类可读摘要（用于进度展示）。"""
    op = step.get("op", "")
    if op == OP_LONG_INPUT:
        loc = step.get("location", "")
        tgt = step.get("target", "")
        desc = step.get("description") or ""
        head = f"{op}-{loc}-{tgt}" if loc else f"{op}-{tgt}"
        return f"{head}-{desc}" if desc else head
    if op in OPS_NEED_VISION and op != OP_DRAG:
        loc = step.get("location", "")
        tgt = step.get("target", "")
        if loc:
            return f"{op}-{loc}-{tgt}"
        return f"{op}-{tgt}"
    if op == OP_DRAG:
        return (f"拖动-{step.get('start_location', '')}-{step.get('start_target', '')}"
                f"-到-{step.get('end_location', '')}-{step.get('end_target', '')}")
    if op == OP_INPUT:
        return f"输入-{step.get('text', '')}"
    if op == OP_KEY:
        return f"按键-{step.get('key', '')}"
    if op == OP_WAIT:
        return f"等待-{step.get('seconds', 1)}"
    if op == OP_CONSULT:
        return f"咨询用户-{step.get('question', '')}"
    return step.get("raw", op)


def steps_need_vision_count(steps: List[Dict[str, Any]]) -> int:
    """统计某步骤列表共需多少次视觉调用（用于进度估算）。"""
    total = 0
    for s in steps:
        total += OP_VISION_CALLS.get(s.get("op", ""), 0)
    return total


# ---------------------------------------------------------------------------
# 长文本变量化（防止模型修改时概括原文本）
# ---------------------------------------------------------------------------

# 长于此长度的输入文本会被变量化
LONG_TEXT_THRESHOLD = 15


def assign_text_variables(steps: List[Dict[str, Any]]) -> None:
    """为步骤列表中的长输入文本分配变量名（就地修改 step dict）。

    - OP_INPUT：长度 <= LONG_TEXT_THRESHOLD 的短文本不变量；长文本替换为变量名
    - OP_LONG_INPUT：description 如果较长也分配变量，避免步骤摘要过长；
      同时标记 type_var="长输入" 以便 UI 用不同颜色按钮区分（红色=变量化长输入，
      紫色=执行时流式生成的长输入）
    - 相同全文复用同一变量名
    """
    var_map: Dict[str, str] = {}  # full_text -> var_name
    counter = 0
    for s in steps:
        op = s.get("op", "")
        if op == OP_INPUT:
            full = s.get("text") or ""
            if len(full) <= LONG_TEXT_THRESHOLD:
                continue
            if full in var_map:
                var = var_map[full]
            else:
                counter += 1
                var = f"{{输入{counter}}}"
                var_map[full] = var
            s["text_full"] = full
            s["text_var"] = var
            s["var_type"] = "input"  # 普通变量化输入
            # 注意：保留 text 字段为全文，执行时仍用全文
        elif op == OP_LONG_INPUT:
            desc = s.get("description") or ""
            if len(desc) > LONG_TEXT_THRESHOLD:
                if desc in var_map:
                    var = var_map[desc]
                else:
                    counter += 1
                    var = f"{{输入{counter}}}"
                    var_map[desc] = var
                # 用 desc_full 保存原始 description（执行时恢复用），
                # text_full 仅存模型生成的正文，避免被 _do_long_input 误粘贴
                s["desc_full"] = desc
                s["text_var"] = var
                s["var_type"] = "long_input_desc"  # 长输入描述变量化
            else:
                s["var_type"] = "long_input"  # 原生短描述，但执行时流式生成


def step_display_summary(step: Dict[str, Any]) -> str:
    """带变量化的步骤摘要（用于执行列表展示）。

    - OP_INPUT：若有 text_var 显示「输入-变量名」
    - OP_LONG_INPUT：始终以「长输入-位置-元素」显示（内容在执行时生成，不在规划阶段出现），
      若 description 过长被变量化则元素后直接跟变量名按钮（不显示长描述）。
    """
    op = step.get("op", "")
    if op == OP_INPUT:
        var = step.get("text_var")
        if var:
            return f"输入-{var}"
        return f"输入-{step.get('text', '')}"
    if op == OP_LONG_INPUT:
        # 长输入：显示「长输入-主题/要求」（或变量名）
        var = step.get("text_var")
        if var and step.get("var_type") == "long_input_desc":
            return f"长输入-{var}"
        desc = step.get("description") or ""
        return f"长输入-{desc}" if desc else "长输入"
    return step_summary(step)


def serialize_step(step: Dict[str, Any]) -> str:
    """把单个 step dict 序列化回 token 格式（如 "单击-桌面上-浏览器图标"）。

    用于编辑时显示在 QLineEdit 中，以及判断步骤是否被修改。
    若 step 有 raw 字段且与当前字段一致，优先用 raw。
    """
    op = step.get("op", "")
    # 优先用 raw（原始 token），但若 raw 缺失则按字段重建
    raw = step.get("raw")
    if raw and op in (OP_CLICK, OP_DBLCLICK, OP_RIGHT, OP_SCROLL_UP, OP_SCROLL_DOWN):
        # 空间操作：raw 即可（含 location/target）
        return raw
    if op == OP_INPUT:
        var = step.get("text_var")
        if var:
            return f"输入-{var}"
        return f"输入-{step.get('text', '')}"
    if op == OP_LONG_INPUT:
        var = step.get("text_var")
        if var and step.get("var_type") == "long_input_desc":
            return f"长输入-{var}"
        desc = step.get("description") or ""
        return f"长输入-{desc}" if desc else "长输入"
    if op == OP_KEY:
        return f"按键-{step.get('key', '')}"
    if op == OP_WAIT:
        return f"等待-{step.get('seconds', 1)}"
    if op == OP_CONSULT:
        return f"咨询用户-{step.get('question', '')}"
    if op == OP_DRAG:
        return (f"拖动-{step.get('start_location', '')}-{step.get('start_target', '')}"
                f"-到-{step.get('end_location', '')}-{step.get('end_target', '')}")
    # 兜底
    return raw or step.get("raw", op)
