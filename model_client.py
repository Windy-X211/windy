# -*- coding: utf-8 -*-
"""统一模型调用层（原生 requests 实现）。

核心函数 call_model(label, context, stream, temperature)：
- label：接口标签，按 tag 路由到不同预设（planning/vision/chat/自定义）；未命中回退 active。
- context：消息上下文。可为字符串（自动包成单条 user 消息）或 OpenAI messages 列表。
- stream：True 时流式输出，通过 on_chunk 回调实时返回增量，函数最终返回完整文本。
- temperature：覆盖预设温度；None 用预设值。

接口不可用（连接拒绝/401/超时）抛 InterfaceUnavailable，由上层提示用户更换接口。
"""

from __future__ import annotations

import base64
import os
import json
import ssl
from typing import Any, Callable, Dict, List, Optional, Union

import requests

import app_config
from step_model import InterfaceUnavailable

# 附件图片直传标记：user 消息字符串中以此前缀开头的行视为「待内嵌图片附件」，
# 其后的内容为图片绝对路径；在 call_model 开始构造请求时读入内存转为 base64 图像部分，
# 使图片以图像数据（而非路径文本）直接传入模型上下文。
EMBED_IMAGE_PREFIX = "@@EMBED_IMAGE@@"
# 允许作为图片附件内嵌直传的扩展名（白名单，避免误读非图片文件）
IMAGE_EXTS = frozenset(
    {
        ".png", ".apng", ".jpg", ".jpeg", ".jpe", ".jfif", ".pjp",
        ".gif", ".bmp", ".dib", ".webp", ".ico",
        ".svg", ".tif", ".tiff", ".avif", ".heic", ".heif",
    }
)
# 扩展名 → MIME 映射（部分格式与扩展名不同或更规范）
_IMG_MIME_OVERRIDES = {
    ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".tif": "image/tiff", ".tiff": "image/tiff",
    ".heic": "image/heic", ".heif": "image/heif",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
    ".jfif": "image/jpeg", ".pjp": "image/jpeg",
}


def _image_mime(ext: str) -> str:
    """根据扩展名给出图片 MIME；未知扩展返回默认 image/<ext>。"""
    return _IMG_MIME_OVERRIDES.get(ext, f"image/{ext.lstrip('.')}")


def _file_to_data_uri(path: str) -> Optional[str]:
    """把本地图片读入内存并转为 data URI；读取失败/非图片返回 None。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTS:
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw:
        return None
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{_image_mime(ext)};base64,{b64}"


def _expand_embedded_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把字符串内容中的 EMBED_IMAGE_PREFIX 标记行展开为真实的图片内容部分。

    仅处理标记所在的那条 user 消息：拆分成「文本部分 + 每张图片一个 image_url 部分」。
    其余消息原样保留。返回新列表，不修改入参。
    """
    if not messages:
        return messages
    has_marker = any(
        isinstance(m.get("content"), str) and EMBED_IMAGE_PREFIX in m.get("content", "")
        for m in messages
    )
    if not has_marker:
        return messages
    out: List[Dict[str, Any]] = []
    for m in messages:
        c = m.get("content")
        if not (isinstance(c, str) and EMBED_IMAGE_PREFIX in c):
            out.append(m)
            continue
        text_lines: List[str] = []
        parts: List[Dict[str, Any]] = []
        for ln in c.splitlines():
            stripped = ln.strip()
            if stripped.startswith(EMBED_IMAGE_PREFIX):
                uri = _file_to_data_uri(stripped[len(EMBED_IMAGE_PREFIX):].strip())
                if uri:
                    parts.append({"type": "image_url", "image_url": {"url": uri}})
                continue
            text_lines.append(ln)
        text = "\n".join(text_lines).strip()
        if text:
            parts.insert(0, {"type": "text", "text": text})
        if parts:
            out.append({**m, "content": parts})
        else:
            out.append(m)
    return out


def _normalize_context(context: Union[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if isinstance(context, str):
        return [{"role": "user", "content": context}]
    if isinstance(context, list):
        return context
    raise TypeError(f"context 必须为 str 或 list，实际为 {type(context)}")


def _resolve_preset(label: str, interfaces: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if interfaces is None:
        interfaces = app_config.load_interfaces()
    return app_config.get_preset_by_label(label, interfaces)


def _build_request_body(
    model: str,
    messages: List[Dict[str, Any]],
    temperature: float,
    max_tokens: int,
    top_p: float,
    top_k: int,
    stream: bool,
    frequency_penalty: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
) -> Dict[str, Any]:
    """构造 API 请求体。支持 OpenAI tools 协议。"""
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
        "top_k": top_k,
        "stream": stream,
    }
    if frequency_penalty is not None:
        body["frequency_penalty"] = frequency_penalty
    if presence_penalty is not None:
        body["presence_penalty"] = presence_penalty
    if tools:
        body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
    return body


def call_model(
    label: str,
    context: Union[str, List[Dict[str, Any]]],
    stream: bool = False,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    top_p: Optional[float] = None,
    top_k: Optional[int] = None,
    frequency_penalty: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
    interfaces: Optional[Dict[str, Any]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
) -> str:
    """调用大模型（原生 requests），返回完整文本。

    stream=True 时通过 on_chunk 实时回调增量文本，期间周期性调用 cancel_check
    以便及时响应暂停/停止。

    接口不可用抛 InterfaceUnavailable；其余异常向上冒泡由调用方处理。
    """
    preset = _resolve_preset(label, interfaces)
    base_url = preset.get("base_url", "")
    api_key = preset.get("api_key", "")
    model = preset.get("model", "")
    preset_temp = preset.get("temperature", 0.2)
    preset_max = preset.get("max_tokens", 4096)
    preset_top_p = preset.get("top_p", 0.9)
    preset_top_k = preset.get("top_k", 50)

    if not base_url or not model:
        raise InterfaceUnavailable(f"接口预设 [{label}] 缺少 base_url 或 model")

    messages = _normalize_context(context)
    # 把用户消息中内嵌图片标记(@@EMBED_IMAGE@@路径)读入内存→真实图像部分
    messages = _expand_embedded_images(messages)
    temp = preset_temp if temperature is None else temperature
    mt = preset_max if max_tokens is None else max_tokens
    tp = preset_top_p if top_p is None else top_p
    tk = preset_top_k if top_k is None else top_k

    # 清理 base_url 末尾斜杠；若已包含 /chat/completions 则不再重复拼接
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        url = base
    else:
        url = base + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or 'none'}",
    }
    body = _build_request_body(model, messages, temp, mt, tp, tk, stream,
                               frequency_penalty, presence_penalty,
                               tools=tools, tool_choice=tool_choice)

    timeout = app_config.get_model_timeout()

    try:
        if stream:
            return _stream_request(url, headers, body, on_chunk, cancel_check, timeout,
                                    parse_tools=bool(tools))
        return _sync_request(url, headers, body, timeout, parse_tools=bool(tools))
    except InterfaceUnavailable:
        raise
    except requests.ConnectionError as e:
        raise InterfaceUnavailable(f"模型接口 [{label}] 连接失败：{e}")
    except requests.Timeout as e:
        raise InterfaceUnavailable(f"模型接口 [{label}] 超时：{e}")
    except Exception as e:
        raise InterfaceUnavailable(f"模型接口 [{label}] 不可用：{e}")


def _sync_request(
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    timeout: float = 60.0,
    parse_tools: bool = False,
) -> Any:
    """同步请求，返回完整响应。

    parse_tools=False 时返回纯文本 content（向后兼容）。
    parse_tools=True 时返回 dict: {"content": str, "tool_calls": list|None, "finish_reason": str}。
    """
    resp = requests.post(
        url,
        headers=headers,
        json=body,
        timeout=timeout,
    )
    _check_http_error(resp)
    resp.encoding = "utf-8"
    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        return {"content": "", "tool_calls": None, "finish_reason": "stop"} if parse_tools else ""

    msg = choices[0].get("message", {})
    text = msg.get("content") or ""
    finish_reason = choices[0].get("finish_reason", "stop")

    if parse_tools:
        raw_tc = msg.get("tool_calls")
        tool_calls: Optional[List[Dict[str, Any]]] = None
        if raw_tc:
            tool_calls = []
            for tc in raw_tc:
                fn = tc.get("function", {})
                tool_calls.append({
                    "id": tc.get("id"),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "") or "{}",
                    },
                })
        return {
            "content": text,
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
        }
    return text or ""


def _stream_request(
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    on_chunk: Optional[Callable[[str], None]],
    cancel_check: Optional[Callable[[], bool]],
    timeout: float = 60.0,
    parse_tools: bool = False,
) -> Any:
    """流式请求，解析 SSE 事件。

    parse_tools=False 时返回完整文本（向后兼容）。
    parse_tools=True 时返回 dict: {"content": str, "tool_calls": list|None, "finish_reason": str}。

    tool_calls 的增量 JSON arguments 按 index 分组拼接后统一解析。
    整个 HTTP 请求在后台线程中执行，主线程每 100ms 轮询队列，确保停止/暂停能快速响应。
    """
    import queue
    import threading

    line_queue: "queue.Queue[Optional[str]]" = queue.Queue()
    response_ref: List[Any] = [None]  # 存放 resp 对象，供主线程关闭
    abort_event = threading.Event()
    thread_error: List[Exception] = []

    def _worker() -> None:
        try:
            # 整个 HTTP 请求（含连接建立）都在后台线程，主线程不会阻塞
            resp = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=timeout,
                stream=True,
            )
            _check_http_error(resp)
            resp.encoding = "utf-8"
            response_ref[0] = resp
            for line in resp.iter_lines(decode_unicode=True):
                if abort_event.is_set():
                    resp.close()
                    break
                line_queue.put(line)
            line_queue.put(None)  # 结束哨兵
        except Exception as e:
            thread_error.append(e)
            line_queue.put(None)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    try:
        content_parts: List[str] = []
        buf = ""
        # tool_calls 流式累积：{index -> {id, name, arguments_parts: []}}
        tc_acc: Dict[int, Dict[str, Any]] = {}
        finish_reason = "stop"

        while True:
            try:
                line = line_queue.get(timeout=0.1)
            except queue.Empty:
                # 超时无数据 → 检查取消
                if cancel_check and cancel_check():
                    abort_event.set()
                    # 若 resp 已就绪，关闭它让后台线程快速退出
                    if response_ref[0] is not None:
                        response_ref[0].close()
                    break
                continue

            if line is None:
                break

            if not line:
                continue
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            try:
                choice = chunk["choices"][0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason", finish_reason)
            except (KeyError, IndexError, TypeError):
                continue

            # === 处理 content 增量 ===
            text_delta = delta.get("content")
            if text_delta:
                content_parts.append(text_delta)
                buf += text_delta
                if "\n" in buf or len(buf) >= 16:
                    if on_chunk:
                        on_chunk(buf)
                    buf = ""
                    if cancel_check and cancel_check():
                        abort_event.set()
                        if response_ref[0] is not None:
                            response_ref[0].close()
                        break

            # === 处理 tool_calls 增量 ===
            if parse_tools:
                raw_tcs = delta.get("tool_calls")
                if raw_tcs:
                    for tc_delta in raw_tcs:
                        idx = tc_delta.get("index", 0)
                        acc = tc_acc.setdefault(idx, {
                            "id": "",
                            "name": "",
                            "arguments_parts": [],
                        })
                        if tc_delta.get("id"):
                            acc["id"] += tc_delta["id"]
                        fn = tc_delta.get("function", {})
                        if fn.get("name"):
                            acc["name"] += fn["name"]
                        if fn.get("arguments"):
                            acc["arguments_parts"].append(fn["arguments"])

        if buf and on_chunk:
            on_chunk(buf)

        if not parse_tools:
            return "".join(content_parts)

        # parse_tools=True：组装完整 tool_calls
        tool_calls: Optional[List[Dict[str, Any]]] = None
        if tc_acc:
            tool_calls = []
            for idx in sorted(tc_acc.keys()):
                acc = tc_acc[idx]
                args_str = "".join(acc["arguments_parts"]) or "{}"
                tool_calls.append({
                    "id": acc["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {
                        "name": acc["name"] or "",
                        "arguments": args_str,
                    },
                })

        return {
            "content": "".join(content_parts),
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
        }
    finally:
        abort_event.set()
        if response_ref[0] is not None:
            try:
                response_ref[0].close()
            except Exception:
                pass


def _check_http_error(resp: requests.Response) -> None:
    """检查 HTTP 状态码，不可用时抛 InterfaceUnavailable。"""
    if resp.status_code == 200:
        return
    if resp.status_code in (401, 403):
        raise InterfaceUnavailable(f"模型接口认证失败 (HTTP {resp.status_code})")
    if resp.status_code == 404:
        raise InterfaceUnavailable(f"模型接口地址不存在 (HTTP 404)")
    if resp.status_code >= 500:
        raise InterfaceUnavailable(f"模型接口服务器错误 (HTTP {resp.status_code})")
    # 其他状态码（如 429）向上冒泡
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# 视觉消息构造辅助
# ---------------------------------------------------------------------------

def build_image_messages(prompt: str, png_bytes: bytes) -> List[Dict[str, Any]]:
    """构造带图片的 user 消息（vision 模型用）。"""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }]


def preset_supports_vision(label: str, interfaces: Optional[Dict[str, Any]] = None) -> bool:
    """接口预设默认支持视觉输入，无需单独配置。"""
    return True


def active_tag(interfaces: Optional[Dict[str, Any]] = None) -> str:
    """委托给 app_config.active_tag，保持单一数据源。"""
    return app_config.active_tag(interfaces)


def invalidate_cache() -> None:
    """接口配置变更后的清理（已无 OpenAI 客户端缓存，保留以兼容调用方）。"""
    pass