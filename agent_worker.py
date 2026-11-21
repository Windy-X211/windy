# -*- coding: utf-8 -*-
"""AgentWorker：编排核心。

状态机：IDLE→PLANNING→AWAIT_CONFIRM→EXECUTING→DONE，含
RECOVERING / CONSULTING_PLANNER / AWAIT_CONSULT / PAUSED。

线程模型：本对象 moveToThread 到一个 QThread。所有模型调用/截图/执行/状态机
推进都在 worker 线程。阻塞点（确认/咨询/暂停）用纯 Python 轮询等待，
避免在非主线程使用 QEventLoop/QTimer/processEvents 导致界面卡死。

UI 永不直接调用模型/截图/执行，全部经此对象的信号/槽中转。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QCoreApplication, Signal, Slot, QTimer, QThread

import app_config
import capture
import executor
import model_client
import planner
import template_matcher
import vision
import agent_mode
import pro_chat
import cos_chat
import programming_pro
from step_model import (
    InterfaceUnavailable,
    OP_CONSULT,
    OP_DRAG,
    OP_LONG_INPUT,
    OP_VISION_CALLS,
    ParseError,
    OPS_NEED_VISION,
    assign_text_variables,
    parse_steps,
    step_summary,
    validate_coord,
)


class _PausedInterrupt(Exception):
    """内部轻量异常：暂停/停止时在关键节点抛出，用于快速原路返回。

    仅在 worker 线程内被捕获，不会泄露到 UI 层；捕获后不标记步骤状态，
    让执行循环在当前步重新进入 _wait_resume，恢复时从当前步从头再执行。
    """
    pass


class AgentWorker(QObject):
    # ---- Worker → UI 信号 ----
    streamChunk = Signal(str, str)            # label, chunk
    assistantMessage = Signal(str, str)        # text, role
    modelCallStarted = Signal(str)            # label（chat/programming）模型调用开始
    planReady = Signal(list, str, str)        # steps, screen_state, goal
    proConsole = Signal(str)                  # Pro 编程：控制台输出（命令执行/程序运行）
    proAgentMessage = Signal(str, str)        # Pro 编程：对话气泡 (text, role∈check/summary)
    proAgentStream = Signal(str)              # Pro 编程：模型思考/汇报 流式输出（实时进折叠框）
    proAgentHistory = Signal(str)             # Pro 编程：整段思考文本收尾（仅写入会话历史）
    proToolCall = Signal(str, str)            # Pro 编程：工具调用日志 (func_name, description)
    proSchedule = Signal(list)                # Pro 编程：[schedule] 计划步骤可视化（创建清单）
    proScheduleDone = Signal(int)             # Pro 编程：某步骤完成（index，UI 置打钩并推进下一步）
    proScheduleSkip = Signal(int)             # Pro 编程：某步骤被跳过（index，UI 打叉并推进下一步）
    proScheduleAllDone = Signal()             # Pro 编程：全部步骤完成（summary 时触发）
    proContextTokens = Signal(int)            # Pro 编程：当前上下文估算 token 数（控制台标题栏展示）
    consultRequested = Signal(str, str)       # question, context
    stepStatusChanged = Signal(int, str, str)  # index, status, note
    stepCoordUpdated = Signal(int, dict)       # index, coord_data（视觉定位后同步坐标到 UI 侧）
    progressTextChanged = Signal(int, str, str)  # index, mark, color
    progressBarChanged = Signal(int, int)    # done, total
    executionStarted = Signal()
    executionFinished = Signal(str)          # summary
    screenshotTaken = Signal(bytes)
    interfaceChanged = Signal(str)           # tag
    statusMessage = Signal(str)
    error = Signal(str, str)                 # code, message
    finished = Signal()
    agentAction = Signal(str, dict, str)     # action, params, result（自主代理模式动作结果）
    agentModelTime = Signal(float)           # 单次模型调用耗时(s，实时)（自主代理模式）
    agentTaskTime = Signal(int)              # 任务总用时(s，整数，实时)（自主代理模式）

    # 流式信号跨线程合并：攒 N 毫秒再 emit，减少排队事件数
    _STREAM_BATCH_MS = 30
    # 流式合并的字符数上限（即使时间没到，攒够这么多字符也发）
    _STREAM_BATCH_MAX_CHARS = 400
    # 当前正在控制台输出块的 label（块头/分隔线用）
    _console_active_label = None
    _CONSOLE_DIVIDER = "---------------------"
    # 控制台输出块的模型名（label -> 中文展示名）：统一「--- X输出 ---……-----」格式
    _CONSOLE_LABELS = {
        "chat": "聊天模型",
        "programming": "编程模型",
        "long_input": "长输入模型",
        "planning": "规划模型",
        "agent_mode": "自主代理模型",
        "guide": "引导模型",
        "cos": "角色模型",
        "programmer": "高级编程模型",
    }

    def __init__(self) -> None:
        super().__init__()
        self._state = "IDLE"
        self._busy = False
        self._paused = False
        self._stopped = False
        # 初始模式必须与 UI 默认一致（聊天模式），否则启动后界面显示“聊天”
        # 但发消息会按 worker 自己的默认值走代理规划执行，造成模式错位
        self._mode = "chat"
        self._goal = ""
        # 是否允许在执行中自动调用规划模型重规划。
        # 母工作流的工作流块关闭「重规划」按钮时应为 False：此时模板匹配失败
        # 或视觉模型无法定位时直接结束执行，不使用规划模型。
        self._replan_enabled = True
        self._steps: List[Dict[str, Any]] = []
        self._step_index: int = 0  # 当前执行到的步骤索引（暂停时=剩余步骤起始点）
        self._injects: List[str] = []
        self._chat_history: List[Dict[str, Any]] = []
        self._pro_chat_enabled = False  # Pro 真人聊天开关
        self._pro_chat: Optional["pro_chat.ProChatController"] = None  # 当前活动的 Pro 聊天会话
        self._cos_chat_enabled = False  # cos 角色扮演聊天开关
        self._cos_chat: Optional["cos_chat.CosChatController"] = None  # 当前活动的 cos 聊天会话
        self._pro_programming_enabled = False  # Pro 编程智能体开关
        self._pro_programming: Optional["programming_pro.ProProgrammingController"] = None
        self._pro_programming_project = ""     # Pro 编程当前项目文件夹
        self._agent_saved_context: List[Dict[str, Any]] = []  # 自主代理模式暂停时保存的消息上下文（旧版兼容）
        self._agent_saved_state: Dict[str, Any] = {}  # 自主代理模式暂停时保存的完整状态
        self._settings = app_config.load_settings()
        self._screen_size = capture.get_real_screen_size()

        # 阻塞等待用的 pending 响应
        self._pending_confirm: Optional[Tuple[List[Dict[str, Any]], bool]] = None
        self._pending_answer: Optional[str] = None

        # ---------- 流式信号跨线程合并 ----------
        # 按 label 分开缓冲：{label: [chunks...]}
        self._stream_buffers: Dict[str, List[str]] = {}
        self._stream_flush_at: Dict[str, float] = {}  # label -> monotonic deadline
        self._stream_len: Dict[str, int] = {}  # label -> 当前缓冲字符数
        self._console_active_label = None  # 当前正在控制台输出块的 label
        self._cos_system_printed = False  # cos 自循环提示词是否已打印（仅首次呈现）
        import time as _time
        self._stream_time = _time.monotonic  # 避免顶层 time 与信号 time 冲突

        # ---------- 进度文本缓存：避免每步每次都重复 emit 同样的 mark ----------
        # (index) -> (mark, color)；仅当值变化时才发射 progressTextChanged
        self._last_progress_marks: Dict[int, Tuple[str, str]] = {}

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _cancel_check(self) -> bool:
        """流式调用期间排空事件队列并返回是否应停止。

        暂停/停止都会令正在进行的流式模型输出立即中止。
        """
        try:
            QCoreApplication.processEvents()
        except Exception:
            pass
        return self._stopped or self._paused

    def _check_interrupt(self) -> None:
        """关键节点的中断检查：若暂停/停止，抛出 _PausedInterrupt 快速原路返回。

        与 _cancel_check 不同：
        - _cancel_check 返回 bool 供流式回调/模型循环使用；
        - _check_interrupt 直接抛异常，用于执行器流程中强制跳出，避免
          函数继续往后走到"标记完成"或"写 step 数据"的分支。
        """
        try:
            QCoreApplication.processEvents()
        except Exception:
            pass
        if self._stopped or self._paused:
            raise _PausedInterrupt

    def _drain_injects(self) -> Optional[str]:
        if not self._injects:
            return None
        text = "\n".join(self._injects)
        self._injects.clear()
        return text

    def _reindex(self, steps: List[Dict[str, Any]], start: int = 0) -> List[Dict[str, Any]]:
        for i, s in enumerate(steps):
            s["index"] = start + i
        # 为长输入文本分配变量（就地修改 step dict）
        assign_text_variables(steps)
        # 重新分配步骤：清空旧的进度 mark 缓存，避免新步骤继承旧的 emit 跳过判断
        self._last_progress_marks.clear()
        return steps

    def _reload_settings(self) -> None:
        self._settings = app_config.load_settings()
        self._screen_size = capture.get_real_screen_size()

    def _capture(self) -> bytes:
        shot = capture.capture_screen(draw_cursor=True)
        self.screenshotTaken.emit(shot)
        return shot

    def flush_stream(self) -> None:
        """把所有流式缓冲立即发射（模型调用返回后调用，确保最后一段不丢）。"""
        for label in list(self._stream_buffers.keys()):
            self._flush_stream_label(label)
        self._close_console_block()

    def _close_console_block(self) -> None:
        """结束当前控制台输出块：打印分隔线并复位活动 label。"""
        if self._console_active_label is not None:
            with agent_mode.CONSOLE_LOCK:
                print(self._CONSOLE_DIVIDER, flush=True)
            self._console_active_label = None

    def _console_guide(self, s: str) -> None:
        """引导模型的控制台输出：先关闭当前活动块（打印聊天块的结束分隔线并复位
        label），再打印引导内容。这样每次聊天模型输出都会重新带块头/分隔线，
        不再因上一个活动 label 仍为 "chat" 而与前后输出粘连。"""
        self._close_console_block()
        with agent_mode.CONSOLE_LOCK:
            print(s, end="", flush=True)

    def _console_cos_turn(self, system_prompt: str, input_text: str) -> None:
        """cos 角色模型：每个回合先打印「输入」块头，再紧跟流式「输出」，构成一对。

        - 输入 = 自循环提示词（仅首次完整呈现，避免每回合重复刷屏）+ 本轮最新输入
          （用户消息 / 系统注入的沉默提示）；若为自主延续回合则无新输入。
        - 不回显整段 role 消息历史，只呈现本轮最新提示词。
        - 上一个活动块若不是 cos，先关闭（补打分隔线）；同块连回合则直接换行分隔。
        """
        if self._console_active_label is not None and self._console_active_label != "cos":
            self._close_console_block()
        self._console_active_label = "cos"
        with agent_mode.CONSOLE_LOCK:
            print("\n--- 角色模型输出 ---", flush=True)
            print("输入：", flush=True)
            if system_prompt and not self._cos_system_printed:
                # 自循环提示词首次呈现即可，后续回合不再重复刷屏
                print(system_prompt, flush=True)
                self._cos_system_printed = True
            if input_text:
                print(input_text, flush=True)
            print("------", flush=True)
            print("输出：", flush=True)

    def _console_cos_body(self, s: str) -> None:
        """cos 角色模型原始输出内容：仅打印控制台，不渲染到对话区。"""
        if not s:
            return
        with agent_mode.CONSOLE_LOCK:
            print(s, end="", flush=True)

    def _flush_stream_label(self, label: str) -> None:
        bufs = self._stream_buffers.get(label)
        if not bufs:
            return
        combined = "".join(bufs)
        bufs.clear()
        self._stream_len[label] = 0
        self._stream_flush_at.pop(label, None)
        if combined:
            self.streamChunk.emit(label, combined)

    def _stream(self, label: str, chunk: str) -> None:
        """流式片段：控制台立即打印，信号跨线程做 30ms / 400 字符合并。

        合并策略：
        - 时间触发：首个 chunk 入队时开启 30ms 截止期，到期即 flush
        - 体积触发：当同一 label 缓冲字符数 >= 400，立即 flush
        - 避免 tiny chunk 导致每秒上百次跨线程排队事件，GUI 侧还有 50ms 二次合并
        """
        if not chunk:
            return
        # 新模型的输出块：先打块头（--- X输出 ---），再打印内容。
        # 块头 + 内容作为一段原子输出，避免被其它线程的 print 从中间劈开。
        with agent_mode.CONSOLE_LOCK:
            if self._console_active_label != label:
                self._close_console_block()
                self._console_active_label = label
                display = self._CONSOLE_LABELS.get(label, label)
                print(f"\n--- {display}输出 ---", flush=True)
            print(chunk, end="", flush=True)
        bufs = self._stream_buffers.setdefault(label, [])
        bufs.append(chunk)
        cur_len = self._stream_len.get(label, 0) + len(chunk)
        self._stream_len[label] = cur_len
        now = self._stream_time()
        # 截止期：如果是首个 chunk，设置 30ms 后 flush
        if label not in self._stream_flush_at:
            self._stream_flush_at[label] = now + (self._STREAM_BATCH_MS / 1000.0)
        # 体积够大 or 时间到 → 立即 flush
        deadline = self._stream_flush_at[label]
        if cur_len >= self._STREAM_BATCH_MAX_CHARS or now >= deadline:
            self._flush_stream_label(label)

    def _stream_agent(self, label: str, chunk: str) -> None:
        """自主代理模式的流式控制台输出（不走 _stream 的块头/分隔逻辑）。

        控制台的 `--- 自主代理模型 --- / 输入 / 输出 / ------------` 由
        主循环 _call_model 统一负责格式，这里只实时打印清洗后的文本片段，
        并把内容送入 streamChunk 信号（供浮窗“思考过程”实时显示）。
        """
        if not chunk:
            return
        # 自主代理模式的流式片段：与 _call_model 的「块头/输出/分割线」共用同一把
        # 控制台互斥锁（RLock，同线程可重入）。当它在 _call_model 的临界区内被回调时
        # 直接重入打印；若脱离该临界区被调用也能自行保证单次打印原子，杜绝交错。
        with agent_mode.CONSOLE_LOCK:
            print(chunk, end="", flush=True)
        bufs = self._stream_buffers.setdefault(label, [])
        bufs.append(chunk)
        cur_len = self._stream_len.get(label, 0) + len(chunk)
        self._stream_len[label] = cur_len
        if label not in self._stream_flush_at:
            self._stream_flush_at[label] = self._stream_time() + (self._STREAM_BATCH_MS / 1000.0)
        deadline = self._stream_flush_at[label]
        if cur_len >= self._STREAM_BATCH_MAX_CHARS or self._stream_time() >= deadline:
            self._flush_stream_label(label)

    # ------------------------------------------------------------------
    # 阻塞等待（纯 Python 轮询，兼容 worker 线程）
    # ------------------------------------------------------------------
    def _await_confirm(self, steps, screen_state, goal) -> Optional[Tuple[List[Dict[str, Any]], bool]]:
        """阻塞直到 UI 确认/取消规划。返回 (steps, proceed) 或 None。"""
        self._pending_confirm = None
        self.planReady.emit(steps, screen_state, goal)
        # 100ms 轮询 — 足够及时响应，CPU 占用极低
        while self._pending_confirm is None:
            if self._stopped:
                return None
            try:
                QCoreApplication.processEvents()
            except Exception:
                pass
            time.sleep(0.1)
        return self._pending_confirm

    def _await_consult(self, question: str, context: str = "") -> Optional[str]:
        """阻塞直到 UI 回答咨询。返回答案或 None（停止）。"""
        self._pending_answer = None
        self.consultRequested.emit(question, context)
        while self._pending_answer is None:
            if self._stopped:
                return None
            try:
                QCoreApplication.processEvents()
            except Exception:
                pass
            time.sleep(0.1)
        return self._pending_answer

    def _wait_resume(self) -> Optional[str]:
        """阻塞直到用户恢复/停止。返回补充信息或 None（停止）。"""
        self.statusMessage.emit("已暂停，点击恢复继续；可在输入框补充信息给规划模型")
        while self._paused and not self._stopped:
            try:
                QCoreApplication.processEvents()
            except Exception:
                pass
            time.sleep(0.1)
        # 恢复后排空补充信息
        return self._drain_injects() if not self._stopped else None

    # ------------------------------------------------------------------
    # UI → Worker 槽
    # ------------------------------------------------------------------
    @Slot(str)
    def submitGoal(self, text: str) -> None:
        if not text or not text.strip():
            return
        if self._busy:
            if self._mode == "agent_mode":
                # 自主代理模式：自动停止当前任务，重启新任务
                self.stop()
                # 当前任务已通过 stop() 停止并清理，_busy=False
                # 但需要回到事件循环重新调度，让旧任务的 finally 先执行完
                QTimer.singleShot(0, lambda: self._do_submit_goal(text.strip()))
                return
            if self._mode == "chat" and self._pro_chat is not None:
                # Pro 真人聊天：直接投递给正在运行的双模型会话
                self._pro_chat.feed_user(text.strip())
                return
            if self._mode == "chat" and self._cos_chat is not None:
                # cos 角色扮演：直接投递给正在运行的角色会话
                self._cos_chat.feed_user(text.strip())
                return
            if self._mode == "programming" and self._pro_programming is not None:
                # Pro 编程：直接投递给正在运行的编程会话（模型处理完当前轮后消费）
                self._pro_programming.feed_user(text.strip())
                return
            # 其他模式：执行中输入视为补充指令
            self.inject(text)
            return
        self._do_submit_goal(text.strip())

    def _do_submit_goal(self, text: str) -> None:
        """实际提交目标执行。"""
        self._busy = True
        self._stopped = False
        try:
            if self._mode == "chat":
                if self._cos_chat_enabled:
                    self._do_cos_chat(text.strip())
                elif self._pro_chat_enabled:
                    self._do_pro_chat(text.strip())
                else:
                    self._do_chat(text.strip())
            elif self._mode == "programming":
                if self._pro_programming_enabled:
                    self._do_pro_programming(text.strip())
                else:
                    self._do_programming(text.strip())
            elif self._mode == "agent_mode":
                self._do_agent_mode(text.strip())
            else:
                self._orchestrate(text.strip())
        except InterfaceUnavailable as e:
            self.error.emit("interface_unavailable", str(e))
        except _PausedInterrupt:
            pass
        except Exception as e:
            self.error.emit("unexpected", str(e))
        finally:
            self._busy = False
            self._paused = False
            self._state = "IDLE"

    @Slot(list, bool)
    def confirmPlan(self, steps, proceed: bool) -> None:
        self._pending_confirm = (steps, proceed)

    @Slot(str)
    def answerConsult(self, answer: str) -> None:
        self._pending_answer = answer

    @Slot()
    def pause(self) -> None:
        self._paused = True
        # 立即刷新流式缓冲，避免暂停后仍有残留 chunk 排队
        self.flush_stream()
        self.statusMessage.emit("已暂停：已停止所有模型输出和操作指令，点击恢复继续")

    @Slot()
    def resume(self) -> None:
        self._paused = False

    @Slot(list)
    def replaceRemainingSteps(self, new_remaining: list) -> None:
        """暂停时用用户编辑后的剩余步骤替换 self._steps 中 self._step_index 之后的部分。

        - 保留已执行段 [0, self._step_index) 不动
        - 新剩余段追加到已执行段后，重新分配 index
        - 仅允许在暂停态调用（self._paused=True），否则忽略
        - 不发 planReady：UI 侧已经在恢复前把显示同步好了，避免信号覆盖用户编辑
        """
        if not self._paused:
            return
        done_part = list(self._steps[: self._step_index])
        new_steps = self._reindex(done_part + list(new_remaining), start=0)
        self._steps = new_steps

    @Slot(str)
    def inject(self, text: str) -> None:
        if text and text.strip():
            self._injects.append(text.strip())
            self.statusMessage.emit("已收到补充信息，将在下一次规划时纳入")

    @Slot()
    def replanRemainingWhilePaused(self) -> None:
        """暂停态：用输入框补充信息让规划模型立即重规划剩余步骤并显示。

        在 _wait_resume 阻塞期间由 UI 触发（worker 线程嵌套事件循环会处理该槽）。
        重规划期间临时放行暂停检查，完成后恢复暂停态，等待用户确认后恢复执行。
        """
        if not self._paused or not self._injects:
            return
        supplement = self._drain_injects()
        if not supplement:
            return
        saved_paused = self._paused
        self._paused = False
        try:
            new_steps = self._do_replan(
                "用户补充信息：" + supplement + "，请据此重新规划剩余步骤。",
                replace_from=self._step_index,
            )
            if new_steps is not None:
                self._steps = new_steps
                self.planReady.emit(new_steps, "", self._goal)
        except Exception:
            # 暂停态重规划失败不应影响暂停状态
            import traceback
            traceback.print_exc()
        finally:
            self._paused = saved_paused

    @Slot()
    def stop(self) -> None:
        """停止当前任务，强制重置所有状态和唤醒所有阻塞点。

        确保在任何情况下停止都能快速返回空闲，强制设置 _busy=False
        保证新任务可以立即启动，不会卡在"当前有任务在执行中"。
        """
        self._stopped = True
        self._paused = False
        if self._pro_programming is not None:
            self._pro_programming.set_stopped(True)
            self._pro_programming.stop_process()
        # 强制重置繁忙标志，确保停止后立即能接受新任务
        # 即使当前正在流式响应网络，也能被上层立即重置
        self._busy = False

    @Slot(str)
    def switchMode(self, mode: str) -> None:
        # 离开聊天模式时结束正在运行的 Pro / cos 会话
        if mode != "chat":
            if self._pro_chat is not None:
                self._pro_chat.set_stopped(True)
            if self._cos_chat is not None:
                self._cos_chat.set_stopped(True)
        # 离开编程模式时停止运行并清理子进程，但保留 controller 和 history，
        # 下次切回来还能基于之前的上下文续跑。只有"新对话"才真正销毁。
        if mode != "programming":
            if self._pro_programming is not None:
                self._pro_programming.set_stopped(True)
                self._pro_programming.stop_process()
        if mode in ("chat", "agent", "programming", "agent_mode"):
            self._mode = mode

    @Slot(bool)
    def setProChat(self, enabled: bool) -> None:
        """开启/关闭 Pro 真人聊天。关闭时立即停止当前 Pro 会话。"""
        self._pro_chat_enabled = enabled
        if not enabled and self._pro_chat is not None:
            self._pro_chat.set_stopped(True)

    @Slot(bool)
    def setCosChat(self, enabled: bool) -> None:
        """开启/关闭 cos 角色扮演聊天。关闭时立即停止当前 cos 会话。"""
        self._cos_chat_enabled = enabled
        if not enabled and self._cos_chat is not None:
            self._cos_chat.set_stopped(True)

    @Slot(bool)
    def setProProgramming(self, enabled: bool) -> None:
        """开启/关闭 Pro 编程智能体。关闭时立即停止当前控制器并终止子进程。"""
        self._pro_programming_enabled = enabled
        if not enabled and self._pro_programming is not None:
            self._pro_programming.set_stopped(True)
            self._pro_programming.stop_process()

    @Slot(str)
    def setProProgrammingProject(self, path: str) -> None:
        """更新 Pro 编程当前项目文件夹。"""
        self._pro_programming_project = (path or "").strip()

    def resetProSession(self, truncate_before_user: Optional[str] = None) -> None:
        """用户撤销消息/还原项目后：停止运行、清理子进程。

        - truncate_before_user 不为空时，把 controller 的 history 截断到该用户消息之前
          （保证还原后模型看到的上下文与项目状态一致）
        - 为空时保留完整 history（仅 set_stopped + 清理子进程）
        只有用户主动点"新对话"才真正销毁 controller。
        """
        if self._pro_programming is not None:
            self._pro_programming.set_stopped(True)
            self._pro_programming.stop_process()
            if truncate_before_user:
                self._pro_programming.truncate_history_before_user(truncate_before_user)
        self._pro_programming_enabled = True

    def getProState(self) -> Optional[Dict]:
        """导出 Pro programming controller 的持久化 state（供 main_window 保存会话）。"""
        ctrl = self._pro_programming
        if ctrl is None:
            return None
        try:
            return ctrl.get_state()
        except Exception:
            return None

    def restoreProState(self, state: Dict) -> None:
        """从持久化 state 恢复 Pro programming controller（用户加载历史会话后调用）。"""
        if not state:
            return
        # 确保 controller 存在
        if self._pro_programming is None:
            self._pro_programming = programming_pro.ProProgrammingController(
                project_dir=self._pro_programming_project,
                on_agent_text=lambda t: self.proAgentHistory.emit(t),
                on_console=self.proConsole.emit,
                on_summary=lambda t, role="summary": self.proAgentMessage.emit(t, role),
                on_run_console=lambda s: self.proConsole.emit(f"[程序] {s}"),
                on_hint=lambda s: self.statusMessage.emit(s),
                on_agent_stream=self.proAgentStream.emit,
                on_tool_call=self.proToolCall.emit,
                on_schedule_plan=self.proSchedule.emit,
                on_schedule_done=self.proScheduleDone.emit,
                on_schedule_skip=self.proScheduleSkip.emit,
                on_schedule_all_done=self.proScheduleAllDone.emit,
                on_context_tokens=self.proContextTokens.emit,
                cancel_check=self._cancel_check,
            )
        if self._pro_programming is not None:
            self._pro_programming.set_state(state)

    def proUndoCommand(self, handle: str) -> None:
        """按 handle 撤销控制台中的一条文件操作。"""
        ctrl = self._pro_programming
        if ctrl is not None:
            ctrl.undo_command(handle)

    @Slot(bool)
    def setReplanEnabled(self, on: bool) -> None:
        """设置是否允许执行中自动重规划。

        母工作流工作流块关闭「重规划」时由主窗口调用 False，
        此时模板匹配失败或视觉无法定位将直接结束执行，不调用规划模型。
        """
        self._replan_enabled = on

    @Slot(str)
    def fixCode(self, prompt: str) -> None:
        """接收代码运行错误，自动调用编程模型修复。"""
        if self._busy:
            return
        self._busy = True
        self._stopped = False
        try:
            self._do_programming(prompt.strip())
        except InterfaceUnavailable:
            self.error.emit("interface_unavailable", "编程模型接口不可用，无法修复代码")
        except Exception as e:
            self.error.emit("fix_code_failed", str(e))
        finally:
            self._busy = False
            self._paused = False
            self._state = "IDLE"

    @Slot(str)
    def selectInterface(self, tag: str) -> None:
        interfaces = app_config.load_interfaces()
        if tag and tag in interfaces.get("presets", {}):
            interfaces["active"] = tag
            app_config.save_interfaces(interfaces)
            model_client.invalidate_cache()
            self.interfaceChanged.emit(tag)
            self.statusMessage.emit(f"已切换接口：{tag}")

    @Slot(bool)
    def setHideOnCapture(self, on: bool) -> None:
        capture.set_hide_on_capture(on)
        s = app_config.load_settings()
        s["hide_on_capture"] = on
        app_config.save_settings(s)
        self._settings = s

    @Slot()
    def reloadSettings(self) -> None:
        self._reload_settings()

    @Slot()
    def newConversation(self) -> None:
        if self._pro_chat is not None:
            self._pro_chat.set_stopped(True)
        if self._cos_chat is not None:
            self._cos_chat.set_stopped(True)
        if self._pro_programming is not None:
            self._pro_programming.set_stopped(True)
            self._pro_programming.stop_process()
            self._pro_programming = None
        # 新对话：重建上下文的唯一时机，自主代理保留的历史状态一并清除
        self._agent_saved_state.clear()
        self._agent_saved_context.clear()
        self._chat_history = []
        self._steps = []
        self._goal = ""
        self._injects.clear()

    @Slot(str)
    def setGoal(self, goal: str) -> None:
        """设置当前目标（供工作流编辑时重规划使用，不触发规划）。"""
        self._goal = goal

    @Slot()
    def resetBusy(self) -> None:
        """重置繁忙状态（用于 UI 提供应急恢复）。"""
        self._busy = False
        self._paused = False
        self._stopped = False
        self._state = "IDLE"

    @Slot(list, str)
    def executeSteps(self, steps: list, goal_text: str = "工作流执行") -> None:
        """直接执行一组步骤（跳过规划/确认）。

        用于：任务完成后的「执行」按钮、工作流面板的「执行」按钮。
        长输入步骤清除已生成文本，执行时重新生成。

        若当前有任务在执行中，自动停止当前任务后再执行新任务，
        不再报"当前有任务在执行中"错误。
        """
        if self._busy:
            # 自动停止当前任务
            self.stop()
            # 当前任务正在执行中，stop() 设了 _busy=False 并唤醒所有阻塞点，
            # 当前执行循环会检测到 _stopped 并退出，然后 finally 块会再次设 _busy=False。
            # 所以这里只需等待一个 event loop 周期让当前任务完成清理，
            # 然后重新检查 _busy 状态。
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._do_execute_steps(steps, goal_text))
            return
        self._do_execute_steps(steps, goal_text)

    def _do_execute_steps(self, steps: list, goal_text: str) -> None:
        """实际执行步骤的内部方法。"""
        self._busy = True
        self._stopped = False
        try:
            self._goal = goal_text
            self._steps = self._reindex(steps)
            self._step_index = 0
            # 长输入步骤：清除已生成文本，执行时重新生成
            for s in self._steps:
                if s.get("op") == OP_LONG_INPUT:
                    s["text"] = None
                    s["text_full"] = None
            self.assistantMessage.emit(
                f"开始执行：{goal_text}（共 {len(self._steps)} 步）", "assistant"
            )
            self.executionStarted.emit()
            self._execute_loop()
            summary = "已停止" if self._stopped else "执行完成"
            self.executionFinished.emit(summary)
        except InterfaceUnavailable:
            raise
        except Exception as e:
            self.error.emit("unexpected", str(e))
        finally:
            self._busy = False
            self._paused = False
            self._state = "IDLE"

    # ------------------------------------------------------------------
    # 聊天模式
    # ------------------------------------------------------------------
    def _do_chat(self, text: str) -> None:
        self._state = "PLANNING"
        self._chat_history.append({"role": "user", "content": text})
        chunks: List[str] = []

        def on_chunk(c: str) -> None:
            chunks.append(c)
            self._stream("chat", c)

        # 通知 UI：模型调用开始（对话区状态条显示“正在连接接口”）
        self.modelCallStarted.emit("chat")
        full = model_client.call_model(
            "chat", list(self._chat_history), stream=True,
            temperature=self._settings.get("temperature", 0.6) if "temperature" in self._settings else 0.6,
            on_chunk=on_chunk, cancel_check=self._cancel_check,
        )
        # 模型返回：立即 flush 流式缓冲，避免最后几个 chunk 延迟
        self.flush_stream()
        if not full:
            full = "".join(chunks)
        self._chat_history.append({"role": "assistant", "content": full})
        self.assistantMessage.emit(full, "assistant")
        self._state = "IDLE"

    def _do_pro_chat(self, text: str) -> None:
        """Pro 真人聊天：双模型（引导模型 ↔ 聊天模型）编排，保持连续会话。

        第一次进入时创建控制器；之后用户新消息在 busy 时直接投递 feed_user，
        由运行中的会话实时处理，直到被停止/暂停/切换模式才结束。
        """
        self._state = "PLANNING"
        if self._pro_chat is None:
            self._pro_chat = pro_chat.ProChatController(
                on_chat_chunk=lambda c: self._stream("chat", c),
                on_chat_message=lambda t: self.assistantMessage.emit(t, "assistant"),
                on_chat_flush=self.flush_stream,
                on_guide_chunk=self._console_guide,
                on_hint=lambda s: self.statusMessage.emit(s),
                cancel_check=self._cancel_check,
                chat_history=list(self._chat_history),
            )
            # 通知 UI：模型调用开始（对话区状态条显示“正在连接接口”）
            self.modelCallStarted.emit("chat")
        self._pro_chat.feed_user(text)
        try:
            self._pro_chat.run()
        finally:
            # 模型结束：flush 流式缓冲，避免最后几个 chunk 延迟
            self.flush_stream()
            if self._pro_chat is not None:
                self._pro_chat.set_stopped(True)
                self._pro_chat = None
            self._state = "IDLE"

    def _do_cos_chat(self, text: str) -> None:
        """cos 角色扮演聊天：单「角色模型」自循环会话。

        第一次进入时创建控制器；之后用户新消息在 busy 时直接投递 feed_user，
        由运行中的会话实时处理，直到 [end]/被停止/暂停/切换模式才结束。
        """
        self._state = "PLANNING"
        if self._cos_chat is None:
            self._cos_chat = cos_chat.CosChatController(
                # 角色模型原始输出（含 [say]/[wait]/[end] 命令符）仅打印控制台；
                # 对话区通过 on_say_stream 边生成边流式打字，on_say_done 提交整条气泡。
                # on_model_input 让控制台每回合先打印【自循环提示词 + 本轮最新输入】，
                # 再紧跟流式输出，形成「一条输入对一条输出」的配对，多回合互不粘连。
                on_model_input=self._console_cos_turn,
                on_chunk=self._console_cos_body,
                on_say_stream=lambda c: self._stream("cos", c),
                on_say_done=lambda t: (self.flush_stream(), self.assistantMessage.emit(t, "assistant")),
                on_hint=lambda s: self.statusMessage.emit(s),
                cancel_check=self._cancel_check,
                chat_history=list(self._chat_history),
            )
            # 通知 UI：模型调用开始（对话区状态条显示“正在连接接口”）
            self.modelCallStarted.emit("cos")
        self._cos_chat.feed_user(text)
        try:
            self._cos_chat.run()
        finally:
            # 模型结束：flush 流式缓冲，避免最后几个 chunk 延迟
            self.flush_stream()
            if self._cos_chat is not None:
                self._cos_chat.set_stopped(True)
                self._cos_chat = None
            self._state = "IDLE"

    # ------------------------------------------------------------------
    # 编程模式
    # ------------------------------------------------------------------

    def _do_programming(self, text: str) -> None:
        """编程模式：用编程模型生成代码，流式输出。"""
        self._state = "PLANNING"
        self._chat_history.append({"role": "user", "content": text})
        preset = app_config.get_preset_by_label("programming")
        system_prompt = app_config.get_system_prompt("programming", preset)
        messages = [
            {"role": "system", "content": system_prompt},
        ] + list(self._chat_history)

        chunks: List[str] = []

        def on_chunk(c: str) -> None:
            chunks.append(c)
            self._stream("programming", c)

        # 通知 UI：模型调用开始（对话区状态条显示“正在连接接口”）
        self.modelCallStarted.emit("programming")
        full = model_client.call_model(
            "programming", messages, stream=True,
            temperature=self._settings.get("temperature", 0.3) if "temperature" in self._settings else 0.3,
            on_chunk=on_chunk, cancel_check=self._cancel_check,
        )
        self.flush_stream()
        if not full:
            full = "".join(chunks)
        self._chat_history.append({"role": "assistant", "content": full})
        # assistantMessage 的 role 用 "programming" 让 UI 区分代码气泡
        self.assistantMessage.emit(full, "programming")
        self._state = "IDLE"

    def _do_pro_programming(self, text: str) -> None:
        """Pro 编程智能体：高级编程模型 ↔ 检查模型 协作自循环。

        第一次进入时创建控制器；之后用户新消息在 busy 时直接投递 feed_user，
        由运行中的会话实时处理，直到 [summary]/被停止/暂停/切换模式才结束。
        """
        self._state = "PLANNING"
        if self._pro_programming is None:
            self.modelCallStarted.emit("programmer")
            self._pro_programming = programming_pro.ProProgrammingController(
                project_dir=self._pro_programming_project,
                on_agent_text=lambda t: self.proAgentHistory.emit(t),
                on_console=self.proConsole.emit,
                on_summary=lambda t, role="summary": self.proAgentMessage.emit(t, role),
                on_run_console=lambda s: self.proConsole.emit(f"[程序] {s}"),
                on_hint=lambda s: self.statusMessage.emit(s),
                on_agent_stream=self.proAgentStream.emit,
                on_tool_call=self.proToolCall.emit,
                on_schedule_plan=self.proSchedule.emit,
                on_schedule_done=self.proScheduleDone.emit,
                on_schedule_skip=self.proScheduleSkip.emit,
                on_schedule_all_done=self.proScheduleAllDone.emit,
                on_context_tokens=self.proContextTokens.emit,
                cancel_check=self._cancel_check,
            )
        self._pro_programming.feed_user(text)
        try:
            self._pro_programming.run()
        finally:
            self.flush_stream()
            # 无论正常结束还是用户中途停止，都保留 controller 和完整 history，
            # 只清理子进程。上下文清空的唯一时机是用户主动点"新对话"（newConversation）
            if self._pro_programming is not None:
                self._pro_programming.stop_process()
            self._state = "IDLE"

    # ------------------------------------------------------------------
    # 自主代理模式
    # ------------------------------------------------------------------
    def _do_agent_mode(self, goal: str) -> None:
        """自主代理模式：使用多模态模型自主截图、决策、执行，直到任务完成。

        不生成步骤列表，不等待用户确认。模型直接观察截图并输出动作指令，
        系统自动解析和执行。支持聚焦（局部放大截图）。
        """
        self._state = "PLANNING"
        self._goal = goal
        self.assistantMessage.emit(
            f"目标：{goal}\n进入自主代理模式，模型将自动截图、决策和执行。", "assistant"
        )
        self.modelCallStarted.emit(agent_mode.LABEL)
        self.executionStarted.emit()

        def on_chunk(chunk: str) -> None:
            self._stream_agent(agent_mode.LABEL, chunk)

        def on_status(status: str) -> None:
            self.statusMessage.emit(status)

        def on_screenshot(shot: bytes) -> None:
            self.screenshotTaken.emit(shot)

        def on_action(action: str, params: dict, result: str) -> None:
            self.agentAction.emit(action, params, result)

        def on_model_time(secs: float) -> None:
            self.agentModelTime.emit(secs)

        def on_task_time(secs: int) -> None:
            self.agentTaskTime.emit(secs)

        executor = agent_mode.AgentModeExecutor(
            on_chunk=on_chunk,
            cancel_check=self._cancel_check,
            on_screenshot=on_screenshot,
            on_status=on_status,
            on_action=on_action,
            on_model_time=on_model_time,
            on_task_time=on_task_time,
        )

        try:
            # 如果有保存的完整状态（暂停后恢复），传给 executor
            initial_state = dict(self._agent_saved_state) if self._agent_saved_state else None
            if initial_state:
                # 续传目标并入上下文：若末条已是 user 消息则合并进去，避免连续多条 user 消息
                msg_list = initial_state.setdefault("messages", [])
                if msg_list and msg_list[-1].get("role") == "user":
                    msg_list[-1]["content"] = f"{msg_list[-1]['content']}\n\n（任务续传）用户目标：{goal}"
                else:
                    msg_list.append({"role": "user", "content": f"（任务续传）用户目标：{goal}"})
            result = executor.run(goal, initial_state=initial_state)
            self.flush_stream()
            self.assistantMessage.emit(result, "assistant")
            if self._stopped:
                # 暂停/停止时保存完整状态（含消息历史与聚焦/结果区等视觉状态）供后续续传
                self._agent_saved_state = executor.get_state()
                self._agent_saved_context.clear()
                summary = "已暂停"
            else:
                # 正常完成：保留全部上下文（消息历史）供同一次会话继续发布任务；
                # 但重置本次任务的瞬时视觉状态（聚焦/操作区结果区），下次任务从全屏观察开始。
                state = executor.get_state()
                state["focus_mode"] = False
                state["last_focus_params"] = {}
                state["region_mode"] = False
                state["op_buttons"] = {}
                state["result_region"] = {}
                state["pending_full_shot"] = False
                self._agent_saved_state = state
                self._agent_saved_context.clear()
                summary = f"任务完成：{result}"
            self.executionFinished.emit(summary)
        except InterfaceUnavailable:
            self.flush_stream()
            raise
        except Exception as e:
            self.flush_stream()
            self.error.emit("agent_mode_error", str(e))
            self.executionFinished.emit(f"任务异常：{e}")
        finally:
            self._state = "IDLE"

    # ------------------------------------------------------------------
    # 代理模式编排
    # ------------------------------------------------------------------
    def _orchestrate(self, goal: str) -> None:
        self._goal = goal
        self._state = "PLANNING"

        # 1. 规划
        steps = self._run_planning(goal, None)
        if steps is None or self._stopped:
            return
        # 规划被暂停中断：等待恢复后重新规划
        if self._paused:
            self._wait_resume()
            if self._stopped:
                return
            return self._orchestrate(goal)

        # 规划模型请求用户裁决
        if len(steps) == 1 and steps[0].get("op") == OP_CONSULT:
            ans = self._await_consult(steps[0].get("question", "需要更多信息"), "规划模型请求用户输入")
            if self._stopped or not ans:
                return
            # 用用户的澄清作为新目标重新规划
            self._orchestrate(ans)
            return

        self._steps = self._reindex(steps)
        self.assistantMessage.emit(
            "目标：" + goal + "\n已规划 " + str(len(self._steps)) + " 个步骤，请确认后执行。", "assistant"
        )

        # 2. 确认（只在初始规划后做一次）
        #    proceed=True → 用户点「确认执行」，开始执行
        #    proceed=False → 用户在对话框输入提示让模型修改，重规划后重新显示
        #    （手动编辑由「修改」按钮在 UI 层处理，不进入此循环）
        replan_count = 0
        replan_limit = self._settings.get("replan_limit", 5)
        while True:
            confirm = self._await_confirm(self._steps, "", goal)
            if confirm is None or self._stopped:
                return
            confirmed, proceed = confirm
            if proceed:
                self._steps = self._reindex(confirmed)
                break
            # proceed=False：对话框修改，用补充信息调模型重规划
            if replan_count >= replan_limit:
                self.error.emit("replan_limit", "已达到重规划上限")
                return
            supplement = self._drain_injects()
            new_steps = self._do_replan(
                "用户要求调整：" + (supplement or "请重新规划剩余步骤"), replace_from=0
            )
            if new_steps is None or self._stopped:
                return
            self._steps = new_steps
            replan_count += 1
            # 循环回 _await_confirm：_await_confirm 内部会 emit planReady 显示新方案

        # 3. 执行
        self.executionStarted.emit()
        self._step_index = 0
        self._execute_loop()
        summary = "任务已停止" if self._stopped else "任务执行完成"
        self.executionFinished.emit(summary)

    def _run_planning(self, goal: str, extra_context: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        """运行规划模型（含重试），返回步骤列表或 None。"""
        try:
            shot = self._capture()
        except Exception as e:
            self.error.emit("capture_failed", f"截图失败：{e}")
            return None

        try:
            _, goal_text, steps, raw = planner.plan(
                goal, screenshot_bytes=shot, extra_context=extra_context,
                on_chunk=lambda c: self._stream(planner.LABEL, c),
                cancel_check=self._cancel_check,
                max_retry=self._settings.get("planner_retry", 2),
            )
            # 规划完成，flush 流式缓冲避免末尾延迟
            self.flush_stream()
        except InterfaceUnavailable:
            raise
        except ParseError as e:
            self.flush_stream()
            # 暂停导致的中断：不报错、不咨询，直接返回 None
            if self._paused:
                return None
            self.error.emit("parse_fail", str(e))
            ans = self._await_consult("规划模型输出无法解析，请补充说明或调整目标后回复。", str(e))
            if ans and not self._stopped:
                return self._run_planning(ans, None)
            return None
        return steps

    def _do_replan(self, feedback: str, replace_from: int) -> Optional[List[Dict[str, Any]]]:
        """重新规划：保留已完成步骤，替换 replace_from 起的剩余步骤。

        不等待用户确认——确认只在初始规划后做一次。
        执行中的重规划直接返回新步骤，由调用方决定是否继续执行。
        """
        # 收集已完成步骤（含 replace_from 当前步）中的输入/长输入内容，
        # 把变量名+内容传给重规划模型，提示「非必要不改动」
        from step_model import OP_INPUT, OP_LONG_INPUT
        preserved: List[Dict[str, Any]] = []
        for s in self._steps[:replace_from]:
            op = s.get("op")
            if op == OP_INPUT:
                tgt = s.get("target", "") or s.get("targets", [""])[0]
                preserved.append({
                    "op": op,
                    "index": s.get("index", ""),
                    "target": tgt,
                    "var_name": s.get("text_var"),
                    "content": s.get("text") or s.get("text_full"),
                    "description": None,
                })
            elif op == OP_LONG_INPUT:
                tgt = s.get("target", "") or s.get("targets", [""])[0]
                content = s.get("text_full") or s.get("text")
                preserved.append({
                    "op": op,
                    "index": s.get("index", ""),
                    "target": tgt,
                    "var_name": s.get("text_var"),
                    "content": content,
                    "description": s.get("description"),
                })
        try:
            shot = self._capture()
        except Exception as e:
            self.error.emit("capture_failed", f"截图失败：{e}")
            return None
        try:
            screen_state, goal_text, new_steps, raw = planner.replan(
                self._goal, feedback, screenshot_bytes=shot,
                preserved_inputs=preserved or None,
                on_chunk=lambda c: self._stream(planner.LABEL, c),
                cancel_check=self._cancel_check,
                max_retry=self._settings.get("planner_retry", 2),
            )
            # 重规划完成，flush 流式缓冲
            self.flush_stream()
        except InterfaceUnavailable:
            raise
        except ParseError as e:
            self.flush_stream()
            # 暂停导致的中断：不报错，直接返回 None
            if self._paused:
                return None
            self.error.emit("parse_fail", str(e))
            return None

        done = self._steps[:replace_from]
        new = self._reindex(new_steps, start=len(done))
        return done + new

    # ------------------------------------------------------------------
    # 执行循环
    # ------------------------------------------------------------------
    def _execute_loop(self) -> None:
        self._state = "EXECUTING"
        replan_total = 0
        replan_limit = self._settings.get("replan_limit", 5)
        scroll_amount = self._settings.get("scroll_amount", 5)
        move_duration = float(self._settings.get("mouse_move_duration", 0.0))
        i = 0
        _max_iterations = len(self._steps) * 50 + 200  # 安全上限：防止无限循环卡死 _busy
        _iter_count = 0
        while i < len(self._steps) and not self._stopped:
            _iter_count += 1
            if _iter_count > _max_iterations:
                self.error.emit("loop_limit", f"执行循环超过安全上限({_max_iterations}次)，已自动终止")
                break
            self._step_index = i
            self._update_progress(i, len(self._steps))
            # 暂停检查
            if self._stopped:
                break
            if self._paused:
                supplement = self._wait_resume()
                if self._stopped:
                    break
                if supplement:
                    new_steps = self._do_replan(
                        "用户补充信息：" + supplement + "，请据此重新规划剩余步骤。", replace_from=i
                    )
                    if new_steps is not None:
                        self._steps = new_steps
                        self.planReady.emit(new_steps, "", self._goal)
                        replan_total += 1
                        continue
                    if self._stopped:
                        break

            step = self._steps[i]
            op = step.get("op", "")

            # 用户手动跳过的步骤：标记为 skipped，直接跳过
            if step.get("user_skip"):
                step["status"] = "skipped"
                self.stepStatusChanged.emit(i, "skipped", "用户跳过")
                i += 1
                continue

            # 咨询用户步骤
            if op == OP_CONSULT:
                ans = self._await_consult(step.get("question", "需要用户指示"), "执行中咨询")
                if self._stopped:
                    break
                if ans:
                    new_steps = self._do_replan(
                        "用户回答：" + ans + "，请据此调整剩余步骤。", replace_from=i
                    )
                    if new_steps is not None:
                        self._steps = new_steps
                        self.planReady.emit(new_steps, "", self._goal)
                        continue
                # 咨询步骤本身标记为 done（已处理）
                step["status"] = "done"
                self.stepStatusChanged.emit(i, "done", "")
                i += 1
                continue

            try:
                # —— 视觉定位 ——
                nvis = OP_VISION_CALLS.get(op, 0)
                if nvis > 0:
                    # 模板匹配模式：用模板图片在屏幕上实时查找
                    if step.get("template_highlighted") and step.get("template_path"):
                        self._check_interrupt()
                        ok = self._locate_by_template(step, i)
                        if not ok:
                            step["status"] = "failed"
                            self.stepStatusChanged.emit(i, "failed", "模板匹配失败，未在屏幕上找到目标元素")
                            # 未开启重规划：直接结束执行，不使用规划模型
                            if not self._replan_enabled:
                                i += 1
                                break
                            if replan_total >= replan_limit:
                                self._await_consult("模板匹配多次失败，已超过重规划上限，请指示如何继续。")
                                i += 1
                                continue
                            new_steps = self._do_replan(
                                "模板匹配失败，未在屏幕上找到目标元素", replace_from=i
                            )
                            if new_steps is not None:
                                self._steps = new_steps
                                self.planReady.emit(new_steps, "", self._goal)
                                replan_total += 1
                                continue
                            if self._stopped:
                                break
                            i += 1
                            continue
                    elif step.get("coord_locked") and self._has_valid_coords(step):
                        # 坐标锁定模式：跳过视觉模型，直接使用已有坐标
                        self._check_interrupt()
                        step["status"] = "locating"
                        self.stepStatusChanged.emit(i, "locating", "")
                    else:
                        self._check_interrupt()
                        ok = self._locate_step(step, i)
                        if not ok:
                            # 定位失败 → 标记 failed，再决定重规划或跳过
                            step["status"] = "failed"
                            self.stepStatusChanged.emit(i, "failed", self._locate_fail_feedback(step))
                            # 未开启重规划：直接结束执行，不使用规划模型
                            if not self._replan_enabled:
                                i += 1
                                break
                            # 定位失败 → 反馈规划模型重规划
                            if replan_total >= replan_limit:
                                self._await_consult("多次无法定位目标，已超过重规划上限，请指示如何继续。")
                                i += 1
                                continue
                            new_steps = self._do_replan(
                                self._locate_fail_feedback(step), replace_from=i
                            )
                            if new_steps is not None:
                                self._steps = new_steps
                                self.planReady.emit(new_steps, "", self._goal)
                                replan_total += 1
                                continue
                            if self._stopped:
                                break
                            i += 1
                            continue

                # —— 执行 ——
                step["status"] = "executing"
                self.stepStatusChanged.emit(i, "executing", "")
                self._check_interrupt()
                # —— 长输入步骤：在当前焦点处由聊天模型流式生成并分段粘贴
                if op == OP_LONG_INPUT:
                    self._execute_long_input(step, i)
                else:
                    # 普通自动化操作：执行前再做一次中断检查（执行器系统调用通常几十毫秒，不再内部打断）
                    executor.execute_step(
                        step, screen_size=self._screen_size, scroll_amount=scroll_amount, move_duration=move_duration
                    )
                # —— 完成（只有函数完整跑完且没有抛中断才允许标记 done）——
                self._check_interrupt()
                step["status"] = "done"
                self.stepStatusChanged.emit(i, "done", "")
                self._update_progress(i + 1, len(self._steps))
                i += 1
            except executor.UnrecognizedParam as e:
                self.error.emit("param_invalid", str(e))
                # 标记失败
                step["status"] = "failed"
                self.stepStatusChanged.emit(i, "failed", str(e))
                if replan_total >= replan_limit:
                    self._await_consult("步骤参数无法识别且已达重规划上限，请指示。", str(e))
                    i += 1
                    continue
                new_steps = self._do_replan(
                    f"步骤「{step.get('raw','')}」参数无法被识别执行：{e}，请调整。", replace_from=i
                )
                if new_steps is not None:
                    self._steps = new_steps
                    self.planReady.emit(new_steps, "", self._goal)
                    replan_total += 1
                    continue
                if self._stopped:
                    break
                i += 1
                continue
            except _PausedInterrupt:
                # 关键：暂停/停止时触发，不标记状态、不 i++，直接 continue
                # 下一轮 while 开头会再次进入 _wait_resume（恢复时从当前步 i 重新开始整步执行）
                # 如果是停止（_stopped=True），下一轮 while 开头的 _stopped 检查会 break
                self.flush_stream()
                continue
            except Exception as e:
                self.error.emit("executor_unknown", str(e))
                # 标记失败
                step["status"] = "failed"
                self.stepStatusChanged.emit(i, "failed", str(e))
                i += 1
                continue

        # 停止时：剩余未完成步骤标记为 skipped
        if self._stopped:
            for j in range(i, len(self._steps)):
                s = self._steps[j]
                if s.get("status") not in ("done", "failed"):
                    s["status"] = "skipped"
                    self.stepStatusChanged.emit(j, "skipped", "已停止")

        self._update_progress(len(self._steps), len(self._steps))

    def _execute_long_input(self, step: Dict[str, Any], index: int) -> None:
        """执行「长输入」步骤：模型流式生成 → 分段粘贴到当前焦点位置。

        无需视觉定位和点击聚焦——假设前序步骤已将焦点置于目标输入框。
        独立的一次模型请求，不携带任何对话历史（不读 self._chat_history，
        也不写入），messages 仅含本次的 system + user。其中 user 只发送
        「写作要求/主题」本身（step.description），不附带总体任务目标、
        输入位置元素名等上下文——长输入的写作上下文就是 description，
        多余信息会干扰模型按要求聚焦输出。使用独立 label ``long_input``
        （未配置该预设时回退到 active），可与 chat/planning 分别配置
        模型/温度/max_tokens。

        全文写入 step["text"] 和 step["text_full"]（便于折叠步骤列表查看、
        变量化按钮呈现）。流式片段同时发射 streamChunk(label="long_input", chunk)
        供 UI/控制台实时显示。

        关键节点加 _check_interrupt：若暂停/停止，抛 _PausedInterrupt 原路返回，
        不写入 step["text"]，交由外层执行循环重新进入 _wait_resume，恢复时
        从本步骤第 0 步重新开始（不标记 done/failed）。
        """
        self._check_interrupt()

        # 组装本次请求的 prompt：仅 description（写作要求/主题），
        # 不附总体任务目标、不附输入位置名等额外上下文，直接发给模型。
        # 注意：description 可能被变量化替换成了变量名（如 {输入1}），
        # 此时从 text_full / desc_full 恢复原始主题文本。
        desc = step.get("description") or ""
        if not desc or (desc.startswith("{") and desc.endswith("}")):
            desc = step.get("desc_full") or step.get("text_full") or ""
        system = app_config.get_system_prompt("long_input", app_config.get_preset_by_label("long_input"))
        if desc:
            user_text = desc.strip()
        else:
            user_text = "请为当前输入框撰写一段合适的正文。"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]

        # 3. 调用聊天模型流式生成，并分段粘贴
        # 分段策略：累积到 PASTE_THRESHOLD 字符或遇到换行符就粘贴一次
        PASTE_THRESHOLD = 120
        chunks: List[str] = []
        buffer: List[str] = []
        buffer_len = 0

        def _flush(buf: List[str], start: int, end: int) -> None:
            """把 buf[start:end] 拼起来粘贴，并从 buf 中删除。"""
            if start >= end:
                return
            seg = "".join(buf[start:end])
            if seg:
                executor.paste_text_chunk(seg)
            del buf[start:end]

        def _on_chunk(c: str) -> None:
            nonlocal buffer_len
            if not c:
                return
            chunks.append(c)
            # 发到 UI / 控制台流
            self._stream("long_input", c)
            buffer.append(c)
            buffer_len += len(c)
            # 从 buffer 中扫描换行："一段 chunk 里可能有多个换行"
            # 简化策略：用"缓冲区当前全部字符串"一次性扫描，找到可整段粘贴的位置
            # 用一个 pointer 指向已处理完的下一段起始（buffer 存 chunk 列表，
            # 但换行可能在 chunk 中间；所以这里用 flatten 后再 slice 成本高，
            # 采用更简单策略：逐 chunk 判断）
            while buffer:
                head = buffer[0]
                # 1) 如果首块里有换行，切到第一个换行（含换行）粘贴
                nl = head.find("\n")
                if nl != -1:
                    first_part = head[: nl + 1]  # 含 \n
                    rest = head[nl + 1 :]
                    executor.paste_text_chunk(first_part)
                    if rest:
                        buffer[0] = rest
                        buffer_len = buffer_len - len(first_part)
                    else:
                        del buffer[0]
                        buffer_len = buffer_len - len(first_part)
                    continue
                # 2) 如果没换行但 buffer 总长度 >= 阈值，整段粘贴（除最后一个 chunk 外）
                if buffer_len >= PASTE_THRESHOLD and len(buffer) > 1:
                    # 粘贴 buffer[:-1]，最后一个 chunk 留下以便下次遇到换行
                    to_paste = buffer[:-1]
                    del buffer[:-1]
                    segment = "".join(to_paste)
                    buffer_len = buffer_len - len(segment)
                    executor.paste_text_chunk(segment)
                    continue
                break

        try:
            # 独立 label "long_input"：未配置该预设时回退到 active
            # 不传 chat_history，messages 仅含本次 system+user
            gen = model_client.call_model(
                "long_input", messages, stream=True,
                temperature=self._settings.get("temperature", 0.6),
                on_chunk=_on_chunk, cancel_check=self._cancel_check,
            )
            # 模型返回：flush 流式缓冲，确保最后一段不延迟
            self.flush_stream()
            self._check_interrupt()
        except InterfaceUnavailable:
            self.flush_stream()
            raise
        except _PausedInterrupt:
            self.flush_stream()
            raise

        # 4. 收尾：把 buffer 中剩余内容粘贴（最后一块，可能 < 阈值）
        self._check_interrupt()
        if buffer:
            executor.paste_text_chunk("".join(buffer))
            buffer.clear()
            buffer_len = 0

        # 5. 保存全文到 step（优先用 call_model 返回值，否则用 chunks 拼接）
        self._check_interrupt()
        final_text = gen if gen else "".join(chunks)
        step["text"] = final_text
        step["text_full"] = final_text

    def _locate_step(self, step: Dict[str, Any], index: int) -> bool:
        """视觉定位并填充坐标。成功返回 True，失败（None/异常）返回 False。

        若中途被暂停/停止，抛出 _PausedInterrupt 由执行循环捕获，避免
        把"用户主动暂停"当作"定位失败"标记为 failed。
        """
        step["status"] = "locating"
        self.stepStatusChanged.emit(index, "locating", "")
        self._check_interrupt()
        try:
            shot = self._capture()
        except Exception as e:
            self.error.emit("capture_failed", f"截图失败：{e}")
            return False
        self._check_interrupt()
        try:
            if step.get("op") == OP_DRAG:
                start, end = vision.locate_pair(
                    step.get("start_target", ""), step.get("start_location", ""),
                    step.get("end_target", ""), step.get("end_location", ""),
                    shot, max_retry=self._settings.get("vision_retry", 2),
                    cancel_check=self._cancel_check,
                )
                self._check_interrupt()
                step["start_coord"] = start
                step["end_coord"] = end
                if not start or not end:
                    return False
                if not validate_coord(start) or not validate_coord(end):
                    return False
            else:
                coord = vision.locate(
                    step.get("target", ""), step.get("location", ""),
                    shot, max_retry=self._settings.get("vision_retry", 2),
                    cancel_check=self._cancel_check,
                )
                self._check_interrupt()
                step["coord"] = coord
                if coord is None:
                    return False
                if not validate_coord(coord):
                    return False
        except InterfaceUnavailable:
            raise
        except _PausedInterrupt:
            # 让调用方（_execute_loop）再次进入 wait_resume
            raise
        except Exception as e:
            self.error.emit("vision_failed", f"视觉定位异常：{e}")
            return False
        # 定位成功：把坐标同步到 UI 侧（worker 的 step 是跨线程副本，
        # UI 侧 session step 不会自动更新，需通过信号显式同步）
        coord_data = {}
        if step.get("op") == OP_DRAG:
            coord_data["start_coord"] = step.get("start_coord")
            coord_data["end_coord"] = step.get("end_coord")
        else:
            coord_data["coord"] = step.get("coord")
        self.stepCoordUpdated.emit(index, coord_data)
        return True

    def _locate_by_template(self, step: Dict[str, Any], index: int) -> bool:
        """用模板图片在屏幕上进行模板匹配定位。

        成功时更新 step 的坐标并同步到 UI，返回 True。
        匹配失败或异常时返回 False（仍保留原坐标作为回退）。
        """
        step["status"] = "locating"
        self.stepStatusChanged.emit(index, "locating", "")
        self._check_interrupt()

        template_path = step.get("template_path", "")
        if not template_path or not os.path.isfile(template_path):
            return False

        try:
            # 优先用步骤级配置的阈值，其次 settings 全局配置，最后默认 0.65
            threshold = step.get(
                "template_threshold",
                self._settings.get("template_threshold", 0.65),
            )
            result = template_matcher.match_template_on_screen(
                template_path, threshold=float(threshold),
            )
            self._check_interrupt()
            if result is not None:
                x, y, confidence = result
                if step.get("op") == OP_DRAG:
                    # 拖动只匹配起点
                    step["start_coord"] = (x, y)
                else:
                    step["coord"] = (x, y)
                # 同步坐标到 UI
                coord_data: Dict[str, Any] = {}
                if step.get("op") == OP_DRAG:
                    coord_data["start_coord"] = step.get("start_coord")
                    coord_data["end_coord"] = step.get("end_coord")
                else:
                    coord_data["coord"] = step.get("coord")
                self.stepCoordUpdated.emit(index, coord_data)
                return True
            return False
        except _PausedInterrupt:
            raise
        except Exception:
            return False

    @staticmethod
    def _has_valid_coords(step: Dict[str, Any]) -> bool:
        """检查步骤是否已有有效坐标（锁定后可跳过视觉模型）。"""
        op = step.get("op", "")
        if op == OP_DRAG:
            return validate_coord(step.get("start_coord")) and validate_coord(step.get("end_coord"))
        return validate_coord(step.get("coord"))

    @staticmethod
    def _locate_fail_feedback(step: Dict[str, Any]) -> str:
        op = step.get("op", "")
        if op == OP_DRAG:
            return (
                f"在「{step.get('start_location','')}」未找到「{step.get('start_target','')}，"
                f"或在「{step.get('end_location','')}」未找到「{step.get('end_target','')}」，"
                "请插入意外处理步骤或重新规划。"
            )
        return (
            f"在「{step.get('location','')}」未找到「{step.get('target','')}」，"
            "请插入意外处理步骤或重新规划。"
        )

    def _update_progress(self, done: int, total: int) -> None:
        self.progressBarChanged.emit(done, max(total, 1))
        color_done = self._settings.get("progress_color_done", "#2E8B57")
        color_pending = self._settings.get("progress_color_pending", "#B0B0B0")
        for idx in range(len(self._steps)):
            new_mark = "✓" if idx < done else "○"
            new_color = color_done if idx < done else color_pending
            old = self._last_progress_marks.get(idx)
            if old is None or old[0] != new_mark or old[1] != new_color:
                # 只在值真正变化时才跨线程 emit，减少信号洪泛
                self._last_progress_marks[idx] = (new_mark, new_color)
                self.progressTextChanged.emit(idx, new_mark, new_color)
