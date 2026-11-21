# -*- coding: utf-8 -*-
"""修复 programming_pro.py 的 list assignment index out of range（补丁脚本，运行后删除）"""
import io

P = r"F:\PythonProjects\TraeProjects\规划性桌面代理\programming_pro.py"

with io.open(P, "r", encoding="utf-8", newline="") as f:
    text = f.read()

# ---------- 修复 1：根因。search_code 映射 2 个参数位但 fixed=1，ordered[1]=... 越界 ----------
old1 = (
    "            # 这些命令的 body 是 \"param1,param2,param3\" 格式\r\n"
    "            fixed = _FIXED_ARGS.get(internal_name, 1)\r\n"
    "            ordered = [\"\"] * fixed\r\n"
    "            for pname, pidx in arg_map.items():\r\n"
    "                val = args.get(pname, \"\")\r\n"
    "                ordered[pidx] = str(val) if val != \"\" else \"0\"\r\n"
)
new1 = (
    "            # 这些命令的 body 是 \"param1,param2,param3\" 格式\r\n"
    "            # 槽位数按参数映射中的最大下标动态分配，避免 fixed 与映射不一致时\r\n"
    "            # ordered[pidx] 越界（如 search_code 映射 2 个参数但 fixed=1）\r\n"
    "            max_idx = max((pidx for pidx in arg_map.values()), default=0)\r\n"
    "            ordered = [\"\"] * (max_idx + 1)\r\n"
    "            for pname, pidx in arg_map.items():\r\n"
    "                val = args.get(pname, \"\")\r\n"
    "                ordered[pidx] = str(val) if val != \"\" else \"0\"\r\n"
)

# ---------- 修复 2：防御。命令构造放 try 内，异常按工具错误反馈而不是崩掉整个会话 ----------
old2 = (
    "        # 构造旧格式的 cmd dict\r\n"
    "        cmd = self._build_internal_cmd(internal_name, args, func_name)\r\n"
    "        if cmd is None:\r\n"
    "            return f\"[错误] 参数不足：{func_name}\"\r\n"
    "\r\n"
    "        # 执行\r\n"
    "        try:\r\n"
    "            if internal_name in (\"stop\",):\r\n"
    "                self.stop_process()\r\n"
    "                return \"[stop] 已停止正在运行的程序\"\r\n"
    "            if internal_name == \"done\":\r\n"
    "                return self._do_done()\r\n"
    "            if internal_name == \"skip\":\r\n"
    "                return self._do_skip()\r\n"
    "            return self._execute(cmd)\r\n"
    "        except CommandError as e:\r\n"
    "            return f\"[{func_name}] 执行失败：{e}\"\r\n"
    "        except Exception as e:\r\n"
    "            return f\"[{func_name}] 出错：{e}\"\r\n"
)
new2 = (
    "        # 构造旧格式的 cmd dict（构造异常也按工具错误反馈，避免整个会话崩溃）\r\n"
    "        try:\r\n"
    "            cmd = self._build_internal_cmd(internal_name, args, func_name)\r\n"
    "            if cmd is None:\r\n"
    "                return f\"[错误] 参数不足：{func_name}\"\r\n"
    "\r\n"
    "            if internal_name in (\"stop\",):\r\n"
    "                self.stop_process()\r\n"
    "                return \"[stop] 已停止正在运行的程序\"\r\n"
    "            if internal_name == \"done\":\r\n"
    "                return self._do_done()\r\n"
    "            if internal_name == \"skip\":\r\n"
    "                return self._do_skip()\r\n"
    "            return self._execute(cmd)\r\n"
    "        except CommandError as e:\r\n"
    "            return f\"[{func_name}] 执行失败：{e}\"\r\n"
    "        except Exception as e:\r\n"
    "            return f\"[{func_name}] 出错：{e}\"\r\n"
)

count1 = text.count(old1)
count2 = text.count(old2)
print("old1 count:", count1)
print("old2 count:", count2)
if count1 != 1 or count2 != 1:
    raise SystemExit("替换目标数量异常，中止，未写入任何修改")

text = text.replace(old1, new1).replace(old2, new2)

with io.open(P, "w", encoding="utf-8", newline="") as f:
    f.write(text)
print("补丁已应用")
