# -*- coding: utf-8 -*-
"""Pro 编程智能体控制器（高级编程模型 ↔ 检查模型 协作自循环）。

在「编程模式」开启 Pro 开关后启用。工作流：
用户选择项目文件夹并在对话框输入需求 → 高级编程模型分析需求、读取项目结构、
通过 [命令符] 自主创建/修改文件、运行程序、把检查模型结果反馈给模型修复，
直到输出 [summary] 或命中收敛守卫。

命令符（由高级编程模型输出，系统解析执行并回填结果）：
- [schedule]<start>1.步骤|2.步骤</end>  规划步骤（步骤用 | 分隔，可逐步规划，步骤文字放 <start>…</end>）
- [structure]               获取项目文件夹结构
- [write:相对路径]<start>文件内容</end>  创建/覆盖文件（内容可含逗号换行）
- [read:相对路径,起,止]     读文件（带行号，正反两套索引：1 首行、-1 末行；1/-1 读取整个文件，0 已废弃）
- [search:范围,字符串]       搜索字符串，返回相对路径+行索引
- [remove:相对路径,起,止]    删除文件中指定范围内容
- [delete:相对路径]        删除文件/文件夹
- [replace:相对路径,起,止]<start>新内容</end> 替换指定范围内容（行数可不同）
- [run:相对路径]           运行程序并实时回传控制台输出
- [stop:相对路径]          停止正在运行的程序
- [check:范围]             交给检查模型只读检查，返回 相对路径:行号:错误
- [summary]<start>总结</end>           总结结果并停止，等待用户反馈

设计：命令解析采用「括号深度扫描 + 固定标量参数拆分」两阶段，天然支持命令内容
含逗号、换行与成对括号的代码；[run]/[stop] 复刻 model_client 的「后台线程 +
abort_event + 100ms 轮询」并发模型，保证停止/暂停能及时响应。

借鉴 DeepSeek Harness 智能体框架，在「观察→思考→行动→验证」自循环上加入：
- 计划守卫：已有未完成计划时，拒绝覆盖为相似的新计划，强制按剩余步骤推进（避免重复规划）；
- 验证闭环：记录最近 [run] 退出码与「编辑后是否重新验证」，[summary] 会被拦截直至
  修复后重新 [run] 验证，把「运行→报错→修复→再运行」串起来，防止修完即停；
- 重复抑制：模型调用带 frequency/presence 惩罚，只读命令结果缓存复用，
  连续同类操作超阈值会注入强制升级引导（更换定位方式，而非盲目重复）；
- 自主推进：无活跃计划时才在自旋超限后停下等用户；有计划时注入提醒并继续，提升自主性。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

# QCoreApplication/QEventLoop/QTimer 已移除：worker 线程中禁用 Qt 事件循环相关 API，
# 改用纯 Python 轮询阻塞，避免非主线程嵌套事件循环导致界面卡死。

import app_config
import model_client

# 任务整体最大模型回合数：超过强制结束并给 [summary]（可在设置中调整）
MAX_ITERATIONS = 60
# 纯正文自旋上限：无活跃计划时连续多轮无命令也无 [summary] 才停下等用户；
# 有活跃计划时尽量让模型自主推进，不轻易停车（仍在每轮重置进度）。
SPIN_LIMIT = 8
# [run] 程序输出回填历史的最大字符数，防止撑爆上下文
MAX_RUN_OUTPUT = 8000
# 内容块标记：模型把 write/replace/schedule/summary 的内容放到命令后，用 <start>…</end> 包围，避免与方括号解析冲突
_CONTENT_START = "<start>"
# 兼容 <end>（gemma 等小模型常漏写斜杠）和 </end>（正确格式）
_CONTENT_END_RE = re.compile(r"</?end\s*>")
# [run] 单次运行的最长等待（秒），超时强制终止
RUN_TIMEOUT = 300.0
# 历史裁剪：最多保留的消息数（含 system）
MAX_HISTORY = 40
# 上下文 token 预算：总输入超限时丢弃最旧的非 system 消息（防止单条大文件读取撑爆上下文）
MAX_CONTEXT_TOKENS = 60000

# 每个命令的反馈说明前缀（中文，让小模型一眼看懂这是什么结果）
_FB_DESC = {
    "structure": "该项目的结构为：",
    "read": "文件读取结果：",
    "search": "搜索结果：",
    "write": "写入完成：",
    "replace": "替换完成：",
    "remove": "删除完成：",
    "delete": "文件删除完成：",
    "run": "程序运行输出：",
    "stop": "程序已停止：",
    "check": "静态检查结果：",
    "schedule": "计划已记录：",
    "done": "步骤已完成：",
    "skip": "剩余步骤已跳过：",
    "chat": "用户回复：",
    "summary": "任务已总结：",
}
# 反馈回填的前缀标记：用于在模型原始输出中识别「模型原样复述的系统反馈」并剔除
_FB_HEAD = "【系统命令结果】"
# 流式过滤的缓冲兜底上限：被暂扣的尾部超过该值仍未解析为完整反馈时，强制整体交付，防止内容被永久抑制
_FB_HOLD_LIMIT = MAX_RUN_OUTPUT + 4000


def _estimate_tokens(text: str) -> int:
    """估算 token 数：CJK 字符按 1 token，其余每 3 字符约 1 token。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]", text))
    other = len(text) - cjk
    return cjk + (other + 2) // 3


def _content_digest(text: str) -> str:
    """内容摘要：用于区分「同路径不同内容」的写操作签名。"""
    return hashlib.md5((text or "").encode("utf-8", errors="replace")).hexdigest()[:12]


class CommandError(Exception):
    """命令执行错误（如路径越界、文件不存在）。"""


# 每个命令的「固定标量参数个数」；第 N 个逗号之后整体保留为尾部自由文本
_FIXED_ARGS: Dict[str, int] = {
    "write": 1,
    "read": 3,
    "search": 1,
    "remove": 3,
    "delete": 1,
    "replace": 3,
    "run": 1,
    "stop": 1,
}

# 可做结果比对的命令：同一签名的命令总是重新执行，但若结果与上次相同则
# 把反馈简略化为「结果与上次相同，省略」，避免重复内容占用上下文。
# 覆盖全部命令（可变命令的结果几乎不会重复，只读命令在文件未变动时常重复）。
_RESULT_CMP_CMD = {"structure", "write", "read", "search", "remove",
                   "delete", "replace", "run", "stop", "check"}
# 会改变项目文件/运行的命令：执行后需使只读缓存失效，并作为「未重新验证」标记
_MUTATING_CMD = {"write", "replace", "remove", "delete", "run"}
# 真正的编辑命令（改变文件内容，需要重新 [run] 验证）
_EDIT_CMD = {"write", "replace", "remove", "delete"}
# 全部已知命令名：用于识别「样子像命令但缺冒号/格式错误」的方括号 token，
# 以便丢弃后补一条系统反馈纠正模型，避免被静默吞掉导致下一轮上下文不更新
_KNOWN_CMD = {"structure", "schedule", "write", "read", "search",
              "remove", "delete", "replace", "run", "stop", "check", "summary",
              "done", "skip", "chat"}

# 项目结构遍历时忽略的目录/文件
_IGNORE_DIRS = {"__pycache__", ".git", ".hg", ".svn", "node_modules",
                "venv", ".venv", ".idea", "dist", "build", "__MACOSX"}

# ============================================================
# 标准 OpenAI tools（function calling）协议的工具定义
# 这是工程级 AI Agent ReAct 循环的核心——模型交替返回 tool_calls
# 和接收 tool result，直到模型不再返回 tool_calls 而是输出纯文本
# ============================================================
TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_structure",
            "description": "获取项目文件夹的完整目录结构树（含所有子目录和文件）。新项目开始时应先调用此工具了解项目布局。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取项目中某个文件的内容，返回带行号的文本。用于定位 bug、理解现有代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的相对路径，如 main.py、utils/helper.py"},
                    "start_line": {"type": "integer", "description": "起始行号（从 1 开始；支持负索引，-1 为最后一行；1 配 -1 即 1~-1 表示读取整个文件；0 已废弃不可用）"},
                    "end_line": {"type": "integer", "description": "结束行号（含；支持负索引，-1 为最后一行；读取整个文件请用 start_line=1、end_line=-1；0 已废弃不可用）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建新文件或完整覆盖已有文件的内容。用于新建文件或大幅重写。建议用 replace_file 做局部修改。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的相对路径，如 main.py、src/app.py。不存在的目录会自动创建"},
                    "content": {"type": "string", "description": "完整文件内容（纯文本），会被完整写入"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_file",
            "description": "用新内容替换文件中指定行范围的内容（行数可以不同）。用于精准修改已有代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的相对路径"},
                    "start_line": {"type": "integer", "description": "起始行号（从 1 开始，含；支持负索引，-1 为最后一行）"},
                    "end_line": {"type": "integer", "description": "结束行号（含；负索引 -1 为最后一行）"},
                    "new_content": {"type": "string", "description": "替换后的新内容（多行用 \\n）"},
                },
                "required": ["path", "start_line", "end_line", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_lines",
            "description": "删除文件中指定行范围的内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的相对路径"},
                    "start_line": {"type": "integer", "description": "起始行号（从 1 开始，含；支持负索引，-1 为最后一行）"},
                    "end_line": {"type": "integer", "description": "结束行号（含；负索引 -1 为最后一行）"},
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_item",
            "description": "删除项目中的文件或整个文件夹（不可恢复）。谨慎使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或文件夹的相对路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "在项目所有文件中搜索包含指定字符串的行，返回相对路径、行号和匹配行内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要搜索的字符串"},
                    "scope": {"type": "string", "description": "搜索范围，留空或填 all 表示整个项目，也可指定子目录"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_program",
            "description": "运行项目中的 Python 程序，实时获取控制台输出和退出码。程序路径会自动用项目中配置的解释器执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要运行的 Python 文件相对路径，如 main.py"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_program",
            "description": "停止当前正在运行的 Python 程序（如果有）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "static_check",
            "description": "让检查模型对项目中的 Python 文件做只读静态检查，找出语法、导入和逻辑错误。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "检查范围，留空或填 all 表示整个项目"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_tasks",
            "description": "把剩余工作拆分为有序步骤。步骤用数组形式传入，模型会按顺序标记进度。建议在开始复杂任务时先规划。",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {"type": "array", "items": {"type": "string"}, "description": "有序的任务步骤列表"},
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_step_done",
            "description": (
                "标记当前计划步骤已完成并自动推进到下一步。"
                "每完成一个计划步骤后必须调用此工具；"
                "系统会返回下一步的具体内容，你拿到后立即开始执行。"
                "如果当前没有活跃的计划步骤，会返回提示。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_remaining",
            "description": "放弃当前计划中剩余的所有步骤。用于发现步骤有误或遇到无法解决的困难时。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "暂停执行并向用户提问/请求补充信息。用户回复后会自动注入为下一轮反馈继续执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "想对用户说的话或问题"},
                },
                "required": ["message"],
            },
        },
    },
]

# 工具名 → 内部执行方法的映射（保持旧方法实现不变，只换入口）
_TOOL_DISPATCH = {
    "list_structure": "structure",
    "read_file": "read",
    "write_file": "write",
    "replace_file": "replace",
    "remove_lines": "remove",
    "delete_item": "delete",
    "search_code": "search",
    "run_program": "run",
    "stop_program": "stop",
    "static_check": "check",
    "plan_tasks": "schedule",
    "mark_step_done": "done",
    "skip_remaining": "skip",
    # ask_user 和 finish 在主循环中特殊处理
}

# 需要 JSON 序列化的参数映射（工具名 → (参数名 → 内部参数名)）
_TOOL_ARG_MAP = {
    "read_file": {"path": 0, "start_line": 1, "end_line": 2},
    "write_file": {"path": 0},
    "replace_file": {"path": 0, "start_line": 1, "end_line": 2},
    "remove_lines": {"path": 0, "start_line": 1, "end_line": 2},
    "delete_item": {"path": 0},
    "search_code": {"query": 0, "scope": 1},
    "run_program": {"path": 0},
    "static_check": {"scope": 0},
}


class ProProgrammingController:
    """驱动 高级编程模型 ↔ 检查模型 的 Pro 编程会话（运行在 worker 线程）。

    回调注入：
    - on_agent_text(text)：高级编程模型的「人话正文/汇报」→ 渲染为对话气泡；
    - on_console(text)：命令执行过程 / 模型原始输出 → 打印到 UI 控制台面板；
    - on_run_console(text)：( [run] 程序的实时输出 → 打印到控制台面板；
    - on_summary(text, role="summary")：最终总结或对话消息 → 渲染为气泡；
    - on_hint(s)：轻量状态提示；
    - on_context_tokens(n)：上下文（裁剪后即将发送给模型的 history）估算 token 数；
    - cancel_check()：返回 True 表示应中断（停止/暂停）。
    """

    def __init__(
        self,
        *,
        project_dir: str,
        on_agent_text: Callable[[str], None],
        on_console: Callable[[str], None],
        on_summary: Optional[Callable[..., None]] = None,
        on_run_console: Optional[Callable[[str], None]] = None,
        on_hint: Optional[Callable[[str], None]] = None,
        on_agent_stream: Optional[Callable[[str], None]] = None,
        on_schedule_plan: Optional[Callable[[list], None]] = None,
        on_schedule_done: Optional[Callable[[int], None]] = None,
        on_schedule_skip: Optional[Callable[[int], None]] = None,
        on_schedule_all_done: Optional[Callable[[], None]] = None,
        on_context_tokens: Optional[Callable[[int], None]] = None,
        on_tool_call: Optional[Callable[[str, str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._project_dir = (project_dir or "").strip().rstrip("/\\")
        self._on_agent_text = on_agent_text
        self._on_console = on_console
        self._on_summary = on_summary or (lambda _s: None)
        self._on_run_console = on_run_console or self._on_console
        self._on_hint = on_hint or (lambda _s: None)
        self._on_agent_stream = on_agent_stream or (lambda _c: None)
        self._on_schedule_plan = on_schedule_plan or (lambda _s: None)
        self._on_schedule_done = on_schedule_done or (lambda _i: None)
        self._on_schedule_skip = on_schedule_skip or (lambda _i: None)
        self._on_schedule_all_done = on_schedule_all_done or (lambda: None)
        self._on_context_tokens = on_context_tokens or (lambda _n: None)
        self._on_tool_call = on_tool_call or (lambda _f, _d: None)
        self._base_cancel = cancel_check or (lambda: False)
        self._settings = app_config.load_settings()
        # 最大模型回合数：优先使用设置中的自定义值
        try:
            self._max_iterations = int(self._settings.get("pro_max_iterations", MAX_ITERATIONS))
        except (TypeError, ValueError):
            self._max_iterations = MAX_ITERATIONS
        if self._max_iterations < 1:
            self._max_iterations = MAX_ITERATIONS

        # 模型重复惩罚与同类操作升级阈值（抑制重复输出/重复命令）
        try:
            self._frequency_penalty = float(self._settings.get("pro_frequency_penalty", 0.4))
        except (TypeError, ValueError):
            self._frequency_penalty = 0.4
        try:
            self._presence_penalty = float(self._settings.get("pro_presence_penalty", 0.1))
        except (TypeError, ValueError):
            self._presence_penalty = 0.1
        # 同类检测阈值（默认 2 次即触发，小模型更容易陷入重复模式）
        try:
            self._same_cap = max(1, int(self._settings.get("pro_same_scheme_cap", 2)))
        except (TypeError, ValueError):
            self._same_cap = 2

        # 高级编程模型视角的历史（含系统提示词与命令执行结果回填）
        self._history: List[Dict[str, str]] = []
        # 已注入历史的反馈文本：用于从模型输出中剔除其原样复述（echo）的部分
        self._injected_feedback: List[str] = []
        # 流式输出过滤缓冲：暂存可能属于"未解析完的反馈复述"的尾部
        self._stream_buf: str = ""
        # 待处理的用户输入队列
        self._pending_input: List[str] = []

        # 控制状态
        self._stopped = False
        self._await_user_after = False
        self._iter = 0
        self._spin = 0
        self._tool_chain_healthy = True   # True=最近工具链正常；False=有 JSON/执行错误待修复

        # 消息历史 — 在首次 run() 时才初始化 system prompt + 项目结构
        self._history: List[Dict[str, Any]] = []

        # [schedule] 计划可视化状态
        self._schedule_steps: List[str] = []
        self._schedule_cur = -1          # 当前"执行中"步骤下标；-1 表示无进行中步骤
        self._turn_had_cmd = False       # 本轮是否执行过命令（用于推进步骤进度）

        # 已完成的可写操作签名 → 结果（防止模型重复执行相同文件操作）
        self._done_ops: Dict[str, str] = {}

        # 交互式命令等待列表：gemma 先命令后内容时暂存
        # 每项: {"name", "body", "valid", "note"}
        self._pending_interactive: List[Dict[str, Any]] = []

        # 运行→报错→修复→再运行 验证闭环状态
        self._last_run: Optional[Dict[str, Any]] = None   # 最近一次 [run] 的 rel / exit
        self._verified_after_last_fix = True              # 上次编辑后是否已重新 [run] 验证
        # 同类命令重复检测（抑制重复输出相同思考与命令）
        self._last_sig: Optional[str] = None
        self._consec_same = 0

        # 文件操作 undo 日志：每步记录 handle 与"操作前"的复原数据
        self._undo_stack: List[Dict[str, Any]] = []
        self._undo_counter = 0
        self._last_undo_handle: Optional[str] = None  # 最近一次可写操作分配的 handle
        self._undo_lock = threading.Lock()

        # 子进程句柄与中止事件（[run]/[stop]）
        self._proc: Optional[subprocess.Popen] = None
        self._proc_abort: Optional[threading.Event] = None
        self._proc_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 对外控制（与 pro_chat 生命周期一致）
    # ------------------------------------------------------------------
    def feed_user(self, text: str) -> None:
        """用户新消息入队（worker 在 busy 时直接投递）。"""
        if text and text.strip():
            self._pending_input.append(text.strip())

    def truncate_history_before_user(self, user_text: str) -> None:
        """把 history 截断到指定用户消息之前，并清理相关状态。

        用于用户右键消息「撤销」还原项目：history 不能和真实项目状态脱节。
        匹配规则：找到 history 中 role=user 且 content 以 user_text 开头的
        那条消息，删除它及之后的所有消息；若找不到匹配则清空整个 history
        保留 system prompt。

        同时重置 schedule / done_ops / last_run 等状态，确保下次 run() 时
        模型看到的上下文与还原后的项目状态一致。
        """
        if not self._history:
            return

        target_role_user = -1
        # 从后往前找，优先匹配最后一条
        for i in range(len(self._history) - 1, -1, -1):
            m = self._history[i]
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str) and user_text and (
                    content.strip().startswith(user_text.strip())
                    or user_text.strip().startswith(content.strip())
                ):
                    target_role_user = i
                    break

        if target_role_user < 0:
            # 没找到精确匹配 → 保守清空到只留 system，避免上下文和项目状态脱节
            system_msgs = [m for m in self._history if m.get("role") == "system"]
            self._history = system_msgs if system_msgs else []
        else:
            # 保留 target 之前的所有消息（system + 更早的 user/assistant/tool）
            self._history = self._history[:target_role_user]

        # 相关状态一起重置 — 它们的记忆已经被截断了
        self._schedule_steps = []
        self._schedule_cur = -1
        self._done_ops = {}
        self._last_run = None
        self._verified_after_last_fix = True
        self._last_sig = None
        self._consec_same = 0
        self._undo_stack = []
        self._undo_counter = 0
        self._pending_interactive = []
        self._pending_input = []
        self._spin = 0
        self._tool_chain_healthy = True

    # ------------------------------------------------------------------
    # 持久化：导出 / 导入 state（用于会话保存和恢复）
    # ------------------------------------------------------------------
    def get_state(self) -> Dict[str, Any]:
        """导出完整 state（history + schedule + 控制变量），供会话持久化。"""
        return {
            "history": list(self._history),
            "schedule_steps": list(self._schedule_steps),
            "schedule_cur": self._schedule_cur,
            "done_ops": dict(self._done_ops),
            "last_run": self._last_run,
            "verified_after_last_fix": self._verified_after_last_fix,
            "undo_counter": self._undo_counter,
            "project_dir": self._project_dir,
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        """从持久化 state 恢复（用户加载了历史会话后调用）。"""
        if not isinstance(state, dict):
            return
        self._history = list(state.get("history") or [])
        self._schedule_steps = list(state.get("schedule_steps") or [])
        self._schedule_cur = int(state.get("schedule_cur", -1))
        self._done_ops = dict(state.get("done_ops") or {})
        self._last_run = state.get("last_run")
        self._verified_after_last_fix = bool(state.get("verified_after_last_fix", True))
        self._undo_counter = int(state.get("undo_counter", 0))
        # 项目目录也恢复（如果用户切换了项目，run() 里会自动追加新项目结构作为参考）
        proj = state.get("project_dir")
        if proj and os.path.isdir(proj):
            self._project_dir = proj
        # 重置运行时控制变量
        self._stopped = False
        self._spin = 0
        self._tool_chain_healthy = True
        self._pending_input = []
        self._undo_stack = []
        self._consec_same = 0
        self._last_sig = None

    @property
    def stopped(self) -> bool:
        return self._stopped

    def set_stopped(self, flag: bool = True) -> None:
        self._stopped = flag

    def stop_process(self) -> None:
        """终止正在运行的子进程（worker 清理 / 用户 [stop] / 停止时调用）。"""
        with self._proc_lock:
            p = self._proc
            abort = self._proc_abort
            self._proc = None
            self._proc_abort = None
        if abort is not None:
            abort.set()
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.kill()
            except Exception:
                pass

    def _cancel_check(self) -> bool:
        return self._base_cancel() or bool(self._pending_input)

    def _take_pending_input(self) -> Optional[str]:
        if not self._pending_input:
            return None
        if len(self._pending_input) == 1:
            return self._pending_input.pop(0)
        latest = self._pending_input[-1]
        self._pending_input.clear()
        return latest

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    # ================================================================
    # 工程级 AI Agent 主循环（标准 ReAct + OpenAI tools 协议）
    # ================================================================

    def run(self) -> None:
        """工程级 AI Agent ReAct 主循环。

        支持两种模式：
        - 首次启动（_history 为空）：初始化 system prompt + 项目结构，从零开始
        - 续跑（_history 已有内容）：复用已有 history，继续基于之前的对话推进

        当模型返回 ask_user 工具时暂停等待用户回复；
        当模型不再返回 tool_calls 而是直接输出纯文本时循环结束。
        """
        self._stopped = False
        self._iter = 0
        self._await_user_after = False
        self._spin = 0
        self._tool_chain_healthy = True

        if not self._project_dir or not os.path.isdir(self._project_dir):
            self._on_summary("未选择有效的项目文件夹，请在界面中选择项目后重试。")
            self._stopped = True
            return

        # === 初始化消息历史（仅首次启动） ===
        if not self._history:
            preset = app_config.get_preset_by_label("programmer")
            system = app_config.get_system_prompt("programmer", preset)
            self._history: List[Dict[str, Any]] = [{"role": "system", "content": system}]

            # 自动注入项目初始结构作为第一条 user 消息
            try:
                struct = self._do_structure()
            except CommandError as e:
                struct = f"(项目结构读取失败：{e})"
            self._history.append({
                "role": "user",
                "content": f"[项目初始结构]\n{struct if struct.strip() else '(项目为空)'}",
            })
            self._push_context_tokens()
        else:
            # 续跑：history 保留，只把当前项目结构追加一条 user 消息（让模型感知
            # 到可能的文件变化，但不覆盖已有的完整对话上下文）
            try:
                struct = self._do_structure()
            except CommandError:
                struct = ""
            if struct.strip():
                self._history.append({
                    "role": "user",
                    "content": f"[当前项目结构（供参考）]\n{struct}",
                })
                self._push_context_tokens()

        # === ReAct 主循环 ===
        while not self._stopped:
            if self._base_cancel():
                self._stopped = True
                break

            # 新用户输入（用户可能在 agent 暂停 ask_user 后回复）
            user_input = self._take_pending_input()
            if user_input is not None:
                self._history.append({"role": "user", "content": user_input})
                self._push_context_tokens()
                self._iter = 0
                # 重置瞬时安全状态
                self._last_run = None
                self._verified_after_last_fix = True
                self._last_sig = None
                self._consec_same = 0
                self._spin = 0  # 用户回复后清零自旋计数，给模型一次全新机会
                self._tool_chain_healthy = True
                # 直接进入下一轮模型调用
                if not self._react_turn():
                    break
                continue

            if self._await_user_after:
                if not self._wait_for_trigger():
                    break
                self._await_user_after = False
                continue

            if self._iter >= self._max_iterations:
                self._on_summary("已达最大迭代次数，已自动停止。")
                break

            # === 一轮完整的 ReAct：模型调用 → 执行 tool_calls → 回填结果 ===
            if not self._react_turn():
                break

        self._on_hint("Pro 编程会话结束")

    def _react_turn(self) -> bool:
        """执行一轮 ReAct：调用模型，处理 tool_calls。

        返回 False 表示会话应结束（模型无输出 / 取消等）。
        """
        self._iter += 1
        self._on_hint(f"高级编程模型分析中…（第 {self._iter}/{self._max_iterations} 轮）")

        # === 调用模型（带 tools schema） ===
        response = self._call_programmer_tools()
        if self._stopped or response is None:
            return False

        content = response.get("content") or ""
        tool_calls = response.get("tool_calls")
        finish_reason = response.get("finish_reason", "stop")

        # === 展示模型的自然语言正文 ===
        if content.strip():
            self._on_agent_text(content.strip())

        # === 无 tool_calls → 不是立即停止，而是触发自旋容错 ===
        if not tool_calls:
            if finish_reason == "tool_calls":
                # API 返回 tool_calls 但解析失败 → 防御性处理，让模型再试一次
                self._on_console("[警告] API 返回了 tool_calls 但解析为空")
                return True

            # 模型输出纯文字（finish_reason=stop）。区分两种情况：
            #   A. 工具链健康（_tool_chain_healthy=True）→ 真自旋，模型偷懒不说人话
            #   B. 工具链不健康 → 假自旋，模型被上一步的 JSON/执行错误吓怂了，
            #      不敢再调工具，此时不应打扰它，应该温和结束
            if not self._tool_chain_healthy:
                # 假自旋：工具链刚断，模型还没回过神来 → 直接结束，别再追问了
                self._on_console(
                    "[工具链不健康] 模型输出纯文字但最近工具调用有错误，"
                    "直接结束避免反复自旋"
                )
                self._finish_schedule()
                self._on_summary(content.strip() or "任务已完成（工具调用出错，智能体温和结束）。")
                self._stopped = True
                return False

            # 真自旋：工具链健康但模型只说不做 → 计数+1，给提醒
            self._spin += 1

            if self._spin <= SPIN_LIMIT:
                # 把模型的纯文字正文也写入 history（assistant 纯文本），
                # 让它"说完那句话"后接收系统提醒继续推进
                if content.strip():
                    self._history.append({
                        "role": "assistant",
                        "content": content.strip(),
                    })
                self._inject_spin_reminder()
                self._on_console(
                    f"[自旋容错] 第 {self._spin}/{SPIN_LIMIT} 次无工具调用，"
                    f"已提醒模型继续推进（本轮不计入迭代）"
                )
                # 不计入迭代消耗 — 自旋容错不是真正的一轮
                self._iter -= 1
                return True

            # 超过自旋限：连续多轮只给纯文字，模型真的不知道该做什么了
            self._finish_schedule()
            self._on_summary(content.strip() or "任务已完成（连续多轮未调用工具，智能体自动结束）。")
            self._stopped = True
            return False

        # === 有 tool_calls → 执行所有工具调用 ===
        # 把 assistant 消息加入 history（包含 content 和 tool_calls）
        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": content if content else None,
            "tool_calls": tool_calls,
        }
        self._history.append(assistant_msg)

        # 逐个执行 tool_call 并把结果回填为 tool 消息
        # 进入本轮前先假设工具链会健康；具体错误在循环内逐层标记为 False
        self._tool_chain_healthy = True
        self._turn_had_cmd = False       # 本轮是否执行了"实际命令"（用于自动推进计划）
        _schedule_cur_before_loop = self._schedule_cur   # 记录进入循环前的当前步骤
        for tc in tool_calls:
            if self._stopped or self._base_cancel():
                self._stopped = True
                return False

            tool_call_id = tc.get("id", f"call_{self._iter}")
            func_name = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments", "{}")

            # 解析 JSON arguments
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                # 参数不是合法 JSON，直接把原始串当作执行错误反馈
                result = f"[错误] 工具 {func_name} 的参数不是合法 JSON：{raw_args[:200]}"
                self._on_console(result)
                self._history.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result,
                })
                # 参数非法 → 工具链不健康，后续模型输出纯文字不应触发自旋容错
                self._tool_chain_healthy = False
                continue

            # === 特殊处理 ask_user（暂停等用户） ===
            if func_name == "ask_user":
                message = (args.get("message") or "").strip()
                if message:
                    self._on_summary(message, role="chat")
                else:
                    self._on_summary("（模型想与你交流，请输入你的问题/补充信息）", role="chat")
                # 执行结果也算作一条 tool 消息（便于上下文连贯）
                self._history.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": "(已暂停，等待用户回复)",
                })
                # ask_user 本身就是合法工具调用 → 工具链恢复健康
                self._tool_chain_healthy = True
                self._await_user_after = True
                return True  # 暂停，等用户回复后继续

            # === 执行工具 ===
            self._on_tool_call(func_name, self._tool_call_description(func_name, args))
            result = self._execute_tool_call(func_name, args)
            self._on_console(result.split("\n", 1)[0])  # 控制台只打一行摘要

            # === 安全检查：连续同类操作检测 ===
            sig = self._op_signature_from_tool(func_name, args)
            if sig is not None:
                if sig == self._last_sig:
                    self._consec_same += 1
                else:
                    self._consec_same = 1
                    self._last_sig = sig

            # 强制同类升级引导
            if sig is not None and self._consec_same >= self._same_cap:
                upgrade_hint = self._build_upgrade_hint(func_name)
                result += upgrade_hint
                self._consec_same = 0

            # === 去重：结果相同则简略化 ===
            dedup_key = func_name + "|" + self._args_digest(args)
            if dedup_key in self._done_ops:
                prev = self._done_ops[dedup_key]
                if prev == result:
                    result = f"[结果与上次相同，省略]\n{self._tool_alt_hint(func_name)}"
            self._done_ops[dedup_key] = result

            # 编辑/运行命令后，只读缓存作废
            if func_name in ("write_file", "replace_file", "remove_lines",
                             "delete_item", "run_program"):
                self._clear_read_cache()
            if func_name in ("write_file", "replace_file", "remove_lines", "delete_item"):
                self._verified_after_last_fix = False

            # === 回填 tool 消息 ===
            self._history.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result[:MAX_RUN_OUTPUT],
            })

            # 工具执行返回错误 → 工具链不健康
            if result.startswith("[错误]"):
                self._tool_chain_healthy = False

            # === 标记「本轮有实际命令」===
            # plan_tasks / mark_step_done / skip_remaining / ask_user 是规划控制命令，
            # 不算"实际干活"；除此之外的任何工具（写/改/删/读/跑/查）都算
            if func_name not in ("plan_tasks", "mark_step_done", "skip_remaining", "ask_user"):
                self._turn_had_cmd = True

        # === 本轮所有 tool_calls 执行完毕 ===
        self._push_context_tokens()
        # 本轮有工具调用（不管部分是否出错）→ 自旋计数器清零
        self._spin = 0
        # 工具链健康度已在 for 循环中根据实际结果维护，此处不再覆盖

        # === 自动推进计划 ===
        # 模型经常忘了显式调 mark_step_done 就直接跳到下一件事，导致计划表停在上一步。
        # 兜底：如果本轮执行了实际命令且存在活跃计划，自动推进 _schedule_cur。
        # 但若本轮内 mark_step_done 已经手动推进过（_schedule_cur > 进入循环前的值），
        # 就不再自动推进，避免跳步。
        if (self._turn_had_cmd
                and self._has_active_plan()
                and _schedule_cur_before_loop == self._schedule_cur):
            idx = self._schedule_cur
            self._on_schedule_done(idx)
            self._schedule_cur += 1
            if self._schedule_cur >= len(self._schedule_steps):
                self._schedule_steps = []
                self._schedule_cur = -1
                self._on_schedule_all_done()

        return True

    def _call_programmer_tools(self) -> Optional[Dict[str, Any]]:
        """调用高级编程模型（带 TOOLS_SCHEMA），返回解析后的响应 dict。

        返回 dict: {"content": str, "tool_calls": list|None, "finish_reason": str}
        错误时返回 None。
        """
        msgs = self._trim_history(self._history)
        self._push_context_tokens()
        chunks: List[str] = []
        self._stream_buf = ""

        # 控制台打印
        print("--- 高级编程模型 ---", flush=True)
        print("输入：")
        if msgs:
            for ti in range(len(msgs) - 1, -1, -1):
                tm = msgs[ti]
                if tm.get("role") == "user" and isinstance(tm.get("content"), str):
                    print(f"  [{ti}] user: {tm['content'][:300]}", flush=True)
                    break
        print("------", flush=True)
        print("输出：")

        def on_chunk(c: str) -> None:
            chunks.append(c)
            if c:
                self._stream_buf += c
                sys.stdout.write(c)
                sys.stdout.flush()
                self._on_agent_stream(c)

        temp = self._settings.get("temperature", 0.3)
        try:
            result = model_client.call_model(
                "programmer", msgs, stream=True,
                temperature=temp,
                frequency_penalty=self._frequency_penalty,
                presence_penalty=self._presence_penalty,
                on_chunk=on_chunk,
                cancel_check=self._cancel_check,
                tools=TOOLS_SCHEMA,
            )
        except Exception as e:
            print(f"\n--- 结束 ---\n[模型调用失败] {e}\n", flush=True)
            self._on_console(f"[模型调用失败] {e}")
            return None
        finally:
            self._stream_buf = ""
            sys.stdout.write("\n--- 结束 ---\n")
            sys.stdout.flush()

        return result

    # ================================================================
    # 工具执行层：把 tools 协议的工具名/参数映射到内部实现
    # ================================================================

    def _inject_spin_reminder(self) -> None:
        """自旋容错时注入的温和 user 提醒，引导模型继续调用工具。

        根据当前状态（是否有活跃计划、是否有待验证的编辑）生成
        差异化提醒，比"一刀切"提示更有效。
        """
        parts: List[str] = ["请继续调用工具推进任务。"]
        if self._has_active_plan():
            cur_step = self._schedule_steps[self._schedule_cur] if 0 <= self._schedule_cur < len(self._schedule_steps) else ""
            if cur_step:
                parts.append(f"当前执行中的步骤是「{cur_step}」，请继续完成。")
            else:
                parts.append("有未完成的计划步骤，请继续执行。")
        if not self._verified_after_last_fix:
            parts.append("你刚才修改了文件但还没 run_program 验证，请立即运行。")
        if self._last_run is not None and self._last_run.get("exit", 0) != 0 and self._verified_after_last_fix:
            parts.append("上一次程序运行报错了，请修复后再 run_program 验证。")
        if self._consec_same >= 2:
            parts.append("连续相同操作已被抑制，请换一种方式。")
        msg = " ".join(parts)
        self._history.append({"role": "user", "content": msg})

    @staticmethod
    def _tool_call_description(func_name: str, args: Dict[str, Any]) -> str:
        """把工具名+参数转为面向用户的中文描述（用于 UI 流式展示）。"""
        _CN = {
            "list_structure": "查看项目结构",
            "read_file": "读取文件",
            "write_file": "创建/写入文件",
            "replace_file": "替换文件内容",
            "remove_lines": "删除文件行",
            "delete_item": "删除文件/文件夹",
            "search_code": "搜索代码",
            "run_program": "运行程序",
            "stop_program": "停止程序",
            "static_check": "静态检查",
            "plan_tasks": "规划任务步骤",
            "mark_step_done": "标记步骤完成",
            "skip_remaining": "跳过剩余步骤",
            "ask_user": "向你提问",
        }
        cn = _CN.get(func_name, func_name)
        details = []
        if func_name == "read_file":
            details.append(f"{args.get('path', '')}")
        elif func_name in ("write_file", "replace_file", "delete_item", "run_program"):
            details.append(f"{args.get('path', '')}")
        elif func_name == "search_code":
            q = args.get("query", "")
            details.append(f"\"{q}\"" if q else "")
        elif func_name == "static_check":
            scope = args.get("scope", "all")
            details.append(scope if scope != "all" else "整个项目")
        elif func_name == "plan_tasks":
            steps = args.get("steps", [])
            n = len(steps) if isinstance(steps, list) else 0
            details.append(f"{n} 步")
        elif func_name == "ask_user":
            m = args.get("message", "")
            details.append(f"\"{m[:50]}\"" if m else "")
        elif func_name == "remove_lines":
            details.append(f"{args.get('path', '')}")
        detail_str = "（" + ", ".join(d for d in details if d) + "）" if details else ""
        return f"{cn}{detail_str}"

    def _execute_tool_call(self, func_name: str, args: Dict[str, Any]) -> str:
        """执行单个 tool_call，返回文本结果。

        映射表 _TOOL_DISPATCH 负责把 tools 协议的工具名映射到内部命令名，
        _TOOL_ARG_MAP 负责把 JSON 参数映射到内部位置参数。
        """
        # 计划管理特殊处理（plan_tasks 的 steps 是数组）
        if func_name == "plan_tasks":
            steps = args.get("steps", [])
            if isinstance(steps, list):
                steps_text = "\n".join(
                    f"{i + 1}. {s}" for i, s in enumerate(steps) if isinstance(s, str) and s.strip()
                )
            else:
                steps_text = str(steps)
            return self._do_schedule(steps_text)

        # 其他工具都走统一的 dispatch 映射
        internal_name = _TOOL_DISPATCH.get(func_name)
        if internal_name is None:
            return f"[错误] 未知工具：{func_name}"

        # 构造旧格式的 cmd dict（构造异常也按工具错误反馈，避免整个会话崩溃）
        try:
            cmd = self._build_internal_cmd(internal_name, args, func_name)
            if cmd is None:
                return f"[错误] 参数不足：{func_name}"

            if internal_name in ("stop",):
                self.stop_process()
                return "[stop] 已停止正在运行的程序"
            if internal_name == "done":
                return self._do_done()
            if internal_name == "skip":
                return self._do_skip()
            return self._execute(cmd)
        except CommandError as e:
            return f"[{func_name}] 执行失败：{e}"
        except Exception as e:
            return f"[{func_name}] 出错：{e}"

    def _build_internal_cmd(
        self, internal_name: str, args: Dict[str, Any], tool_name: str
    ) -> Optional[Dict[str, Any]]:
        """把 JSON 参数转为旧 _execute 方法能识别的 cmd dict。"""
        if internal_name in ("stop", "done", "skip", "structure"):
            return {"name": internal_name, "body": ""}

        arg_map = _TOOL_ARG_MAP.get(tool_name, {})
        body_parts: List[str] = []

        if internal_name in ("read", "remove", "replace", "search", "check"):
            # 这些命令的 body 是 "param1,param2,param3" 格式
            # 槽位数按参数映射中的最大下标动态分配，避免 fixed 与映射不一致时
            # ordered[pidx] 越界（如 search_code 映射 2 个参数但 fixed=1）
            max_idx = max((pidx for pidx in arg_map.values()), default=0)
            ordered = [""] * (max_idx + 1)
            for pname, pidx in arg_map.items():
                val = args.get(pname, "")
                if val == "" and pidx in (1, 2):
                    # 行号缺省：起点默认 1、终点默认 -1（1~-1 表示整个文件）；0 已废弃
                    ordered[pidx] = "1" if pidx == 1 else "-1"
                else:
                    ordered[pidx] = str(val) if val != "" else "0"
            body_parts = ordered

            if internal_name == "replace":
                # replace 需要 content
                content = args.get("new_content", "")
                return {"name": internal_name, "body": ",".join(body_parts), "content": content}

        elif internal_name in ("write",):
            # write: path 是唯一位置参数，content 从 content 字段取
            path = str(args.get("path", ""))
            content = args.get("content", "")
            return {"name": internal_name, "body": path, "content": content}

        elif internal_name in ("delete", "run"):
            path = str(args.get("path", ""))
            return {"name": internal_name, "body": path}

        # check / search 的 scope 参数
        if internal_name == "check":
            scope = str(args.get("scope", "all"))
            return {"name": internal_name, "body": scope}

        if internal_name == "search":
            query = str(args.get("query", ""))
            scope = str(args.get("scope", "all"))
            return {"name": internal_name, "body": scope + "," + query}

        return {"name": internal_name, "body": ",".join(body_parts)}

    def _op_signature_from_tool(self, func_name: str, args: Dict[str, Any]) -> Optional[str]:
        """从 tools 协议的工具名+参数生成去重/同类检测签名。"""
        if func_name == "list_structure":
            return "structure"
        if func_name in ("stop_program", "mark_step_done", "skip_remaining", "ask_user"):
            return func_name
        path = args.get("path", "")
        if func_name == "read_file":
            return f"read|{path}|{args.get('start_line', 1)}|{args.get('end_line', -1)}"
        if func_name == "write_file":
            return f"write|{path}|{_content_digest(args.get('content', ''))}"
        if func_name == "replace_file":
            return f"replace|{path}|{args.get('start_line', 1)}|{args.get('end_line', -1)}"
        if func_name == "remove_lines":
            return f"remove|{path}|{args.get('start_line', 1)}|{args.get('end_line', -1)}"
        if func_name == "delete_item":
            return f"delete|{path}"
        if func_name == "search_code":
            return f"search|{args.get('query', '')}"
        if func_name == "run_program":
            return f"run|{path}"
        if func_name == "static_check":
            return f"check|{args.get('scope', 'all')}"
        if func_name == "plan_tasks":
            steps = args.get("steps", [])
            step_text = "|".join(str(s) for s in steps) if isinstance(steps, list) else str(steps)
            return "schedule|" + _content_digest(step_text)
        return None

    @staticmethod
    def _args_digest(args: Dict[str, Any]) -> str:
        """对参数 dict 做简短摘要，用于去重 key。"""
        try:
            return json.dumps(args, sort_keys=True, ensure_ascii=False)[:80]
        except Exception:
            return str(sorted(args.items()))[:80]

    def _build_upgrade_hint(self, func_name: str) -> str:
        """连续同类操作超阈值时的升级引导。"""
        if func_name in ("list_structure", "read_file", "search_code"):
            return "\n（[系统引导] 连续只读操作无进展。别再只读了！请用 write_file 创建 / replace_file 修改 / run_program 运行。）"
        if func_name in ("write_file", "replace_file", "remove_lines", "delete_item"):
            return "\n（[系统引导] 连续编辑操作无进展。请先 read_file 看清当前文件再精准 replace；改完务必 run_program 验证。）"
        if func_name == "run_program":
            return "\n（[系统引导] 连续运行无进展。请 read_file 看报错行、static_check 让检查模型帮忙定位，再 replace_file 修复。）"
        if func_name == "plan_tasks":
            cur = self._schedule_steps[self._schedule_cur] if self._has_active_plan() else ""
            return f"\n（[系统强引导] 反复规划不执行！{'当前执行：' + cur if cur else ''}严禁继续 plan_tasks，立即执行！）"
        if func_name == "mark_step_done":
            return "\n（[系统引导] 连续 mark_step_done 但步骤未推进。请先执行实际操作再标记。）"
        return "\n（[系统引导] 连续相同操作无进展，请换一种方式解决问题。）"

    def _tool_alt_hint(self, func_name: str) -> str:
        """去重简略化时的替代建议。"""
        if func_name == "list_structure":
            return "提示：结构已看过，直接 read_file / write_file / run_program。"
        if func_name == "read_file":
            return "提示：文件已读过，用 replace_file 修改或 run_program 验证。"
        if func_name == "search_code":
            return "提示：搜索已做，去 read_file 相关文件或直接 replace_file。"
        if func_name == "run_program":
            return "提示：运行结果已知，根据输出定位后用 replace_file 修复。"
        return ""

    # ------------------------------------------------------------------
    # 以下为旧版 legacy 方法（tools 协议已接管主流程，保留以供 _execute 内部调用）
    # ------------------------------------------------------------------
    def _call_programmer(self) -> str:
        """调用高级编程模型；返回完整文本（原始输出含命令符，执行阶段才打印）。

        与自主代理模式保持一致：每次调用都把「输入（最近一条 user 文本消息，完整不打
        断）」与「输出（流式正文）」打印到原控制台，便于排查对话上下文与模型行为。
        """
        msgs = self._trim_history(self._history)
        self._push_context_tokens()
        chunks: List[str] = []
        self._stream_buf = ""
        # 块头 + 输入 + 输出 一体打印，格式对齐自主代理模式
        print("--- 高级编程模型 ---", flush=True)
        print("输入：")
        if msgs:
            _text_idx, _text_content = -1, ""
            for _ti in range(len(msgs) - 1, -1, -1):
                _tm = msgs[_ti]
                if _tm.get("role") == "user" and isinstance(_tm.get("content"), str):
                    _text_idx, _text_content = _ti, _tm["content"]
                    break
            if _text_idx >= 0:
                print(f"  [{_text_idx}] user: {_text_content}", flush=True)
            else:
                print("  (无纯文本 user 消息)", flush=True)
        print("------", flush=True)
        print("输出：")

        def on_chunk(c: str) -> None:
            chunks.append(c)
            if c:
                self._stream_buf += c
                self._drain_stream()

        temp = self._settings.get("temperature", 0.3)
        try:
            out = model_client.call_model(
                "programmer", msgs, stream=True,
                temperature=temp,
                frequency_penalty=self._frequency_penalty,
                presence_penalty=self._presence_penalty,
                on_chunk=on_chunk, cancel_check=self._cancel_check,
            )
        finally:
            # 本轮结束：交付剩余缓冲（剔除完整反馈复述，并从可能未收尾的复述残尾处截断）
            rest = self._strip_feedback(self._stream_buf)
            cut = self._fb_cut(rest)
            if cut is not None:
                rest = rest[:cut]
            if rest:
                sys.stdout.write(rest)
                sys.stdout.flush()
                self._on_agent_stream(rest)
            self._stream_buf = ""
            sys.stdout.write("\n--- 结束 ---\n")
            sys.stdout.flush()
        # 返回用于解析的完整文本；剔除反馈复述，防止其被当作模型正文或命令处理
        return self._strip_feedback("".join(chunks))

    def _strip_feedback(self, text: str) -> str:
        """从头文本中剔除模型原样复述的系统反馈（反馈为完整字符串，逐条替换即可）。"""
        for fb in self._injected_feedback:
            if fb and fb in text:
                text = text.replace(fb, "")
        return text

    @staticmethod
    def _fb_prefix_len(text: str) -> int:
        """返回 text 与反馈前缀标记 _FB_HEAD 的最长公共前缀长度（无匹配为 0）。"""
        if not text or text[0] != _FB_HEAD[0]:
            return 0
        n = min(len(text), len(_FB_HEAD))
        k = 0
        while k < n and text[k] == _FB_HEAD[k]:
            k += 1
        return k

    def _fb_cut(self, text: str) -> Optional[int]:
        """返回 text 中最靠前的「可能正在开始一个反馈」的下标。

        判定条件：从该位置起与反馈前缀标记出现 ≥2 个连续相同字符（如「[命」）或
        完整标记，即可视为反馈起点。无则返回 None。
        """
        n = len(text)
        i = 0
        while i < n:
            if text[i] == _FB_HEAD[0]:
                k = self._fb_prefix_len(text[i:])
                if k == len(_FB_HEAD) or k >= 2:
                    return i
            i += 1
        return None

    def _drain_stream(self) -> None:
        """流式剔除模型复述的反馈，并把确认安全的前缀交付到控制台与思考折叠框。

        为衔接跨块被拆开的反馈标记，始终在缓冲尾部保留一段文本（keep-tail），
        一旦检测到「[命」等反馈前缀即暂扣其后内容，待完整反馈到达后被剔除。
        """
        buf = self._stream_buf
        stripped = self._strip_feedback(buf)
        cut = self._fb_cut(stripped)
        keep = len(_FB_HEAD) - 1  # 跨块衔接标记所需保留的尾部长度
        if len(buf) > _FB_HOLD_LIMIT:
            safe, carry = stripped, ""
        elif cut is not None:
            safe, carry = stripped[:cut], stripped[cut:]
        else:
            split = max(0, len(stripped) - keep)
            safe, carry = stripped[:split], stripped[split:]
        self._stream_buf = carry
        if safe:
            sys.stdout.write(safe)
            sys.stdout.flush()
            self._on_agent_stream(safe)

    # ------------------------------------------------------------------
    # 小模型输出预处理（gemma 等常犯的格式错误）
    # ------------------------------------------------------------------

    # 1) 移除所有 gemma 风格的原生标记
    #    gemma 可能输出：<|tool_call> (不闭合!), <|tool_call_start|>, <|"|>, <|end|> 等
    #    策略：
    #    a) 先移除完整闭合的 <|xxx|>
    #    b) 再移除不闭合的 <|xxx> （注意：不要跨过方括号，只匹配 gemma token 常见的字符）
    _GEMMA_PIPE_TOKEN_RE = re.compile(r"<\|[a-zA-Z0-9_\-*&|]*\|>")
    _GEMMA_PIPE_UNCLOSED_RE = re.compile(r"<\|[a-zA-Z0-9_\-*&|>]*")

    # 2) gemma 的 call 命令格式：call:write:main.py<start> → [write:main.py]<start>
    #    或 call:run:main.py → [run:main.py]
    _GEMMA_CALL_CMD_RE = re.compile(
        r"call\s*:\s*"                       # call: 或 call :
        r"(write|read|replace|remove|delete|run|stop|search|structure|check|schedule|summary|done|skip|chat)"  # 命令名
        r"(?::\s*([^<\s]+))?"                 # 可选的冒号+参数（不含 < 或空白）
        r"(<start>)?"                         # 可选的 <start> 内容标记
        r"(.*?(?=<start>|$|call:|write:|run:))"  # 可选的内联内容（直到下一个标记或换行）
    )

    # 3) 裸命令行匹配：行首就是 done / skip / summary / chat
    _BARE_CMD_RE = re.compile(
        r"^\s*(done|skip|summary|chat)\s*[:：]?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    # 4) 带内容的裸 chat/summary
    _BARE_CMD_WITH_TEXT_RE = re.compile(
        r"^\s*(chat|summary)\s*[:：\s]+(.+)$",
        re.IGNORECASE | re.MULTILINE,
    )

    def _preprocess_model_output(self, raw: str) -> str:
        """预处理模型输出，兼容 gemma 等小模型常见的格式错误。

        gemma 常见格式问题：
        - 用 <|tool_call>call:write:main.py<start> 代替 [write:main.py]<start>
        - 用 <|xx|> 当闭合标记（<|"|>, <|end|> 等）
        - 用 <br> 当换行
        - 裸 done / skip 不带方括号
        - write 的闭合标记漏斜杠 <end>
        - schedule 步骤里把 </end> 当换行
        """
        if not raw:
            return raw

        # === Step 1: gemma 格式转标准格式 ===
        # 先把 call:xxx:yyy 转成 [xxx:yyy]（要在移除 <|... 之前做，否则 <|tool_call> 会干扰）
        raw = self._convert_gemma_call_commands(raw)

        # gemma 有时把闭合标记写成 </start>（应该是 </end>）——统一修正
        raw = re.sub(r"</start\s*>", "</end>", raw, flags=re.IGNORECASE)

        # 移除 gemma 原生标记（闭合 + 不闭合）
        raw = self._GEMMA_PIPE_TOKEN_RE.sub("", raw)          # <|xxx|>
        raw = self._GEMMA_PIPE_UNCLOSED_RE.sub("", raw)       # <|xxx  (不闭合)
        # 清理可能残留的孤立 | 或 >（保守处理）
        raw = re.sub(r"<\|", "", raw)
        raw = re.sub(r"\|>", "", raw)

        # === Step 2: gemma 有时把 </end> 当换行用（schedule 步骤里每步后面加 </end>）===
        # 在 scan 之前清理：如果 </end> 后面紧跟数字或换行，且前面是步骤文本，把 </end> 替换成 \n
        # 但更安全的做法：直接移除孤立的 </end>（不是跟在 <start> 配对里的）
        # 实际上我们的正则已经兼容 <end> 和 </end>，所以只需要处理 schedule 步骤里的误用
        # 这里简单移除 schedule 内容块内部孤立的 </end>/<end> 实例
        raw = self._fix_schedule_ends_inline(raw)

        # === Step 3: HTML 换行 → \n ===
        raw = re.sub(r"<br\s*/?\s*>", "\n", raw, flags=re.IGNORECASE)
        raw = re.sub(r"<\s*br\s*>", "\n", raw, flags=re.IGNORECASE)

        # === Step 4: 裸命令转方括号 ===
        def _fix_bare_with_text(m):
            cmd = m.group(1).lower()
            text = m.group(2).strip()
            return f"[{cmd}]<start>{text}</end>"
        raw = self._BARE_CMD_WITH_TEXT_RE.sub(_fix_bare_with_text, raw)
        def _fix_bare_cmd(m):
            return f"[{m.group(1).lower()}]"
        raw = self._BARE_CMD_RE.sub(_fix_bare_cmd, raw)

        # === Step 5: 确保每个 <start> 都有对应的闭合标记 ===
        raw = self._fix_unclosed_content_blocks(raw)

        return raw

    def _convert_gemma_call_commands(self, raw: str) -> str:
        """把 gemma 的 call:xxx 格式命令转换成标准 [xxx] 方括号格式。

        gemma 格式：call:write:main.py<start> 或 call:done<start></end>
        转换后：    [write:main.py]<start> 或 [done]<start></end>

        必须先处理带参数的（更长），再处理无参数的。
        """
        ALL_CMDS = (
            "write", "read", "replace", "remove", "delete", "run", "stop",
            "search", "structure", "check", "schedule", "summary", "done", "skip", "chat",
        )
        # 先处理带参数的（call:cmd:param）
        for cmd in ALL_CMDS:
            raw = re.sub(
                rf"call\s*:\s*{cmd}\s*:\s*([^<\s]+)",
                f"[{cmd}:" + r"\1]",
                raw,
                flags=re.IGNORECASE,
            )
        # 再处理无参数的（call:cmd）
        for cmd in ALL_CMDS:
            raw = re.sub(
                rf"call\s*:\s*{cmd}\b",
                f"[{cmd}]",
                raw,
                flags=re.IGNORECASE,
            )
        return raw

    def _fix_schedule_ends_inline(self, raw: str) -> str:
        """gemma 在 schedule 步骤里把 </end> 当换行标记。

        例如：[schedule]<start>1. 步骤一</end>2. 步骤二</end>3. 步骤三</end></start>
        这导致 content 块在第一个 </end> 就闭合了。

        修复：用贪婪匹配找到 [schedule]<start>... 到最后一个 </?end> 的整块，
        把内部所有的 </end> / <end> 替换成换行，再正确闭合。
        """
        # 贪婪匹配：从 [schedule]<start> 到最后一个 </?end>
        pattern = re.compile(
            r"(\[schedule\]\s*<start>)(.*)(</?end\s*>)",
            re.DOTALL | re.IGNORECASE,
        )
        result = raw
        m = pattern.search(raw)
        if not m:
            return result
        prefix = m.group(1)  # [schedule]<start>
        body = m.group(2)    # 全部内容（含内部多个 </end>）
        # 把 body 里所有的 </end> / <end> 替换成换行
        body_fixed = re.sub(r"</?end\s*>", "\n", body, flags=re.IGNORECASE)
        # 清理多余空行
        body_fixed = re.sub(r"\n{3,}", "\n\n", body_fixed)
        # 正确闭合
        replacement = prefix + body_fixed.strip() + "</end>"
        result = raw[:m.start()] + replacement + raw[m.end():]
        return result

    def _fix_unclosed_content_blocks(self, raw: str) -> str:
        """确保每个 <start> 都有对应的闭合标记。

        gemma 有时会写出孤立的 <start> 而不闭合，或用奇怪标记如 <|"|> 代替。
        这里在字符串末尾补齐缺失的 </end>。
        """
        start_count = raw.count("<start>")
        # 用兼容正则找所有闭合标记
        close_count = len(re.findall(r"</?end\s*>", raw, re.IGNORECASE))
        missing = start_count - close_count
        if missing > 0:
            raw += "</end>" * missing
        return raw

    # ------------------------------------------------------------------
    # 命令解析（两阶段）
    # ------------------------------------------------------------------
    @staticmethod
    def _scan_items(raw: str) -> List[Dict[str, Any]]:
        """按出现顺序扫描出两类 token：cmd（[命令:...]）与 content（<start>…<end>）。

        返回项形如 {"type":"cmd","name","body","span"} 或
        {"type":"content","value","span"}。内容块优先于命令被识别，避免正文里的
        括号和逗号干扰命令解析。
        """
        items: List[Dict[str, Any]] = []
        i, n = 0, len(raw)
        while i < n:
            if raw[i] == "<" and raw.startswith(_CONTENT_START, i):
                m = _CONTENT_END_RE.search(raw, i + len(_CONTENT_START))
                if m:
                    value = raw[i + len(_CONTENT_START):m.start()]
                    if value.startswith("\n"):
                        value = value[1:]
                    if value.endswith("\n"):
                        value = value[:-1]
                    items.append({
                        "type": "content",
                        "value": value,
                        "span": (i, m.end()),
                    })
                    i = m.end()
                    continue
                i += len(_CONTENT_START)
                continue
            if raw[i] == "[":
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    if raw[j] == "[":
                        depth += 1
                    elif raw[j] == "]":
                        depth -= 1
                    if depth == 0:
                        break
                    j += 1
                if j >= n or depth != 0:
                    i += 1
                    continue
                body = raw[i + 1:j]
                name, sep, rest = body.partition(":")
                name = name.strip().lower()
                if sep and name:
                    items.append({"type": "cmd", "name": name, "body": rest,
                                  "span": (i, j)})
                elif body.strip().lower() in ("structure", "stop", "summary", "schedule", "done", "skip", "chat"):
                    items.append({"type": "cmd", "name": body.strip().lower(),
                                  "body": "", "span": (i, j)})
                else:
                    # 容错：样子像命令（开头的词是已知命令名）但缺冒号/格式错误，
                    # 记为 badcmd，由会话层丢弃并补一条系统反馈纠正模型
                    _lead = re.split(r"[\s,:]", body.strip(), 1)[0].strip("[]").lower()
                    if _lead in _KNOWN_CMD:
                        items.append({"type": "badcmd", "name": _lead,
                                      "body": body, "span": (i, j)})
                i = j + 1
                continue
            i += 1
        return items

    @staticmethod
    def _text_between(raw: str, a_end: int, b_start: int) -> str:
        """取 (a_end, b_start) 开区间内的原始文本，用于判断命令与内容块之间是否仅空白。"""
        return raw[a_end + 1:b_start]

    @staticmethod
    def _scan_commands(raw: str) -> List[Dict[str, Any]]:
        """括号深度扫描切出所有 [命令:...] token。

        支持内容含逗号、换行与成对括号的代码（如 [1,2]、{a:b} 不影响，
        只要方括号成对平衡）。未闭合的 [ 视为正文。
        """
        cmds: List[Dict[str, Any]] = []
        i, n = 0, len(raw)
        while i < n:
            if raw[i] != "[":
                i += 1
                continue
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if raw[j] == "[":
                    depth += 1
                elif raw[j] == "]":
                    depth -= 1
                if depth == 0:
                    break
                j += 1
            if j >= n or depth != 0:
                # 未闭合：当作正文，继续向后扫描
                i += 1
                continue
            body = raw[i + 1:j]
            name, sep, rest = body.partition(":")
            name = name.strip().lower()
            if sep and name:
                cmds.append({"name": name, "body": rest, "span": (i, j)})
            elif body.strip().lower() in ("structure", "stop", "summary", "schedule", "done", "skip", "chat"):
                cmds.append({"name": body.strip().lower(), "body": "", "span": (i, j)})
            i = j + 1
        return cmds

    @staticmethod
    def _split_args(name: str, body: str):
        """按「固定标量参数个数」切分参数；第 N 个逗号之后整体为尾部自由文本。"""
        fixed = _FIXED_ARGS.get(name, 0)
        parts = body.split(",", fixed) if fixed else [body]
        args = [p.strip() for p in parts[:fixed]]
        tail = parts[fixed].strip() if len(parts) > fixed else ""
        return args, tail

    @staticmethod
    def _extract_prose(raw: str, spans: List[tuple]) -> str:
        """去掉命令 token 后的正文（供对话气泡展示），压缩空白。"""
        out: List[str] = []
        last = 0
        for s, e in sorted(spans):
            out.append(raw[last:s])
            last = e + 1
        out.append(raw[last:])
        text = "".join(out)
        return re.sub(r"[\s\u3000]+", " ", text).strip()

    # ------------------------------------------------------------------
    # 命令执行分发
    # ------------------------------------------------------------------
    def _execute(self, cmd: Dict[str, Any]) -> str:
        name = cmd["name"]
        args, tail = self._split_args(name, cmd["body"])
        if name == "structure":
            return self._do_structure()
        if name == "schedule":
            return self._do_schedule(cmd.get("content") or tail)
        if name == "write":
            self._require(args, 1)
            content = cmd.get("content") or tail
            return self._do_write(args[0], content)
        if name == "read":
            self._require(args, 3); return self._do_read(args[0], args[1], args[2])
        if name == "search":
            self._require(args, 1); return self._do_search(args[0], tail)
        if name == "remove":
            self._require(args, 3); return self._do_remove(args[0], args[1], args[2])
        if name == "delete":
            self._require(args, 1); return self._do_delete(args[0])
        if name == "replace":
            self._require(args, 3)
            content = cmd.get("content") or tail
            return self._do_replace(args[0], args[1], args[2], content)
        if name == "run":
            self._require(args, 1); return self._do_run(args[0])
        if name == "stop":
            self.stop_process(); return "[stop] 已停止正在运行的程序"
        if name == "done":
            return self._do_done()
        if name == "skip":
            return self._do_skip()
        # chat/summary 已在调用处被拦截处理并提前返回，不会进入 _execute
        if name == "check":
            return self._do_check(tail or "all")
        return f"[{name}] 未知命令"

    @staticmethod
    def _require(args: List[str], n: int) -> None:
        if len(args) < n:
            raise CommandError(f"参数不足（需要 {n} 个）")

    @staticmethod
    def _normalize_line_range(
        start: int, end: int, total: int
    ) -> Tuple[int, int, List[str]]:
        """把 (start, end) 归一化到 [1, total] 范围内。

        正反两套行号索引（行号从 1 开始，没有 0 号行）：
        - 正索引：1 = 第一行，2 = 第二行，……
        - 负索引：-1 = 最后一行，-2 = 倒数第二行，……
        - 1~-1 表示读取整个文件（起点 1、终点 -1）。
        - 0 已废弃（旧约定 0/0 表示全部），传入 0 直接报错并给出引导。
        起点/终点超出范围自动截断。

        返回 (norm_start, norm_end, adjustments)，adjustments 是长度为 0 或 2 的列表，
        记录被修正的情况，用于生成警告提示。
        """
        total = max(total, 1)
        adjustments: List[str] = []

        def _resolve(val: int, label: str) -> int:
            # 0 已废弃：行号从 1 开始，不存在 0 号行
            if val == 0:
                raise CommandError(
                    f"行号 {label} 不能为 0：行号从 1 开始（正索引 1、2、3…；"
                    "负索引 -1 为最后一行），读取整个文件请用 1 和 -1（即 1~-1），"
                    "0 已废弃不再使用。"
                )
            # 负索引 → 转正数：-1 对应 total，-2 对应 total-1，...
            if val < 0:
                resolved = val + total + 1
                if resolved < 1:
                    adjustments.append(
                        f"（{label} {val} 修正为 {resolved}，已截断到 1）"
                    )
                    resolved = 1
                elif resolved > total:
                    resolved = total
                return resolved
            # 正数超范围
            if val > total:
                adjustments.append(
                    f"（{label} {val} 超出总行数 {total}，已截断到 {total}）"
                )
                return total
            return val

        s = _resolve(start, "起始行")
        e = _resolve(end, "结束行")

        # 保证 s <= e
        if s > e:
            adjustments.append(f"（起始行 {s} > 结束行 {e}，已交换）")
            s, e = e, s

        return s, e, adjustments

    # ------------------------------------------------------------------
    # [schedule] 计划可视化 与 命令去重/下一步引导
    # ------------------------------------------------------------------
    # 去重规则见模块级常量：_DEDUP_CMD（可缓存）、_MUTATING_CMD（破坏缓存）、_EDIT_CMD（编辑）。
    def _do_schedule(self, tail: str) -> str:
        """解析 [schedule] 计划步骤并通知 UI 创建可视化的步骤清单。

        严格防循环策略：
        1. 活跃计划期间若新 schedule 与旧计划相似度 > 60%，直接拒绝并强制引导执行；
        2. 活跃计划期间若模型已连续 2 次 schedule 而无 write/run/read 等实际操作，
           直接拒绝并强制引导"必须先执行第 N 步"；
        3. 新方向（全新计划）允许覆盖，但给简短执行引导。
        """
        # 严格按「序号 + 点」分隔步骤（如 1. 2. 3.），不依赖 | 、 等其他符号
        steps = []
        for p in re.split(r"(?=\d+\s*[.])", tail):
            m = re.match(r"^\s*\d+\s*[.]\s*(.*)$", p, re.S)
            if m and m.group(1).strip():
                steps.append(m.group(1).strip())
        if not steps:
            # 解析失败（可能模型没按格式来）：直接给简短引导
            if self._has_active_plan():
                cur = self._schedule_steps[self._schedule_cur]
                return f"[plan: active] 当前有未完成计划：{cur}\n请用 write/run/read 执行；完成后用 [done]。"
            return "[schedule] 未识别到步骤（需要 1. 2. 3. 格式）。请先 [structure] 了解项目再用 write/run 等命令操作。"

        # === 防循环：活跃计划期间严格检查 ===
        if self._has_active_plan():
            # 1) 相似度检测：与旧计划的剩余步骤比对
            remaining = self._remaining_steps()
            overlap = self._plan_overlap(remaining, steps)
            # 2) 连续 schedule 计数（最近 N 次都是 schedule 类型命令）
            consecutive = self._count_recent_plan_only()
            if overlap > 0.6 or consecutive >= 2:
                cur = self._schedule_steps[self._schedule_cur]
                # 拒绝 + 强制引导执行
                return (
                    f"[plan: rejected] 检测到你在有活跃计划时反复规划相同内容，已拒绝本次 schedule。\n"
                    f"当前执行中：第 {self._schedule_cur + 1}/{len(self._schedule_steps)} 步「{cur}」\n"
                    "严禁继续 schedule！请立即用 write/run/read 执行当前步骤；"
                    "完成后用 [done]；若确实要放弃请用 [skip]。"
                )
            # 3) 全新方向：允许覆盖，但要求立即执行
            self._on_schedule_skip(self._schedule_cur)
            self._schedule_steps = steps
            self._schedule_cur = 0
            self._on_schedule_plan(steps)
            return (
                f"[plan: replaced] {len(steps)} 步，已替换旧计划。\n"
                f"立即执行第 1 步：{steps[0]}\n"
                "完成后用 [done]，禁止再次 schedule。"
            )

        # === 无活跃计划：正常设置 ===
        self._schedule_steps = steps
        self._schedule_cur = 0
        self._on_schedule_plan(steps)
        return (
            f"[plan: {len(steps)}步] 1. {steps[0]}\n"
            "请用 write/run/read 执行；完成后用 [done]。"
        )

    def _count_recent_plan_only(self) -> int:
        """统计最近 history 中连续只发 schedule 而无 write/run/read 等实际操作的次数。

        用于检测「反复规划不执行」的循环模式——扫描最后几条 user 反馈，
        如果系统连续多次返回 schedule 结果且没有 write/run/read 被执行过，
        说明模型陷入了规划循环。
        """
        count = 0
        for i in range(len(self._history) - 1, max(0, len(self._history) - 10), -1):
            msg = self._history[i]
            if msg.get("role") != "user":
                break
            content = msg.get("content", "")
            if "【系统命令结果】" not in content:
                break
            # 提取系统命令结果的首行，看是什么命令的反馈
            first_line = content.split("\n", 1)[1] if "\n" in content else content
            if "[已记录计划]" in first_line or "[plan:" in first_line:
                count += 1
            elif any(tok in first_line for tok in ("[write", "[run", "[read", "[replace", "[delete", "[structure", "[check")):
                # 出现过实际操作命令的反馈 → 停止计数
                break
            else:
                break
        return count

    def _has_active_plan(self) -> bool:
        """是否存在尚未执行完成的计划。"""
        return bool(self._schedule_steps) and 0 <= self._schedule_cur < len(self._schedule_steps)

    def _remaining_steps(self) -> List[str]:
        """返回计划中尚未执行的步骤列表。"""
        if not self._schedule_steps:
            return []
        if self._schedule_cur < 0:
            return list(self._schedule_steps)
        return list(self._schedule_steps[self._schedule_cur:])

    @staticmethod
    def _plan_overlap(a: List[str], b: List[str]) -> float:
        """两个计划步骤列表的重叠度（Jaccard 近似），用于识别「重复规划相似步骤」。"""
        if not a or not b:
            return 0.0
        sa, sb = set(a), set(b)
        return len(sa & sb) / max(len(sa), len(sb))

    def _do_done(self) -> str:
        """[done]：由模型显式发送的「当前步骤已完成」标志。

        推进计划表到下一步，并给模型一条**包含具体下一步内容 + 行动指令**的 tool 消息，
        让模型拿到后立即知道"我现在要干什么"，而不是只说"请立即执行"。
        """
        if not self._schedule_steps or not (0 <= self._schedule_cur < len(self._schedule_steps)):
            return "[done] 当前没有进行中的计划步骤"
        idx = self._schedule_cur
        self._on_schedule_done(idx)  # UI：当前步打钩并推进下一步转圈
        self._schedule_cur += 1
        if self._schedule_cur < len(self._schedule_steps):
            cur = self._schedule_steps[self._schedule_cur]
            # 构造包含"下一步内容 + 建议工具 + 必须 mark_step_done"的指令
            hint = self._build_step_execution_hint(cur)
            return (
                f"[done] ✔ 第 {idx + 1} 步已完成。\n"
                f"➡️ 当前第 {self._schedule_cur + 1}/{len(self._schedule_steps)} 步：「{cur}」\n"
                f"{hint}\n"
                f"⚠ 完成本步后必须再次调用 mark_step_done 推进计划表。"
            )
        self._schedule_steps = []
        self._schedule_cur = -1
        return "[done] ✔ 全部步骤完成。请调用 summary 工具进行总结。"

    @staticmethod
    def _build_step_execution_hint(step: str) -> str:
        """根据步骤内容给出建议工具组合（启发式，帮助模型快速决策）。"""
        s = step.lower()
        if any(k in s for k in ("创建", "新建", "create", "new", "写", "write")):
            return "建议工具：write_file → run_program"
        if any(k in s for k in ("修改", "替换", "修复", "fix", "replace", "改")):
            return "建议工具：read_file（看上下文）→ replace_file → run_program"
        if any(k in s for k in ("运行", "执行", "测试", "run", "test", "调试", "debug")):
            return "建议工具：run_program（查看结果/报错）"
        if any(k in s for k in ("读取", "查看", "分析", "read", "analyze", "list")):
            return "建议工具：list_structure / read_file / grep_in_files"
        if any(k in s for k in ("删除", "清理", "remove", "delete", "删")):
            return "建议工具：delete_file"
        if any(k in s for k in ("搜索", "查找", "search", "find")):
            return "建议工具：grep_in_files"
        return "建议：根据步骤内容选择合适工具执行"

    def _do_skip(self) -> str:
        """[skip]：跳过整个剩余计划。

        当发现当前步骤（及后续）有误、不应继续执行时，模型输出 [skip] 放弃当前及
        剩余全部步骤；UI 将跳过部分整体打叉（✗），并清除计划，模型可重新规划或总结。
        """
        if not self._schedule_steps or not (0 <= self._schedule_cur < len(self._schedule_steps)):
            return "[skip] 当前没有进行中的计划步骤"
        idx = self._schedule_cur
        skipped = len(self._schedule_steps) - idx
        self._on_schedule_skip(idx)  # UI：从当前步起（含后续）全部打叉
        self._schedule_steps = []
        self._schedule_cur = -1
        return (
            f"[skip] ✗ 已跳过剩余 {skipped} 步。"
            "请确认目标后重新 [schedule] 规划，或直接 [summary] 结束。"
        )

    def _finish_schedule(self) -> None:
        """[summary] 时把剩余所有步骤置为完成（全部打钩）。"""
        if self._schedule_steps:
            self._on_schedule_all_done()
        self._schedule_steps = []
        self._schedule_cur = -1

    def _op_signature(self, cmd: Dict[str, Any]) -> Optional[str]:
        """返回命令签名；用于「结果相同则省略」的比对 + 同类重复检测。

        覆盖 _RESULT_CMP_CMD 内全部命令，为每条命令生成稳定签名；
        schedule/done/skip 等规划控制命令也返回签名参与连续同类检测，
        防止小模型反复输出相同规划命令而不执行任何实际操作。
        """
        name = cmd["name"]
        if name in ("schedule",):
            # schedule：用内容摘要做签名，检测重复规划
            content = cmd.get("content") or ""
            if not content:
                content = self._split_args(name, cmd.get("body", ""))[1] or cmd.get("body", "")
            return "~schedule|" + _content_digest(content)
        if name in ("done", "skip", "chat", "summary"):
            # done/skip/chat/summary：命令类型本身就是签名
            return "~" + name
        if name not in _RESULT_CMP_CMD:
            # 其他不在比对列表内的命令（replace/remove/delete 等）
            if name in ("replace", "remove", "delete"):
                args = self._split_args(name, cmd.get("body", ""))[0]
                key = "|".join(args[:3]) if args else ""
                return "~" + f"{name}|{key}"
            return None
        args = self._split_args(name, cmd.get("body", ""))[0]
        if name == "write":
            content = cmd.get("content") or (self._split_args(name, cmd.get("body", ""))[1] or "")
            key = (args[0] if args else "") + "|" + _content_digest(content)
        elif name == "read":
            key = "|".join(args[:3]) if args else ""
        elif name == "search":
            key = (args[0] if args else "") + "|" + (self._split_args(name, cmd.get("body", ""))[1] or "")
        elif name == "run":
            key = args[0] if args else ""
        elif name == "stop":
            key = args[0] if args else ""
        elif name == "check":
            key = self._split_args(name, cmd.get("body", ""))[1] or "all"
        elif name in ("remove", "replace"):
            key = "|".join(args[:3]) if args else ""
        elif name == "delete":
            key = args[0] if args else ""
        else:  # structure
            key = ""
        return f"{name}|{key}"

    def _clear_read_cache(self) -> None:
        """编辑/运行命令执行后，清空只读类命令的缓存，避免模型读到过期内容。"""
        keep = {}
        for k, v in self._done_ops.items():
            prefix = k.split("|", 1)[0].lstrip("$~")
            if prefix not in ("read", "search", "structure"):
                keep[k] = v
        self._done_ops = keep

    def _summary_gate(self) -> Optional[str]:
        """summary 拦截：若上次 [run] 仍失败且修复后被验证过却没有重新运行，要求先验证。

        目的是把「运行→报错→修复→再运行」闭环强制串起来，避免模型修完 bug 立即 [summary] 自动停。
        返回需回填给模型的引导文本；返回 None 表示允许 [summary]。
        """
        last = self._last_run
        if not last:
            return None
        if last.get("exit", 0) == 0:
            return None
        if self._verified_after_last_fix:
            return None
        return (
            f"（系统拦截了你的 [summary]。）上次 [run:{last.get('rel', '')}] 退出码为 "
            f"{last.get('exit')}，你在此后又修改了代码但尚未重新 [run] 验证。"
            "修复脚本类问题后必须先 [run] 确认运行通过，再 [summary] 结束；"
            "若你确认该报错不影响需求完成或有其他理由停止，请先 [run] 一次以确认当前实际状态后再总结。"
        )

    @staticmethod
    def _build_feedback(name: str, result: str, dup: bool) -> str:
        """给模型的结果反馈：包含执行结果（带中文说明前缀）、下一步决策引导（含修补重复/收尾）。"""
        desc = _FB_DESC.get(name, f"[{name}] 结果：")
        if dup:
            # 引导语放在结果之前，避免大结果被 MAX_RUN_OUTPUT 截断时把引导语一并截掉
            return (
                "（本次操作与此前已成功执行的操作完全相同，已跳过，请勿重复执行，也不要复述此结果。）\n"
                f"{desc}{result}"
            )
        return (
            f"{desc}{result}"
        )

    def _inject_feedback(self, body: str, dup: bool = False) -> None:
        """把一条系统反馈回填为 user 消息注入历史（含反馈复述硬过滤标记与上下文去重）。

        供命令执行结果与各类拦截/引导消息统一复用，保证 history 结构与反馈硬过滤一致。
        dup=True 表示本结果来自缓存复用（真正的重复命令），只有此时才允许
        「结果与上一条完全相同」的合并省略；run/check 等会真实重新执行的命令，
        即使两次输出恰好相同也必须原样回填，否则模型会误以为结果未变化，
        破坏「运行→修复→再运行」验证闭环。
        """
        # 不再追加固定的操作指令尾部，让提示词更灵活，降低模型重复输出相同内容的概率
        result_content = (
            "{_head}\n"
            "{body}"
        ).format(
            _head=_FB_HEAD,
            body=body,
        )
        # 上下文去重：仅当结果来自缓存复用（dup）且与历史某条消息完全相同时，才合并省略
        if dup and any(m.get("content") == result_content for m in self._history):
            result_content = (
                "{_head}\n"
                "（结果与上一条上下文完全相同，已合并省略；请直接参考上文，"
                "不要复述此内容。）\n"
                "请据此继续输出下一步行动。"
            ).format(_head=_FB_HEAD)
        self._history.append({"role": "user", "content": result_content})
        self._injected_feedback.append(result_content)
        self._history = self._trim_history(self._history)
        self._push_context_tokens()

    def _safe_join(self, rel: str) -> str:
        """把相对路径安全拼到项目根，拒绝路径越界。"""
        base = os.path.realpath(self._project_dir)
        full = os.path.realpath(os.path.join(base, rel))
        if not (full == base or full.startswith(base + os.sep)):
            raise CommandError(f"路径越界被拒绝：{rel}")
        return full

    def _read_full(self, abspath: str) -> str:
        try:
            with open(abspath, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as e:
            raise CommandError(str(e))

    # ---- 具体命令实现 ----
    def _suggest_alternative_action(self, cmd_name: str) -> Optional[str]:
        """当某命令被去重简略化时，给出建设性替代建议。

        目的：防止小模型陷入 structure→structure→structure 的死循环。
        structure 被去重 → 说明项目目录已经看过了 → 建议 run 或 read。
        """
        if cmd_name == "structure":
            # 扫描项目里有没有明显可运行文件
            runnable = self._find_runnable_files()
            if runnable:
                if len(runnable) == 1:
                    return f"（提示：项目里已经有 {runnable[0]}，你不需要再看结构了，直接试试 [run:{runnable[0]}]）"
                return f"（提示：项目里已经有 {len(runnable)} 个可运行文件 {runnable[:3]}，不需要再看结构了，直接 [run] 或 [read] 吧）"
            # 项目里有 Python 文件但没 main
            py_files = self._find_files_by_ext(".py", limit=5)
            if py_files:
                return f"（提示：项目里有 {len(py_files)} 个 .py 文件如 {py_files[:3]}，不需要再看结构了，挑一个 [run] 或 [read]）"
            return "（提示：结构已经看过了，下一步用 write 创建代码文件，或 summary 结束）"
        if cmd_name == "read":
            return "（提示：文件内容已经读过了，下一步用 replace 精准修改，或 run 运行，或 check 让检查模型帮你找 bug）"
        if cmd_name == "search":
            return "（提示：搜索结果已经看过了，下一步去 read 相关文件，或直接 replace 修改）"
        return None

    def _find_runnable_files(self) -> List[str]:
        """找到项目里最像"入口文件"的文件列表（main.py / app.py / index.py 等）。"""
        if not os.path.isdir(self._project_dir):
            return []
        names = []
        for dirpath, _, filenames in os.walk(self._project_dir):
            for fn in filenames:
                low = fn.lower()
                if low.startswith(("main.", "app.", "index.", "run.", "game.")) or \
                   (low.endswith(".py") and not low.startswith("test_")):
                    names.append(os.path.relpath(os.path.join(dirpath, fn), self._project_dir))
        names.sort()
        # main.py / app.py 优先
        names.sort(key=lambda p: 0 if os.path.basename(p).lower() in ("main.py", "app.py") else 1)
        return names[:5]

    def _find_files_by_ext(self, ext: str, limit: int = 10) -> List[str]:
        """按扩展名找文件。"""
        if not os.path.isdir(self._project_dir):
            return []
        result = []
        for dirpath, _, filenames in os.walk(self._project_dir):
            for fn in filenames:
                if fn.lower().endswith(ext.lower()):
                    result.append(os.path.relpath(os.path.join(dirpath, fn), self._project_dir))
                    if len(result) >= limit:
                        return result
        return result

    # ------------------------------------------------------------------
    # 交互式命令：write/replace 分两轮（先命令校验 → 后补 content）
    # ------------------------------------------------------------------

    def _validate_write(self, rel: str) -> Tuple[bool, str]:
        """校验 write 参数是否合法。返回 (valid, note)。"""
        if not rel or not rel.strip():
            return False, "文件路径不能为空"
        abspath = self._safe_join(rel)
        # 检查路径是否在项目内（_safe_join 已做越界防护）
        try:
            os.path.dirname(abspath) or abspath
        except Exception as e:
            return False, f"路径不合法：{e}"
        existed = os.path.isfile(abspath)
        if existed:
            size = os.path.getsize(abspath)
            return True, f"文件已存在（{size} 字节），将覆盖写入"
        # 检查父目录是否可创建
        parent = os.path.dirname(abspath)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                return False, f"无法创建目录 {parent}：{e}"
        return True, "文件不存在，将创建新文件"

    def _validate_replace(self, rel: str, start: int, end: int) -> Tuple[bool, str]:
        """校验 replace 参数是否合法。返回 (valid, note)。

        正反两套索引：正索引从 1 开始，负索引 -1 = 最后一行；1~-1 表示整个文件；
        0 已废弃（会报错引导）；超出范围自动截断。
        """
        if not rel or not rel.strip():
            return False, "文件路径不能为空"
        abspath = self._safe_join(rel)
        if not os.path.isfile(abspath):
            return False, f"文件不存在：{rel}"
        try:
            total = sum(1 for _ in open(abspath, "r", encoding="utf-8", errors="replace"))
        except Exception as e:
            return False, f"读取文件失败：{e}"
        ns, ne, _adj = self._normalize_line_range(start, end, total)
        warn = "（已自动修正行范围：起始行 {start}→{ns}，结束行 {end}→{ne}）".format(
            start=start, end=end, ns=ns, ne=ne
        ) if _adj else ""
        return (
            True,
            f"文件 {rel} 共 {total} 行，将替换第 {ns}~{ne} 行（共 {ne - ns + 1} 行）{warn}",
        )

    def _set_pending(self, name: str, body: str, valid: bool, note: str) -> None:
        """存入/更新交互式等待列表。同 name+body 覆盖。"""
        # 同 key 覆盖
        for i, p in enumerate(self._pending_interactive):
            if p["name"] == name and p["body"] == body:
                self._pending_interactive[i] = {"name": name, "body": body, "valid": valid, "note": note}
                return
        self._pending_interactive.append({"name": name, "body": body, "valid": valid, "note": note})

    def _clear_pending(self, name: str = "", body: str = "") -> None:
        """清理 pending。指定 name+body 清特定项；只指定 name 清该类全部；都不指定全清。"""
        if not name and not body:
            self._pending_interactive.clear()
            return
        self._pending_interactive = [
            p for p in self._pending_interactive
            if not (p["name"] == name and (not body or p["body"] == body))
        ]

    def _find_pending(self, name: str, body: str = "") -> Optional[Dict[str, Any]]:
        """查 pending 里是否已有同 name+body 的项。有则返回该项，无则返回 None。"""
        for p in self._pending_interactive:
            if p["name"] == name and p["body"] == body:
                return p
        return None

    def _format_pending_feedback(self) -> str:
        """把 pending 列表格式化成反馈文本，注入 history 让 gemma 知道参数已确认。"""
        if not self._pending_interactive:
            return ""
        lines = ["【交互式命令等待】以下命令参数已确认合法，请在下一轮填写内容完成执行："]
        for p in self._pending_interactive:
            tag = f"[{p['name']}:{p['body']}]" if p["body"] else f"[{p['name']}]"
            if p["valid"]:
                lines.append(f"  {tag} ✓ {p['note']}")
                lines.append(f"    → 请发送：{tag}<start>你的{ '完整' if p['name']=='write' else '替换' }内容</end>")
            else:
                lines.append(f"  {tag} ✗ {p['note']}")
                lines.append(f"    → 参数无效，请用正确参数重发命令（带完整内容块）")
        lines.append("（你也可以一次性重新发送完整命令 [write:xxx]<start>内容</end> 跳过此交互）")
        return "\n".join(lines)

    def _do_structure(self) -> str:
        """返回项目文件夹的层级结构树。"""
        if not os.path.isdir(self._project_dir):
            return "(项目为空)"
        root_real = os.path.realpath(self._project_dir)
        lines: List[str] = []
        try:
            for dirpath, dirnames, filenames in os.walk(root_real, topdown=True):
                dirnames[:] = [d for d in dirnames
                               if d not in _IGNORE_DIRS and not d.startswith(".")]
                filenames = [f for f in filenames
                             if f not in _IGNORE_DIRS and not f.endswith(".pyc")]
                filenames.sort()
                rel = os.path.relpath(dirpath, root_real)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                indent = "    " * depth
                if depth == 0:
                    lines.append(os.path.basename(root_real) + "/")
                else:
                    lines.append(f"{indent}{os.path.basename(dirpath)}/")
                child_indent = "    " * (depth + (0 if depth == 0 else 1))
                for fn in filenames:
                    lines.append(f"{child_indent}{fn}")
        except OSError:
            return "(项目为空)"
        return "\n".join(lines) if lines else "(项目为空)"

    def _atomic_write(self, abspath: str, content: str, then_fd: bool = False):
        """原子写入字符串内容（同目录临时文件 + os.replace）。"""
        content = content or ""
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(abspath) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, abspath)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # 文件操作 undo
    # ------------------------------------------------------------------
    def _record_undo(self, kind: str, rel: str, *, existed: bool = False,
                     old: str = "", backup_dir: str = "") -> None:
        """登记一个可撤销的文件操作，并把分配的 handle 存到 _last_undo_handle。

        复原数据保留"操作前"状态：
        - write：existed 标记是否覆盖旧文件，old 为旧内容；
        - edit（remove/replace）：old 为操作前全文；
        - delete：old 为被删文件内容（was_dir=False）；
        - delete_dir：backup_dir 为剪贴的整目录备份。
        """
        with self._undo_lock:
            self._undo_counter += 1
            handle = f"H{self._undo_counter}"
            self._undo_stack.append({
                "handle": handle, "kind": kind, "rel": rel,
                "existed": existed, "old": old, "backup_dir": backup_dir,
            })
        self._last_undo_handle = handle

    def undo_command(self, handle: str):
        """按 handle 撤销对应文件操作（用户可在控制台选中命令调用）。"""
        with self._undo_lock:
            entry = None
            for i, e in enumerate(self._undo_stack):
                if e["handle"] == handle:
                    entry = self._undo_stack.pop(i)
                    break
        if entry is None:
            return f"[撤销] 未找到命令 {handle}"
        rel = entry["rel"]
        try:
            abspath = self._safe_join(rel)
            kind = entry["kind"]
            if kind == "write":
                if entry["existed"]:
                    self._atomic_write(abspath, entry["old"])
                elif os.path.isdir(abspath):
                    shutil.rmtree(abspath, ignore_errors=True)
                elif os.path.isfile(abspath):
                    os.remove(abspath)
            elif kind in ("edit", "delete"):
                if os.path.isdir(abspath):
                    shutil.rmtree(abspath, ignore_errors=True)
                os.makedirs(os.path.dirname(abspath) or ".", exist_ok=True)
                self._atomic_write(abspath, entry["old"])
            elif kind == "delete_dir":
                if os.path.isdir(abspath):
                    shutil.rmtree(abspath, ignore_errors=True)
                shutil.copytree(entry["backup_dir"], abspath)
        except Exception as e:
            msg = f"[撤销] {rel} 失败：{e}"
            self._on_console(msg)
            return msg
        msg = f"[撤销] 已还原 {rel}（#{handle}）"
        self._on_console(msg)
        return msg

    def _strip_fences(self, content: str) -> str:
        """移除模型输出常带的 Markdown 代码围栏（首行 ```[lang] 与末行 ```）。"""
        lines = content.split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[0].lstrip().startswith("```") and \
                lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
            return "\n".join(lines)
        return content

    def _do_write(self, rel: str, content: str) -> str:
        content = self._strip_fences(content)
        # 空内容警告：对小模型来说很有用——避免写入 0 字节而不自知
        if not content.strip():
            return f"[write] 警告：你要写入 {rel} 的内容为空，已拒绝。请确认 <start>...</end> 标签内是否有内容。"
        abspath = self._safe_join(rel)
        os.makedirs(os.path.dirname(abspath) or abspath, exist_ok=True)
        # 记录 undo：如果文件存在，保存原内容
        existed = os.path.isfile(abspath)
        old_text = self._read_full(abspath) if existed else ""
        # 同目录临时文件 + 原子替换，避免写一半损坏
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(abspath) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, abspath)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._record_undo("write", rel, existed=existed, old=old_text)
        lines = content.count("\n") + (1 if content else 0)
        # 附带前 40 字符摘要，帮助小模型快速确认写入了什么
        preview = content[:40].replace("\n", " ").strip()
        if len(content) > 40:
            preview += "..."
        return f"[write #{self._last_undo_handle}] {rel}（{len(content)} 字节/{lines} 行）预览: {preview}"

    def _do_read(self, rel: str, start: str, end: str) -> str:
        abspath = self._safe_join(rel)
        if not os.path.isfile(abspath):
            raise CommandError(f"文件不存在：{rel}")
        lines = self._read_full(abspath).split("\n")
        try:
            raw_s, raw_e = int(start), int(end)
        except (ValueError, TypeError):
            # 非数字行号 → 回退为读取整个文件（1~-1；0 已废弃）
            raw_s, raw_e = 1, -1
        total = len(lines)
        s, e, adjustments = self._normalize_line_range(raw_s, raw_e, total)
        adjust_str = "".join(adjustments)
        out = []
        for i in range(s - 1, e):
            out.append(f"{i + 1:4d} | {lines[i]}")
        if not out:
            return (
                f"[read] {rel}（请求 {raw_s}~{raw_e}，文件共 {total} 行）："
                f"结果为空{adjust_str}"
            )
        return (
            f"[read:{rel},{start},{end}]（实际返回 {s}~{e} 行，文件共 {total} 行）"
            f"{adjust_str}\n" + "\n".join(out)
        )

    def _do_search(self, scope: str, query: str) -> str:
        if not query:
            return "[search] 搜索关键字为空"
        hits: List[str] = []
        rel_candidates = self._collect_files(scope)
        for rel in rel_candidates:
            abspath = self._safe_join(rel)
            try:
                for i, line in enumerate(self._read_full(abspath).split("\n"), 1):
                    if query in line:
                        hits.append(f"{rel}:{i}: {line.strip()[:120]}")
            except CommandError:
                continue
        if not hits:
            return f"[search] 未找到「{query}」"
        shown = hits[:80]
        return "[search] 命中：\n" + "\n".join(shown) + (f"\n…共 {len(hits)} 处" if len(hits) > 80 else "")

    def _do_remove(self, rel: str, start: str, end: str) -> str:
        abspath = self._safe_join(rel)
        if not os.path.isfile(abspath):
            raise CommandError(f"文件不存在：{rel}")
        old_full = self._read_full(abspath)
        lines = old_full.split("\n")
        try:
            raw_s, raw_e = int(start), int(end)
        except (ValueError, TypeError):
            raise CommandError("起始/结束行号无效（行号从 1 开始，负索引 -1 为最后一行，0 已废弃；读取整个文件请用 1 和 -1）")
        total = len(lines)
        s, e, adjustments = self._normalize_line_range(raw_s, raw_e, total)
        adjust_str = "".join(adjustments)
        removed = lines[s - 1:e]
        new_lines = lines[:s - 1] + lines[e:]
        self._write_lines(abspath, new_lines)
        self._record_undo("edit", rel, existed=True, old=old_full)
        extra = f"；{adjust_str.strip('（）')}" if adjust_str else ""
        return f"[remove #{self._last_undo_handle}] {rel} 已删除 {s}~{e} 行（{len(removed)} 行{extra}）"

    def _do_delete(self, rel: str) -> str:
        abspath = self._safe_join(rel)
        if os.path.isdir(abspath):
            backup = tempfile.mkdtemp(prefix="pro_undo_dir_")
            shutil.copytree(abspath, os.path.join(backup, "root"))
            shutil.rmtree(abspath)
            self._record_undo("delete_dir", rel, backup_dir=os.path.join(backup, "root"))
            return f"[delete #{self._last_undo_handle}] 已删除文件夹 {rel}"
        if os.path.isfile(abspath):
            old_full = self._read_full(abspath)
            os.remove(abspath)
            self._record_undo("delete", rel, existed=True, old=old_full)
            return f"[delete #{self._last_undo_handle}] 已删除文件 {rel}"
        raise CommandError(f"不存在：{rel}")

    def _do_replace(self, rel: str, start: str, end: str, content: str) -> str:
        content = self._strip_fences(content)
        abspath = self._safe_join(rel)
        if not os.path.isfile(abspath):
            raise CommandError(f"文件不存在：{rel}")
        old_full = self._read_full(abspath)
        lines = old_full.split("\n")
        try:
            raw_s, raw_e = int(start), int(end)
        except (ValueError, TypeError):
            raise CommandError("起始/结束行号无效（行号从 1 开始，负索引 -1 为最后一行，0 已废弃；读取整个文件请用 1 和 -1）")
        total = len(lines)
        s, e, adjustments = self._normalize_line_range(raw_s, raw_e, total)
        new_lines = lines[:s - 1] + content.split("\n") + lines[e:]
        self._write_lines(abspath, new_lines)
        self._record_undo("edit", rel, existed=True, old=old_full)
        extra = ""
        if adjustments:
            # 精简警告信息
            extra = "（" + ",".join(a.strip("（）") for a in adjustments) + "）"
        return f"[replace #{self._last_undo_handle}] {rel} 已用新内容替换 {s}~{e} 行{extra}"

    def _write_lines(self, abspath: str, lines: List[str]) -> None:
        content = "\n".join(lines)
        if content:
            content += "\n"
        olen = len(lines)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(abspath) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, abspath)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _ = olen

    def _resolve_interpreter(self) -> str:
        interp = (self._settings.get("pro_programming_interpreter") or "").strip()
        if interp and os.path.isfile(interp):
            return interp
        return sys.executable or "python"

    def _do_run(self, rel: str) -> str:
        abspath = self._safe_join(rel)
        if not os.path.isfile(abspath):
            raise CommandError(f"文件不存在：{rel}")
        self.stop_process()  # 每次新 [run] 先停旧的
        interp = self._resolve_interpreter()
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [interp, abspath],
            cwd=self._project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        abort = threading.Event()
        with self._proc_lock:
            self._proc = proc
            self._proc_abort = abort
        line_q: "queue.Queue[Optional[str]]" = queue.Queue()

        def _reader() -> None:
            try:
                for line in proc.stdout:
                    if abort.is_set():
                        break
                    line_q.put(line)
            finally:
                line_q.put(None)

        threading.Thread(target=_reader, daemon=True).start()
        deadline = time.time() + RUN_TIMEOUT
        out: List[str] = []
        while True:
            try:
                line = line_q.get(timeout=0.1)
            except queue.Empty:
                if self._base_cancel() or abort.is_set():
                    self.stop_process()
                    break
                if time.time() > deadline:
                    self.stop_process()
                    out.append(f"\n[系统] 程序运行超过 {int(RUN_TIMEOUT)} 秒，已强制终止")
                    break
                # 纯 Python 轮询，不在非主线程调用 processEvents（会导致界面卡死）
                continue
            if line is None:
                break
            out.append(line)
            self._on_run_console(line)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        rc = proc.returncode if proc.returncode is not None else proc.poll()
        text = "".join(out)
        if len(text) > MAX_RUN_OUTPUT:
            text = text[-MAX_RUN_OUTPUT:]
        # 记录本次运行结果，并标记「已重新验证」（供 summary 拦截与修复闭环判断）
        self._last_run = {"rel": rel, "exit": rc, "success": (rc == 0)}
        self._verified_after_last_fix = True
        return f"[run:{rel}] exit={rc}\n{text}"

    def _collect_files(self, scope: str) -> List[str]:
        """按检查/搜索范围收集项目内相对路径的代码/文件。"""
        scope = (scope or "").strip()
        if scope in ("", "all"):
            want_dirs = [self._project_dir]
            want_files = None
        else:
            cand = self._safe_join(scope)
            if os.path.isfile(cand):
                return [self._rel_of(cand)]
            if os.path.isdir(cand):
                want_dirs = [cand]
                want_files = None
            else:
                raise CommandError(f"搜索/检查范围不存在：{scope}")
        files: List[str] = []
        for base in want_dirs:
            for dirpath, dirnames, filenames in os.walk(base, topdown=True):
                dirnames[:] = [d for d in dirnames
                               if d not in _IGNORE_DIRS and not d.startswith(".")]
                for fn in filenames:
                    if fn.endswith(".pyc"):
                        continue
                    abspath = os.path.join(dirpath, fn)
                    files.append(self._rel_of(abspath))
        files.sort()
        return files

    def _rel_of(self, abspath: str) -> str:
        return os.path.relpath(abspath, self._project_dir).replace("\\", "/")

    def _do_check(self, scope: str) -> str:
        """把范围内 Python 文件交给检查模型只读检查，返回其结论。"""
        files = [f for f in self._collect_files(scope) if f.endswith(".py")]
        if not files:
            return "[check] 检查范围内没有可检查的 Python 文件"
        chunks = [
            "对以下项目文件做只读检查，仅报告语法/导入/逻辑错误，"
            "输出格式：相对路径:行号: 错误信息（若都无错误则只输出：无错误）\n"
        ]
        for fp in files:
            abspath = self._safe_join(fp)
            try:
                content = self._read_full(abspath)
            except CommandError:
                continue
            chunks.append(f"=== {fp} ===\n{content}\n")
        preset = app_config.get_preset_by_label("checker")
        system = app_config.get_system_prompt("checker", preset)
        print("--- 检查模型输出 ---", flush=True)
        try:
            result = model_client.call_model(
                "checker",
                [{"role": "system", "content": system},
                 {"role": "user", "content": "".join(chunks)}],
                stream=False,
                temperature=0.1,
                cancel_check=self._base_cancel,
            )
            sys.stdout.write(result)
            sys.stdout.write("\n--- 结束 ---\n")
            sys.stdout.flush()
            result = result.strip()
        except Exception as e:
            sys.stdout.write(f"\n--- 结束 ---\n[check] 检查模型调用失败：{e}\n")
            sys.stdout.flush()
            return f"[check] 检查模型调用失败：{e}"
        if not result:
            result = "无错误"
        return f"[check:{scope}]\n{result[:MAX_RUN_OUTPUT]}"

    @staticmethod
    def _format_result(name: str, result: str) -> str:
        first = result.split("\n", 1)[0]
        return f"[{name}] {first}"

    # ------------------------------------------------------------------
    # 内存控制 & 阻塞等待
    # ------------------------------------------------------------------
    @staticmethod
    def _trim_history(msgs: List[Dict[str, str]], max_len: int = MAX_HISTORY,
                      max_tokens: int = MAX_CONTEXT_TOKENS) -> List[Dict[str, str]]:
        """裁剪历史：先按条数上限裁，再按总 token 预算裁（保留 system + 最近消息）。"""
        if len(msgs) > max_len:
            msgs = msgs[:1] + msgs[-(max_len - 1):]
        if max_tokens <= 0 or len(msgs) <= 2:
            return msgs
        total = sum(_estimate_tokens(m.get("content", "")) for m in msgs)
        i = 1
        # 从最旧的非 system 消息开始丢弃，直到低于预算（始终保留至少 2 条）
        while total > max_tokens and i < len(msgs) - 1:
            total -= _estimate_tokens(msgs[i].get("content", ""))
            i += 1
        if i > 1:
            msgs = msgs[:1] + msgs[i:]
        return msgs

    def _push_context_tokens(self) -> None:
        """实时统计将发送给模型接口的 message 总 token 数，推给 UI 显示模型真实带有的上下文。"""
        msgs = self._trim_history(self._history)
        self._on_context_tokens(sum(_estimate_tokens(m.get("content", "")) for m in msgs))

    def _wait_for_trigger(self) -> bool:
        """阻塞直到 有新用户输入 / 停止。返回 False 表示会话应结束。

        用纯 Python 轮询 + QCoreApplication.processEvents 等待，
        与 agent_worker._await_confirm/_await_consult/_wait_resume 保持同一模式：
        processEvents 处理排队到 worker 线程的槽信号（如停止请求），
        time.sleep(0.1) 保证及时响应且 CPU 占用低。
        避免使用 QEventLoop/QTimer 嵌套事件循环——这在非主线程会导致界面卡死。
        """
        while True:
            if self._stopped:
                return False
            if self._pending_input:
                return True
            if self._base_cancel():
                self._stopped = True
                return False
            # 处理排队到 worker 线程的槽信号（如用户点"停止"后的 stop 槽）
            try:
                from PySide6.QtCore import QCoreApplication
                QCoreApplication.processEvents()
            except Exception:
                pass
            time.sleep(0.1)