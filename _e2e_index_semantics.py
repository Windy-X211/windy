# -*- coding: utf-8 -*-
"""端到端验证：_do_read / _do_remove / _do_replace 在新索引语义下的真实文件行为（验证后删除）"""
import io
import os
import shutil
import sys
import tempfile

P = r"F:\PythonProjects\TraeProjects\规划性桌面代理"
sys.path.insert(0, P)
import programming_pro as pp

proj = tempfile.mkdtemp(prefix="pro_idx_test_")
rel = "demo.py"
abspath = os.path.join(proj, rel)
content = "L1\nL2\nL3\nL4\nL5\n"  # split("\n") -> 6 个元素（末尾空串算 1 行，与代码既有口径一致）
with io.open(abspath, "w", encoding="utf-8") as f:
    f.write(content)

ctrl = object.__new__(pp.ProProgrammingController)
ctrl._project_dir = proj
ctrl._record_undo = lambda *a, **k: None  # 跳过 undo（object.__new__ 未初始化锁）
ctrl._last_undo_handle = 1

ok = True

def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"[{'OK' if cond else 'FAIL'}] {label} {detail}")

def read_file():
    with io.open(abspath, "r", encoding="utf-8") as f:
        return f.read()

# 1) read 整个文件：1,-1 → 1~6 行（L1..L5 + 尾空行）
r = ctrl._do_read(rel, "1", "-1")
check("read 1,-1 返回全量", "L1" in r and "L5" in r and "1 | L1" in r, f"-> {r.splitlines()[0]}")

# 2) read 负索引：-2,-1 → 5~6 行（L5 + 尾空行），不含 L1/L4
r = ctrl._do_read(rel, "-2", "-1")
check("read -2,-1 返回倒数两行", "5 | L5" in r and "L1" not in r and "L4" not in r,
      f"-> {r.splitlines()[0]}")

# 3) read 0,0 → 报错（0 已废弃，带引导）
try:
    ctrl._do_read(rel, "0", "0")
    check("read 0,0 应报错", False, "未报错")
except pp.CommandError as e:
    check("read 0,0 报 CommandError 且含引导", "1~-1" in str(e), f"-> {str(e)[:50]}")

# 4) read 非数字 → 回退全量（1,-1）
r = ctrl._do_read(rel, "abc", "def")
check("read 非数字回退全量", "L1" in r and "L5" in r, f"-> {r.splitlines()[0]}")

# 5) remove 1,-1 → 删除全部 6 行，文件为空
r = ctrl._do_remove(rel, "1", "-1")
check("remove 1,-1 删除全部", "删除 1~6 行" in r, f"-> {r}")
check("remove 后文件为空", read_file().strip() == "", f"-> 剩余 {len(read_file())} 字节")

# 6) 恢复内容，remove -1,-1 → 删除最后一行（尾空行），剩 L1..L5
with io.open(abspath, "w", encoding="utf-8") as f:
    f.write(content)
r = ctrl._do_remove(rel, "-1", "-1")
check("remove -1,-1 删除末行", "删除 6~6 行" in r, f"-> {r}")
check("remove -1 后剩 L1..L5", read_file().strip() == "L1\nL2\nL3\nL4\nL5", f"-> {read_file()!r}")

# 7) replace 1,-1 → 全量替换
with io.open(abspath, "w", encoding="utf-8") as f:
    f.write(content)
r = ctrl._do_replace(rel, "1", "-1", "NEW1\nNEW2\nNEW3\n")
check("replace 1,-1 全量替换", "替换 1~6 行" in r and read_file().strip() == "NEW1\nNEW2\nNEW3",
      f"-> {read_file()!r} | {r}")

# 8) 恢复内容，replace -1,-1 → 只改最后一行（尾空行 → L5_EDITED）
with io.open(abspath, "w", encoding="utf-8") as f:
    f.write(content)
r = ctrl._do_replace(rel, "-1", "-1", "L5_EDITED")
check("replace -1,-1 只改末行", "替换 6~6 行" in r and read_file().strip() == "L1\nL2\nL3\nL4\nL5\nL5_EDITED",
      f"-> {read_file()!r} | {r}")

# 9) replace 0,0 → 报错
try:
    ctrl._do_replace(rel, "0", "0", "X")
    check("replace 0,0 应报错", False, "未报错")
except pp.CommandError as e:
    check("replace 0,0 报 CommandError", "0" in str(e), f"-> {str(e)[:40]}")

# 10) 工具层缺省链路：read_file 不传 start_line/end_line → body '1,-1' → 全量读取
cmd = pp.ProProgrammingController._build_internal_cmd(
    ctrl, pp._TOOL_DISPATCH["read_file"], {"path": rel}, "read_file")
r = pp.ProProgrammingController._execute(ctrl, cmd)
check("工具层缺省行号=1,-1 全量读取", cmd["body"] == f"{rel},1,-1" and "L1" in r and "L5" in r,
      f"-> body={cmd['body']} | {r.splitlines()[0]}")

shutil.rmtree(proj, ignore_errors=True)
print("\n结论:", "全部通过" if ok else "存在失败项")
sys.exit(0 if ok else 1)
