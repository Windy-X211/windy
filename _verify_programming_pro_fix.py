# -*- coding: utf-8 -*-
"""验证 programming_pro.py 修复：search_code 不再越界，其他工具无回归（验证脚本，运行后删除）"""
import io
import sys
import traceback

P = r"F:\PythonProjects\TraeProjects\规划性桌面代理\programming_pro.py"

# 1) 语法编译检查
with io.open(P, "rb") as f:
    src = f.read()
compile(src, P, "exec")
print("[1] 语法编译检查通过")

# 2) 导入模块并直接测 _build_internal_cmd（该方法是实例方法但只用类常量与参数）
sys.path.insert(0, r"F:\PythonProjects\TraeProjects\规划性桌面代理")
import programming_pro as pp

ctrl = object.__new__(pp.ProProgrammingController)  # 不执行 __init__，避免 Qt/配置依赖

cases = [
    # (工具名, 参数, 期望 body)
    ("search_code", {"query": "def main", "scope": "all"}, "all,def main"),
    ("search_code", {"query": "TODO"}, "all,TODO"),
    ("read_file", {"path": "main.py", "start_line": 1, "end_line": 20}, "main.py,1,20"),
    ("read_file", {"path": "main.py"}, "main.py,1,-1"),
    ("replace_file", {"path": "a.py", "start_line": 3, "end_line": 5, "new_content": "x"}, "a.py,3,5"),
    ("remove_lines", {"path": "a.py", "start_line": 1, "end_line": 2}, "a.py,1,2"),
    ("static_check", {"scope": "all"}, "all"),
    ("static_check", {}, "all"),
]

ok = True
for func_name, args, expect_body in cases:
    try:
        internal = pp._TOOL_DISPATCH[func_name]
        cmd = pp.ProProgrammingController._build_internal_cmd(ctrl, internal, args, func_name)
    except Exception as e:
        ok = False
        print(f"[FAIL] {func_name} {args} -> 异常 {type(e).__name__}: {e}")
        traceback.print_exc()
        continue
    body = cmd.get("body") if cmd else None
    status = "OK" if body == expect_body else "MISMATCH"
    if body != expect_body:
        ok = False
    print(f"[{status}] {func_name} body={body!r} (期望 {expect_body!r})")

# 3) 模拟 _execute_tool_call 完整链路（search_code 带 scope），确认不再抛异常
def fake_do_search(scope, query):
    return f"搜索范围={scope} 关键字={query}"

ctrl._do_search = fake_do_search
try:
    result = pp.ProProgrammingController._execute_tool_call(
        ctrl, "search_code", {"query": "def main", "scope": "all"}
    )
    print("[3] _execute_tool_call(search_code) 结果:", result)
    if "关键字=def main" not in result:
        ok = False
except Exception as e:
    ok = False
    print(f"[FAIL] _execute_tool_call(search_code) 异常 {type(e).__name__}: {e}")

# 4) 验证新索引语义：_normalize_line_range 正反两套索引，1~-1 表示全部，0 废弃
NR = pp.ProProgrammingController._normalize_line_range
norm_cases = [
    # (start, end, total, 期望 (ns, ne, has_adjustment))
    (1, -1, 10, (1, 10, False)),     # 1~-1 = 整个文件
    (1, 10, 10, (1, 10, False)),     # 正索引全量
    (-1, -1, 10, (10, 10, False)),   # 最后一行
    (-2, -1, 10, (9, 10, False)),    # 倒数第二行 ~ 最后一行
    (-3, -3, 10, (8, 8, False)),
    (3, 5, 10, (3, 5, False)),
    (1, 99, 10, (1, 10, True)),      # 终点超范围截断
    (-50, -1, 10, (1, 10, True)),    # 起点负得太多截断到 1
    (5, 2, 10, (2, 5, True)),        # 起 > 止 交换
]
for start, end, total, expect in norm_cases:
    try:
        ns, ne, adj = NR(start, end, total)
        got = (ns, ne, bool(adj))
    except Exception as e:
        ok = False
        print(f"[FAIL] NR({start},{end},{total}) -> 异常 {type(e).__name__}: {e}")
        continue
    status = "OK" if got == expect else "MISMATCH"
    if got != expect:
        ok = False
    print(f"[{status}] NR({start},{end},{total}) -> {got} (期望 {expect})")

# 0 已废弃：必须抛 CommandError 且带引导语
try:
    NR(0, 0, 10)
    ok = False
    print("[FAIL] NR(0,0,10) 未报错（0 应已废弃）")
except pp.CommandError as e:
    msg = str(e)
    if "0" in msg and "1" in msg and "-1" in msg:
        print(f"[OK]   NR(0,0,10) 按预期报 CommandError：{msg[:60]}…")
    else:
        ok = False
        print(f"[FAIL] NR(0,0,10) 报错但引导不完整：{msg}")
except Exception as e:
    ok = False
    print(f"[FAIL] NR(0,0,10) 抛错类型错误：{type(e).__name__}: {e}")

print("\n结论:", "全部通过" if ok else "存在失败项")
sys.exit(0 if ok else 1)
