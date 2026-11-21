# -*- coding: utf-8 -*-
"""Pro 版聊天控制器（双模型编排）。

在普通聊天基础上，引入一个「引导模型」（独立配置的 guide 预设，充当编导），实时监控
用户输入与聊天模型的输出，决定何时、以何种方式让「聊天模型」（chat 预设）输出，
使对话更像真人：可以一次只说一句、被打断及时收住、主动开启话题等。

引导模型输出控制命令符（命令符之间的自然文本作为对聊天模型的引导指令）：
- [direct]       把用户这条消息原样转发给聊天模型，让它直接回答
- [stop]         中断当前聊天模型的输出
- [max_tokens=N] 限制下一次聊天模型输出的最大 token 数
- [start=引导词] 让聊天模型以「引导词」开头
- [lead=提示词]  在这里写对聊天模型的引导提示词
- [end]          仅在用户告别/结束时用于终止对话
- [wait=N]       让聊天模型输出后停下，等用户 N 秒，超时则主动开启话题
"""

from __future__ import annotations

import re
import time as _time
from typing import Callable, Dict, List, Optional



import app_config
import model_client

# 连续引导安全上限：一次用户输入触发出的指令序列里，最多连续产出多少次聊天输出，
# 防止引导模型异常导致无限循环。「倒数」这类循序渐进场景远低于此上限。
MAX_CONSECUTIVE_OUTPUTS = 25

# 自循环安全上限：连续多少回合引导模型既未让聊天模型产出、也未用 [wait]/[end]/[stop]
# 让出（即纯文本/空指令的无意义空转）。超过则强制停下等用户，避免无限热调用接口。
MAX_SPIN_TURNS = 20


class ProChatController:
    """驱动 引导模型 ↔ 聊天模型 的会话编排循环（运行在 worker 线程）。

    通过回调与 UI/worker 解耦：
    - on_chat_chunk(c)：聊天模型输出的流式增量（直接在对话区渲染）；
    - on_chat_message(text)：一段完整聊天回复完成时回调；
    - on_guide_chunk(c)：引导模型输出的流式增量（仅打印到控制台，不渲染到对话区）；
    - on_chat_flush()：一段聊天输出结束后、on_chat_message 之前冲刷流式缓冲（可选）；
    - on_hint(s)：引导模型的轻量状态提示（如“引导模型分析中”）；
    - cancel_check()：返回 True 表示应中断（停止/暂停/有新输入）。
    """

    def __init__(
        self,
        *,
        on_chat_chunk: Callable[[str], None],
        on_chat_message: Callable[[str], None],
        on_guide_chunk: Optional[Callable[[str], None]] = None,
        on_chat_flush: Optional[Callable[[], None]] = None,
        on_hint: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        chat_history: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        self._on_chat_chunk = on_chat_chunk
        self._on_chat_message = on_chat_message
        self._on_guide_chunk = on_guide_chunk
        self._on_chat_flush = on_chat_flush
        self._on_hint = on_hint or (lambda _s: None)
        self._base_cancel = cancel_check or (lambda: False)
        self._settings = app_config.load_settings()
        # 可见聊天历史：user/assistant 交替，供聊天模型保持对话上下文
        self._chat_history: List[Dict[str, str]] = list(chat_history) if chat_history else []
        # 引导模型视角：用户在说什么、聊天模型输出什么、引导指示了什么
        self._guide_convo: List[Dict[str, str]] = []
        # 等待处理的用户输入（worker 线程内追加，同线程消费）
        self._pending_input: List[str] = []
        # 控制状态
        self._stopped = False
        self._await_user_after = False      # 引导是否已表示“停下等用户”
        self._deadline: Optional[float] = None  # [wait=N] 的绝对截止时刻
        self._outputs_this_turn = 0
        self._spin_count = 0

    # ------------------------------------------------------------------
    # 对外控制
    # ------------------------------------------------------------------
    def feed_user(self, text: str) -> None:
        """用户新消息入队（供 worker 在 busy 时直接投递）。"""
        if text and text.strip():
            self._pending_input.append(text.strip())

    @property
    def stopped(self) -> bool:
        return self._stopped

    def set_stopped(self, flag: bool = True) -> None:
        """强制停止（事件循环靠定时器轮询，无需额外唤醒）。"""
        self._stopped = flag

    # 取消检查：用户有新输入也会中断正在输出的聊天模型
    def _cancel_check(self) -> bool:
        return self._base_cancel() or bool(self._pending_input)

    def _take_pending_input(self) -> Optional[str]:
        """取最新一条待处理用户输入；多条并存时合并取最新话题。"""
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
    def run(self) -> None:
        """在 worker 线程持续驱动引导↔聊天，直到停止。"""
        self._stopped = False
        self._guide_convo = [{
            "role": "system",
            "content": app_config.get_system_prompt("guide", app_config.get_preset_by_label("guide")),
        }]
        self._await_user_after = False
        self._deadline = None

        while not self._stopped:
            user_input = self._take_pending_input()
            if user_input is not None:
                self._append_to_guide(
                    "user",
                    f"[用户发来新消息]\n用户说：{user_input}\n请分析意图并决定如何引导聊天模型回复。",
                )
                self._chat_history.append({"role": "user", "content": user_input})
                self._deadline = None  # 用户输入取消计划中的主动发言
                self._outputs_this_turn = 0
                self._spin_count = 0
                self._on_hint("引导模型分析中…")
                self._guide_turn("user")
            elif self._deadline is not None and _time.time() >= self._deadline:
                self._deadline = None
                self._append_to_guide(
                    "user",
                    "[已经过数秒，用户仍未说话。请主动引导聊天模型开启一个新话题，让对话继续。]",
                )
                self._on_hint("引导模型主动开启话题…")
                self._guide_turn("proactive")
            elif self._await_user_after:
                if not self._wait_for_trigger_reason():
                    break  # 被停止/暂停
                # 回来重新判断触发原因（用户输入 / 主动到时 优先）
                continue
            else:
                # 上一条聊天输出结束且引导仍需继续（如“倒数”里的“下一个”）
                self._guide_turn("continue")

        self._on_hint("Pro 聊天会话结束")

    # ------------------------------------------------------------------
    # 引导回合
    # ------------------------------------------------------------------
    def _guide_turn(self, reason: str) -> None:
        # 每回合独立重判是否让出：清除历史遗留的 wait/stop 状态，避免上次 [wait]
        # 让本回合产出新聊天输出后，主循环仍卡在等待、不再给引导模型 read-back 回合
        self._await_user_after = False
        if self._on_guide_chunk:
            self._on_guide_chunk("\n--- 引导模型输出 ---\n")
        directive = self._call_guide(reason)
        if self._on_guide_chunk:
            self._on_guide_chunk("\n---------------------")
        if self._stopped:
            return
        self._append_to_guide("assistant", f"[你的引导指示]\n{directive}")
        cmd, instruction = self._parse_directive(directive)
        # 强制单命令：一次只允许一个命令符（如发生组合，仅保留优先级最高的一个）
        cmd = self._coerce_single_command(cmd)

        # 只有命令才能真正触发聊天模型输出（纯文本不作引导指令，避免模型偷懒）
        lead = (cmd.get("lead") or "").strip()
        produces = cmd.get("direct") or bool(lead) or cmd.get("start") is not None \
            or cmd.get("max_tokens") is not None
        if produces:
            direct_flag = cmd.get("direct")
            eff = "" if direct_flag else lead
            self._spin_count = 0
            self._run_chat_output(
                eff, start=cmd.get("start"), max_tokens=cmd.get("max_tokens"),
                direct=direct_flag,
            )

        # 依据 [end] / [wait=N] / [stop] 决定是否让出给用户等待。
        # 关键：未产出/纯文本/空输出 时【不停止】，主循环继续自循环，
        # 让引导模型读取最新内容后自主决定下一步，直到给出 [wait=N] 或 [end]。
        if cmd.get("end") or cmd.get("wait") is not None or cmd.get("stop"):
            self._await_user_after = True
            self._deadline = (None if (cmd.get("end") or cmd.get("stop"))
                              else _time.time() + max(0.0, float(cmd["wait"])))
            self._spin_count = 0
        else:
            # 未让出：继续自循环；但连续过多回合既未产出也未让出时，强制停下防热转
            self._spin_count += 1
            if self._spin_count > MAX_SPIN_TURNS:
                self._await_user_after = True
                self._deadline = None
                self._spin_count = 0

    def _coerce_single_command(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """强制「只使用一个命令」：若模型组合了多个命令符，按优先级仅保留一个。

        优先级：direct > lead > start > max_tokens > wait > end > stop。
        产出类命令靠前，保证“先产出、再由下一回合读取输出后决定等待/结束”。
        """
        order = ("direct", "lead", "start", "max_tokens", "wait", "end", "stop")
        chosen = next((k for k in order if cmd.get(k)), None)
        if not chosen:
            return cmd
        for k in order:
            if k == chosen:
                continue
            if k in ("stop", "end"):
                cmd[k] = False
            else:
                cmd[k] = None
        return cmd

    def _run_chat_output(
        self,
        instruction: str,
        start: Optional[str] = None,
        max_tokens: Optional[int] = None,
        direct: bool = False,
    ) -> None:
        self._outputs_this_turn += 1
        if self._outputs_this_turn > MAX_CONSECUTIVE_OUTPUTS:
            self._await_user_after = True
            self._deadline = None
            return

        chat_instruct = self._trim_chat_history(self._chat_history)
        if direct and not start:
            # [direct]：用户消息已在上下文中，直接转发，无需追加指令包装
            user_msg = None
        else:
            user_msg = self._build_chat_instruct(instruction if not direct else "", start)
        if user_msg:
            chat_instruct.append({"role": "user", "content": user_msg})

        chunks: List[str] = []

        def on_chunk(c: str) -> None:
            chunks.append(c)
            self._on_chat_chunk(c)

        temp = self._settings.get("temperature", 0.6) if "temperature" in self._settings else 0.6
        full = model_client.call_model(
            "chat", chat_instruct, stream=True, temperature=temp,
            max_tokens=max_tokens, on_chunk=on_chunk, cancel_check=self._cancel_check,
        )
        if not full:
            full = "".join(chunks)
        reply = full

        self._chat_history.append({"role": "assistant", "content": reply})
        self._append_to_guide(
            "user",
            f"[聊天模型完成了一次输出]\n聊天模型说：{reply}\n"
            "请阅读后决定下一步：继续引导（如倒数里的下一句）、停下等待用户（[end]）、"
            "或计划主动找话题（[wait=N]）。",
        )
        # 先把本回合最后一段流式缓冲冲刷到 UI，再通知“本次输出完成”。
        # 否则最后那句会滞留在 worker 缓冲里，等下一次聊天输出到来时才被一起
        # 提交，渲染进下一个气泡顶部，表现为“重复上一句话”。
        if self._on_chat_flush:
            self._on_chat_flush()
        self._on_chat_message(reply)

    def _call_guide(self, reason: str) -> str:
        """调用引导模型；输出流式打印到控制台（供调试观察），不渲染到对话区。"""
        msgs = self._trim_guide(self._guide_convo)
        chunks: List[str] = []

        def on_chunk(c: str) -> None:
            chunks.append(c)
            if self._on_guide_chunk:
                self._on_guide_chunk(c)

        return model_client.call_model(
            "guide", msgs, stream=True,
            temperature=0.2, on_chunk=on_chunk, cancel_check=self._cancel_check,
        )

    # ------------------------------------------------------------------
    # 命令解析与消息构造
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_directive(raw: str):
        cmd = {"stop": False, "end": False, "max_tokens": None, "start": None, "lead": None, "wait": None, "direct": False}
        instr = raw
        for m in re.findall(r"\[(.*?)\]", raw):
            body = m.strip()
            low = body.lower()
            if low == "stop":
                cmd["stop"] = True
            elif low == "direct":
                cmd["direct"] = True
            elif low == "end":
                cmd["end"] = True
            elif low.startswith("max_tokens="):
                try:
                    cmd["max_tokens"] = max(1, int(body.split("=", 1)[1].strip()))
                except (ValueError, TypeError):
                    pass
            elif low.startswith("start="):
                val = body.split("=", 1)[1].strip()
                if val:
                    cmd["start"] = val
            elif low.startswith("lead="):
                val = body.split("=", 1)[1].strip()
                if val:
                    cmd["lead"] = val
            elif low.startswith("wait="):
                try:
                    cmd["wait"] = float(body.split("=", 1)[1].strip())
                except (ValueError, TypeError):
                    pass
            else:
                # 未识别的命令符：原样保留为引导文本的一部分
                continue
            instr = instr.replace("[" + m + "]", " ")
        return cmd, instr.strip()

    def _build_chat_instruct(self, instruction: str, start: Optional[str]) -> str:
        parts: List[str] = []
        if start:
            parts.append(f'请以这句话开头开始本次回复：“{start}”')
        if instruction:
            parts.append(f"请依照引导输出：{instruction}")
        else:
            parts.append("请自然地输出一句话回复。")
        parts.append("当前处于 Pro 真人聊天模式：请像真人一样，短小自然、口语化，贴合语境。")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 内存控制
    # ------------------------------------------------------------------
    def _append_to_guide(self, role: str, content: str) -> None:
        self._guide_convo.append({"role": role, "content": content})

    @staticmethod
    def _trim_guide(msgs: List[Dict[str, str]], max_len: int = 24) -> List[Dict[str, str]]:
        if len(msgs) <= max_len:
            return msgs
        return msgs[:1] + msgs[-(max_len - 1):]

    @staticmethod
    def _trim_chat_history(msgs: List[Dict[str, str]], max_len: int = 40) -> List[Dict[str, str]]:
        if len(msgs) <= max_len:
            return msgs
        return msgs[-max_len:]

    # ------------------------------------------------------------------
    # 阻塞等待（纯 Python 轮询，兼容 worker 线程）
    # ------------------------------------------------------------------
    def _wait_for_trigger_reason(self) -> bool:
        """阻塞直到 有新用户输入 / [wait] 到时 / 停止。返回 False 表示会话应结束。"""
        while True:
            if self._stopped:
                return False
            if self._pending_input:
                return True
            if self._deadline is not None and _time.time() >= self._deadline:
                return True
            if self._base_cancel():
                # 被暂停/停止：结束本次会话
                self._stopped = True
                return False
            # 100ms 轮询 — 足够及时响应，CPU 占用极低
            _time.sleep(0.1)