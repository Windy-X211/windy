# -*- coding: utf-8 -*-
"""cos 角色扮演聊天控制器（单模型自循环）。

与 Pro 的双模型编排不同，cos 模式把「导演」和「聊天」统一成一个「角色模型」：
它既是对话者（扮演设定的角色人设），也是节奏掌控者，自主循环决策，直到输出 [end] 才结束。

角色模型输出的控制命令符：
- [say=文字]  把「文字」呈现给用户，作为角色对用户说的话（唯一发言方式）
- [wait=N]    说一句后等用户 N 秒；N 秒内用户发消息则优先回应，否则主动延续话题
- [end]       仅当用户明确告别/结束时终止整场对话
命令符以外的自然文本仅作模型的内心独白，不渲染给用户。
"""

from __future__ import annotations

import difflib
import re
import time as _time
from typing import Callable, Dict, List, Optional



import app_config
import model_client

# 连续产出安全上限：一次触发出的指令序列里最多连续多少次 [say]，防异常无限循环。
MAX_CONSECUTIVE_OUTPUTS = 25

# 自循环空转上限：连续多少回合既未 [say] 也未 [wait]/[end] 让出，则强制停下等用户。
MAX_SPIN_TURNS = 20

# 缺省让出秒数：模型 [say] 后若没给 [wait]/[end]，自动等这么多秒再把话语权交给用户，防连发刷屏。
DEFAULT_WAIT = 12

# 自主发言上限：自上次用户发言以来，角色模型最多连续主动开口的次数；超过后停下静候用户。
AUTONOMOUS_SAY_LIMIT = 4

# 复读判定阈值：与上一条已呈现内容相似度 ≥ 此值视为复读，直接拦截不再呈现。
REPETITION_THRESHOLD = 0.85

# 连续复读上限：被拦截到该次数后，停止自循环唤醒、无限期静候用户，避免反复复读浪费。
REPEAT_STRIKE_LIMIT = 2


class CosChatController:
    """驱动「角色模型」自循环会话（运行在 worker 线程）。

    回调解耦：
    - on_chunk(text)：角色模型输出内容（含命令符）的流式增量，用于控制台打印；
    - on_message(text)：一段 [say=...] 完整发言完成时回调（作为 assistant 消息）；
    - on_hint(s)：轻量状态提示（如“角色模型思考中…”）；
    - on_model_input(system_prompt, input_text)：每次调用模型前回调，携带【自循环提示词】与
      【本轮最新输入】（用户消息 / 系统注入的沉默提示），供控制台打印「一条输入对一条输出」；
    - cancel_check()：返回 True 表示应中断（停止/暂停/有新输入）。
    """

    def __init__(
        self,
        *,
        on_chunk: Callable[[str], None],
        on_message: Optional[Callable[[str], None]] = None,
        on_say_stream: Optional[Callable[[str], None]] = None,
        on_say_done: Optional[Callable[[str], None]] = None,
        on_hint: Optional[Callable[[str], None]] = None,
        on_model_input: Optional[Callable[[str, str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        chat_history: Optional[List[Dict[str, str]]] = None,
        persona: Optional[Dict[str, str]] = None,
    ) -> None:
        self._on_chunk = on_chunk
        self._on_message = on_message or (lambda _t: None)
        # 流式呈现回调：on_say_stream 增量推送可见正文（打字机效果），
        # on_say_done 在某条 [say]（或兜底正文）闭合后提交整条气泡。
        self._on_say_stream = on_say_stream or (lambda _t: None)
        self._on_say_done = on_say_done or on_message or (lambda _t: None)
        self._on_hint = on_hint or (lambda _s: None)
        # 控制台「输入→输出」配对打印：系统提示词（自循环提示词）+ 本轮最新输入
        self._on_model_input = on_model_input or (lambda _s, _i: None)
        self._system_prompt = ""  # 会话开始时构建，作为自循环提示词传入控制台
        self._base_cancel = cancel_check or (lambda: False)
        self._settings = app_config.load_settings()
        self._persona = persona if persona else app_config.get_active_persona()
        # 角色模型视角的完整会话：system + 可见历史 + user 消息 + 角色原始输出
        self._messages: List[Dict[str, str]] = []
        if chat_history:
            self._messages.extend(
                m for m in chat_history if m.get("role") in ("user", "assistant")
            )
        # 等待处理的用户输入（worker 线程内追加，同线程消费）
        self._pending_input: List[str] = []
        # 控制状态
        self._stopped = False
        self._await_user_after = False   # 是否已“停下等用户”
        self._deadline: Optional[float] = None  # [wait=N] 的绝对截止时刻
        self._last_wait: Optional[float] = None  # 最近一次 [wait=N] 的秒数
        self._outputs_this_turn = 0
        self._spin_count = 0
        self._last_say: Optional[str] = None  # 最近一次已呈现给用户的话（复读检测）
        self._says_since_user = 0  # 自上次用户发言以来的连续自主开口数
        self._repeat_strikes = 0  # 连续复读被拦截次数（达上限后停止自循环）

        # 流式呈现的回合内状态（每次 _model_turn 开始时重置）
        self._stream_raw = ""            # 本回合累积的原始输出
        self._stream_done_count = 0      # 已闭合并提交的 [say] 数
        self._stream_open_emitted = 0    # 当前 [say] 已流式推送的字符数
        self._stream_committed = 0       # 本回合已提交的气泡数（含复读）
        self._stream_repeated = 0        # 其中被判为复读的条数
        self._stream_any = False         # 本回合是否产生过任何可见正文
        self._turn_by_user = False       # 本回合是否由用户消息触发

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
        """在 worker 线程持续驱动角色模型自循环，直到停止或 [end]。"""
        self._stopped = False
        self._await_user_after = False
        self._deadline = None
        self._system_prompt = self._build_system_prompt()
        self._messages.insert(0, {"role": "system", "content": self._system_prompt})
        self._on_hint("cos 角色对话开始")

        while not self._stopped:
            user_input = self._take_pending_input()
            if user_input is not None:
                self._messages.append({"role": "user", "content": user_input})
                self._deadline = None  # 用户输入取消计划中的主动发言
                self._outputs_this_turn = 0
                self._spin_count = 0
                self._says_since_user = 0  # 用户开口后重置自主发言计数
                self._repeat_strikes = 0  # 用户开口后重置复读拦截计数
                self._on_hint("角色模型思考中…")
                self._model_turn(by_user=True)
            elif self._deadline is not None and _time.time() >= self._deadline:
                # [wait=N] 到点且用户仍沉默：把沉默时长作为上下文告知角色模型，
                # 由它依据人设（沉默处理 + 情绪感知 + 活跃度）决定是否主动开口。
                self._deadline = None
                self._on_hint("角色模型继续…")
                self._model_turn(silence=self._last_wait)
            elif self._await_user_after:
                if not self._wait_for_trigger_reason():
                    break  # 被停止/暂停
                continue  # 回来重新判断触发原因（用户输入 / 主动到时 优先）
            else:
                # 上一条 [say] 结束且角色仍需继续（自主循环）
                self._model_turn()

        self._on_hint("cos 角色对话结束")

    # ------------------------------------------------------------------
    # 角色回合
    # ------------------------------------------------------------------
    def _model_turn(self, silence: Optional[float] = None, by_user: bool = False) -> None:
        # 每回合重置让出状态：避免上次 [wait] 让本回合产出新 [say] 后，
        # 主循环仍卡在等待、不再给角色模型继续自主循环的机会。
        self._await_user_after = False
        # 空转防护：连续多回合既无产出也未让出时，强制停下等用户，防热调用接口
        if self._outputs_this_turn == 0:
            self._spin_count += 1
            if self._spin_count > MAX_SPIN_TURNS:
                self._yield_to_user(None)
                self._spin_count = 0
                return

        # 若因用户沉默超时进入本回合，插入一条系统提示：区分“上句是否提问”来写沉默应对，
        # 并明确告知沉默时长与“上一句已说过什么”，从源头减少复读与机械追问。
        if silence is not None and silence > 0:
            last = self._last_say
            if last and self._is_question(last):
                # 上句是提问 → 用户没回答提问：侧重“耐心应对提问沉默”，不要反复追问。
                note = (
                    f"（系统：你向用户提问后，用户已沉默约 {silence:.0f} 秒没有回答。"
                    "请依据你的人设与活跃度应对这种沉默：不要机械地重复提问、催促或刷屏；"
                    "若你上句是复杂/开放的问题，应给出更长的 [wait=N]（10 秒或更长）继续耐心等待或者[end]停止对话，直到用户回应；"
                    "也可以自然地说一句关心、换个轻松话题，让对话不至于冷场。"
                    "若用户始终不回应，也可 [end] 温和收尾。"
                )
            else:
                # 上句非提问 → 用户沉默：提示模型进一步主动输出，推动对话前进。
                note = (
                    f"（系统：用户已沉默约 {silence:.0f} 秒没有回应你上句话。"
                    "既然你上句不是提问，请进一步主动输出：依据你的人设与活跃度，"
                    "自然地继续展开话题、分享想法或轻轻发问，推动对话前进；"
                    "若觉得该收尾了再用 [end]。"
                )
            #if last:
                #note += f"你上一句说的是：\"{last}\"。请不要再重复这句话。"
            #note += "不要连续刷屏多句。）"
            self._messages.append({"role": "system", "content": note})
            # 本轮最新输入：系统注入的沉默提示
            turn_input = note
        elif by_user:
            # 本轮最新输入：用户刚发送的消息（已由 run() 追加到末尾）
            turn_input = ""
            for _m in reversed(self._messages):
                if _m.get("role") == "user":
                    turn_input = _m["content"]
                    break
        else:
            # 自主延续回合：无新用户输入
            turn_input = ""

        # 控制台「一条输入对一条输出」：先把本轮输入（自循环提示词 + 最新消息）打印出来，
        # 后面的流式输出 on_chunk 紧接其后，构成完整配对。
        self._on_model_input(self._system_prompt, turn_input)

        chunks: List[str] = []
        # 回合开始：重置流式状态，并记录本回合是否由用户消息触发（影响自主发言上限判定）
        self._turn_by_user = by_user
        self._stream_raw = ""
        self._stream_done_count = 0
        self._stream_open_emitted = 0
        self._stream_committed = 0
        self._stream_repeated = 0
        self._stream_any = False

        def on_chunk(c: str) -> None:
            chunks.append(c)
            self._on_chunk(c)
            self._stream_visible(c)  # 边生成边把可见正文推给界面（打字机效果）

        temp = self._settings.get("temperature", 0.8) if "temperature" in self._settings else 0.8
        raw = model_client.call_model(
            "cos", self._trim(self._messages), stream=True, temperature=temp,
            on_chunk=on_chunk, cancel_check=self._cancel_check,
        )
        if not raw:
            raw = "".join(chunks)
        if self._stopped:
            return

        says, wait, end, plain = self._parse(raw)
        # 兜底：模型漏包 [say=] 时，把命令符外的正文当作要呈现给用户的话，
        # 避免想说的内容被当成内心独白而丢失（此时通常还带着 [wait=N]）。
        if not says and plain:
            says = [plain]

        # 收尾：补交流式阶段未闭合 / 非流式返回时未提交的内容
        self._finalize_stream(raw)

        presented_any = self._stream_committed > 0
        # 本回合所有已产出气泡均为复读且拦截已达上限：停止再唤醒模型，静候用户。
        all_repeated = (
            self._stream_committed > 0
            and self._stream_committed == self._stream_repeated
        )

        if end:
            self._stopped = True
            self._await_user_after = True
            self._deadline = None
        elif all_repeated and self._repeat_strikes >= REPEAT_STRIKE_LIMIT:
            self._on_hint("角色模型仍在重复发言，已停止自循环并等待你的回应")
            self._yield_to_user(None)
        elif not by_user and self._says_since_user > AUTONOMOUS_SAY_LIMIT:
            self._on_hint("角色模型已连续主动多句，暂停并等你回应")
            self._yield_to_user(None)
        elif wait is not None:
            self._last_wait = wait
            self._await_user_after = True
            self._deadline = _time.time() + max(0.0, float(wait))
        else:
            # 缺省让出：说了话却没给 [wait]/[end] 时，自动等 DEFAULT_WAIT 秒，
            # 避免自主循环立刻再次开口、形成连发刷屏。
            self._yield_to_user(DEFAULT_WAIT)
        self._spin_count = 0

    def _yield_to_user(self, wait: Optional[float]) -> None:
        """让出话语权等待用户。wait=None 表示无限等待（直到用户发言或会话停止）。"""
        self._await_user_after = True
        self._deadline = None if wait is None else _time.time() + max(0.0, float(wait))

    def _is_repetition(self, text: str) -> bool:
        """判断 text 是否与上一条已呈现内容高度重复（复读）。"""
        last = self._last_say
        if not last:
            return False
        return self._similarity(text, last) >= REPETITION_THRESHOLD

    @staticmethod
    def _is_question(text: str) -> bool:
        """粗略判断一句话是否属于“提问”（以问号 / 吗 / 呢 结尾视为提问）。"""
        return text.rstrip().endswith(("？", "?", "吗", "呢"))

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[\s\u3000\W_]+", "", text).lower()

    @classmethod
    def _similarity(cls, a: str, b: str) -> float:
        na = cls._normalize(a)
        nb = cls._normalize(b)
        if not na or not nb:
            return 0.0
        return difflib.SequenceMatcher(None, na, nb).ratio()

    # ------------------------------------------------------------------
    # 命令解析与消息构造
    # ------------------------------------------------------------------
    @staticmethod
    def _parse(raw: str):
        """解析 [say=…]/[wait=N]/[end]，返回 (says, wait, end, plain)。

        - says：按出现顺序收集**所有** [say=…]（支持一次输出连发多条，模拟连续气泡）。
        - wait：至多一个 [wait=N]（最后一个生效）。
        - end：是否出现 [end]。
        - plain：所有命令符之外的非命令正文（去除 [say=…]/[wait]/[end] 后的剩余文字）。
          当模型漏包 [say=] 时，用该正文兜底，避免想说的话被当成内心独白而丢失。
        """
        says: List[str] = []
        wait = None
        end = False
        for m in re.findall(r"\[(.*?)\]", raw):
            body = m.strip()
            low = body.lower()
            if low == "end":
                end = True
            elif low.startswith("say="):
                val = body.split("=", 1)[1].strip()
                if val:
                    says.append(val)
            elif low.startswith("wait="):
                try:
                    wait = float(body.split("=", 1)[1].strip())
                except (ValueError, TypeError):
                    pass
        plain = re.sub(r"\[.*?\]", "", raw).strip()
        return says, wait, end, plain

    # ------------------------------------------------------------------
    # 流式呈现：增量解析可见正文，边生成边打字机式推送
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_visible(raw: str):
        """增量解析 raw，返回 (completed_says, open_say, plain)。

        - completed_says：已完整闭合的 [say=...] 内容（按出现顺序）。
        - open_say：当前尚未闭合的 [say=... 的部分内容（可为空）。
        - plain：整条输出里没有任何 [say= 时，命令符外的正文（兜底要呈现的话）。
        """
        completed = [m.group(1) for m in re.finditer(r"\[say=(.*?)\]", raw, re.S)]
        # 检测末尾是否存在未闭合的 [say=...
        lower = raw.lower()
        idx = lower.rfind("[say=")
        open_say = ""
        if idx != -1:
            after = raw[idx + 5:]
            if "]" not in after:
                open_say = after
        if not completed and not open_say:
            # 当整条输出里没有任何 [say] 时，命令符外的正文才是“要呈现的话”。
            # 仅当存在未闭合的 `[`（正在敲 [say=/[wait=/[end]）时抑制兜底流式，
            # 避免把 [say= 前缀漏出去；括号闭合时则去掉命令符取正文。
            if raw.count("[") <= raw.count("]"):
                plain = re.sub(r"\[.*?\]", "", raw).strip()
            else:
                plain = ""
        else:
            plain = ""
        return completed, open_say, plain

    def _stream_visible(self, c: str) -> None:
        """每收到一个 chunk 时调用：把新出现的可见正文增量推送给界面。"""
        self._stream_raw += c
        completed, open_say, plain = self._extract_visible(self._stream_raw)
        # 新完整闭合的 [say]：先把尚未推送的尾巴补流式，再逐条提交为独立气泡
        if self._stream_done_count < len(completed):
            for say in completed[self._stream_done_count:]:
                remaining = say[self._stream_open_emitted:]
                if remaining:
                    self._stream_any = True
                    self._on_say_stream(remaining)
                self._stream_done_count += 1
                self._commit_say(say)
                self._stream_open_emitted = 0  # 之后打开的 [say] 属于新气泡
        vis = open_say if open_say else plain
        if len(vis) > self._stream_open_emitted:
            self._stream_any = True
            self._on_say_stream(vis[self._stream_open_emitted:])
            self._stream_open_emitted = len(vis)
        elif len(vis) < self._stream_open_emitted:
            # 当前 [say] 被闭合，[say=] 前缀已结束：重置，让下一条 [say] 从头流式输出
            self._stream_open_emitted = 0

    def _commit_say(self, say: str) -> None:
        """一条 [say]（或兜底正文）已完整产出：做复读/自主发言判定后提交气泡。"""
        self._outputs_this_turn += 1
        if self._outputs_this_turn > MAX_CONSECUTIVE_OUTPUTS:
            return
        self._stream_committed += 1
        if self._is_repetition(say):
            # 复读：仍闭合已流式显示的气泡，但不记为新的发言，累计拦截次数用于终止循环
            self._stream_repeated += 1
            self._repeat_strikes += 1
            self._on_hint("角色模型在重复发言，已自动跳过该句")
        else:
            self._repeat_strikes = 0
            self._last_say = say
            self._messages.append({"role": "assistant", "content": say})
            # 自主发言上限：用户一直没回话时，角色模型最多连续主动开口几句
            if not self._turn_by_user:
                self._says_since_user += 1
        self._on_say_done(say)

    def _finalize_stream(self, raw: str) -> None:
        """回合收尾：把未在流式阶段提交的已闭合 [say]、仍打开的 [say] 或兜底正文补交为气泡。"""
        completed, open_say, _ = self._extract_visible(raw)
        # 补交已闭合但尚未提交的 [say]（主要覆盖非流式返回 / 被中断的情况）
        for say in completed[self._stream_done_count:]:
            self._stream_done_count += 1
            self._commit_say(say)
        if open_say and open_say not in completed:  # 避免与已闭合提交的重复
            self._commit_say(open_say)
        elif not completed and not open_say:
            # 兜底正文：没有 [say] 时，把命令符外的文字（去掉所有括号）作为要呈现的话
            plain = re.sub(r"\[.*?\]", "", raw).strip()
            if plain:
                self._commit_say(plain)

    def _build_system_prompt(self) -> str:
        base = app_config.get_system_prompt("cos", app_config.get_preset_by_label("cos"))
        persona = self._persona or {}
        content = (persona.get("content") or "").strip()
        tag = (persona.get("tag") or "").strip()
        if content:
            base += "\n\n## 你当前的角色人设\n"
            if tag:
                base += f"角色名/标签：{tag}\n"
            base += content
        return base

    # ------------------------------------------------------------------
    # 内存控制
    # ------------------------------------------------------------------
    @staticmethod
    def _trim(msgs: List[Dict[str, str]], max_len: int = 60) -> List[Dict[str, str]]:
        if len(msgs) <= max_len:
            return msgs
        return msgs[:1] + msgs[-(max_len - 1):]

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
