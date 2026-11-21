# -*- coding: utf-8 -*-
"""把 Pro 编程模式文件内容索引从「0/0 表示全部」改为「正反两套索引，1~-1 表示全部，0 废弃」。
逐项做精确字符串替换，并校验每项替换次数，避免误改。
"""
import io
import sys

P = r"F:\PythonProjects\TraeProjects\规划性桌面代理\programming_pro.py"
V = r"F:\PythonProjects\TraeProjects\规划性桌面代理\_verify_programming_pro_fix.py"

with io.open(P, "r", encoding="utf-8") as f:
    src = f.read()

# (说明, 旧文本, 新文本, 期望次数)
EDITS = [
    # 1) read_file 工具 schema：start_line / end_line 描述
    (
        "read_file schema start_line",
        '"start_line": {"type": "integer", "description": "起始行号（从 1 开始），设为 0 表示读取全部"},',
        '"start_line": {"type": "integer", "description": "起始行号（从 1 开始；支持负索引，-1 为最后一行；1 配 -1 即 1~-1 表示读取整个文件；0 已废弃不可用）"},',
        1,
    ),
    (
        "read_file schema end_line",
        '"end_line": {"type": "integer", "description": "结束行号（含），设为 0 表示读取全部"},',
        '"end_line": {"type": "integer", "description": "结束行号（含；支持负索引，-1 为最后一行；读取整个文件请用 start_line=1、end_line=-1；0 已废弃不可用）"},',
        1,
    ),
    # 2) replace_file / remove_lines 工具 schema（两处文本相同，共 2 次）
    (
        "replace/remove schema start_line",
        '"start_line": {"type": "integer", "description": "起始行号（从 1 开始，含）"},',
        '"start_line": {"type": "integer", "description": "起始行号（从 1 开始，含；支持负索引，-1 为最后一行）"},',
        2,
    ),
    (
        "replace/remove schema end_line",
        '"end_line": {"type": "integer", "description": "结束行号（含）"},',
        '"end_line": {"type": "integer", "description": "结束行号（含；负索引 -1 为最后一行）"},',
        2,
    ),
    # 3) _build_internal_cmd：行号缺省默认值 0 → 1 / -1
    (
        "_build_internal_cmd default",
        """                val = args.get(pname, "")
                ordered[pidx] = str(val) if val != "" else "0\"""",
        """                val = args.get(pname, "")
                if val == "" and pidx in (1, 2):
                    # 行号缺省：起点默认 1、终点默认 -1（1~-1 表示整个文件）；0 已废弃
                    ordered[pidx] = "1" if pidx == 1 else "-1"
                else:
                    ordered[pidx] = str(val) if val != "" else "0\"""",
        1,
    ),
    # 4) _op_signature_from_tool：去重签名默认值同步为 1 / -1
    (
        "op signature read",
        "return f\"read|{path}|{args.get('start_line', 0)}|{args.get('end_line', 0)}\"",
        "return f\"read|{path}|{args.get('start_line', 1)}|{args.get('end_line', -1)}\"",
        1,
    ),
    (
        "op signature replace",
        "return f\"replace|{path}|{args.get('start_line', 0)}|{args.get('end_line', 0)}\"",
        "return f\"replace|{path}|{args.get('start_line', 1)}|{args.get('end_line', -1)}\"",
        1,
    ),
    (
        "op signature remove",
        "return f\"remove|{path}|{args.get('start_line', 0)}|{args.get('end_line', 0)}\"",
        "return f\"remove|{path}|{args.get('start_line', 1)}|{args.get('end_line', -1)}\"",
        1,
    ),
    # 5) _normalize_line_range：整体重写（0 废弃报错；1~-1 表示全部；负索引语义保留）
    (
        "_normalize_line_range rewrite",
        '''    @staticmethod
    def _normalize_line_range(
        start: int, end: int, total: int
    ) -> Tuple[int, int, List[str]]:
        """把 (start, end) 归一化到 [1, total] 范围内。

        支持负索引（Python 切片风格）：-1 = 最后一行，-2 = 倒数第二行，以此类推。
        0 等同于 -1（取到末尾）。起点/终点超出范围自动截断。

        返回 (norm_start, norm_end, adjustments)，adjustments 是长度为 0 或 2 的列表，
        记录被修正的情况，用于生成警告提示。
        """
        total = max(total, 1)
        adjustments: List[str] = []

        def _resolve(val: int, label: str) -> int:
            # 0 或负数 → 转正数
            if val <= 0:
                # -1 对应 total，-2 对应 total-1，...
                resolved = val + total + 1
                if resolved < 1:
                    adjustments.append(
                        f"（{label} {val} 修正为 {resolved}，已截断到 1）"
                    )
                    resolved = 1
                elif resolved > total:
                    # 不太可能，负数最大的 -1 就是 total
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

        return s, e, adjustments''',
        '''    @staticmethod
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

        return s, e, adjustments''',
        1,
    ),
    # 6) _do_read：非数字行号回退默认值 0/0 → 1/-1
    (
        "_do_read fallback",
        """        try:
            raw_s, raw_e = int(start), int(end)
        except (ValueError, TypeError):
            raw_s = raw_e = 0
        total = len(lines)""",
        """        try:
            raw_s, raw_e = int(start), int(end)
        except (ValueError, TypeError):
            # 非数字行号 → 回退为读取整个文件（1~-1；0 已废弃）
            raw_s, raw_e = 1, -1
        total = len(lines)""",
        1,
    ),
    # 7) _do_remove / _do_replace：行号无效报错补充引导（两处相同文本，共 2 次）
    (
        "invalid line number message",
        'raise CommandError("起始/结束行号无效")',
        'raise CommandError("起始/结束行号无效（行号从 1 开始，负索引 -1 为最后一行，0 已废弃；读取整个文件请用 1 和 -1）")',
        2,
    ),
    # 8) _validate_replace docstring 同步说明
    (
        "_validate_replace docstring",
        """        \"\"\"校验 replace 参数是否合法。返回 (valid, note)。

        支持负索引：-1 = 最后一行，超出范围自动截断（不再严格拒绝）。
        \"\"\"""",
        """        \"\"\"校验 replace 参数是否合法。返回 (valid, note)。

        正反两套索引：正索引从 1 开始，负索引 -1 = 最后一行；1~-1 表示整个文件；
        0 已废弃（会报错引导）；超出范围自动截断。
        \"\"\"""",
        1,
    ),
]

failed = False
for label, old, new, expect in EDITS:
    cnt = src.count(old)
    if cnt != expect:
        print(f"[FAIL] {label}: 期望 {expect} 处，实际 {cnt} 处")
        failed = True
        continue
    src = src.replace(old, new)
    print(f"[OK]   {label}（{cnt} 处）")

if failed:
    print("\n存在未匹配项，已中止写回，文件未修改。")
    sys.exit(1)

with io.open(P, "w", encoding="utf-8", newline="") as f:
    f.write(src)
print("\nprogramming_pro.py 已写回（UTF-8 无 BOM，保留原换行）。")

# ---- 同步更新验证脚本的预期值 ----
with io.open(V, "r", encoding="utf-8") as f:
    vsrc = f.read()

vedit = [
    (
        "verify read default",
        '("read_file", {"path": "main.py"}, "main.py,0,0"),',
        '("read_file", {"path": "main.py"}, "main.py,1,-1"),',
    ),
    (
        "verify replace args",
        '("replace_file", {"path": "a.py", "start_line": 3, "end_line": 5, "new_content": "x"}, "a.py,3,5"),',
        '("replace_file", {"path": "a.py", "start_line": 3, "end_line": 5, "new_content": "x"}, "a.py,3,5"),',
    ),
]
for label, old, new in vedit:
    cnt = vsrc.count(old)
    if cnt != 1:
        print(f"[FAIL] {label}: 期望 1 处，实际 {cnt} 处")
        failed = True
    else:
        vsrc = vsrc.replace(old, new)
        print(f"[OK]   {label}")

if not failed:
    with io.open(V, "w", encoding="utf-8", newline="") as f:
        f.write(vsrc)
    print("_verify_programming_pro_fix.py 已写回。")
