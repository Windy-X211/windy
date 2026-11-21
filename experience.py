# -*- coding: utf-8 -*-
"""自主代理经验库：常用经验 + 专用经验。

- 常用经验（common）：任务无关的通用高效做法，调用自主代理模型时自动附加到系统提示词；
- 专用经验（special）：特定领域/任务的专用经验，仅在模型主动「搜索经验」时被检索。

持久化：用户数据目录/experience.json
线程安全：所有读改写均通过模块级锁，供 UI 线程与自主代理工作线程并发调用。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Tuple

import app_config

_FILENAME = "experience.json"

_lock = threading.Lock()
_common: List[str] = []
_special: List[str] = []
_loaded = False


def _path() -> str:
    return os.path.join(app_config.resolve_data_dir(), _FILENAME)


def _load_locked() -> None:
    """从磁盘加载（锁内调用）。文件缺失/损坏时回退为空库。"""
    global _common, _special, _loaded
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        _common = [str(x).strip() for x in (data.get("common") or []) if str(x).strip()]
        _special = [str(x).strip() for x in (data.get("special") or []) if str(x).strip()]
    except FileNotFoundError:
        _common, _special = [], []
    except Exception:
        _common, _special = [], []
    _loaded = True


def _ensure_loaded() -> None:
    global _loaded
    if not _loaded:
        _load_locked()


def _save_locked() -> None:
    """原子写盘（先写临时文件再替换，避免写坏）。锁内调用。"""
    try:
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"common": _common, "special": _special}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, _path())
    except Exception:
        pass


def _dedup_texts(texts: List[str]) -> List[str]:
    """去掉空行与重复项，保持原有顺序。"""
    seen = set()
    out: List[str] = []
    for t in texts:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def get_common() -> List[str]:
    with _lock:
        _ensure_loaded()
        return list(_common)


def get_special() -> List[str]:
    with _lock:
        _ensure_loaded()
        return list(_special)


def counts() -> Tuple[int, int, int]:
    """返回 (常用数, 专用数, 总数)。"""
    with _lock:
        _ensure_loaded()
        return len(_common), len(_special), len(_common) + len(_special)


def common_prompt_section() -> str:
    """构造附加到系统提示词的常用经验段落（空库返回空字符串）。"""
    with _lock:
        _ensure_loaded()
        items = list(_common)
    if not items:
        return ""
    lines = "\n".join(f"- {t}" for t in items)
    return ("## 常用经验（历史任务中沉淀的高效做法，对你完成任务很有帮助，优先参考并遵循）\n"
            + lines)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def add(category: str, text: str) -> bool:
    """单条添加。category: "common" | "special"。已存在返回 False。"""
    text = (text or "").strip()
    if not text or category not in ("common", "special"):
        return False
    with _lock:
        _ensure_loaded()
        bucket = _common if category == "common" else _special
        if text in bucket:
            return False
        bucket.append(text)
        _save_locked()
        return True


def extend(category: str, texts: List[str]) -> int:
    """批量添加（去重、忽略空行），返回实际新增条数。"""
    if category not in ("common", "special"):
        return 0
    cleaned = _dedup_texts(texts)
    if not cleaned:
        return 0
    with _lock:
        _ensure_loaded()
        bucket = _common if category == "common" else _special
        existing = set(bucket)
        added = [t for t in cleaned if t not in existing]
        if not added:
            return 0
        bucket.extend(added)
        _save_locked()
        return len(added)


def delete(category: str, text: str) -> bool:
    """删除单条，返回是否删除成功。"""
    text = (text or "").strip()
    if not text or category not in ("common", "special"):
        return False
    with _lock:
        _ensure_loaded()
        bucket = _common if category == "common" else _special
        if text in bucket:
            bucket.remove(text)
            _save_locked()
            return True
        return False


def delete_many(category: str, texts: List[str]) -> int:
    """批量删除，返回实际删除条数。"""
    if category not in ("common", "special"):
        return 0
    target = set(t for t in (texts or []) if (t or "").strip())
    if not target:
        return 0
    with _lock:
        _ensure_loaded()
        bucket = _common if category == "common" else _special
        new = [t for t in bucket if t not in target]
        removed = len(bucket) - len(new)
        if removed:
            bucket[:] = new
            _save_locked()
        return removed


def edit(category: str, old_text: str, new_text: str) -> bool:
    """编辑一条经验：按旧文本定位替换为新文本（保留顺序）。

    用「文本定位」而非索引，避免 UI 在编辑期间刷新导致的索引错位。
    旧文本已不存在（被删/被改）或新文本为空时返回 False。
    """
    old_text = (old_text or "").strip()
    new_text = (new_text or "").strip()
    if not old_text or not new_text or category not in ("common", "special"):
        return False
    if old_text == new_text:
        return True
    with _lock:
        _ensure_loaded()
        bucket = _common if category == "common" else _special
        try:
            idx = bucket.index(old_text)
        except ValueError:
            return False
        bucket[idx] = new_text
        _save_locked()
        return True


def set_common(texts: List[str]) -> None:
    """整体替换常用经验（管理界面「清空」用）。"""
    with _lock:
        _ensure_loaded()
        _common[:] = _dedup_texts(texts)
        _save_locked()


def set_special(texts: List[str]) -> None:
    """整体替换专用经验（管理界面「清空」用）。"""
    with _lock:
        _ensure_loaded()
        _special[:] = _dedup_texts(texts)
        _save_locked()


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

def search(keywords: List[str], top_k: int = 5) -> List[Dict[str, str]]:
    """按多个关键字在总经验库（常用 + 专用）中检索相关经验。

    Args:
        keywords: 关键字列表（一个及以上命中才返回）。
        top_k: 最多返回条数。

    Returns:
        按匹配度排序的条目列表：[{"category": "common"|"special", "text": ...}]
        匹配度 = 命中的关键字个数优先，其次经验文本较短者优先。
    """
    kws = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return []
    with _lock:
        _ensure_loaded()
        entries = [("common", t) for t in _common] + [("special", t) for t in _special]
    scored = []
    for cat, text in entries:
        low = text.lower()
        hits = [k for k in kws if k in low]
        if hits:
            scored.append((len(hits), len(text), cat, text))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"category": cat, "text": text} for _, _, cat, text in scored[:top_k]]
