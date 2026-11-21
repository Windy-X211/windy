# 规划性桌面代理（Windy）

Windows 桌面自动化智能体。以「多模型协作 + 原子化操作」为核心，提供**自主代理、规划执行、聊天、编程**四种工作模式，并附带母工作流编排、脚本库、经验库与任务清单等能力。界面为 PySide6 实现的微信式对话窗口，可收进悬浮球常驻桌面。

> 仅支持 Windows（依赖 Win32 API：窗口隐藏、鼠标钩子、剪贴板、DPI 等）。

## 功能一览

| 模式 | 模式值 | 模型角色 | 说明 |
| --- | --- | --- | --- |
| 自主代理模式 | `agent_mode` | `agent_mode` | 多模态模型直接看屏幕、自主决策、循环执行工具，直到调用 `complete` / `fail` |
| 规划模式 | `agent` | `planning` + `vision` | 规划模型产出原子化步骤，视觉模型逐条定位，执行器落地键鼠操作 |
| 聊天模式 | `chat` | `chat`（可叠加 `guide` / `cos` / `persona`） | 标准对话 / Pro 真人双模型 / cos 角色扮演 |
| 编程模式 | `programming` | `programming`（Pro 时为 `programmer` + `checker`） | 标准代码生成 / Pro 编程智能体自主改项目 |

模式在输入框上方下拉框切换，默认聊天模式。聊天模式、编程模式各有一个三点位开关用于切换「标准 / Pro（/ cos）」。

## 架构

### 模型路由：接口预设 + 角色映射

`model_client.call_model(label, ...)` 是唯一的模型调用层。所有接口都是 **OpenAI 兼容协议**（用 `requests` 直连 `/chat/completions`，不依赖官方 SDK）。

- `interfaces.json` 的 `presets` 以**标签**（`tag`）区分不同接口：`base_url` / `api_key` / `model` / `max_tokens` / `temperature` / `top_p` / `top_k` / `supports_vision`。
- `model_map` 把**模型角色**映射到预设标签，空值回退到 `active`：

| 角色 | 用途 | 角色 | 用途 |
| --- | --- | --- | --- |
| `planning` | 规划模式出步骤 | `programming` | 编程模式生成代码 |
| `vision` | 单轮视觉定位 | `programmer` | Pro 编程主模型 |
| `chat` | 聊天模式对话 | `checker` | Pro 编程的检查模型 |
| `cos` | cos 角色扮演 | `agent_mode` | 自主代理决策 |
| `guide` | Pro 聊天「引导导演」 | `long_input` | 长文本/写作 |
| `persona` | 人设生成 | | |

这样规划、视觉、聊天、编程可以各自挂在不同接口/不同本地模型上（例如本地 Ollama 跑视觉、云端跑编程）。

### 运行架构

```
main.py
 ├─ MainWindow（UI 主线程，永不直接调用模型/截图/执行）
 ├─ AgentWorker.moveToThread(QThread)（全部模型调用、截图、键鼠执行、状态机）
 └─ FloatingBall（悬浮球）
```

- **状态机**：`IDLE → PLANNING → AWAIT_CONFIRM → EXECUTING → DONE`，另有 `RECOVERING / CONSULTING_PLANNER / AWAIT_CONSULT / PAUSED`。
- **阻塞点**（等确认、等补充信息、暂停）用**纯 Python 轮询等待**实现，刻意不在工作线程里用 `QEventLoop` / `processEvents`，避免界面卡死与跨线程崩溃。
- **截图**：`mss` 抓屏；抓屏前通过 `ShowWindow(SW_HIDE)` 临时隐藏主窗口与悬浮球（Win11 上 `SetWindowDisplayAffinity` 会让窗口彻底不可见，故不用这种方式隐藏主窗口，只有思考浮窗用 `WDA_EXCLUDEFROMCAPTURE`）。
- **执行**：`executor` 用显式 dispatch 字典把步骤转成 pyautogui 键鼠动作，**零 `exec`/`eval`**；中文文本走「剪贴板置 `CF_UNICODETEXT` + `ctrl+v`」而非模拟键入，避免输入法干扰。

## 四种模式

### 1. 自主代理模式

多模态模型循环「看截图 → 决定动作 → 执行 → 再看截图」，不预先规划步骤，适合步骤随界面变化的开放任务。

- **坐标**：统一归一化 `(0~1)`，左上角 `(0,0)`，`x` 向右、`y` 向下。
- **工具集**（模型以 function call 调用）：

| 类别 | 工具 |
| --- | --- |
| 鼠标 | `click` `double_click` `right_click` `move_mouse` `scroll` |
| 键盘 | `type`（焦点处输入文本） `key`（`enter` / `ctrl+c` / `alt+f4` …） |
| 等待 | `wait`（固定秒数） `wait_change`（对比截图，等屏幕变化并稳定） |
| 观察 | `focus`（以某点为中心放大截取局部，提高小控件成功率） |
| 命令 | `run_cmd`（cmd 执行） `run_python`（执行 Python 代码） |
| 经验 | `search_experience`（卡住时按关键字检索专用经验） `summarize_experience`（沉淀通用/专用经验） |
| 操作区方案 | `set_region` `op_click` `op_double_click` `op_right_click` `update_button` `full_view` `stop_region` |
| 结束 | `complete` `fail` |

- **操作区/结果区方案**：面对「一排固定按钮 + 内容随点击变化的结果区」时，模型一次性识别所有按钮坐标并框定结果区；此后每轮只截结果区放大图，用按钮名点击，避免每次全屏重新定位。
- **思考浮窗**：右下角半透明浮窗实时显示模型推理、动作与结果，自身用 `WDA_EXCLUDEFROMCAPTURE` 排除在截图之外。
- **停止**：全局快捷键 `Ctrl+.`（设置项 `stop_shortcut`），或界面按钮；可暂停后补充信息再继续。
- ⚠️ `run_cmd` / `run_python` 是**高权限工具**，模型可执行任意本机命令与 Python 代码，请只在可信环境使用。

### 2. 规划模式

「规划 → 视觉定位 → 执行 → 失败重规划」的经典链路：

1. **规划模型**：屏幕截图 + 目标 → 固定格式步骤串：

   ```
   当前屏幕状态：桌面
   目标：搜索李白是谁
   [单击-桌面上-浏览器图标>单击-浏览器界面中-搜索栏>输入-李白是谁>按键-enter>单击-搜索结果页中-第一个条目]
   ```

   操作类型：单击、双击、右键、上滑、下滑、拖动、输入、长输入、按键、等待、咨询用户。解析失败时追加引导消息重新请求（不重复原 prompt），超过 `planner_retry` 抛 `ParseError`。

2. **步骤可编辑**：解析后以两列表格呈现（第一列操作指令下拉框，第二列参数编辑器），可右键添加输入变量 / 上下插入空行 / 删除行，也可发消息让模型重新规划。行内**「🎯 手动定位」**可用鼠标中键直接拾取坐标，或框选屏幕区域存为模板图。

3. **定位方式**（每条步骤二选一）：
   - **视觉模型**：单轮无上下文，只回归一化坐标 `x.xx,x.xx` 或 `None`；
   - **模板匹配**：用 `OpenCV`（CLAHE + 多尺度 + top1/top2 margin + 边缘重合度二次校验）在屏幕找模板图，阈值取步骤级 → 全局 `template_threshold` → 默认 0.65。

4. **执行**：点击「确认执行」后窗口变透明，只留进度文本（✓ 绿 / ○ 灰）+ 进度条 + 「暂停 / 不被截图」按钮；执行中可暂停并补充信息，恢复时据此重新规划（`replan_limit` 限制次数）。

5. **错误处理**：接口不可用 → 提示更换接口；坐标越界或视觉返回 `None` → 反馈规划模型重规划，或插入意外处理步骤（`RECOVERING`）。

### 3. 聊天模式

三点位开关切换三种子模式：

- **标准**：单模型多轮对话，`chat` 角色。
- **Pro（真人双模型）**：`guide` 模型充当「编导」，实时看用户输入与聊天模型输出，用命令符控制节奏——`[direct]` 原样转发、`[stop]` 打断、`[max_tokens=N]` 限长、`[start=引导词]` 指定开头、`[lead=提示词]` 下指令、`[wait=N]` 停 N 秒后主动开话题、`[end]` 结束。目的是让回复更像真人：一次只说一句、被打断能收住。
- **cos（角色扮演）**：单模型自循环，模型既是角色也是导演，只通过 `[say=文字]` 发言、`[wait=N]` 控制停顿、`[end]` 收场；命令符之外的文本只做内心独白不渲染。人设从 `personas.json` 选择/编辑（界面「人设」按钮），也可让模型按描述生成新人格。

附件图片以 base64 内嵌进上下文（`model_client` 的 `@@EMBED_IMAGE@@` 机制），不是把路径当文本丢给模型。

### 4. 编程模式

- **标准**：`programming` 角色，按需求生成代码并流式输出，结果存 `code/`。
- **Pro 编程智能体**（开关开启后）：选定**项目文件夹**，模型通过命令符自主读写文件、跑程序、自我验证，循环直到 `[summary]`：

| 命令符 | 作用 |
| --- | --- |
| `[schedule]<start>步骤\|步骤</end>` | 规划/更新步骤清单 |
| `[structure]` | 取项目目录结构 |
| `[read:路径,起,止]` | 读文件（带行号，正反索引：1 首行、-1 末行） |
| `[write:路径]<start>内容</end>` | 创建/覆盖文件 |
| `[replace:路径,起,止]<start>新内容</end>` | 替换指定行区间 |
| `[remove:路径,起,止]` / `[delete:路径]` | 删除内容 / 删除文件或目录 |
| `[search:范围,字符串]` | 搜索并返回 `相对路径 + 行号` |
| `[run:路径]` / `[stop:路径]` | 运行程序并实时回传控制台输出 / 停止 |
| `[check:范围]` | 交 `checker` 模型只读检查，回 `路径:行号:错误` |
| `[summary]<start>总结</end>` | 总结并等待用户反馈 |

命令解析用「括号深度扫描 + 固定标量参数拆分」两阶段，内容可含逗号、换行与成对括号。自循环内置三重守卫：

- **计划守卫**：已有未完成计划时拒绝被相似的新计划覆盖，强制按剩余步骤推进；
- **验证闭环**：记录最近 `[run]` 退出码，编辑后未重新运行验证时拦截 `[summary]`；
- **重复抑制**：frequency/presence 惩罚 + 只读命令结果缓存 + 同类操作超 `pro_same_scheme_cap` 次注入强制升级引导。

配套内置 **IDE 窗口**（查看/编辑项目文件、跑代码，主题可切 light/dark）与项目文件夹下拉历史。

## 编排与辅助功能

- **控制中心**：母工作流编排 + 快捷入口（工作流列表 / 脚本库）。母工作流是块式编排，执行语义类似 Python（缩进控制嵌套）：
  - 块类型：`timer` 定时器、`workflow` 调用工作流、`script` 调用脚本库脚本（支持变量传递）、`condition` if/elif/else、`loop` while（模板匹配或次数+间隔）、`continue`、`break`；
  - 支持右键启用/禁用、重命名、删除，`▶ 运行所有` 批量运行；出错时悬浮球变红并可直接点开出错的母工作流。
- **任务清单**：屏幕右上角半透明小表格，列出**已启用**的母工作流，当日完成打 ✓、失败打 ✗ 并标注时刻；`WS_EX_TRANSPARENT + WS_EX_LAYERED` 实现**系统级鼠标穿透**（点击落到下层窗口），透明度可调。
- **工作流 / 脚本库**：分别存在 `workflows/`、`scripts/`，可在独立面板中编辑、导出。
- **经验库**：`experience.json` 分「常用经验」（自动附加到自主代理系统提示词）与「专用经验」（模型主动检索），读写加模块级锁，UI 线程与工作线程并发安全。
- **人设库**：`personas.json`。
- **历史会话**：每次对话一个目录 `history/<会话ID>/`，含 `session.json` 与随消息保存的截图/附件。
- **悬浮球**：单击展开/隐藏主窗口；拖动移动；右键菜单含置顶、控制中心、任务清单、退出；颜色反映母工作流状态。
- **界面**：背景图等比裁剪不变形（`max(sw/pw, sh/ph)` 填满、右顶对齐、溢出处裁掉），支持视频背景；气泡与任务清单透明度可调；toast 提示条。

## 文件结构

```
规划性桌面代理/
├── main.py                    # 入口：DPI/Qt 插件路径、QApplication、窗口、Worker 线程、悬浮球、截图隐藏钩子
├── app_config.py              # 配置与数据管理：interfaces/settings/personas/experience、会话/工作流/脚本/母工作流/代码存储
├── model_client.py            # 统一模型调用（OpenAI 兼容、requests、流式 on_chunk、图片 base64 内嵌）
├── step_model.py              # Step 模型、parse_steps、坐标解析校验、异常类型
├── planner.py                 # 规划模型（固定 prompt + 重试引导）
├── vision.py                  # 视觉模型（单轮定位；拖动分两次调用）
├── executor.py                # 执行器（显式 dispatch、零 exec/eval、中文剪贴板输入、拖动）
├── capture.py                 # mss 截图、hide-on-capture 钩子、红色十字鼠标标记
├── template_matcher.py        # OpenCV 模板匹配（CLAHE、多尺度、margin 校验、ROI）
├── manual_positioner.py       # 鼠标中键拾取坐标 / 框选模板（WH_MOUSE_LL 全局钩子）
├── agent_mode.py              # 自主代理执行器与工具集
├── agent_overlay.py           # 自主代理思考浮窗（排除截图）
├── agent_worker.py            # 编排核心：状态机、信号槽、四种模式调度
├── pro_chat.py                # Pro 聊天双模型编排（guide 命令符）
├── cos_chat.py                # cos 角色扮演单模型自循环
├── programming_pro.py         # Pro 编程智能体（命令符解析 + 计划/验证/重复守卫 + run 进程管理）
├── master_workflow_executor.py# 母工作流块式执行器（timer/workflow/script/condition/loop/continue/break）
├── experience.py              # 经验库（常用 + 专用，线程安全）
├── test_template_matcher.py   # 模板匹配交互式测试脚本
├── ui/
│   ├── main_window.py         # 主窗口：对话区、底部模式栏、各面板调度
│   ├── chat_view.py           # 消息区渲染（气泡、代码块、进度、折叠框）
│   ├── agent_panel.py         # 规划模式步骤表格（操作下拉 + 参数编辑、确认/修改、🎯 手动定位、进度）
│   ├── execution_overlay.py   # 执行中的透明覆盖层（进度/暂停）
│   ├── control_center.py      # 控制中心（母工作流编排）
│   ├── master_workflow_editor.py # 母工作流块编辑器
│   ├── workflow_panel.py      # 工作流列表
│   ├── script_panel.py        # 脚本库
│   ├── ide_dialog.py          # 内置 IDE
│   ├── persona_dialog.py      # 人设管理
│   ├── experience_dialog.py   # 经验库查看/编辑
│   ├── model_config_dialog.py # 接口预设配置
│   ├── settings_dialog.py     # 设置
│   ├── history_panel.py       # 历史会话
│   ├── task_list.py           # 右上角穿透任务清单
│   ├── floating_ball.py       # 悬浮球
│   ├── video_background.py    # 视频背景
│   └── toast_banner.py        # 轻提示条
└── AppData/                   # 运行时数据（首次启动生成，默认不入版本库）
```

> 根目录 `_*.py`（`_probe_*.py`、`_t*.py`、`_patch_*.py` 等）是开发期调试/验证探针脚本，不参与程序运行。

## 环境与运行

**环境要求**

- Windows 10 / 11（64 位）
- Python 3.10+（实测 3.13.9）
- 一个 OpenAI 兼容、且**具备视觉能力**的模型接口（本地 Ollama / vLLM / LM Studio / 云端均可）

**依赖**

```powershell
pip install PySide6 openai Pillow mss pyautogui pynput keyboard requests
pip install opencv-python numpy   # template_matcher 需要，requirements.txt 未收录
```

`requirements.txt` 中 `openai` 目前不是必需项（`model_client` 用 `requests` 直连），保留以兼容部分走 SDK 的旧环境。

**启动**

```powershell
& 'F:\anaconda3\python.exe' 'F:\PythonProjects\TraeProjects\规划性桌面代理\main.py'
```

首次启动会创建 `AppData/` 及默认子目录，并生成一份指向本地 Ollama 的默认接口预设（`http://localhost:11434/v1`、`api_key=sk-local`、`model=vl`）。**该默认值只是占位**，请到「⚙ 设置 → 模型接口」改成你自己的地址、密钥与模型名，并为规划/视觉/聊天/编程等角色挂上对应预设（见上文 `model_map`）。

**注意**

- Anaconda 环境下偶发 `Could not find the Qt platform plugin windows`：`main.py` 已在 `QApplication` 构造前用 `QLibraryInfo` 显式补 `QT_QPA_PLATFORM_PLUGIN_PATH`，若仍失败请检查 PySide6 安装完整性。
- 搜狗输入法沙箱冲突可能导致进程直接退出。建议从终端直接运行，或把 `C:\Users\<用户>\AppData\LocalLow\SogouPY` 加入可写目录。
- 高 DPI：不再手动 `SetProcessDPIAware`（Qt6 自身设定 `PER_MONITOR_AWARE_V2`，重复设置会触发拒绝访问警告），坐标换算靠 `GetSystemMetrics` 取真实物理分辨率。

## 配置与数据

配置文件固定在程序同级的 `AppData/`，业务数据放在「用户数据根目录」（默认同为 `AppData/`，可在设置里改为其他绝对/相对路径）。

| 文件 / 目录 | 内容 |
| --- | --- |
| `interfaces.json` | `{active, presets{tag:{base_url,api_key,model,max_tokens,temperature,top_p,top_k,supports_vision}}, model_map{角色:tag}}`；旧单配置格式会自动迁移 |
| `settings.json` | 背景图、窗口标题、用户数据目录、进度字体/颜色、`hide_on_capture`、滚动档量、气泡/任务清单透明度、`model_timeout`、各重试上限、`template_threshold`、自主代理惩罚与 `agent_post_wait`、`stop_shortcut`、Pro 编程相关项、IDE 主题等 |
| `personas.json` | 人设库 |
| `experience.json` | 经验库（common / special） |
| `history/<会话ID>/` | `session.json` + 消息附件与截图 |
| `workflows/` `master_workflows/` `scripts/` `code/` `cache/` | 工作流、母工作流、脚本、生成的代码、缓存 |

## 版本控制说明

仓库：https://github.com/Windy-X211/windy

`.gitignore` 已排除 `AppData/`——它含**真实 API Key**、本机绝对路径与任务历史，不应入库。克隆后请自行在界面里配置接口；若你要备份配置，请先把密钥移到环境变量或独立的本地文件，再单独提交。

## 已知限制

- 只做 Windows：截图、隐藏、中键钩子、剪贴板、DPI 全部依赖 Win32 API。
- 规划模式的成败高度依赖视觉模型；坐标在**屏幕缩放**或分辨率变化后需重新定位。
- 全屏独占类应用（部分游戏、受保护视频）可能无法截图，需改用窗口模式。
- 自主代理的 `run_cmd` / `run_python` 无沙箱约束，请只在可信模型与可信环境下启用。
