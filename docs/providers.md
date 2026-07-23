> [English](en/providers.md) | 中文

<a id="providers"></a>

# 提供方

CCGram 支持多种代理 CLI 后端。每个 Telegram 话题可以使用不同的提供方 —— 通过目录浏览器创建会话时选择。

<a id="overview"></a>

## 总览

| 提供方      | CLI 命令 | Hook 事件 | Resume | Continue | 转录格式   | 状态检测                                                              |
| ----------- | -------- | --------- | ------ | -------- | ---------- | --------------------------------------------------------------------- |
| Claude Code | `claude` | 是        | 是     | 是       | JSONL      | Hook 事件 + pyte VT100 + 加载动画                                     |
| Codex CLI   | `codex`  | 是        | 是     | 是       | JSONL      | Hook Stop + pyte VT100 交互式 UI + 转录活动启发式                     |
| Gemini CLI  | `gemini` | 是        | 是     | 是       | JSONL      | Hook AfterAgent + 面板标题 + 交互式 UI + `/status` 快照               |
| Pi          | `pi`     | 是        | 是     | 是       | JSONL (v3) | Hook-runner Stop + 转录活动启发式                                     |
| Shell       | `bash`   | 否        | 否     | 否       | 无         | Shell 提示符空闲检测                                                  |

<a id="choosing-a-provider"></a>

## 选择提供方

**从 Telegram**：创建新话题并选择目录后 —— 若目录是符合条件的 git 仓库，先选择使用当前分支还是在新分支上创建新 worktree（非 git 目录跳过这一步）—— 随后出现提供方选择器，包含 Claude（默认）、Codex、Gemini、Pi 和 Shell。选定提供方后，CCGram 询问会话模式：

- `✅ Standard`（正常审批）
- `🚀 YOLO`（各提供方专属的宽松模式）

**从终端**：如果你手动创建窗口并启动代理 CLI，CCGram 会从运行中的进程名自动检测提供方。当面板命令是 JS 运行时封装（node、bun）时，会检查面板的前台进程以可靠地识别实际的 CLI。前台进程的读取方式由复用器后端负责 —— tmux 使用 `ps -t <tty>`，herdr 读取 `pane process-info`（无需 tty）—— 因此检测在两种后端上表现一致。shell 提供方使用同一接缝来识别纯 shell 面板。作为最后手段，还会检查 Gemini 的面板标题符号（`✦`、`✋`、`◇`）。

**默认提供方**：设置 `CCGRAM_PROVIDER=codex`（或 `gemini`、`pi`、`shell`）修改默认值。未设置时默认为 Claude。

<a id="session-mode-standard-vs-yolo"></a>

## 会话模式（Standard 与 YOLO）

CCGram 按窗口存储模式，并在恢复/继续/选择恢复流程中复用。

- `normal` 模式按原样启动提供方命令。
- `yolo` 模式追加各提供方原生的宽松权限参数：
  - Claude：`--dangerously-skip-permissions`
  - Codex：`--dangerously-bypass-approvals-and-sandbox`
  - Gemini：`--yolo`

YOLO 会话在 Telegram 话题标题中以 `🚀` 徽标标识，在 `/sessions` 中以 `[YOLO]` 标签标识。远程控制（Remote Control）激活时，话题标题还会显示 `📡` 徽标。

<a id="custom-launch-commands"></a>

## 自定义启动命令

通过 `CCGRAM_<NAME>_COMMAND` 环境变量覆盖各提供方的启动命令：

```ini
CCGRAM_CLAUDE_COMMAND=ce --current
CCGRAM_CODEX_COMMAND=my-codex-wrapper
CCGRAM_GEMINI_COMMAND=/opt/gemini/run
CCGRAM_PI_COMMAND=pi --model sonnet
```

`<NAME>` 为大写：`CLAUDE`、`CODEX`、`GEMINI`、`PI`。未设置时默认为提供方的内置命令（`claude`、`codex`、`gemini`、`pi`）。新增提供方自动支持 `CCGRAM_<NAME>_COMMAND`，无需改代码。

也可以用它做全局"今日"配置（对所有新会话生效），例如：

```ini
CCGRAM_CLAUDE_COMMAND=claude --dangerously-skip-permissions
CCGRAM_CODEX_COMMAND=codex --dangerously-bypass-approvals-and-sandbox
CCGRAM_GEMINI_COMMAND=gemini --yolo
```

<a id="provider-specific-commands"></a>

## 提供方专属命令

每个提供方向 Telegram 菜单暴露自己的斜杠命令。示例：

- **Claude**：`/clear`、`/compact`、`/cost`、`/doctor`、`/permissions`……
- **Codex**：`/model`、`/mode`、`/status`、`/diff`、`/compact`、`/mcp`……
- **Gemini**：`/chat`、`/clear`、`/compress`、`/model`、`/memory`、`/vim`……
- **Pi**：`/new`、`/compact`、`/followup`、`/scoped_models`、`/export`、`/name`、`/reload`、`/session`、`/share`、`/changelog`……（外加动态发现的 skills/prompts/extensions）

---

<a id="claude-code"></a>

## Claude Code

Claude Code 的集成最为丰富 —— hook 事件（SessionStart、Notification、Stop、StopFailure、SessionEnd、SubagentStart、SubagentStop、TeammateIdle、TaskCompleted）提供即时会话追踪、交互式 UI 检测、完成/空闲检测、API 错误告警、会话生命周期清理、子代理活动监控和代理团队通知。

机器人还能检测远程控制模式（📡 话题徽标 + 一键激活按钮）。Claude 的 `/remote-control` 不报告结果，因此 ccgram 在触发 RC 后约 1.5 秒捕获面板（每 1.5 秒重扫，最长 10 秒），将结果分类为 —— 成功且有分享 URL、成功但无 URL、不可用、失败 —— 并在话题中发送单条状态回复。此功能仅限 Claude；其他提供方保持原有的"不支持"回复。机器人使用 pyte VT100 屏幕缓冲区作为终端状态解析的回退手段。多面板窗口（如代理团队产生的）会被自动扫描以发现阻塞的面板，并以内联键盘告警的形式呈现。

<a id="hooks"></a>

### Hooks

使用 `ccgram hook --install` 安装 hooks。

hooks 缺失时，ccgram 会在启动时给出警告并附上修复命令。hooks 是可选的 —— 终端抓取可作为回退手段。

<a id="claude-transcript"></a>

### Claude 转录

Claude 转录是 `~/.claude/projects/` 下的 JSONL 文件，通过字节偏移量增量读取，轮询高效。

<a id="task-lists"></a>

### 任务列表

Claude 任务状态源自转录文件，而非抓取终端底栏。CCGram 识别结构化的 `TaskCreate`、`TaskUpdate`、`TaskList` 工具流程以及旧式 `TodoWrite`，并将当前任务渲染在话题唯一的可编辑状态气泡中。Hook 通知用于更快刷新等待状态标头（如等待输入或审批提示），但不取代转录文件作为事实来源。

<a id="codex-cli"></a>

## Codex CLI

Codex CLI 支持功能开关控制的 hooks。使用 `ccgram hook --provider codex --install` 安装 ccgram 的生命周期 hooks；ccgram 会在用户级 `~/.codex/hooks.json` 中写入 `SessionStart` 和 `Stop` 条目，并在 `~/.codex/config.toml` 中启用 `[features].codex_hooks = true`。转录发现仍作为回退手段和消息事实来源。

<a id="interactive-prompts"></a>

### 交互式提示

Codex 的交互式提示（问题列表、权限提示及其他选择 UI）通过 pyte 从终端屏幕内容中检测，并以内联键盘控件呈现。

<a id="edit-approval-formatting"></a>

### 编辑审批格式化

当 Codex 请求文件编辑审批时，终端输出可能包含密集的并排 diff 行，在 Telegram 中难以阅读。CCGram 在发送交互式提示前会重新格式化该内容：

- 保留审批控件和操作提示（`Yes/No`、`Press enter`、`Esc`）。
- 添加紧凑摘要（`File`、`Changes: +N -M`）。
- 在可用时添加已解析变更行的简短预览。
- 略去不可读的折行 diff 数据块，而非转发嘈杂的原始文本。

典型输出形式：

```text
Do you want to make this edit to src/ccgram/example.py?
File: src/ccgram/example.py
Changes: +1 -1
Preview:
  - return old_value
  + return new_value

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for these files (a)
  3. No, and tell Codex what to do differently (esc)
Press enter to confirm or esc to cancel
```

<a id="status-fallback"></a>

### 状态回退

对于 Codex，`/status` 会在 Telegram 中发送一份基于转录的回退快照（会话/cwd/token/速率限制摘要），因为某些 Codex 版本在终端 UI 中渲染状态，而不产生转录中的助手消息。

<a id="codex-transcript"></a>

### Codex 转录

Codex 转录是 `~/.codex/sessions/` 下的 JSONL 文件，通过字节偏移量增量读取。

<a id="gemini-cli"></a>

## Gemini CLI

Gemini CLI 支持在 `settings.json` 中配置命令 hooks。使用 `ccgram hook --provider gemini --install` 安装 ccgram 的生命周期 hooks；ccgram 会在用户级 `~/.gemini/settings.json` 中写入 `SessionStart`、`AfterAgent`、`SessionEnd` 和 `Notification` 条目。转录发现仍作为回退手段和消息事实来源。

Gemini 会设置面板标题（`Working: ✦`、`Action Required: ✋`、`Ready: ◇`），CCGram 读取它们获取状态；其 `@inquirer/select` 权限提示会被检测为交互式 UI。Gemini 转录发现仅匹配项目哈希/别名（不做跨项目全盘扫描），以避免关联到错误的会话。

<a id="launch-hardening"></a>

### 启动加固

对于 ccgram 管理的 Gemini 启动，CCGram 会注入 `GEMINI_CLI_SYSTEM_SETTINGS_PATH=~/.ccgram/gemini-system-settings.json` 并设置 `tools.shell.enableInteractiveShell=false`，以避免 tmux 中的 node-pty `EBADF` 崩溃。如果你设置了 `CCGRAM_GEMINI_COMMAND`，则按原样使用你的覆盖值。

<a id="gemini-transcript"></a>

### Gemini 转录

自 Gemini CLI v0.40+ 起，转录是 `~/.gemini/tmp/<project-hash>/chats/` 下的追加式 JSONL 文件。每行是一条 JSON 记录（首行头部包含 `sessionId`/`projectHash`/`startTime`，之后是消息记录和 `{"$set": {...}}` 元数据更新）。CCGram 通过共享的 `JsonlProvider` 字节偏移读取器增量读取，并对重复的消息 id 和挂起的 tool_use id 去重 —— 单次工具宣告，随后在携带结果的更新上产生一条 tool_result。

旧的 `session-*.json` 整文件转录不再被监控；只识别 `.jsonl` 文件。

<a id="status-snapshot"></a>

### 状态快照

Gemini 支持 `/status` 快照：CCGram 解析近期转录活动，渲染当前会话的内联摘要（最后活动、挂起工具、tool_use 计数），无需等待下一次面板刷新。

<a id="pi"></a>

## Pi

[Pi](https://pi.dev) 是基于 Node.js 的 CLI，使用 JSONL v3 转录。配合 `cc-thingz` 的 `hook-runner` 扩展，Pi 向 `ccgram hook` 发出与 Claude 兼容的生命周期 hooks，实现即时的 `SessionStart`、`Stop`、`SessionEnd` 和子代理信号。没有 hook-runner 时，会话追踪回退为扫描 `~/.pi/agent/sessions/--<encoded-cwd>--/`，找出头部 `cwd` 与窗口工作目录匹配的最新转录。

<a id="launch"></a>

### 启动

默认命令为 `pi`。通过 `CCGRAM_PI_COMMAND` 覆盖以更改模型、参数或封装工具。

<a id="resume"></a>

### Resume

Resume 始终使用 `--session <path-or-uuid>`。Pi 的 `--resume` 参数会打开一个 ccgram 无法通过 `send_keys` 操作的交互式选择器，因此 ccgram 始终直接传入解析出的转录路径（或 UUID 前缀）。Continue 恢复按钮使用 Pi 自身的 `--continue`。

<a id="pi-transcript"></a>

### Pi 转录

Pi 转录是 `~/.pi/agent/sessions/--<encoded-cwd>--/<timestamp>_<uuid>.jsonl` 下的 JSONL 文件（v3 格式）。规范的会话 id 位于头部行（`{"type":"session","id":"<uuid>","cwd":"...","version":3}`）—— 文件名前缀只是时间戳。转录通过字节偏移量增量读取。

<a id="commands"></a>

### 命令

Pi 基于 [Pi 使用文档](https://pi.dev/docs/latest/usage)暴露一份 Telegram 安全的命令列表：`/changelog`、`/clone`、`/compact`、`/copy`、`/export`、`/fork`、`/hotkeys`、`/login`、`/logout`、`/model`、`/name`、`/new`、`/quit`、`/reload`、`/scoped_models`（原生为 `/scoped-models`）、`/session`、`/settings`、`/share` 和 `/tree`。`/followup <message>` 是 ccgram 面向 Pi 独有的桥接，对应 Pi 的 Alt+Enter 行为：它将消息排队到当前工作完成之后，而不是干预正在进行的轮次。`/clear` 作为 Pi `/new` 的隐藏兼容别名被接受，但不公开展示，因为 Pi 并未定义 `/clear`。Pi 的 `/resume` 不公开展示，因为它与 ccgram 机器人原生的会话选择器冲突。动态发现另外提供三类来源：

- **Skills** —— 位于 `~/.pi/agent/skills/<name>/`、`~/.agents/skills/<name>/`、`<project>/.pi/skills/<name>/` 或 `<project>/.agents/skills/<name>/` 下的 `SKILL.md`。`~/.pi/agent/skills/` 或 `<project>/.pi/skills/` 根目录下散落的 `.md` 文件也会被识别。
- **提示词模板** —— `~/.pi/agent/prompts/` 或 `<project>/.pi/prompts/` 下的 `.md` 文件（按项目遍历时，遇到第一个含 `.git` 的祖先目录即停止）。
- **扩展命令** —— `~/.pi/agent/extensions/` 或 `<project>/.pi/extensions/` 下的 TypeScript/JavaScript 文件（`.ts`、`.js`、`.mjs`、`.cjs`），扫描其中的 `pi.registerCommand("name", ...)` 调用。遍历器在下探前会剪除 `node_modules`、`dist`、`build` 和 `.git`。

命名冲突时按首个来源去重（skills > prompts > extensions）。

<a id="status-detection"></a>

### 状态检测

安装了 hook-runner 时，Pi 的 `Stop` hook 立即将话题标记为就绪。没有 hooks 时，状态从转录活动推断 —— 最新助手消息没有挂起工具调用时为空闲，存在未返回的工具调用时为工作中。

<a id="toolbar"></a>

### 工具栏

Pi 的默认工具栏省略了 Mode/Think/YOLO（pi 没有模式循环），并添加了专用导航行以操作 Pi 的 `/model` 和 `/session` 选择器：

- 第 1 行：`📷 Screen, ⏹ Ctrl-C, 📺 Live`
- 第 2 行：`⎋ Esc, ⇥ Tab, π Model`
- 第 3 行：`🔼 Up, ⏎ Enter, 🔽 Down, 📤 Send, ✖ Close`

可在 `~/.ccgram/toolbar.toml` 中用 `[providers.pi]` 块覆盖。

<a id="shell"></a>

## Shell

shell 提供方在 tmux 中打开一个普通的 shell 会话。它没有 hooks、没有转录，也不支持 resume/continue —— shell 会话是临时性的。

文本消息会经过 LLM 生成 shell 命令；加 `!` 前缀可发送原始命令。未配置 LLM 时，所有文本作为原始命令转发。

<a id="llm-configuration"></a>

### LLM 配置

配置 LLM 提供方以启用自然语言到 shell 命令的生成。

| 设置项     | 环境变量                 | 默认值             |
| ---------- | ------------------------ | ------------------ |
| LLM 提供方 | `CCGRAM_LLM_PROVIDER`    | _（空）_           |
| LLM API 密钥 | `CCGRAM_LLM_API_KEY`   | _（空）_           |
| LLM 基础 URL | `CCGRAM_LLM_BASE_URL`  | _（取自提供方）_   |
| LLM 模型   | `CCGRAM_LLM_MODEL`       | _（取自提供方）_   |
| LLM 温度   | `CCGRAM_LLM_TEMPERATURE` | `0.1`              |

API 密钥解析顺序：`CCGRAM_LLM_API_KEY` > 提供方专属环境变量（如 `XAI_API_KEY`）> `OPENAI_API_KEY`（通用回退）。

搭配廉价/快速模型时，将温度设为 `0` 可获得确定性输出。

<a id="supported-llm-providers"></a>

#### 支持的 LLM 提供方

**OpenAI**（默认模型：`gpt-5.4-nano`）：

```bash
CCGRAM_LLM_PROVIDER=openai
# Uses OPENAI_API_KEY by default — no extra key needed
```

**x.ai / Grok**（默认模型：`grok-3-fast`）：

```bash
CCGRAM_LLM_PROVIDER=xai
XAI_API_KEY=xai-...              # or set OPENAI_API_KEY as fallback
```

**DeepSeek**（默认模型：`deepseek-chat`）：

```bash
CCGRAM_LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...          # or set OPENAI_API_KEY as fallback
```

**Anthropic**（默认模型：`claude-sonnet-4-20250514`）：

```bash
CCGRAM_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...     # or set OPENAI_API_KEY as fallback
```

**Groq**（默认模型：`llama-3.3-70b-versatile`）：

```bash
CCGRAM_LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...             # or set OPENAI_API_KEY as fallback
```

**Ollama**（默认模型：`llama3.1`，无需 API 密钥）：

```bash
CCGRAM_LLM_PROVIDER=ollama
CCGRAM_LLM_BASE_URL=http://localhost:11434/v1
```

<a id="command-generation-flow"></a>

### 命令生成流程

1. 发送一条描述需求的文本消息（例如"列出所有 Python 文件"）
2. LLM 生成一条 shell 命令（例如 `find . -name "*.py"`）
3. 出现审批键盘：**▶ Run** | **✏ Edit** | **✕ Cancel**
4. 点击 **Run** 执行，**Edit** 复制修改，或 **Cancel** 丢弃
5. 危险命令（`rm -rf`、`dd` 等）会额外显示一步确认

<a id="raw-commands"></a>

### 原始命令

加 `!` 前缀可绕过 LLM，直接发送到 shell：

- `!ls -la` → 直接发送 `ls -la`
- `! git status` → 发送 `git status`（去除开头空格）

<a id="voice-messages"></a>

### 语音消息

shell 话题中的语音消息会自动经过 Whisper 转写 → LLM 命令生成 → 审批键盘的流程。

<a id="shell-status"></a>

### Shell 状态

- 提示符空闲时："🐚 Shell ready"（标准状态下为 "✓ Ready"）
- `/history` 不可用（无转录）
- 不支持 Resume 和 Continue（shell 会话是临时性的）
