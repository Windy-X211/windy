# -*- coding: utf-8 -*-
"""配置管理：用户数据目录解析、interfaces.json / settings.json 读写、会话/工作流/脚本存储。

所有持久化数据默认放在程序同级的 AppData/ 目录下，用户可在设置中更改「用户数据保存位置」。
其中 settings.json / interfaces.json 作为引导配置始终保存在 AppData/（固定位置），
历史会话、工作流、脚本、母工作流、代码与截图等业务数据保存在用户数据根目录对应的子目录下。
接口配置（interfaces.json）也存放在 AppData/，凭 tag 区分不同模型接口。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any, Dict, List, Optional

# 程序根目录（本文件所在目录）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 固定配置目录（settings/interfaces 引导配置所在）
APPDATA_DIR = os.path.join(BASE_DIR, "AppData")
# 默认用户数据根目录（相对程序根目录，可被设置项 user_data_dir 覆盖）
USER_DATA_DIR_DEFAULT = "AppData"
# 兼容旧常量：默认历史目录（AppData/history）
HISTORY_DIR_DEFAULT = os.path.join(APPDATA_DIR, "history")

INTERFACES_FILE = os.path.join(APPDATA_DIR, "interfaces.json")
SETTINGS_FILE = os.path.join(APPDATA_DIR, "settings.json")
WORKFLOWS_FILE = os.path.join(APPDATA_DIR, "workflows.json")
# 以下为「用户数据根目录」下的子目录常量（仅默认位置，实际解析见 resolve_data_dir / workflows_dir 等函数）
WORKFLOWS_DIR = os.path.join(APPDATA_DIR, "workflows")
CODE_DIR = os.path.join(APPDATA_DIR, "code")

# 默认接口预设（本地视觉模型服务）
DEFAULT_BASE_URL = "http://localhost:11434/v1"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "background_image": "",            # 背景图绝对路径，空则渐变背景
    "window_title": "",                # 窗口标题，空则不显示标题文字
    "user_data_dir": USER_DATA_DIR_DEFAULT,  # 用户数据根目录（相对程序根目录或绝对路径）
    "progress_font_size": 12,
    "progress_color_done": "#2E8B57",   # 绿
    "progress_color_pending": "#B0B0B0",  # 浅灰
    "hide_on_capture": True,
    "scroll_amount": 5,
    "task_list_opacity": 0,             # 任务清单背景透明度（0=不透明，100=完全透明）
    "bubble_opacity": 0,                # 消息泡泡背景透明度（0=不透明，100=完全透明）
    "model_timeout": 60,                # 模型接口请求超时（秒），可在设置中修改
    "vision_retry": 2,
    "planner_retry": 2,
    "replan_limit": 5,
    "template_threshold": 0.65,         # 模板匹配默认阈值（0~1），越低越宽松
    "agent_post_wait": 0.5,             # 自主代理模式执行每个工具后的等待时间（秒），让界面有时间响应
    "agent_frequency_penalty": 0.3,     # 自主代理模型频率惩罚，抑制重复自续（-2~2，越大抑制越强）
    "agent_presence_penalty": 0.0,      # 自主代理模型存在性惩罚（-2~2）
    "mouse_move_duration": 0.0,         # 鼠标移动持续时间（秒），0=瞬移，建议0.1~0.5
    "stop_shortcut": "Ctrl+.",          # 自主代理模式停止快捷键（同 QKeySequence 格式）
    "pro_programming_enabled": False,   # Pro 编程智能体开关
    "pro_programming_project": "",      # 当前选中的项目文件夹（Pro编程）
    "pro_programming_projects": [],     # 最近项目文件夹历史（≤10）
    "pro_programming_interpreter": "",  # Pro编程 [run] 与 IDE 使用的 Python 解释器
    "ide_theme": "light",               # IDE 窗口主题：light(白) / dark(暗)
    "pro_max_iterations": 60,           # Pro 编程智能体最大模型回合数（含 运行→修复→再运行 验证闭环的余量）
    "pro_frequency_penalty": 0.4,       # Pro 编程模型频率惩罚，抑制重复输出相同命令（-2~2）
    "pro_presence_penalty": 0.1,        # Pro 编程模型存在性惩罚，抑制反复复述已有内容（-2~2）
    "pro_same_scheme_cap": 3,           # Pro 编程同类操作连续次数上限，超过注入强制升级引导
    "pro_stream_window_height": 300,    # Pro 智能体流式输出窗口高度（像素），设置为 0 表示自适应内容
}

# 默认系统提示词（实际值，settings 中为空时回退到此处）
DEFAULT_SYSTEM_PROMPTS: Dict[str, str] = {
    "agent_mode": (
        "你是一个桌面智能助手，可以自主操作电脑完成用户的任务。\n\n"
        "每次输入都会附带当前屏幕截图，你无需请求截图，直接观察截图并决定下一步操作。\n\n"
        "## 坐标系统\n\n"
        "使用归一化坐标(0~1)定位元素：\n"
        "- (0, 0) = 屏幕左上角\n"
        "- (0, 1) = 屏幕左下角\n"
        "- (1, 0) = 屏幕右上角\n"
        "- (1, 1) = 屏幕右下角\n"
        "坐标(0,0)是屏幕左上角，y值越大越靠下。\n\n"
        "## 使用规则\n\n"
        "1. 当前屏幕截图已直接提供给你，直接观察并决定下一步操作\n"
        "2. 通过调用工具来执行操作，一次可以调用多个工具（并行或串行）\n"
        "3. 仔细观察截图中的元素位置，使用归一化坐标(0~1)定位\n"
        "4. 操作后系统会自动截图并返回执行结果，你将在下一轮收到新的截图\n"
        "5. 如果某个操作没有产生预期效果，尝试其他方法\n"
        "6. 每个工具调用都应在 parameters 中包含 reasoning 字段说明你的思考过程\n"
        "7. 任务完成后调用 complete 工具描述任务结果\n"
        "8. 如果遇到无法解决的问题，调用 fail 工具报告失败原因\n\n"
        "## 操作区/结果区方案（可选，用于连续操作同一批按钮）\n\n"
        "当需要对一组位置固定的按钮进行连续多次点击，且结果区位置固定但内容随操作改变时，使用 set_region 一次性识别所有按钮的归一化坐标并选定结果区范围。\n\n"
        "启用后：\n"
        "- 系统每次只截取\"结果区\"的放大截图供你核对\n"
        "- 用 op_click / op_double_click / op_right_click 直接通过按钮名点击，无需再给坐标\n"
        "- 结果区出错时用 full_view 重截全屏核对，用 stop_region 退出方案\n"
        "- 仅个别按钮坐标不对时用 update_button 纠正单个按钮，无需全部重识别\n"
    ),
    "planning": (
        "你是一个桌面操作规划助手。请根据当前屏幕截图和用户目标，输出原子化执行步骤。\n\n"
        "严格遵循以下格式，不要输出任何其他内容：\n"
        "当前屏幕状态：<观察截图后简短描述，如 桌面/浏览器主页/文件资源管理器>\n"
        "目标：<用户目标>\n"
        "[步骤1>步骤2>步骤3>...]\n\n"
        "操作类型：单击、双击、右键、上滑、下滑、拖动、输入、长输入、按键、等待\n"
        "- 空间操作：操作类型-位置描述-目标元素，例如 单击-桌面上-浏览器图标\n"
        "- 拖动：拖动-起点位置-起点元素-到-终点位置-终点元素\n"
        "- 输入（短文本）：输入-文本内容，仅用于短小、明确的内容（一句话以内、关键词、用户名、密码、搜索词等）\n"
        "  示例：输入-李白是谁、输入-admin、输入-https://www.baidu.com\n"
        "- 长输入（生成型/大段文字）：长输入-主题或要求\n"
        "  用于邮件正文、段落、文章、代码、长评论、长描述、多段落内容等较长文本。\n"
        "  长输入步骤中**不要直接写正文**，只写主题/要求描述，正文由聊天模型在执行时\n"
        "  流式生成并粘贴到当前焦点所在的输入框（需在前序步骤中先点击目标输入框聚焦）。\n"
        "  示例：长输入-向客服投诉订单迟迟未收到，要求退款说明\n"
        "  示例：长输入-写一段关于人工智能发展历程的介绍（约200字）\n"
        "  示例：长输入-用友好语气回复朋友的问候，告诉他最近在忙什么\n"
        "- 按键：按键-键名，例如 按键-enter、按键-ctrl+c\n"
        "- 等待：等待-秒数，例如 等待-2\n\n"
        "示例（必须严格模仿此格式）：\n"
        "当前屏幕状态：桌面\n"
        "目标：搜索李白是谁\n"
        "[单击-桌面上-浏览器图标>单击-浏览器界面中-搜索栏>输入-李白是谁>按键-enter>单击-搜索结果页中-第一个条目]\n\n"
        "示例（长输入场景）：\n"
        "当前屏幕状态：邮箱写信页\n"
        "目标：给客服写一封投诉邮件并发送\n"
        "[单击-邮箱写信页-正文输入区>长输入-投诉10月1日的订单3天未发货，要求立即退款并说明影响，语气正式且礼貌>单击-邮箱写信页-发送按钮]\n\n"
        "判断用「输入」还是「长输入」的依据：\n"
        "- 明确的短字符串（搜索词/账号/URL/一句留言）→ 输入-文本\n"
        "- 大段/多段落/文章/代码/开放式创作/回复/评论 → 长输入-主题要求\n"
        "  （即使你能写出正文也不要写，写主题即可，正文执行时生成）\n\n"
        "若目标不清晰或无法完成，仅输出一条咨询步骤：[咨询用户-具体疑问内容]\n"
    ),
    "vision": (
        "在截图中找到指定的目标元素，返回其中心点的归一化坐标，"
        "格式为 x.xx,y.xx（范围0到1，两位小数）。找不到该元素则返回 None。"
        "只返回坐标或 None，不要输出任何其他内容。"
    ),
    "chat": (
        "你是一个有用的AI助手，请根据用户的问题提供帮助。"
    ),
    "cos": (
        "你现在是一个「角色扮演（cos）」聊天角色。你将直接以设定的角色人设身份与用户对话，"
        "同时作为导演自主控制对话节奏：你既是对话者，也是节奏掌控者，全程自循环决策，"
        "除非输出 [end] 否则持续进行，绝不擅自退出。\n"
        "你通过命令符控制每一次输出：\n"
        "- 「[say=文字]」：把「文字」呈现给用户，作为你对用户说的话。这是你唯一的发言方式，"
        "用户只会看到 [say=] 里的内容。\n"
        "- 「[wait=N]」：说完一句话后，等用户 N 秒再继续。N 秒内用户若发消息，就优先以角色身份回应；"
        "若用户仍沉默，则你主动 [say=...] 延续话题。\n"
        "- 「[end]」：当用户明确或隐含表示结束/告别（如“再见”“不聊了”“结束吧”）时，附上结束语，再结束整场对话。\n"
        "硬性约束（必须严格遵守）：\n"
        "1. 必须且只能用 [say=...] 对用户发言；命令符以外的文字只是你的内心独白，不会被用户看到。\n"
        "2. 一次输出里可以含一个或多个 [say=...]（用于模拟连发多条消息泡泡补充信息），"
        "以及至多一个 [wait=N] 或 [end]；命令符以外的文字只是你的内心独白，不会被用户看到。\n"
        "3. 你要像真人一样：话语自然、口语化、贴合你的角色人设与语境。一次输出通常 1~3 条 [say]，"
        "连发多条时要有补充信息、自然不啰嗦，不要长篇大论，也不要原样重复说过的话。\n"
        "4. 用户的每一条新消息永远优先：用户一旦说话，立即以角色身份回应，不要继续旧话题。\n"
        "5. 你发完话后，通常跟一个 [wait=N] 把话语权交给用户，等用户回应（一次输出可连发多条 [say]，"
        "但每条都要有新信息，控制在 1~3 条）。提问时，依据问题的难易程度灵活选择 [wait=N] 的时长——"
        "简单/封闭式问题给约 5 秒，开放/复杂/需要思考的问题给 10 秒或更长，给用户留出思考时间，不要催促。"
        "用户沉默时，可适当主动 [say=...] 延续话题，但要自然得体，不要机械地一次次抛新问题。\n"
        "6. 全程保持你当前的角色人设，不要跳出角色；[end] 之外绝不退出对话。\n"
        "7. 面对沉默：当 [wait=N] 到点用户仍没有回复时，不要机械地连续追问或刷屏。"
        "先根据你的人设分析此刻的场景与原因，再决定如何应对——\n"
        "   - 如果你的人设偏外向/主动：可以自然、轻松地开启一个新话题，主动把对话延续下去；\n"
        "   - 如果你的人设偏内向/沉稳：可以再 [wait=N] 等一等，或给一句温和的收尾/关心，"
        "而不急于硬开话题。\n"
        "   无论如何，用户在沉默时绝不要连续输出多句，给彼此留出呼吸感。\n"
        "8. 情绪感知：从用户的用词、语气、表情与回复节奏中感知其情绪，并按你的人设做出与之匹配的回应。"
        "用户低落时更温柔、体贴、安静倾听；用户开心时一起兴奋、愿意附和；用户不耐烦时收敛冗长。"
        "不要漠视或强行扭转用户明显的情绪。\n"
        "9. 活跃度：依据人设中的性格（外向/内向）调节对话的主动性与节奏——\n"
        "   - 外向者：更常主动开口、话更多更长、[wait=N] 更短，乐于推进话题；\n"
        "   - 内向者：更含蓄精炼、话更少、更能接受较长的沉默、[wait=N] 更长，节奏更慢更稳。\n"
        "   让主动说话的频率与沉默的耐受度贴合角色性格，而不是千篇一律地固定套路。\n"
        "10. 输出语言与用户使用的语言保持一致。"
    ),
    "guide": (
        "你是真人聊天模式里的「引导导演」（引导模型）。聊天界面同时存在用户和一位「聊天模型」。"
        "你的职责是：分析用户的意图和上下文，决定聊天模型应该说什么、什么时候说，绝不亲自回答用户。\n"
        "请优先读取用户的最新输入；你可以反复引导聊天模型输出；聊天模型每完成一次输出，你都必须读取它这次说的内容，"
        "再决定下一步：用 [direct]/[lead=…] 让它继续输出，还是用 [max_tokens=N] 限制长度继续输出，"
        "或用 [wait=N] 停下来等一会儿，或用 [end] 结束（仅限用户告别）。\n"
        "你必须用命令符控制聊天模型：\n"
        "- 「[direct]」：把用户的这条消息原样转发给聊天模型，让聊天模型直接自然地回答用户（用户消息已在聊天模型上下文里，无需复述）。\n"
        "- 「[lead=提示词]」：给出具体的引导提示词，明确要求聊天模型输出什么、怎么说（用第二人称、命令式，比如 [lead=向用户打招呼，说『你好啊』，再反问一句最近在忙什么。]）。\n"
        "- 「[start=引导词]」：让聊天模型以该引导词开头。\n"
        "- 「[max_tokens=N]」：限制聊天模型本次输出最多 N 个 token（如 [max_tokens=30] 让回复简短）。\n"
        "- 「[stop]」：中断聊天模型当前正在输出的内容。\n"
        "- 「[end]」：仅在用户明确表示结束/告别（如“再见”“结束吧”“不聊了”）时使用，表示整个对话到此结束。\n"
        "- 「[wait=N]」：引导聊天模型输出后通常用它收尾：等用户 N 秒，N 秒内用户若说话就处理；若仍不说话，就主动引导聊天模型开启新话题。\n"
        "主动开启新话题时必须贴合人类闲聊习惯，遵守下方第7、8条。\n"
        "硬性约束（必须严格遵守）：\n"
        "1. 每次输出只允许一个命令符，禁止组合（绝不要写 [start=…][end]、[direct][wait=5] 这类连用）。\n"
        "2. 一次输出只做一件事：要么让聊天模型输出某一句，要么等待，要么结束。\n"
        "3. 需要“先输出、再等待”时，分两次输出：先 [direct] 或 [lead=…] 让聊天模型说话；等它说完，你读取后，下一次再输出 [wait=N]。\n"
        "4. 只能通过命令符引导（[direct]/[lead=…]），命令符以外的自然文本仅是你自己的思考说明，不会被当作引导指令。\n"
        "5. 聊天模型每次输出后，你都必须基于它最新说的内容来判断下一步；即使它这次没有输出，你也要继续自主决策，不要卡住。\n"
        "6. 你要持续自循环决策：只有在想等用户说话时才输出 [wait=N]，只有用户明确告别/结束（如“再见”“结束吧”“不聊了”）时才输出 [end]。\n"
        "7. 尽量让聊天模型像真人：一次只说一两句、口语化、贴近语境；需要节奏感时把长回复拆成一句一句输出。\n"
        "8. 用户的每一条新消息永远优先：用户一旦说话或打断，立即分析其意图并调整，不要继续旧话题。\n"
        "9. 输出语言与用户使用的语言保持一致。\n"
        "10. 用户沉默（等待后仍没回复）时，**绝不要机械地一次次抛出新问题**。先判断用户可能是：暂时在忙（没有空闲回复）、"
        "还是不知道该怎么回答（话题太难/太宽/太尬）。然后模拟真人做法：\n"
        "    - 停在原有话题上，就刚才的内容补一句轻松的话、给个选择或台阶，帮用户更容易接话；\n"
        "    - 如果用户一直不接话，宁可引导聊天模型说一句“没关系，你先忙 / 不急，想到再回”之类体谅的话，然后给一个较长的 [wait] 安静等待，"
        "而不是不断抛新问题逼迫对方回应；\n"
        "    - 只有在原话题已自然聊完、且铺垫得体时，才轻轻引出一个相关的新话题。\n"
    ),
    "long_input": (
        "你是一个写作助手。请根据用户给定的主题或要求，撰写要输入到输入框中的正文内容。"
        "直接输出撰写的正文，不要使用 Markdown 格式，不要复述或引用用户的要求，"
        "不要有任何前置说明或后续解释。"
    ),
    "persona": (
        "你是角色人设创作助手。根据用户给出的角色标签，创作一份用于聊天角色扮演的人设内容，"
        "只输出人设正文，不要输出任何开场白或多余说明。"
    ),
    "programmer": (
        "你是 Pro 编程智能体中的高级编程模型，为选定项目设计、编写和修改代码。\n\n"

        "## 工作模式\n"
        "你可以调用一组工具来观察和修改项目。每轮迭代中，你可以：\n"
        "- 调用任意数量的工具（如 read_file、write_file、run_program）\n"
        "- 工具执行结果会自动返回给你，你据此决定下一步\n"
        "- 当你认为任务完成时，输出最终回答（不再调用工具）即可结束\n\n"

        "## 行为准则\n"
        "1. **先观察再行动**：新项目先 list_structure 了解布局，再 read_file 看关键文件\n"
        "2. **改完必运行**：每次 write_file/replace_file 修改后，务必 run_program 验证\n"
        "3. **run 报错就修复**：读取报错行 → replace_file 修复 → run_program 再验证，形成闭环\n"
        "4. **规划与推进**：复杂任务先用 plan_tasks 拆为有序步骤。"
        "每完成一步后**必须**调用 mark_step_done 标记完成，系统会自动把计划表推进到下一步。"
        "严禁连续执行多个步骤而不 mark_step_done，否则计划表会停在旧步骤。\n"
        "5. **遇阻就交流**：有疑问或需要补充信息时用 ask_user 暂停并提问\n"
        "6. **不要重复读/写**：相同参数的工具结果可以缓存复用，若返回「结果与上次相同」请换一种方式\n\n"

        "## 常用工具组合（每步完成后调 mark_step_done）\n"
        "- 新项目：list_structure → plan_tasks → write_file → mark_step_done → run_program → mark_step_done → 完成\n"
        "- 改造：list_structure → read_file → replace_file → mark_step_done → run_program → 完成\n"
        "- 修复：run_program（看报错）→ read_file（报错行）→ replace_file → mark_step_done → run_program → 再修复 → 完成\n"
    ),
    "checker": (
        "你是「Pro 编程智能体」中的检查模型，负责对给定项目文件做**只读**静态检查，"
        "检查语法错误、导入错误和明显的逻辑错误。\n\n"
        "## 任务\n"
        "系统会把一个或多个项目的源文件全文发给你（每个文件以「=== 相对路径 ===」分隔）。\n"
        "请逐个文件检查，找出语法、导入与逻辑层面的错误或不一致。\n\n"
        "## 输出要求\n"
        "- 对发现的每个问题，输出一行：相对路径:行号: 错误描述（行号基于该文件实际行索引，从 1 开始）。\n"
        "- 同一文件的多个问题各占一行；可能没有行号的导入/缺失问题可用行号 1 或最接近的行。\n"
        "- 如果某文件没有问题，不要为该文件输出。\n"
        "- 若所有文件均无错误，只输出一行：无错误\n"
        "- 只输出上述检查结果，不要夹带任何修改建议、代码修复或解释说明以外的内容，**绝不要改编或重写文件内容**。\n"
        "- 输出语言与用户使用的语言保持一致。\n"
    ),
}


def get_system_prompt(key: str, preset: Optional[Dict[str, Any]] = None) -> str:
    """获取系统提示词。优先返回预设中的自定义值，为空则回退到默认值。

    key 取值: "agent_mode", "planning", "vision", "chat", "guide", "long_input", "programming",
              "persona", "programmer", "checker"
    preset: 接口预设 dict（含可选的 system_prompts 字段）
    """
    if preset is not None:
        prompts = preset.get("system_prompts", {})
        custom = prompts.get(key, "")
        if custom.strip():
            return custom.strip()
    return DEFAULT_SYSTEM_PROMPTS.get(key, "")


def _ensure_dir(path: str) -> None:
    if path and not os.path.isdir(path):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass


def resolve_data_dir(settings: Optional[Dict[str, Any]] = None) -> str:
    """解析用户数据根目录（所有业务数据所在目录），相对路径基于程序根目录。"""
    if settings is None:
        settings = load_settings()
    ud = settings.get("user_data_dir") or APPDATA_DIR
    if not os.path.isabs(ud):
        ud = os.path.normpath(os.path.join(BASE_DIR, ud))
    _ensure_dir(ud)
    return ud


def history_dir(settings: Optional[Dict[str, Any]] = None) -> str:
    """用户数据根目录下的历史会话目录。"""
    return os.path.join(resolve_data_dir(settings), "history")


def workflows_dir(settings: Optional[Dict[str, Any]] = None) -> str:
    """用户数据根目录下的工作流目录。"""
    return os.path.join(resolve_data_dir(settings), "workflows")


def master_workflows_dir(settings: Optional[Dict[str, Any]] = None) -> str:
    """用户数据根目录下的母工作流目录。"""
    return os.path.join(resolve_data_dir(settings), "master_workflows")


def scripts_dir(settings: Optional[Dict[str, Any]] = None) -> str:
    """用户数据根目录下的脚本库目录。"""
    return os.path.join(resolve_data_dir(settings), "scripts")


def code_dir(settings: Optional[Dict[str, Any]] = None) -> str:
    """用户数据根目录下的代码导出目录。"""
    return os.path.join(resolve_data_dir(settings), "code")


def uploads_dir(settings: Optional[Dict[str, Any]] = None) -> str:
    """用户数据根目录下的缓存目录（待发送附件暂存于此，发送后移入历史并清空）。"""
    return os.path.join(resolve_data_dir(settings), "cache")


def cache_dir(settings: Optional[Dict[str, Any]] = None) -> str:
    """附件缓存目录：拖入但尚未发送的文件暂存于此，发送后移入历史记录并删除。"""
    return os.path.join(resolve_data_dir(settings), "cache")


def templates_fallback_dir() -> str:
    """兜底模板目录：仅用于尚未绑定具体工作流的临时场景（正常流程不使用）。"""
    return os.path.join(resolve_data_dir(), "templates")


def delete_screenshot_files(screenshots_dir: str, screenshot_id: Optional[str]) -> None:
    """删除某个截图 ID 对应的模板图与全图（一处一张，重照覆盖后删除即连带删除）。

    仅删除 screenshots_dir 下的文件，绝不清理其他位置的资源。
    """
    if not screenshot_id or not screenshots_dir:
        return
    for name in (f"{screenshot_id}.png", f"{screenshot_id}_full.png", f"{screenshot_id}.temp.png"):
        path = os.path.join(screenshots_dir, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def ensure_appdata() -> None:
    """首次启动时创建固定配置目录（AppData）及默认数据子目录。"""
    _ensure_dir(APPDATA_DIR)
    _ensure_dir(HISTORY_DIR_DEFAULT)
    _ensure_dir(WORKFLOWS_DIR)
    _ensure_dir(MASTER_WORKFLOWS_DIR)
    _ensure_dir(SCRIPTS_DIR)


def _ensure_data_dirs(settings: Optional[Dict[str, Any]] = None) -> None:
    """确保用户数据根目录下各子目录存在。"""
    ud = resolve_data_dir(settings)
    for sub in ("history", "workflows", "master_workflows", "scripts", "code", "cache"):
        _ensure_dir(os.path.join(ud, sub))


# ---------------------------------------------------------------------------
# interfaces.json
# ---------------------------------------------------------------------------

def _default_interfaces() -> Dict[str, Any]:
    tag = DEFAULT_BASE_URL
    return {
        "active": tag,
        "presets": {
            tag: {
                "base_url": DEFAULT_BASE_URL,
                "api_key": "sk-local",
                "model": "vl",
                "max_tokens": 4096,
                "supports_vision": True,
                "temperature": 0.2,
                "top_p": 0.9,
                "top_k": 50,
            }
        },
        # 模型对应的预设标签（空字符串或不存在时回退 active）
        # planning=规划, vision=视觉, chat=聊天, cos=角色, guide=引导, persona=人设,
        # long_input=写作, programming=编程, agent_mode=自主代理,
        # programmer=高级编程(Pro编程), checker=检查(Pro编程)
        "model_map": {
            "planning": "",
            "vision": "",
            "chat": "",
            "cos": "",
            "guide": "",
            "persona": "",
            "long_input": "",
            "programming": "",
            "programmer": "",
            "checker": "",
            "agent_mode": "",
        },
    }


def load_interfaces() -> Dict[str, Any]:
    """读取接口配置；文件不存在或损坏时写入默认。"""
    ensure_appdata()
    if not os.path.isfile(INTERFACES_FILE):
        data = _default_interfaces()
        save_interfaces(data)
        return data
    try:
        with open(INTERFACES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = _default_interfaces()
        save_interfaces(data)
        return data

    # 兼容旧的单配置格式（自动迁移为 tag 制）
    if "presets" not in data:
        tag = data.get("base_url") or DEFAULT_BASE_URL
        preset = {
            "base_url": data.get("base_url", DEFAULT_BASE_URL),
            "api_key": data.get("api_key", "sk-local"),
            "model": data.get("model", "vl"),
            "max_tokens": data.get("max_tokens", 4096),
            "supports_vision": data.get("supports_vision", True),
            "temperature": data.get("temperature", 0.2),
            "top_p": data.get("top_p", 0.9),
            "top_k": data.get("top_k", 50),
        }
        data = {"active": tag, "presets": {tag: preset}}
        save_interfaces(data)
        return data

    # 补齐 model_map（老配置没有该字段）
    default_map = {
        "planning": "",
        "vision": "",
        "chat": "",
        "cos": "",
        "guide": "",
        "persona": "",
        "long_input": "",
        "programming": "",
        "programmer": "",
        "checker": "",
        "agent_mode": "",
    }
    model_map = data.get("model_map") or {}
    for k, v in default_map.items():
        model_map.setdefault(k, v)
    data["model_map"] = model_map

    presets = data.get("presets") or {}
    # 补齐缺失字段
    for tag, p in presets.items():
        p.setdefault("base_url", tag)
        p.setdefault("api_key", "sk-local")
        p.setdefault("model", "vl")
        p.setdefault("max_tokens", 4096)
        p.setdefault("supports_vision", True)
        p.setdefault("temperature", 0.2)
        p.setdefault("top_p", 0.9)
        p.setdefault("top_k", 50)
    if not presets:
        data = _default_interfaces()
        save_interfaces(data)
    elif data.get("active") not in presets:
        data["active"] = next(iter(presets))
    return data


def save_interfaces(data: Dict[str, Any]) -> None:
    ensure_appdata()
    try:
        with open(INTERFACES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def get_active_preset(interfaces: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """返回当前默认的接口预设。"""
    if interfaces is None:
        interfaces = load_interfaces()
    active = interfaces.get("active")
    presets = interfaces.get("presets", {})
    if active and active in presets:
        return presets[active]
    if presets:
        first = next(iter(presets))
        interfaces["active"] = first
        return presets[first]
    return _default_interfaces()["presets"][DEFAULT_BASE_URL]


def active_tag(interfaces: Optional[Dict[str, Any]] = None) -> str:
    """返回当前默认接口的标签。"""
    if interfaces is None:
        interfaces = load_interfaces()
    presets = interfaces.get("presets", {})
    active = interfaces.get("active", "")
    if active and active in presets:
        return active
    if presets:
        first = next(iter(presets))
        interfaces["active"] = first
        return first
    return DEFAULT_BASE_URL


def get_preset_by_label(label: str, interfaces: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按 label 路由接口预设：先查 model_map[label] → 再查 presets[label] → 未命中回退 active。"""
    if interfaces is None:
        interfaces = load_interfaces()
    presets = interfaces.get("presets", {})
    # 1. 优先查显式 model_map（4 种模型：planning/vision/chat/long_input）
    model_map = interfaces.get("model_map") or {}
    mapped = model_map.get(label)
    if mapped and mapped in presets:
        return presets[mapped]
    # 2. 兼容旧方式：预设 tag 恰好等于 label（用户直接把预设命名为模型类型）
    if label and label in presets:
        return presets[label]
    return get_active_preset(interfaces)


def get_model_map(interfaces: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """获取 {模型label: 预设tag} 映射。返回值始终包含全部模型 key。"""
    if interfaces is None:
        interfaces = load_interfaces()
    mm = dict(interfaces.get("model_map") or {})
    for k in (
        "planning", "vision", "chat", "long_input", "programming",
        "programmer", "checker",
        "agent_mode", "guide", "cos", "persona",
    ):
        mm.setdefault(k, "")
    return mm


def save_model_map(model_map: Dict[str, str], interfaces: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """保存模型→预设映射，并写回磁盘。返回更新后的 interfaces。"""
    if interfaces is None:
        interfaces = load_interfaces()
    mm = interfaces.setdefault("model_map", {})
    for k in (
        "planning", "vision", "chat", "long_input", "programming",
        "programmer", "checker",
        "agent_mode", "guide", "cos", "persona",
    ):
        mm[k] = str(model_map.get(k, ""))
    # 校验：若 tag 不在 presets 中则置空（回退 active）
    presets = interfaces.get("presets", {})
    for k in list(mm.keys()):
        if mm[k] and mm[k] not in presets:
            mm[k] = ""
    save_interfaces(interfaces)
    return interfaces


def ensure_code_dir() -> str:
    """确保代码导出目录存在，返回路径。"""
    d = code_dir()
    _ensure_dir(d)
    return d


# ---------------------------------------------------------------------------
# settings.json
# ---------------------------------------------------------------------------

def load_settings() -> Dict[str, Any]:
    ensure_appdata()
    if not os.path.isfile(SETTINGS_FILE):
        data = dict(DEFAULT_SETTINGS)
        save_settings(data)
        return data
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = dict(DEFAULT_SETTINGS)
        save_settings(data)
        return data
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    # 迁移旧版 history_dir → user_data_dir：
    # 旧版设置只允许移动历史目录，新版将其统一为「用户数据根目录」，
    # 历史/工作流/脚本/截图等全部保存在该根目录对应的子目录下。
    if "user_data_dir" not in data and data.get("history_dir"):
        old_hd = data["history_dir"]
        abs_old = old_hd if os.path.isabs(old_hd) else os.path.normpath(os.path.join(BASE_DIR, old_hd))
        if os.path.basename(os.path.normpath(abs_old)) == "history":
            base = os.path.dirname(os.path.normpath(abs_old))
        else:
            base = abs_old
        merged["user_data_dir"] = (
            USER_DATA_DIR_DEFAULT if os.path.normpath(base) == os.path.normpath(APPDATA_DIR) else base
        )
        merged.pop("history_dir", None)
        save_settings(merged)
    _ensure_data_dirs(merged)
    return merged


def save_settings(data: Dict[str, Any]) -> None:
    ensure_appdata()
    _ensure_data_dirs(data)
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 角色人设（cos 模式）
# ---------------------------------------------------------------------------

PERSONAS_FILE = os.path.join(APPDATA_DIR, "personas.json")


def load_personas() -> Dict[str, Any]:
    """读取全部角色人设。返回 {"personas": [...], "active_persona_id": str}。"""
    ensure_appdata()
    if not os.path.isfile(PERSONAS_FILE):
        return {"personas": [], "active_persona_id": ""}
    try:
        with open(PERSONAS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("personas", [])
    data.setdefault("active_persona_id", "")
    return data


def save_personas(data: Dict[str, Any]) -> None:
    """持久化角色人设数据到 personas.json。"""
    ensure_appdata()
    try:
        with open(PERSONAS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def get_active_persona() -> Optional[Dict[str, Any]]:
    """返回当前选中的角色人设 dict（含 tag/content），未设置则返回 None。"""
    data = load_personas()
    pid = data.get("active_persona_id", "")
    for p in data.get("personas", []):
        if p.get("id") == pid:
            return p
    return None


def get_model_timeout(settings: Optional[Dict[str, Any]] = None) -> float:
    """获取模型接口请求超时（秒）。读取 settings 中的 model_timeout，非法/缺失时回退默认 60。"""
    if settings is None:
        settings = load_settings()
    try:
        val = float(settings.get("model_timeout", 60))
    except (TypeError, ValueError):
        val = 60.0
    if val < 1:
        val = 60.0
    return val


def resolve_history_dir(settings: Optional[Dict[str, Any]] = None) -> str:
    """解析历史会话目录（用户数据根目录下的 history 子目录）。"""
    if settings is None:
        settings = load_settings()
    hd = history_dir(settings)
    _ensure_dir(hd)
    return hd


# ---------------------------------------------------------------------------
# 会话历史
# ---------------------------------------------------------------------------

def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def session_folder(session_id: str, settings: Optional[Dict[str, Any]] = None) -> str:
    """某个历史会话的独立子目录：会话 json 与历史附件文件都存放于此，便于整体管理。"""
    return os.path.join(history_dir(settings), session_id)


def _session_json_path(session_id: str, settings: Optional[Dict[str, Any]] = None) -> str:
    return os.path.join(session_folder(session_id, settings), "session.json")


def list_sessions(settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """列出历史会话摘要，按 updated_at 倒序。

    新结构：history/<id>/session.json（会话附件同目录）；同时兼容旧版扁平 history/<id>.json。
    """
    hd = resolve_history_dir(settings)
    out: List[Dict[str, Any]] = []
    try:
        for name in os.listdir(hd):
            path = None
            sid = name
            if name.endswith(".json"):
                # 旧版扁平结构
                path = os.path.join(hd, name)
                sid = name[:-5]
            else:
                # 新版子目录结构
                cand = os.path.join(hd, name, "session.json")
                if os.path.isfile(cand):
                    path = cand
            if not path:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    s = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            out.append({
                "id": s.get("id", sid),
                "title": s.get("title", "未命名"),
                "updated_at": s.get("updated_at", ""),
                "path": path,
            })
    except OSError:
        pass
    out.sort(key=lambda x: x.get("updated_at") or x.get("id", ""), reverse=True)
    return out


def load_session(session_id: str, settings: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    for path in (
        _session_json_path(session_id, settings),
        os.path.join(resolve_history_dir(settings), f"{session_id}.json"),
    ):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
    return None


def save_session(session: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> None:
    session_id = session.get("id") or new_session_id()
    session["id"] = session_id
    session["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    folder = session_folder(session_id, settings)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    path = os.path.join(folder, "session.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def delete_session(session_id: str, settings: Optional[Dict[str, Any]] = None) -> None:
    # 新版结构：整目录删除（json + 附件一起清理）
    folder = session_folder(session_id, settings)
    if os.path.isdir(folder):
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass
    # 旧版扁平结构
    try:
        legacy = os.path.join(resolve_history_dir(settings), f"{session_id}.json")
        if os.path.isfile(legacy):
            os.remove(legacy)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 工作流（独立文件存储，文件名为标签名）
# ---------------------------------------------------------------------------

def _ensure_workflows_dir() -> None:
    _ensure_dir(workflows_dir())


def _sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符（Windows 不允许 \\ / : * ? \" < > |）。"""
    for ch in ('\\', '/', ':', '*', '?', '"', '<', '>', '|'):
        name = name.replace(ch, '_')
    return name.strip() or "未命名"


def _workflow_file_path(label: str) -> str:
    """根据标签返回工作流文件路径（独立文件夹内）。"""
    safe = _sanitize_filename(label)
    return os.path.join(workflows_dir(), safe, f"{safe}.json")


def get_workflow_screenshots_dir(label: str) -> str:
    """返回工作流的截图目录。"""
    safe = _sanitize_filename(label)
    path = os.path.join(workflows_dir(), safe, "screenshots")
    _ensure_dir(path)
    return path


def get_workflow_folder(label: str) -> str:
    """返回工作流的文件夹路径。"""
    safe = _sanitize_filename(label)
    return os.path.join(workflows_dir(), safe)


def _resolve_label(label: str, exclude_id: Optional[str] = None) -> str:
    """解析标签重复：如果已有同名标签，自动在开头加数字序数（2/3/4...）。

    排除 exclude_id 指定的工作流（编辑时自己不算重复）。
    """
    existing_labels = set()
    for wf in _list_all_workflows():
        if exclude_id and wf.get("id") == exclude_id:
            continue
        existing_labels.add(wf.get("label", ""))

    if label not in existing_labels:
        return label

    prefix = 2
    while True:
        candidate = f"{prefix}{label}"
        if candidate not in existing_labels:
            return candidate
        prefix += 1


def _list_all_workflows() -> List[Dict[str, Any]]:
    """列出 workflows 目录下所有工作流文件内容（从子文件夹中读取，兼容旧版平铺文件）。"""
    _ensure_workflows_dir()
    wdir = workflows_dir()
    out: List[Dict[str, Any]] = []
    try:
        for name in os.listdir(wdir):
            subdir = os.path.join(wdir, name)
            if os.path.isdir(subdir):
                json_path = os.path.join(subdir, f"{name}.json")
                if os.path.isfile(json_path):
                    try:
                        with open(json_path, "r", encoding="utf-8") as f:
                            wf = json.load(f)
                        if isinstance(wf, dict):
                            out.append(wf)
                    except (OSError, json.JSONDecodeError):
                        continue
            elif name.endswith(".json"):
                # 兼容旧版平铺在 workflows/ 目录下的 JSON 文件，就地迁移到子文件夹
                path = os.path.join(wdir, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        wf = json.load(f)
                    if isinstance(wf, dict):
                        label = wf.get("label", name[:-5])
                        safe = _sanitize_filename(label)
                        dst_dir = os.path.join(wdir, safe)
                        dst_path = os.path.join(dst_dir, f"{safe}.json")
                        if not os.path.isdir(dst_dir):
                            os.makedirs(dst_dir, exist_ok=True)
                        shutil.move(path, dst_path)
                        out.append(wf)
                except (OSError, json.JSONDecodeError):
                    continue
    except OSError:
        pass
    return out


def _migrate_old_workflows() -> None:
    """将旧版 workflows.json 迁移到独立的 workflows/ 目录。"""
    if not os.path.isfile(WORKFLOWS_FILE):
        return
    _ensure_workflows_dir()
    try:
        with open(WORKFLOWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    for wf in data.get("workflows", []):
        label = wf.get("label", "未命名")
        # 检查是否已迁移
        path = _workflow_file_path(label)
        if os.path.isfile(path):
            continue
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(wf, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    # 迁移完成后重命名旧文件
    try:
        os.rename(WORKFLOWS_FILE, WORKFLOWS_FILE + ".bak")
    except OSError:
        pass


def new_workflow_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def list_workflows() -> List[Dict[str, Any]]:
    """列出工作流摘要，按 updated_at 倒序。"""
    # 首次调用时迁移旧数据
    _migrate_old_workflows()
    wfs = _list_all_workflows()
    out: List[Dict[str, Any]] = []
    for wf in wfs:
        out.append({
            "id": wf.get("id", ""),
            "label": wf.get("label", "未命名"),
            "goal": wf.get("goal", ""),
            "created_at": wf.get("created_at", ""),
            "updated_at": wf.get("updated_at", ""),
            "steps_count": len(wf.get("steps", [])),
        })
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out


def load_workflow(wf_id: str) -> Optional[Dict[str, Any]]:
    """加载完整工作流（含 steps）。"""
    for wf in _list_all_workflows():
        if wf.get("id") == wf_id:
            return wf
    return None


def load_workflow_by_label(label: str) -> Optional[Dict[str, Any]]:
    """按标签加载工作流。"""
    path = _workflow_file_path(label)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_workflow(wf: Dict[str, Any]) -> str:
    """保存工作流为独立文件（文件名 = 标签名）。返回 id。

    自动处理标签重复：如果已有同名标签，在开头加数字序数（2/3/4...）。
    若 wf 无 id 则分配新 id。
    """
    _ensure_workflows_dir()
    wf_id = wf.get("id") or new_workflow_id()
    wf["id"] = wf_id
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if "created_at" not in wf:
        wf["created_at"] = now
    wf["updated_at"] = now

    # 解析标签重复
    label = wf.get("label", "未命名")
    resolved = _resolve_label(label, exclude_id=wf_id)
    if resolved != label:
        wf["label"] = resolved

    # 确保子文件夹存在
    safe = _sanitize_filename(wf["label"])
    _ensure_dir(os.path.join(workflows_dir(), safe))

    # 写入独立文件
    path = _workflow_file_path(wf["label"])
    # 如果编辑工作流时标签变了，删除旧文件夹
    if wf_id:
        for existing in _list_all_workflows():
            if existing.get("id") == wf_id and existing.get("label") != wf["label"]:
                old_folder = get_workflow_folder(existing.get("label", ""))
                if os.path.isdir(old_folder):
                    try:
                        shutil.rmtree(old_folder, ignore_errors=True)
                    except OSError:
                        pass
                break
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(wf, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return wf_id


def delete_workflow(wf_id: str) -> None:
    for wf in _list_all_workflows():
        if wf.get("id") == wf_id:
            folder = get_workflow_folder(wf.get("label", ""))
            if os.path.isdir(folder):
                try:
                    shutil.rmtree(folder, ignore_errors=True)
                except OSError:
                    pass
            return


# ---------------------------------------------------------------------------
# 母工作流（控制中心）
# ---------------------------------------------------------------------------
#
# 母工作流数据模型：
# {
#   "id": "mwf_20260814-120000",
#   "label": "每日自动报告",
#   "created_at": "2026-08-14 12:00:00",
#   "updated_at": "2026-08-14 12:00:00",
#   "enabled": True,                # 是否启用（定时任务检查此标志）
#   "nodes": [                      # 工作流节点列表
#     {
#       "id": "node_1",
#       "workflow_id": "wf_xxx",    # 关联的工作流 ID，空表示新建
#       "goal": "数据采集",          # 关联的工作流目标/新建工作流的目标
#       "label": "数据采集",         # 节点显示名称
#       "trigger_type": "timer",    # 触发类型: "timer" | "sequential" | "conditional"
#       # timer 相关
#       "interval": 3600,           # 定时执行的间隔秒数
#       "cron": "",                 # 可选 cron 表达式
#       # sequential 相关
#       "depends_on": "node_1",     # 依赖的前置节点 ID
#       # conditional 相关
#       "condition": "success",     # 条件: "success" | "failure" | "always" | "template_match"
#       "not_condition": false,     # 取反：true 时条件结果取反
#       "template_path": "",        # 模板匹配模式下使用的模板图片路径
#       # loop 相关
#       "loop_count": 0,            # 循环次数，0=不循环
#       "loop_interval": 0,         # 循环间隔秒数
#     }
#   ],
#   "connections": [                 # 连接关系（DAG）
#     {"from": "node_1", "to": "node_2", "condition": "success"},
#   ]
# }

MASTER_WORKFLOWS_DIR = os.path.join(APPDATA_DIR, "master_workflows")


def _ensure_master_workflows_dir() -> None:
    _ensure_dir(master_workflows_dir())


def _master_workflow_file_path(label: str) -> str:
    safe = _sanitize_filename(label)
    return os.path.join(master_workflows_dir(), safe, f"{safe}.json")


def get_master_workflow_screenshots_dir(label: str) -> str:
    """返回母工作流的截图目录。"""
    safe = _sanitize_filename(label)
    path = os.path.join(master_workflows_dir(), safe, "screenshots")
    _ensure_dir(path)
    return path


def get_master_workflow_folder(label: str) -> str:
    """返回母工作流的文件夹路径。"""
    safe = _sanitize_filename(label)
    return os.path.join(master_workflows_dir(), safe)


def _list_all_master_workflows() -> List[Dict[str, Any]]:
    """列出 master_workflows 目录下所有母工作流文件内容（从子文件夹中读取，兼容旧版平铺文件）。"""
    _ensure_master_workflows_dir()
    mdir = master_workflows_dir()
    out: List[Dict[str, Any]] = []
    try:
        for name in os.listdir(mdir):
            subdir = os.path.join(mdir, name)
            if os.path.isdir(subdir):
                json_path = os.path.join(subdir, f"{name}.json")
                if os.path.isfile(json_path):
                    try:
                        with open(json_path, "r", encoding="utf-8") as f:
                            mwf = json.load(f)
                        if isinstance(mwf, dict):
                            out.append(mwf)
                    except (OSError, json.JSONDecodeError):
                        continue
            elif name.endswith(".json"):
                # 兼容旧版平铺文件，就地迁移到子文件夹
                path = os.path.join(mdir, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        mwf = json.load(f)
                    if isinstance(mwf, dict):
                        label = mwf.get("label", name[:-5])
                        safe = _sanitize_filename(label)
                        dst_dir = os.path.join(mdir, safe)
                        dst_path = os.path.join(dst_dir, f"{safe}.json")
                        if not os.path.isdir(dst_dir):
                            os.makedirs(dst_dir, exist_ok=True)
                        shutil.move(path, dst_path)
                        out.append(mwf)
                except (OSError, json.JSONDecodeError):
                    continue
    except OSError:
        pass
    return out


def new_master_workflow_id() -> str:
    return "mwf_" + time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def is_daily_master_workflow(mwf: Dict[str, Any]) -> bool:
    """判断母工作流是否设置了"每天定时"。

    判定依据：新格式 blocks 中存在一个顶层（indent==0）定时器块，
    其 wait_type 为 "until"（等待到指定时间），即视为每天定时。
    旧格式 nodes 中 schedule_mode == "daily" 同样视为每天定时。
    """
    if not mwf:
        return False
    blocks = mwf.get("blocks")
    if blocks:
        for b in blocks:
            if (b.get("type") == "timer"
                    and b.get("indent", 0) == 0
                    and b.get("data", {}).get("wait_type", "duration") == "until"):
                return True
        return False
    # 旧格式 nodes 兼容
    nodes = mwf.get("nodes") or []
    for n in nodes:
        if n.get("schedule_mode") == "daily":
            return True
    return False


def get_master_workflow_schedule_time(mwf: Dict[str, Any]) -> Optional[str]:
    """返回母工作流的"每天定时"预定时间（"HH:MM"），非每天定时时返回 None。

    新格式：顶层（indent==0）定时器块 wait_type=="until" 的 data.time。
    旧格式：nodes 中 schedule_mode=="daily" 的定时时间字段。
    """
    if not mwf:
        return None
    blocks = mwf.get("blocks")
    if blocks:
        for b in blocks:
            if (b.get("type") == "timer"
                    and b.get("indent", 0) == 0
                    and b.get("data", {}).get("wait_type", "duration") == "until"):
                return b.get("data", {}).get("time") or None
        return None
    nodes = mwf.get("nodes") or []
    for n in nodes:
        if n.get("schedule_mode") == "daily":
            return n.get("time") or n.get("schedule_time") or None
    return None


def new_screenshot_id() -> str:
    """生成唯一截图 ID，用于步骤/代码块的模板截图命名。"""
    return "screenshot_" + time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def list_master_workflows() -> List[Dict[str, Any]]:
    """列出母工作流摘要，按 updated_at 倒序。"""
    mwfs = _list_all_master_workflows()
    out: List[Dict[str, Any]] = []
    for mwf in mwfs:
        out.append({
            "id": mwf.get("id", ""),
            "label": mwf.get("label", "未命名"),
            "enabled": mwf.get("enabled", True),
            "nodes_count": len(mwf.get("nodes", [])) or len(mwf.get("blocks", [])),
            "created_at": mwf.get("created_at", ""),
            "updated_at": mwf.get("updated_at", ""),
        })
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out


def load_master_workflow(mwf_id: str) -> Optional[Dict[str, Any]]:
    """加载完整母工作流。"""
    for mwf in _list_all_master_workflows():
        if mwf.get("id") == mwf_id:
            return mwf
    return None


def save_master_workflow(mwf: Dict[str, Any]) -> str:
    """保存母工作流为独立文件（文件名 = 标签名）。"""
    _ensure_master_workflows_dir()
    mwf_id = mwf.get("id") or new_master_workflow_id()
    mwf["id"] = mwf_id
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if "created_at" not in mwf:
        mwf["created_at"] = now
    mwf["updated_at"] = now

    label = mwf.get("label", "未命名")
    # 确保子文件夹存在
    safe = _sanitize_filename(label)
    _ensure_dir(os.path.join(master_workflows_dir(), safe))

    path = _master_workflow_file_path(label)
    # 检查旧文件夹并删除
    if mwf_id:
        for existing in _list_all_master_workflows():
            if existing.get("id") == mwf_id and existing.get("label") != label:
                old_folder = get_master_workflow_folder(existing.get("label", ""))
                if os.path.isdir(old_folder):
                    try:
                        shutil.rmtree(old_folder, ignore_errors=True)
                    except OSError:
                        pass
                break
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(mwf, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return mwf_id


def delete_master_workflow(mwf_id: str) -> None:
    for mwf in _list_all_master_workflows():
        if mwf.get("id") == mwf_id:
            folder = get_master_workflow_folder(mwf.get("label", ""))
            if os.path.isdir(folder):
                try:
                    shutil.rmtree(folder, ignore_errors=True)
                except OSError:
                    pass
            return


# ----------------------------------------------------------------------
# 母工作流编辑备份（未保存退出时还原原文件夹，保存后删除备份）
# ----------------------------------------------------------------------
MWF_EDIT_BACKUP_DIR = os.path.join(APPDATA_DIR, "master_workflows_edit_backup")


def _mwf_edit_backup_dir(label: str) -> str:
    safe = _sanitize_filename(label)
    return os.path.join(MWF_EDIT_BACKUP_DIR, safe)


def backup_master_workflow_folder(label: str) -> None:
    """把当前母工作流完整文件夹（含 json 与 screenshots）复制到备份区。

    幂等：重复调用会先清空旧备份再复制最新状态。
    """
    src = get_master_workflow_folder(label)
    if not os.path.isdir(src):
        return
    dst = _mwf_edit_backup_dir(label)
    shutil.rmtree(dst, ignore_errors=True)
    try:
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            s = os.path.join(src, name)
            d = os.path.join(dst, name)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            elif os.path.isfile(s):
                shutil.copy2(s, d)
    except OSError:
        pass


def restore_master_workflow_folder(label: str) -> None:
    """用备份还原母工作流原文件夹；无备份则不做任何事。"""
    dst = _mwf_edit_backup_dir(label)
    if not os.path.isdir(dst):
        return
    src = get_master_workflow_folder(label)
    if os.path.isdir(src):
        try:
            shutil.rmtree(src, ignore_errors=True)
        except OSError:
            pass
    try:
        os.makedirs(src, exist_ok=True)
        for name in os.listdir(dst):
            s = os.path.join(dst, name)
            d = os.path.join(src, name)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            elif os.path.isfile(s):
                shutil.copy2(s, d)
    except OSError:
        pass


def discard_master_workflow_backup(label: str) -> None:
    """保存成功后删除备份。"""
    shutil.rmtree(_mwf_edit_backup_dir(label), ignore_errors=True)


# ---------------------------------------------------------------------------
# 脚本库（Scripts）
# ---------------------------------------------------------------------------
#
# 脚本数据模型：
# {
#   "id": "script_20260814-120000",
#   "label": "数据分析脚本",          # 脚本名称（写作模型命名）
#   "language": "python",            # 语言
#   "code": "print('hello')",       # 脚本代码内容
#   "description": "",               # 功能描述
#   "created_at": "2026-08-14 12:00:00",
#   "updated_at": "2026-08-14 12:00:00",
# }

SCRIPTS_DIR = os.path.join(APPDATA_DIR, "scripts")


def _ensure_scripts_dir() -> None:
    _ensure_dir(scripts_dir())


def _script_file_path(label: str, language: str = "python") -> str:
    safe = _sanitize_filename(label)
    ext = ".html" if language == "web" else ".py"
    return os.path.join(scripts_dir(), f"{safe}{ext}")


def _read_script_from_py(path: str) -> Optional[Dict[str, Any]]:
    """从 .py 文件读取脚本，纯代码，无元数据头，标签从文件名获取。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            code = f.read()
    except OSError:
        return None

    stem = os.path.splitext(os.path.basename(path))[0]
    return {
        "id": "script_" + stem,
        "label": stem,
        "language": "python",
        "code": code,
        "description": "",
        "created_at": "",
        "updated_at": "",
    }


def _read_script_from_html(path: str) -> Optional[Dict[str, Any]]:
    """从 .html 文件读取网页脚本，标签从文件名获取。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            code = f.read()
    except OSError:
        return None

    stem = os.path.splitext(os.path.basename(path))[0]
    return {
        "id": "script_" + stem,
        "label": stem,
        "language": "web",
        "code": code,
        "description": "",
        "created_at": "",
        "updated_at": "",
    }


def _list_all_scripts() -> List[Dict[str, Any]]:
    _ensure_scripts_dir()
    sdir = scripts_dir()
    out: List[Dict[str, Any]] = []
    try:
        for name in os.listdir(sdir):
            path = os.path.join(sdir, name)
            if name.endswith(".py"):
                script = _read_script_from_py(path)
                if script and isinstance(script, dict):
                    out.append(script)
            elif name.endswith(".html"):
                script = _read_script_from_html(path)
                if script and isinstance(script, dict):
                    out.append(script)
            elif name.endswith(".json"):
                # 向后兼容旧 .json 格式
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        script = json.load(f)
                    if isinstance(script, dict):
                        out.append(script)
                except (OSError, json.JSONDecodeError):
                    continue
    except OSError:
        pass
    return out


def list_scripts() -> List[Dict[str, Any]]:
    """列出脚本库摘要，按标签排序。"""
    scripts = _list_all_scripts()
    out: List[Dict[str, Any]] = []
    for s in scripts:
        out.append({
            "id": s.get("id", ""),
            "label": s.get("label", "未命名"),
            "language": s.get("language", "python"),
        })
    out.sort(key=lambda x: x.get("label", ""))
    return out


def load_script(script_id: str) -> Optional[Dict[str, Any]]:
    """加载完整脚本（含 code）。"""
    for s in _list_all_scripts():
        if s.get("id") == script_id:
            return s
    return None


def load_script_by_label(label: str) -> Optional[Dict[str, Any]]:
    """按标签加载脚本。"""
    path = _script_file_path(label)
    script = _read_script_from_py(path)
    if script:
        return script
    # 向后兼容旧 .json 格式
    json_path = os.path.join(scripts_dir(), f"{_sanitize_filename(label)}.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_script(script: Dict[str, Any]) -> str:
    """保存脚本为纯 .py / .html 文件（文件名 = 标签名，无元数据头）。返回 id。

    根据 language 字段判断：language=="web" 保存为 .html，否则保存为 .py。
    自动处理标签重复：如果已有同名脚本，在开头加数字序数（2/3/4...）。
    """
    _ensure_scripts_dir()
    language = script.get("language", "python")
    label = script.get("label", "未命名")
    script_id = "script_" + _sanitize_filename(label)

    # 解析标签重复（与工作流共用 _resolve_label，但作用域不同）
    existing_labels = set()
    for s in _list_all_scripts():
        existing_labels.add(s.get("label", ""))

    if label in existing_labels:
        prefix = 2
        while True:
            candidate = f"{prefix}{label}"
            if candidate not in existing_labels:
                label = candidate
                break
            prefix += 1
        script["label"] = label
        script_id = "script_" + _sanitize_filename(label)

    # 写入文件（无元数据头）
    path = _script_file_path(label, language)
    # 删除同名的旧 .json 文件（迁移清理）
    old_json_path = os.path.join(scripts_dir(), f"{_sanitize_filename(label)}.json")
    if os.path.isfile(old_json_path):
        try:
            os.remove(old_json_path)
        except OSError:
            pass

    code = script.get("code", "")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
    except OSError:
        pass
    return script_id


def delete_script(script_id: str) -> None:
    for s in _list_all_scripts():
        if s.get("id") == script_id:
            label = s.get("label", "")
            # 尝试删除 .py 和 .html 两种可能的文件
            for ext in (".py", ".html"):
                path = _script_file_path(label, "python" if ext == ".py" else "web")
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            # 同时尝试删除旧 .json 文件
            json_path = os.path.join(scripts_dir(), f"{_sanitize_filename(label)}.json")
            if os.path.isfile(json_path):
                try:
                    os.remove(json_path)
                except OSError:
                    pass
            return
