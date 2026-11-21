# -*- coding: utf-8 -*-
"""母工作流执行器：运行母工作流中的块式编排逻辑。

支持块类型：
- 定时器（timer）：等待指定时间后继续执行
- 工作流（workflow）：执行关联的工作流
- 脚本（script）：执行脚本库中的脚本，支持变量传递
- 条件判断（condition）：if/elif/else，支持模板匹配等条件
- 循环（loop）：while 循环，支持模板匹配或次数+间隔
- continue：跳过当前循环剩余部分
- break：退出当前循环

执行语义类似 Python 代码：按顺序依次执行块，通过缩进控制嵌套层级。
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import subprocess
import time
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import app_config
import capture
import template_matcher

# 执行回调类型
# 参数：workflow_id, goal, callback_on_finish, node（可选，含 _inline_steps 时使用）
ExecuteCallback = Callable[[str, str, Optional[Callable[[bool, str], None]], Optional[Dict]], None]

# 块类型常量
BLOCK_TIMER = "timer"
BLOCK_WORKFLOW = "workflow"
BLOCK_SCRIPT = "script"
BLOCK_CONDITION = "condition"
BLOCK_LOOP = "loop"
BLOCK_CONTINUE = "continue"
BLOCK_BREAK = "break"

# 块类型显示名称
BLOCK_LABELS = {
    BLOCK_TIMER: "定时器",
    BLOCK_WORKFLOW: "工作流",
    BLOCK_SCRIPT: "脚本",
    BLOCK_CONDITION: "条件判断",
    BLOCK_LOOP: "循环",
    BLOCK_CONTINUE: "continue",
    BLOCK_BREAK: "break",
}

# 条件类型中文名
COND_TYPE_LABELS = {
    "template_match": "模板匹配",
    "always": "始终",
    "success": "成功",
    "failure": "失败",
    "if": "if",
    "elif": "elif",
    "else": "else",
}


class _LoopControl(Exception):
    """循环控制异常，用于实现 continue/break。"""
    pass


class _ContinueLoop(_LoopControl):
    """continue：跳过当前循环剩余部分，进入下一次迭代。"""
    pass


class _BreakLoop(_LoopControl):
    """break：退出当前循环。"""
    pass


class MasterWorkflowExecutor:
    """母工作流执行器，负责编排和运行母工作流中的块。"""

    def __init__(self, execute_workflow_callback: ExecuteCallback):
        """
        Args:
            execute_workflow_callback: 执行单个工作流的回调函数
                签名： (workflow_id, goal, on_finish) -> None
                on_finish: (success: bool, message: str) -> None
        """
        self._execute_callback = execute_workflow_callback
        self._running: Dict[str, bool] = {}  # mwf_id -> 是否正在运行
        self._failed: Dict[str, bool] = {}  # mwf_id -> 本次运行是否失败
        self._lock = threading.Lock()
        self._on_completed: Optional[Callable[[str, bool, str], None]] = None  # (mwf_id, success, end_time)

    def set_on_completed(self, callback: Optional[Callable[[str, bool, str], None]]) -> None:
        """设置母工作流运行完成回调。
        参数：(mwf_id, success: bool, end_time: str "HH:MM")
        """
        self._on_completed = callback

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run_master_workflow(self, mwf_id: str) -> None:
        """运行母工作流的所有块。"""
        mwf = app_config.load_master_workflow(mwf_id)
        if not mwf:
            return
        if not mwf.get("enabled", True):
            return

        with self._lock:
            if self._running.get(mwf_id, False):
                return
            self._running[mwf_id] = True

        # 支持新格式 blocks 和旧格式 nodes
        blocks = mwf.get("blocks", [])
        if not blocks:
            nodes = mwf.get("nodes", [])
            if nodes:
                # 旧格式兼容
                self._run_old_nodes(mwf_id, nodes)
            else:
                with self._lock:
                    self._running[mwf_id] = False
            return

        # 新格式：在后台线程中执行块序列
        thr = threading.Thread(target=self._run_blocks,
                               args=(mwf_id, blocks),
                               daemon=True)
        thr.start()

    def stop_master_workflow(self, mwf_id: str) -> None:
        """停止母工作流。"""
        with self._lock:
            self._running[mwf_id] = False

    def is_running(self, mwf_id: str) -> bool:
        with self._lock:
            return self._running.get(mwf_id, False)

    def run_all_enabled(self) -> None:
        """运行所有已启用的母工作流。"""
        for mwf in app_config.list_master_workflows():
            if mwf.get("enabled", True):
                self.run_master_workflow(mwf["id"])

    # ------------------------------------------------------------------
    # 新块式执行
    # ------------------------------------------------------------------

    def _run_blocks(self, mwf_id: str, blocks: List[Dict]) -> None:
        """执行块序列，类似 Python 代码执行。"""
        with self._lock:
            self._failed[mwf_id] = False
        try:
            # 检查是否存在任何测试选中的块
            has_test_selected = any(
                b.get("data", {}).get("test_selected", False)
                for b in blocks
            )
            if has_test_selected:
                # 用户点击选中哪些就只执行哪些
                # 选中条件块的子块时，条件默认直接通过（缩进重置为0）
                filtered: List[Dict] = []
                for b in blocks:
                    if b.get("data", {}).get("test_selected", False):
                        b_copy = dict(b)
                        b_copy["indent"] = 0  # 子块独立执行，缩进重置
                        # 深拷贝 data 避免影响原数据
                        if "data" in b_copy and b_copy["data"] is not None:
                            b_copy["data"] = dict(b_copy["data"])
                        filtered.append(b_copy)
                self._execute_block_sequence(mwf_id, filtered, 0, None)
            else:
                # 没有测试选中，执行所有块
                self._execute_block_sequence(mwf_id, blocks, 0, None)
        finally:
            with self._lock:
                self._running[mwf_id] = False
            success = not self._failed.get(mwf_id, False)
            if self._on_completed:
                try:
                    end_time = time.strftime("%H:%M")
                    self._on_completed(mwf_id, success, end_time)
                except Exception:
                    pass

    def _execute_block_sequence(
        self, mwf_id: str, blocks: List[Dict], indent: int,
        loop_ctx: Optional[Dict], start_idx: int = 0
    ) -> None:
        """执行指定缩进级别的块序列。

        Args:
            mwf_id: 母工作流 ID
            blocks: 完整块列表
            indent: 当前缩进级别
            loop_ctx: 循环上下文，用于 continue/break 控制
            start_idx: 起始索引，从该位置开始扫描
        """
        if not self._running.get(mwf_id, False):
            return

        # 获取母工作流标签
        mwf = app_config.load_master_workflow(mwf_id)
        mwf_label = mwf.get("label", "") if mwf else ""

        i = start_idx
        while i < len(blocks):
            block = blocks[i]
            block_indent = block.get("indent", 0)

            # 只处理当前缩进级别的块
            if block_indent < indent:
                break
            if block_indent > indent:
                i += 1
                continue

            if not self._running.get(mwf_id, False):
                return

            btype = block.get("type", "")
            data = block.get("data", {})
            block_seq = i + 1  # 1-based 序号

            # 获取块名称
            if btype == BLOCK_CONDITION:
                branch = data.get("branch", "if")
                block_name = COND_TYPE_LABELS.get(branch, branch)
            else:
                block_name = data.get("block_name", "") or BLOCK_LABELS.get(btype, btype)

            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            # 根据块的缩进层级生成日志前缀（每层 4 个空格）
            pad = "    " * block_indent

            try:
                if btype == BLOCK_TIMER:
                    print(f"{pad}[母工作流] {mwf_label} {block_seq} {block_name} {current_time}")
                    self._execute_timer_block(data)
                elif btype == BLOCK_WORKFLOW:
                    self._execute_workflow_block(mwf_id, block, data, block_seq)
                elif btype == BLOCK_SCRIPT:
                    print(f"{pad}[母工作流] {mwf_label} {block_seq} {block_name} {current_time}")
                    self._execute_script_block(mwf_id, block, data)
                elif btype == BLOCK_CONDITION:
                    # 条件分支链：收集从当前位置开始的连续 if/elif/else 块
                    branch_blocks = self._collect_condition_chain(blocks, i, indent)
                    child_start = i + len(branch_blocks)
                    self._execute_condition_chain(
                        mwf_id, branch_blocks, blocks, indent, loop_ctx,
                        child_start, mwf_label, block_seq, block_indent)
                    # 跳过已处理的块
                    i += len(branch_blocks)
                    continue
                elif btype == BLOCK_LOOP:
                    print(f"{pad}[母工作流] {mwf_label} {block_seq} {block_name} {current_time}")
                    child_blocks = self._get_child_blocks(blocks, i + 1, indent)
                    child_start = i + 1
                    self._execute_loop_block(
                        mwf_id, block, data, blocks, indent, loop_ctx, child_start)
                    # 跳过子块
                    i += 1 + len(child_blocks)
                    continue
                elif btype == BLOCK_CONTINUE:
                    print(f"{pad}[母工作流] {mwf_label} {block_seq} {block_name} {current_time}")
                    if loop_ctx:
                        raise _ContinueLoop()
                elif btype == BLOCK_BREAK:
                    print(f"{pad}[母工作流] {mwf_label} {block_seq} {block_name} {current_time}")
                    if loop_ctx:
                        raise _BreakLoop()
            except _LoopControl:
                raise
            except Exception as e:
                print(f"[母工作流] 块执行出错: {e}")

            i += 1

    def _get_child_blocks(self, blocks: List[Dict], start_idx: int,
                          parent_indent: int) -> List[Dict]:
        """获取从指定位置开始的子块（缩进大于父级）。"""
        children = []
        for i in range(start_idx, len(blocks)):
            bi = blocks[i].get("indent", 0)
            if bi <= parent_indent:
                break
            children.append(blocks[i])
        return children

    def _collect_condition_chain(self, blocks: List[Dict], start_idx: int,
                                 indent: int) -> List[Dict]:
        """收集从 start_idx 开始的连续条件分支（if/elif/else）。"""
        chain = []
        for i in range(start_idx, len(blocks)):
            b = blocks[i]
            if b.get("indent", 0) != indent:
                break
            if b.get("type") != BLOCK_CONDITION:
                break
            chain.append(b)
        return chain

    # ------------------------------------------------------------------
    # 定时器块
    # ------------------------------------------------------------------

    def _execute_timer_block(self, data: Dict) -> None:
        """执行定时器块。"""
        wait_type = data.get("wait_type", "duration")
        if wait_type == "duration":
            duration = data.get("duration", 10)
            time.sleep(duration)
        else:
            # 等待到指定时间
            target_time = data.get("time", "09:00")
            try:
                parts = target_time.split(":")
                target_h, target_m = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                target_h, target_m = 9, 0

            now = datetime.datetime.now()
            target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            delay = (target - now).total_seconds()
            time.sleep(delay)

    # ------------------------------------------------------------------
    # 工作流块
    # ------------------------------------------------------------------

    def _execute_workflow_block(self, mwf_id: str, block: Dict, data: Dict,
                                 block_seq: int = 0) -> None:
        """执行工作流块。"""
        workflow_id = data.get("workflow_id", "")
        goal = data.get("goal", "")
        inline_steps = data.get("inline_steps", [])
        block_name = data.get("block_name", "") or data.get("label", "") or "工作流"
        replan_enabled = data.get("replan_enabled", False)
        replan_goal = data.get("replan_goal", "")
        # 获取母工作流标签
        mwf = app_config.load_master_workflow(mwf_id)
        mwf_label = mwf.get("label", "") if mwf else ""

        # 重规划按钮灰色时，不连接规划模型，直接跳过
        if not replan_enabled and not workflow_id and not goal and not inline_steps:
            return

        # 使用同步事件等待回调完成
        event = threading.Event()
        result = [False, ""]

        def _on_finish(success: bool, message: str) -> None:
            result[0] = success
            result[1] = message
            event.set()

        if replan_enabled and replan_goal:
            # 重规划模式：使用规划模型生成步骤
            node = {
                "_block_name": block_name,
                "_mwf_label": mwf_label,
                "_block_seq": block_seq,
                "_block_indent": block.get("indent", 0),
                "_replan_enabled": True,
            }
            self._execute_callback("", replan_goal, _on_finish, node)
        elif inline_steps:
            # 内联工作流：构造节点 dict 传递给回调（回调需要 _inline_steps 在顶层）
            node = {
                "_inline_steps": inline_steps,
                "_inline_goal": goal or "内联工作流",
                "_block_name": block_name,
                "_mwf_label": mwf_label,
                "_block_seq": block_seq,
                "_block_indent": block.get("indent", 0),
                "_replan_enabled": False,
            }
            self._execute_callback("", goal or "内联工作流", _on_finish, node)
        else:
            node = {
                "_block_name": block_name,
                "_mwf_label": mwf_label,
                "_block_seq": block_seq,
                "_block_indent": block.get("indent", 0),
                "_replan_enabled": False,
            }
            self._execute_callback(workflow_id, goal, _on_finish, node)
        event.wait()

        # 记录失败，供整体完成回调判断成功/失败
        if not result[0]:
            with self._lock:
                self._failed[mwf_id] = True

        # 重规划模式下执行失败时终止整个母工作流
        if replan_enabled and not result[0]:
            err_msg = result[1] or "未知错误"
            pad = "    " * block.get("indent", 0)
            print(f"{pad}[母工作流] {mwf_label} {block_seq} {block_name} 执行出错: {err_msg}")
            print(f"{pad}[母工作流] 重规划模式自动终止")
            with self._lock:
                self._running[mwf_id] = False
            raise Exception(f"重规划模式终止: {err_msg}")

    # ------------------------------------------------------------------
    # 脚本块
    # ------------------------------------------------------------------

    def _execute_script_block(self, mwf_id: str, block: Dict, data: Dict) -> None:
        """执行脚本块。"""
        script_id = data.get("script_id", "")
        if not script_id:
            return

        script = app_config.load_script(script_id)
        if not script:
            print(f"[母工作流] 脚本不存在: {script_id}")
            return

        code = script.get("code", "")
        if not code.strip():
            return

        language = script.get("language", "python")

        # 网页脚本：写入临时 .html 文件并打开浏览器
        if language == "web":
            import tempfile, os, webbrowser
            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".html", prefix="mwf_web_")
                os.close(fd)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(code)
                print(f"[母工作流] 正在打开网页脚本: {tmp_path}")
                webbrowser.open(f"file://{tmp_path}")
            except Exception as e:
                print(f"[母工作流] 网页脚本执行异常: {e}")
            return

        input_vars: Dict[str, str] = data.get("input_vars", {})
        output_vars: Dict[str, str] = data.get("output_vars", {})

        try:
            wrapper_lines = []
            wrapper_lines.append("# -*- coding: utf-8 -*-")
            wrapper_lines.append("")
            for var_name, default_val in input_vars.items():
                safe_name = var_name.strip()
                if not safe_name:
                    continue
                try:
                    val_repr = repr(json.loads(default_val)) if default_val else "None"
                except (json.JSONDecodeError, TypeError):
                    val_repr = repr(default_val)
                wrapper_lines.append(f"{safe_name} = {val_repr}")

            wrapper_lines.append("")
            wrapper_lines.append("# ===== 脚本代码开始 =====")
            wrapper_lines.append(code)
            wrapper_lines.append("# ===== 脚本代码结束 =====")
            wrapper_lines.append("")

            if output_vars:
                wrapper_lines.append("import json as _json")
                wrapper_lines.append("__mwf_output_vars__ = {}")
                for var_name in output_vars:
                    safe_name = var_name.strip()
                    if not safe_name:
                        continue
                    wrapper_lines.append(
                        f"try:\n"
                        f"    __mwf_output_vars__[{safe_name!r}] = {safe_name}\n"
                        f"except NameError:\n"
                        f"    __mwf_output_vars__[{safe_name!r}] = None"
                    )
                wrapper_lines.append(
                    'print("__SCRIPT_OUTPUT__:" + _json.dumps(__mwf_output_vars__))'
                )

            wrapper_code = "\n".join(wrapper_lines)

            fd, tmp_path = tempfile.mkstemp(suffix=".py", prefix="mwf_script_")
            os.close(fd)
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(wrapper_code)

            python_exe = sys.executable
            result = subprocess.run(
                [python_exe, tmp_path],
                capture_output=True,
                text=True,
                timeout=300,
            )

            try:
                os.remove(tmp_path)
            except OSError:
                pass

            stdout = result.stdout or ""
            stderr = result.stderr or ""

            if result.returncode != 0:
                err_msg = stderr.strip() or f"退出码 {result.returncode}"
                print(f"[母工作流] 脚本执行失败: {err_msg}")
                return

            extracted: Dict[str, Any] = {}
            for line in stdout.split("\n"):
                line = line.strip()
                if line.startswith("__SCRIPT_OUTPUT__:"):
                    try:
                        payload = line[len("__SCRIPT_OUTPUT__:"):]
                        extracted = json.loads(payload)
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break

            if extracted:
                block["_last_output_vars"] = extracted

        except subprocess.TimeoutExpired:
            print("[母工作流] 脚本执行超时（300秒）")
        except Exception as e:
            print(f"[母工作流] 脚本执行异常: {e}")

    # ------------------------------------------------------------------
    # 条件块链（if/elif/else）
    # ------------------------------------------------------------------

    def _execute_condition_chain(
        self, mwf_id: str, chain: List[Dict],
        all_blocks: List[Dict], parent_indent: int,
        loop_ctx: Optional[Dict], child_start: int = 0,
        mwf_label: str = "", chain_block_seq: int = 0,
        chain_indent: int = 0
    ) -> None:
        """执行条件链（if/elif/else）。"""
        pad = "    " * chain_indent
        for branch in chain:
            data = branch.get("data", {})
            branch_type = data.get("branch", "if")
            cond_type = data.get("condition_type", "template_match")
            not_cond = data.get("not_condition", False)
            tpl_path = data.get("template_path", "")

            if branch_type == "else":
                # else 无条件，直接执行子块
                child_indent = branch.get("indent", 0) + 1
                self._execute_block_sequence(
                    mwf_id, all_blocks, child_indent, loop_ctx, child_start)
                return

            # 评估条件
            condition_met = self._evaluate_condition(data)
            cond_label = COND_TYPE_LABELS.get(cond_type, cond_type)
            result_text = "通过" if condition_met else "未通过"
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            block_name = COND_TYPE_LABELS.get(branch_type, branch_type)
            print(f"{pad}[母工作流] {mwf_label} {chain_block_seq} {block_name} ({cond_label} {result_text}) {current_time}")
            if condition_met:
                child_indent = branch.get("indent", 0) + 1
                self._execute_block_sequence(
                    mwf_id, all_blocks, child_indent, loop_ctx, child_start)
                return

        # 所有条件都不满足，不执行任何块

    def _evaluate_condition(self, data: Dict) -> bool:
        """评估条件是否满足。"""
        cond_type = data.get("condition_type", "template_match")
        not_condition = data.get("not_condition", False)

        result = False
        if cond_type == "always":
            result = True
        elif cond_type == "success":
            result = True  # 默认成功
        elif cond_type == "failure":
            result = False  # 默认不失败
        elif cond_type == "template_match":
            result = self._evaluate_template_match(data)

        if not_condition:
            result = not result
        return result

    def _evaluate_template_match(self, data: Dict) -> bool:
        """评估模板匹配条件。"""
        tpl_path = data.get("template_path", "")
        if not tpl_path:
            return False
        threshold = data.get("template_threshold", 0.8)
        retry_count = data.get("template_retry_count", 0)
        retry_interval = data.get("template_retry_interval", 5)

        max_attempts = retry_count if retry_count > 0 else 1

        old_hide = capture.is_hide_on_capture()
        capture.set_hide_on_capture(False)
        try:
            for attempt in range(max_attempts):
                result = template_matcher.match_template_on_screen(tpl_path, threshold)
                if result is not None:
                    return True
                if attempt < max_attempts - 1 and retry_interval > 0:
                    time.sleep(retry_interval)
                elif attempt < max_attempts - 1:
                    time.sleep(0.1)
            return False
        finally:
            capture.set_hide_on_capture(old_hide)

    # ------------------------------------------------------------------
    # 循环块
    # ------------------------------------------------------------------

    def _execute_loop_block(
        self, mwf_id: str, block: Dict, data: Dict,
        all_blocks: List[Dict], parent_indent: int,
        outer_loop_ctx: Optional[Dict], child_start: int = 0
    ) -> None:
        """执行循环块。"""
        loop_type = data.get("loop_type", "count")
        child_indent = block.get("indent", 0) + 1
        child_pad = "    " * child_indent
        loop_pad = "    " * block.get("indent", 0)
        loop_ctx = {"type": "loop"}  # 为子块提供循环上下文

        if loop_type == "count":
            count = data.get("count", 10)
            interval = data.get("interval", 1)
            for iteration in range(count):
                if not self._running.get(mwf_id, False):
                    break
                print(f"{child_pad}第{iteration + 1}次循环")
                try:
                    self._execute_block_sequence(
                        mwf_id, all_blocks, child_indent, loop_ctx, child_start)
                except _ContinueLoop:
                    continue
                except _BreakLoop:
                    break
                if interval > 0 and iteration < count - 1:
                    time.sleep(interval)
                # 迭代之间的分隔线（最后一次由循环结束后统一打印）
                if iteration < count - 1:
                    print(f"{child_pad}----------------------------------------")
            # 循环结束后打印一条分隔线（缩进与循环块一致）
            print(f"{loop_pad}----------------------------------------")
        else:
            # 模板匹配循环：循环直到模板匹配（或消失）
            while self._running.get(mwf_id, False):
                matched = self._evaluate_template_match(data)
                should_continue = data.get("not_condition", False)
                if should_continue:
                    # 取反：模板消失时继续
                    if not matched:
                        try:
                            self._execute_block_sequence(
                                mwf_id, all_blocks, child_indent, loop_ctx, child_start)
                        except _ContinueLoop:
                            continue
                        except _BreakLoop:
                            break
                    else:
                        break
                else:
                    # 正常：模板出现时执行
                    if matched:
                        try:
                            self._execute_block_sequence(
                                mwf_id, all_blocks, child_indent, loop_ctx, child_start)
                        except _ContinueLoop:
                            continue
                        except _BreakLoop:
                            break
                    else:
                        # 等待后重试
                        time.sleep(1)
                        continue

    # ------------------------------------------------------------------
    # 旧格式兼容
    # ------------------------------------------------------------------

    def _run_old_nodes(self, mwf_id: str, nodes: List[Dict]) -> None:
        """兼容旧格式的 nodes 执行。"""
        try:
            timer_nodes = [n for n in nodes if n.get("trigger_type") == "timer"]
            chain_nodes = [n for n in nodes if n.get("trigger_type") in ("sequential", "conditional")]

            for node in timer_nodes:
                self._start_old_timer_node(mwf_id, node)

            if chain_nodes:
                self._run_old_chain_nodes(mwf_id, chain_nodes)

            if not timer_nodes and not chain_nodes:
                pass
        finally:
            with self._lock:
                self._running[mwf_id] = False

    def _start_old_timer_node(self, mwf_id: str, node: Dict) -> None:
        """兼容旧格式的定时节点。"""
        from threading import Timer as ThreadTimer
        schedule_mode = node.get("schedule_mode", "interval")
        interval = node.get("interval", 3600)
        loop_count = node.get("loop_count", 0)

        if schedule_mode == "daily":
            schedule_time = node.get("schedule_time", "09:00")

            def _daily_run(remaining: int = 0):
                if not self._running.get(mwf_id, False):
                    return
                self._execute_node_workflow(mwf_id, node, lambda *args: None)
                if loop_count > 0 and remaining > 1:
                    actual_interval = node.get("loop_interval", 0) or 86400
                    t = ThreadTimer(actual_interval, _daily_run, args=[remaining - 1])
                    t.daemon = True
                    t.start()
                else:
                    try:
                        parts = schedule_time.split(":")
                        target_h, target_m = int(parts[0]), int(parts[1])
                    except (ValueError, IndexError):
                        target_h, target_m = 9, 0
                    now = datetime.datetime.now()
                    target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
                    if target <= now:
                        target += datetime.timedelta(days=1)
                    delay = (target - now).total_seconds()
                    t = ThreadTimer(delay, _daily_run, args=[loop_count])
                    t.daemon = True
                    t.start()

            try:
                parts = schedule_time.split(":")
                target_h, target_m = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                target_h, target_m = 9, 0
            now = datetime.datetime.now()
            target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            delay = (target - now).total_seconds()
            t = ThreadTimer(delay, _daily_run, args=[loop_count])
            t.daemon = True
            t.start()
        else:
            def _interval_run(current_loop: int = 0):
                if not self._running.get(mwf_id, False):
                    return
                if loop_count > 0 and current_loop >= loop_count:
                    return
                self._execute_node_workflow(mwf_id, node, lambda *args: None)
                if loop_count > 0:
                    next_loop = current_loop + 1
                    if next_loop < loop_count:
                        actual_interval = node.get("loop_interval", 0) or interval
                        t = ThreadTimer(actual_interval, _interval_run, args=[next_loop])
                        t.daemon = True
                        t.start()
                else:
                    t = ThreadTimer(interval, _interval_run)
                    t.daemon = True
                    t.start()

            t = ThreadTimer(interval, _interval_run)
            t.daemon = True
            t.start()

    def _run_old_chain_nodes(self, mwf_id: str, nodes: List[Dict]) -> None:
        """兼容旧格式的链式节点执行。"""
        ready = []
        dep_map: Dict[str, List[Dict]] = {}
        node_map = {n.get("id", ""): n for n in nodes}

        for node in nodes:
            node_id = node.get("id", "")
            depends_on = node.get("depends_on", "")
            if not depends_on:
                ready.append(node)
            else:
                if depends_on not in dep_map:
                    dep_map[depends_on] = []
                dep_map[depends_on].append(node)

        _chain_vars: Dict[str, Any] = {}
        results: Dict[str, bool] = {}

        def _evaluate_template_match(node: dict, not_condition: bool = False) -> bool:
            tpl_path = node.get("template_path", "")
            if not tpl_path:
                return False
            threshold = node.get("template_threshold", 0.8)
            retry_count = node.get("template_retry_count", 0)
            retry_interval = node.get("template_retry_interval", 5)
            max_attempts = retry_count if retry_count > 0 else 1
            old_hide = capture.is_hide_on_capture()
            capture.set_hide_on_capture(False)
            try:
                for attempt in range(max_attempts):
                    result = template_matcher.match_template_on_screen(tpl_path, threshold)
                    matched = result is not None
                    if not_condition:
                        if not matched:
                            return True
                    else:
                        if matched:
                            return True
                    if attempt < max_attempts - 1 and retry_interval > 0:
                        time.sleep(retry_interval)
                    elif attempt < max_attempts - 1:
                        time.sleep(0.1)
                return False
            finally:
                capture.set_hide_on_capture(old_hide)

        def _execute_next(node_list: List[Dict]) -> None:
            if not node_list:
                return
            node = node_list[0]
            rest = node_list[1:]
            node_id = node.get("id", "")

            condition = node.get("condition", "success")
            if condition == "template_match":
                not_condition = node.get("not_condition", False)
                should_run = _evaluate_template_match(node, not_condition)
                if not should_run:
                    _execute_next(rest)
                    return

            self._execute_node_workflow(mwf_id, node, lambda s, m: None)

            loop_count = node.get("loop_count", 0)
            if loop_count > 1:
                for _ in range(loop_count - 1):
                    loop_interval = node.get("loop_interval", 0)
                    if loop_interval > 0:
                        time.sleep(loop_interval)
                    self._execute_node_workflow(mwf_id, node, lambda s, m: None)

            _trigger_dependents(node_id, True)
            _execute_next(rest)

        def _trigger_dependents(node_id: str, success: bool) -> None:
            dependents = dep_map.get(node_id, [])
            for dep_node in dependents:
                condition = dep_node.get("condition", "success")
                not_condition = dep_node.get("not_condition", False)
                should_run = self._evaluate_old_condition(condition, success, dep_node, not_condition)
                if should_run:
                    self._execute_node_workflow(mwf_id, dep_node, lambda s, m: None)

        _execute_next(ready)

    def _evaluate_old_condition(self, condition: str, predecessor_success: bool,
                                 node: dict, not_condition: bool = False) -> bool:
        """评估旧格式条件。"""
        if condition == "always":
            result = True
        elif condition == "success":
            result = predecessor_success
        elif condition == "failure":
            result = not predecessor_success
        elif condition == "template_match":
            tpl_path = node.get("template_path", "")
            if not tpl_path:
                return False
            threshold = node.get("template_threshold", 0.8)
            retry_count = node.get("template_retry_count", 0)
            retry_interval = node.get("template_retry_interval", 5)
            max_attempts = retry_count if retry_count > 0 else 1
            old_hide = capture.is_hide_on_capture()
            capture.set_hide_on_capture(False)
            try:
                for attempt in range(max_attempts):
                    result = template_matcher.match_template_on_screen(tpl_path, threshold)
                    matched = result is not None
                    if not_condition:
                        if not matched:
                            return True
                    else:
                        if matched:
                            return True
                    if attempt < max_attempts - 1 and retry_interval > 0:
                        time.sleep(retry_interval)
                    elif attempt < max_attempts - 1:
                        time.sleep(0.1)
                return False
            finally:
                capture.set_hide_on_capture(old_hide)
        else:
            result = False
        if not_condition:
            result = not result
        return result

    def _execute_node_workflow(self, mwf_id: str, node: Dict[str, Any],
                                on_finish: Callable[[bool, str], None]) -> None:
        """执行节点关联的工作流或脚本（旧格式兼容）。"""
        node_type = node.get("node_type", "workflow")

        if node_type == "script":
            workflow_id = node.get("workflow_id", "")
            goal = node.get("goal", "")
            if not workflow_id and not goal:
                on_finish(False, "节点未关联工作流")
                return
            self._execute_callback(workflow_id, goal, on_finish)
            return

        workflow_id = node.get("workflow_id", "")
        goal = node.get("goal", "")
        inline_steps = node.get("_inline_steps")

        if not workflow_id and not inline_steps and not goal:
            on_finish(False, "节点未关联工作流")
            return

        if inline_steps:
            self._execute_callback(workflow_id, goal, on_finish, node)
        else:
            self._execute_callback(workflow_id, goal, on_finish)