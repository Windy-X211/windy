# 规划性桌面代理

基于「规划模型 + 视觉模型 + 执行器」双模型架构的桌面自动化代理。PySide6 实现，微信式对话界面。

## 工作原理

1. **规划模型**：观察当前屏幕截图 + 用户目标，输出原子化执行步骤，格式固定：
   ```
   当前屏幕状态：桌面
   目标：搜索李白是谁
   [单击-桌面上-浏览器图标>单击-浏览器界面中-搜索栏>输入-李白是谁>按键-enter>单击-搜索结果页中-第一个条目]
   ```
2. **视觉模型**：按步骤截图定位元素，只返回归一化坐标 `x.xx,x.xx` 或 `None`，单轮无上下文。
3. **执行函数**：把步骤参数转为键鼠操作（单击/双击/右键/上滑/下滑/拖动/输入/按键/等待）。
4. **错误处理**：接口不可用→提示更换；解析失败→加引导重新输出（不重复原 prompt）；坐标校验 0-1 且两值；视觉 None/执行失败→反馈规划模型重规划或插入意外处理步。

## 文件结构

```
规划性桌面代理/
├── main.py              # 入口（DPI 修正、QApplication、连线、悬浮球、热键）
├── app_config.py        # AppData 配置与历史管理
├── model_client.py      # 统一 call_model（标签路由/缓存/流式）
├── step_model.py        # Step 数据模型、parse_steps、validate_coord
├── planner.py           # 规划模型（固定 prompt + 重试引导）
├── vision.py            # 视觉模型（单轮定位 + 拖动双定位）
├── executor.py          # 执行器（显式 dispatch，零 exec/eval）
├── capture.py           # mss 截图 + hide-on-capture 钩子
├── agent_worker.py      # 编排核心（状态机 + 信号槽 + QEventLoop）
├── ui/                  # 界面（主窗口/气泡/代理面板/执行覆盖/设置/历史/悬浮球）
└── AppData/             # 运行时数据（interfaces.json / settings.json / history/）
```

## 运行

> **注意**：搜狗输入法沙箱冲突可能导致进程退出。建议从终端直接运行，或在 Trae 设置→对话→自定义沙箱配置中加入可写目录 `C:\Users\yangq\AppData\LocalLow\SogouPY`。

```powershell
& 'F:\anaconda3\python.exe' f:\PythonProjects\TraeProjects\规划性桌面代理\main.py
```

默认接口预填本地视觉模型服务 `http://127.0.0.1:8008/v1`，可在「⚙ 设置 → 模型接口」中新增/切换其他接口（凭标签区分）。

## 使用流程

1. **聊天模式**：底部切换为「聊天模式」，与模型正常多轮对话。
2. **代理模式**（默认）：
   - 输入目标（如「搜索李白是谁」）→ 规划模型产出步骤并显示。
   - 步骤可手动编辑；点击「确认执行」开始，或「修改」重新解析显示。
   - 也可通过对话框输入文字调整规划。
   - 执行时窗口变透明，仅显示暂停/不被截图按钮 + 进度文本（✓ 绿/○ 灰）+ 进度条。
   - 执行中可随时暂停，并在输入框补充信息给规划模型（恢复后据此重新规划）。
3. **悬浮球**：单击展开/隐藏主窗口；可拖动。
4. **历史**：左上角「≡ 历史」查看/加载历史对话（保存位置可在设置中更改，默认 `AppData/history`）。
5. **设置**：接口预设（增改删，标签默认取接口地址）、背景图、历史位置、进度字体大小与颜色。

## 关键设计

- **线程模型**：QThread worker 跑所有模型/截图/执行；UI 经 Qt 信号槽中转；确认/咨询/暂停用嵌套 QEventLoop 阻塞等待，期间仍可处理暂停与补充信息。
- **不变形背景**：`max(sw/pw, sh/ph)` 等比缩放填满窗口，右顶对齐，溢出处裁剪，绝不拉伸。
- **截图隐藏**：截图前 `ShowWindow(SW_HIDE)` 临时隐藏主窗口与悬浮球，截图后恢复（Win11 上 `SetWindowDisplayAffinity` 会让窗口完全不可见，故不用）。
- **中文输入**：剪贴板置 `CF_UNICODETEXT` + `ctrl+v`，可靠输入中文。
- **高 DPI**：入口 `SetProcessDPIAware` + `GetSystemMetrics` 取真实分辨率，归一化坐标 × 物理像素。
- **安全执行**：执行器显式 dispatch 字典，零 `exec`/`eval`。

## 配置文件

- `AppData/interfaces.json`：`{active, presets{tag:{base_url,api_key,model,max_tokens,supports_vision,temperature}}}`
- `AppData/settings.json`：背景图、历史目录、进度字体/颜色、截图隐藏开关、滑动档量、各重试上限
- `AppData/history/<session_id>.json`：单会话消息与步骤
