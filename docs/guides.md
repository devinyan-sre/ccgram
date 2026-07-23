> [English](en/guides.md) | 中文

<a id="guides"></a>

# 使用指南

<a id="upgrading"></a>

## 升级

```bash
uv tool upgrade ccgram                # uv (recommended)
pipx upgrade ccgram                   # pipx
brew upgrade ccgram                   # Homebrew
```

<a id="cli-reference"></a>

## CLI 参考

```
ccgram                        # Start the bot
ccgram status                 # Show running state (no token needed)
ccgram doctor                 # Validate setup and diagnose issues
ccgram doctor --fix           # Auto-fix issues (install hook, kill orphans)
ccgram hook --install         # Install Claude Code hooks
ccgram hook --uninstall       # Remove all hooks
ccgram hook --status          # Check per-event hook installation status
ccgram --version              # Show version
ccgram -v                     # Run with debug logging
```

<a id="getting-started"></a>

## 快速上手

<a id="botfather-setup"></a>

### BotFather 配置

运行 CCGram 需要一个 Telegram 机器人 token，可通过 [@BotFather](https://t.me/BotFather) 创建。

1. **打开 [@BotFather](https://t.me/BotFather)**，发送 `/start`
2. **创建新机器人：** 发送 `/newbot` 并按提示操作
   - 名称：任意（例如 "MyCodeBot"）
   - 用户名：必须唯一且以 `bot` 结尾（例如 "my_code_bot"）
   - 你会收到一个 **Bot Token** —— 保存下来，用于 `TELEGRAM_BOT_TOKEN`
3. **配置机器人设置：** 发送 `/mybots` → 选择你的机器人 → **Bot Settings**
   - 开启 **Allow Groups**: On
   - 设置 **Group Privacy**: Off _（必须关闭，机器人才能看到话题中的所有消息）_
   - 开启 **Topics**: On
4. **将机器人加入你的 Telegram 群组：**
   - 创建或打开一个启用了话题（Topics）的 Telegram 群组
   - 邀请机器人进群
   - **将机器人提升为管理员**，并授予以下权限：
     - 创建话题（Create Topics）
     - 置顶消息（Pin Messages）
     - 读取消息 / 查看聊天（Read Messages / View The Chat）
5. **获取你的用户 ID：** 打开 [@userinfobot](https://t.me/userinfobot)，它会显示你的数字用户 ID。保存下来，用于 `ALLOWED_USERS`
6. **获取群组 ID：** 在群里打开 [@RawDataBot](https://t.me/RawDataBot)，在 **Peer ID** 下记录该数字（去掉前缀 `-100` 或保留均可，两种格式都支持）
   - 保存下来，用于 `CCGRAM_GROUP_ID`（如有需要请加上 `-100` 前缀）
7. **创建 `~/.ccgram/.env`：**

   ```ini
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   ALLOWED_USERS=your_user_id_here
   CCGRAM_GROUP_ID=your_group_id_here
   ```

8. **测试：** 运行 `ccgram`，在 Telegram 群组中创建一个新话题并发送一条消息，此时应弹出目录浏览器。

<a id="validation"></a>

### 校验

可随时运行 `ccgram doctor` 检查你的配置：

```bash
ccgram doctor         # Check configuration, hooks, multiplexer, agent CLIs
ccgram doctor --fix   # Auto-fix common issues (install hooks, kill orphans, etc.)
```

<a id="local-dev-in-tmux"></a>

## 在 tmux 中进行本地开发

推荐的本地开发模式：

- 在专用控制窗口 `ccgram:__main__` 中运行 ccgram。
- 将代理窗口放在同一个 `ccgram` tmux 会话中。
- 通过向控制面板发送 Ctrl-C 来重启。

使用辅助脚本：

```bash
./scripts/restart.sh start      # fresh start; creates ccgram:__main__ if missing and installs Claude hooks
./scripts/restart.sh status     # show current command + last logs
./scripts/restart.sh restart    # sends Ctrl-C to control pane (supervisor restarts)
./scripts/restart.sh stop       # sends Ctrl-\ to control pane (supervisor exits)
```

在控制面板（`ccgram:__main__`）中直接按键的行为：

- `Ctrl-C`：重启 ccgram。
- `Ctrl-\`：停止本地开发 supervisor 循环。

<a id="fresh-start-guide"></a>

### 从零开始

如果你是从零开始：

1. `cd /path/to/ccgram`
2. `./scripts/restart.sh start`
3. `tmux attach -t ccgram`
4. 在另一个终端（或另一个面板）中，在同一 tmux 会话内打开你的代理窗口。

`start` 命令会在 tmux 会话/窗口不存在时自动创建，安装或更新 Claude hooks，然后启动 supervisor。无需手动初始化 tmux。

<a id="testing"></a>

## 测试

CCGram 有三个测试层级：

| 层级 | 命令                    | 耗时     | 依赖              |
| ---- | ----------------------- | -------- | ----------------- |
| 单元 | `make test`             | ~10s     | 无（全部 mock）   |
| 集成 | `make test-integration` | ~7s      | tmux              |
| E2E  | `make test-e2e`         | ~3-4 min | tmux + 代理 CLI   |

`make check` 会一并运行单元测试、集成测试，以及格式化、lint 和类型检查。

<a id="e2e-tests"></a>

### E2E 测试

端到端测试覆盖完整生命周期：注入伪造的 Telegram 更新 → 真实的 PTB 应用 → 真实的 tmux 窗口 → 真实的代理 CLI 进程 → 拦截 Bot API 响应。如果某个提供方的 CLI 未安装，其测试会自动跳过。

**前置条件：**

- 已安装 tmux 且在 PATH 中
- 已安装并完成认证的一个或多个代理 CLI：`claude`、`codex`、`gemini`、`pi`

**各提供方的测试覆盖：**

| 提供方 | 测试数 | 场景                                                                                                                      |
| ------ | ------ | ------------------------------------------------------------------------------------------------------------------------- |
| Claude | 9      | 生命周期、`/sessions`、`/screenshot`、`/help` 转发、恢复（新建 + 继续）、状态切换、多话题隔离、通知模式循环切换            |
| Codex  | 3      | 生命周期、命令转发、恢复                                                                                                   |
| Gemini | 3      | 生命周期、命令转发、恢复                                                                                                   |
| Pi     | —      | 仅有单元 + 契约测试覆盖；暂无 e2e 生命周期套件                                                                             |

**工作原理：** Bot API 的 HTTP 层被 mock —— 通过 `app.process_update()` 注入伪造的 `Update` 对象，并拦截记录所有出站 API 调用以供断言。测试驱动完整的话题绑定流程（目录浏览器 → 可选的 worktree 选择器 → 提供方选择器 → 模式选择 → 窗口创建），并验证代理进程启动、消息被转发、响应被送达。

**运行方式：**

```bash
make test-e2e                                         # All providers
uv run pytest tests/e2e/test_claude_lifecycle.py -v   # Claude only
uv run pytest tests/e2e/test_codex_lifecycle.py -v    # Codex only
uv run pytest tests/e2e/test_gemini_lifecycle.py -v   # Gemini only
# Pi: covered by unit + contract tests in tests/ccgram/providers/test_pi.py
```

测试会创建一个隔离的 `ccgram-e2e` tmux 会话，不会干扰正在运行的 `ccgram` 实例。可以安全地在 tmux 窗口内运行。

<a id="configuration"></a>

## 配置

所有设置同时支持 CLI 参数和环境变量，CLI 参数优先。`TELEGRAM_BOT_TOKEN` 出于安全考虑仅支持环境变量（CLI 参数在 `ps` 中可见）。

| 变量 / 参数                                          | 默认值                         | 说明                                                                                                 |
| ---------------------------------------------------- | ------------------------------ | ---------------------------------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`                                 | _（必填）_                     | 来自 @BotFather 的机器人 token（仅环境变量）                                                         |
| `ALLOWED_USERS` / `--allowed-users`                  | _（必填）_                     | 逗号分隔的 Telegram 用户 ID                                                                          |
| `CCGRAM_DIR` / `--config-dir`                        | `~/.ccgram`                    | 配置与状态目录                                                                                       |
| `CLAUDE_CONFIG_DIR` / `--claude-config-dir`          | `~/.claude`                    | 覆盖 Claude 配置目录（用于 ce、cc-mirror 等封装工具）                                                |
| `TMUX_SESSION_NAME` / `--tmux-session`               | `ccgram`                       | tmux 会话名                                                                                          |
| `CCGRAM_MULTIPLEXER`                                 | `tmux`                         | 终端复用器后端：`tmux`（默认）或 `herdr`                                                             |
| `CCGRAM_PROVIDER` / `--provider`                     | `claude`                       | 默认代理提供方（`claude`、`codex`、`gemini`、`pi`、`shell`）                                         |
| `CCGRAM_<NAME>_COMMAND`                              | _（取自提供方）_               | 各提供方的启动命令（仅环境变量，见下文）                                                             |
| `CCGRAM_GROUP_ID` / `--group-id`                     | _（所有群组）_                 | 限定到某一个 Telegram 群组                                                                           |
| `CCGRAM_INSTANCE_NAME` / `--instance-name`           | 主机名                         | 本实例的显示名称                                                                                     |
| `CCGRAM_LOG_LEVEL` / `--log-level`                   | `INFO`                         | 日志级别（DEBUG、INFO、WARNING、ERROR）                                                              |
| `MONITOR_POLL_INTERVAL` / `--monitor-interval`       | `2.0`                          | 转录文件轮询间隔（秒）                                                                               |
| `CCGRAM_FS_EVENTS`                                   | `1`                            | 文件系统事件唤醒（inotify）：转录/事件文件一有写入立即处理,轮询间隔仅作兜底;设 `0` 禁用             |
| `AUTOCLOSE_DONE_MINUTES` / `--autoclose-done`        | `30`                           | 已完成话题 N 分钟后自动关闭（0=关闭该功能）                                                          |
| `AUTOCLOSE_DEAD_MINUTES` / `--autoclose-dead`        | `10`                           | 已死亡会话 N 分钟后自动关闭（0=关闭该功能）                                                          |
| `CCGRAM_WHISPER_PROVIDER` / `--whisper-provider`     | _（空）_                       | Whisper 提供方：`openai`、`groq`，留空则禁用                                                         |
| `CCGRAM_WHISPER_API_KEY`                             | _（空）_                       | API 密钥（仅环境变量）；回退到 OPENAI_API_KEY/GROQ_API_KEY                                           |
| `CCGRAM_WHISPER_BASE_URL` / `--whisper-base-url`     | _（提供方默认值）_             | 自定义 OpenAI 兼容端点 URL                                                                           |
| `CCGRAM_WHISPER_MODEL` / `--whisper-model`           | _（提供方默认值）_             | 模型覆盖（例如 `whisper-large-v3-turbo`）                                                            |
| `CCGRAM_WHISPER_LANGUAGE` / `--whisper-language`     | _（自动检测）_                 | 强制指定语言代码（例如 `en`、`zh`）                                                                  |
| `CCGRAM_LLM_PROVIDER`                                | _（空 = 禁用）_                | 用于 shell 命令生成的 LLM 提供方                                                                     |
| `CCGRAM_LLM_API_KEY`                                 | _（空）_                       | LLM 提供方的 API 密钥（仅环境变量）                                                                  |
| `CCGRAM_LLM_BASE_URL`                                | _（取自提供方）_               | 自定义 LLM API 端点                                                                                  |
| `CCGRAM_LLM_MODEL`                                   | _（取自提供方）_               | LLM 模型覆盖                                                                                         |
| `CCGRAM_LLM_TEMPERATURE`                             | `0.1`                          | LLM 采样温度（0 = 确定性输出）                                                                       |
| `CCGRAM_LIVE_VIEW_INTERVAL` / `--live-view-interval` | `5`                            | 实时视图刷新间隔（秒，最小 1）                                                                       |
| `CCGRAM_LIVE_VIEW_TIMEOUT` / `--live-view-timeout`   | `300`                          | 实时视图自动停止超时（秒，最小 1）                                                                   |
| `CCGRAM_STATUS_MODE` / `--status-mode`               | `system`                       | 话题表情配色方案：`system`（绿=工作中）或 `user`（绿=就绪）                                          |
| `CCGRAM_HIDE_TOOL_CALLS` / `--hide-tool-calls`       | `false`                        | 设为 `true` 全局隐藏 `tool_use`/`tool_result` 消息（每窗口可用 `/toolcalls` 覆盖）                   |
| `CCGRAM_PROMPT_MODE` / `--prompt-mode`               | `wrap`                         | Shell 提示符标记：`wrap`（追加 `⌘N⌘`）或 `replace`（旧式 `{prefix}:N❯`）                             |
| `CCGRAM_PROMPT_MARKER`                               | `ccgram`                       | 仅 `replace` 模式使用的标记前缀                                                                      |
| `CCGRAM_PANE_LIFECYCLE_NOTIFY`                       | `false`                        | 每窗口面板创建/关闭通知的默认值（可用 `/panes` 切换）                                                |
| `CCGRAM_SHOW_HIDDEN_DIRS` / `--show-hidden-dirs`     | `false`                        | 在目录浏览器中显示点开头的目录                                                                       |
| `CCGRAM_SEND_SEARCH_DEPTH`                           | `5`                            | `/send` 文件搜索的最大目录深度                                                                       |
| `CCGRAM_SEND_MAX_RESULTS`                            | `50`                           | `/send` 搜索返回的最大文件数                                                                         |
| `CCGRAM_TOOLBAR_CONFIG`                              | `~/.ccgram/toolbar.toml`       | 自定义工具栏 TOML 的路径；文件不存在时回退到内置默认值                                               |
| `CCGRAM_STATUS_POLL_INTERVAL`                        | `1.0`                          | 状态轮询间隔（秒，最小 0.5）                                                                         |
| `CCGRAM_MINIAPP_BASE_URL`                            | _（禁用）_                     | Mini App 仪表盘的外部可达 HTTPS URL                                                                  |
| `CCGRAM_MINIAPP_HOST`                                | `127.0.0.1`                    | Mini App aiohttp 服务的本地绑定主机                                                                  |
| `CCGRAM_MINIAPP_PORT`                                | `8765`                         | Mini App aiohttp 服务的本地绑定端口                                                                  |
| `CCGRAM_LANG`                                        | `en`                           | 机器人界面语言;设为 `zh` 切换为简体中文                                                             |
| `CCGRAM_QUIET_HOURS`                                 | _(关闭)_                       | 免打扰时段 `HH:MM-HH:MM`(服务器本地时间,支持跨午夜);时段内自动消息静默送达                        |
| `CCGRAM_DAILY_DIGEST`                                | _(关闭)_                       | 每日摘要时间 `HH:MM`(服务器本地时间);向 General 话题发送各话题过去 24 小时的活动汇总               |
| `CCGRAM_TTS_PROVIDER`                                | _（禁用）_                     | 语音回复的 TTS 后端：`edge`（免费）或 `openai`                                                       |
| `CCGRAM_TTS_VOICE`                                   | `en-US-EmmaMultilingualNeural` | 语音名称                                                                                             |
| `CCGRAM_TTS_MODEL`                                   | `gpt-4o-mini-tts`              | OpenAI TTS 模型（仅当 `CCGRAM_TTS_PROVIDER=openai` 时使用）                                          |
| `CCGRAM_TTS_API_KEY`                                 | _（空）_                       | OpenAI TTS 的 API 密钥；回退到 `OPENAI_API_KEY`                                                      |

<a id="topic-emoji-color-scheme"></a>

## 话题表情配色方案

话题表情会随代理状态变换颜色。颜色与含义的映射可配置：

| 模式             | 🟢 绿色              | 🟡 黄色      | 适用场景                     |
| ---------------- | -------------------- | ------------ | ---------------------------- |
| `system`（默认） | 代理正在工作         | 代理空闲     | "现在有什么在运行吗？"       |
| `user`           | 代理空闲 / 等待输入  | 代理正在工作 | "有什么需要我处理的吗？"     |

通过 `CCGRAM_STATUS_MODE=user` 或 `--status-mode user` 全局设置。无效值回退到 `system`。

<a id="tool-call-visibility"></a>

## 工具调用可见性

默认情况下，来自 Claude/Codex/Gemini 的 `tool_use` 和 `tool_result` 事件会转发到 Telegram。当它们噪音大于信号时（例如大量文件操作或 grep 工作），可以全局或按窗口屏蔽。

- **全局**：`CCGRAM_HIDE_TOOL_CALLS=true` 或 `--hide-tool-calls` 将全局默认值设为隐藏。
- **按窗口**：在话题中使用 `/toolcalls` 循环切换 `default → shown → hidden`。窗口级设置始终优先于全局默认值。

Hook 事件（Stop、StopFailure、SubagentStart/Stop、TaskCompleted、TeammateIdle）**永不**被屏蔽 —— 它们绕过该开关，确保重要信息不会丢失。

<a id="voice-message-transcription"></a>

## 语音消息转写

在 Telegram 中发送语音消息，自动转写后转发给代理。

<a id="setup"></a>

### 配置

设置 whisper 提供方和 API 密钥：

```ini
# Groq (fast, generous free tier)
CCGRAM_WHISPER_PROVIDER=groq
GROQ_API_KEY=gsk_xxxxxxxx

# Or OpenAI
CCGRAM_WHISPER_PROVIDER=openai
OPENAI_API_KEY=sk-xxxxxxxx

# Or any OpenAI-compatible endpoint
CCGRAM_WHISPER_PROVIDER=openai
CCGRAM_WHISPER_API_KEY=your_key
CCGRAM_WHISPER_BASE_URL=http://localhost:8000/v1
```

可选覆盖项：

```ini
CCGRAM_WHISPER_MODEL=whisper-large-v3-turbo   # default depends on provider
CCGRAM_WHISPER_LANGUAGE=en                     # omit for auto-detect
```

<a id="how-it-works"></a>

### 工作原理

1. 在绑定了代理的话题中发送语音消息
2. 机器人下载音频（最大 25 MB）并发送给 Whisper API
3. 转写结果显示，并附带 **✓ 发送给代理** 和 **✗ 丢弃** 按钮
4. 点击 **发送** 将文本转发给代理，或点击 **丢弃** 取消

在 shell 话题中，语音转写会自动经过 LLM 生成命令（需设置 `CCGRAM_LLM_PROVIDER`）。在代理话题中，转写文本直接发送给代理。

将 `CCGRAM_WHISPER_PROVIDER` 留空（默认）即可禁用语音转写。

<a id="tmux-session-auto-detection"></a>

## Tmux 会话自动检测

> 本节适用于 `CCGRAM_MULTIPLEXER=tmux`（默认）。herdr 后端使用自己的工作区/标签页模型，不使用 tmux 会话名。

当 ccgram 在已有的 tmux 会话内启动时，它会自动检测会话名并附加到该会话，而不是新建一个 `ccgram` 会话。当你已经有一个包含代理窗口的 tmux 会话时，这很有用。

**工作原理：**

1. 如果设置了 `$TMUX` 且未传 `--tmux-session` 参数，ccgram 检测当前会话名
2. 机器人自身所在的 tmux 窗口自动从窗口列表中排除
3. 如果同一会话中已有另一个 ccgram 实例在运行，则拒绝启动

**覆盖：** `--tmux-session=NAME` 或 `TMUX_SESSION_NAME=NAME` 始终优先于自动检测。

**在 tmux 之外：** 行为不变 —— ccgram 创建一个带 `__main__` 占位窗口的 `ccgram` 会话。

| 场景                              | 行为                                             |
| --------------------------------- | ------------------------------------------------ |
| tmux 之外，无参数                 | 创建 `ccgram` 会话 + `__main__` 窗口             |
| tmux 之外，`--tmux-session=X`     | 创建/附加 `X` + `__main__` 窗口                  |
| tmux 之内，无参数                 | 自动检测会话，跳过自身窗口，不新建               |
| tmux 之内，`--tmux-session=X`     | 覆盖自动检测，使用 `X`                           |

<a id="herdr-backend-alternative-multiplexer"></a>

## Herdr 后端（可选复用器）

ccgram 通过一个后端中立的接缝与终端复用器通信。tmux 是默认后端；[herdr](https://github.com/ogulcancelik/herdr) 是可选替代方案，通过 `CCGRAM_MULTIPLEXER=herdr` 启用。其他一切 —— 话题、提供方、hooks、状态、恢复 —— 行为完全相同；只有底层复用器变了。

<a id="setup-1"></a>

### 配置

1. **安装 herdr**，并确保 `herdr` 二进制在 `PATH` 中。启动其服务端，使控制套接字存在。
2. **选择后端：** 设置 `CCGRAM_MULTIPLEXER=herdr`（环境变量或 `.env`）。默认值为 `tmux`。
3. **套接字路径（可选）：** ccgram 读取 `$HERDR_SOCKET_PATH` 来定位服务端。留空则使用 herdr 的默认套接字；也可以设置它以指向特定服务端。
4. **照常安装 ccgram hook：** `ccgram hook --install`。同一个 Claude Code hook 在两种后端上都能工作 —— 它通过 `$HERDR_PANE_ID`（tmux 使用 `$TMUX_PANE`）解析触发窗口，因此无需针对 herdr 的额外 hook 步骤。
5. **验证：** `ccgram doctor`。当 `CCGRAM_MULTIPLEXER=herdr` 时，doctor 检查 `herdr` 二进制、套接字可达性、固定的协议版本，以及 ccgram 与 herdr 各自的 Claude hooks 在 `settings.json` 中是否共存（代替 tmux 相关检查）。

```bash
# .env or shell environment
CCGRAM_MULTIPLEXER=herdr
# HERDR_SOCKET_PATH=/path/to/herdr.sock   # optional; defaults to herdr's socket
```

<a id="protocol-version-pinning"></a>

### 协议版本固定

ccgram 接受 herdr 套接字协议 14、15、16 且不产生警告。首次调用时它会读取 `herdr status`；协议过旧、过新、缺失或未知时会发出警告并以尽力而为模式继续运行，因此在 herdr 升级或降级后，基于 CLI 的操作仍可能正常工作。服务端未启动、status 命令失败或 status 响应格式错误仍会阻止启动。在依赖未经测试的协议之前，请先运行 herdr 在线契约测试套件。

<a id="differences-from-tmux"></a>

### 与 tmux 的差异

herdr 通过接缝声明自己的能力；用户可感知的行为差异如下：

| 方面                 | tmux                     | herdr                                                                   |
| -------------------- | ------------------------ | ----------------------------------------------------------------------- |
| 话题 = 窗口          | 每个窗口都符合条件       | 只有**代理标签页**会成为话题 —— 纯 shell 标签页不会                     |
| 前台进程检测         | `ps -t <tty>`            | `pane process-info`（无 tty）                                           |
| 回滚缓冲区捕获       | 无上限                   | 限制为 **1000 行**；更长的输出会被标记为已截断                          |
| 代理状态             | 从终端抓取推断           | 原生（herdr 直接报告代理状态）                                          |
| 重启后的窗口 ID      | 稳定                     | herdr **服务端**重启后重新分配 —— ccgram 通过会话 ID 重新解析           |
| 话题标签             | 窗口名                   | 自适应 `"<workspace> ▸ <tab>"`（以标签页名为主）                        |

在 herdr 上从终端创建会话的方式见 [从终端创建会话](#creating-sessions-from-the-terminal)。

> **工作区选择器：** 在 herdr 上，`/new` 会在目录选择后多显示一步 —— 工作区选择器，让你把新标签页固定到已有的 herdr 工作区内。如果尚无任何工作区（或没有与所选目录匹配的），则跳过该选择器，ccgram 自动创建一个新工作区。
>
> **自托管逃生口：** 标签匹配 `__*__`（如 `__main__`）的工作区或标签页对 ccgram 不可见。利用这一命名约定，可以在 herdr 内运行 ccgram 本身，而不会让它把自己的终端自动收养为话题。

<a id="auto-close-behavior"></a>

## 自动关闭行为

CCGram 会在会话结束时自动关闭 Telegram 话题，减少杂乱：

- **已完成的话题**（`--autoclose-done`，默认：30 分钟）—— 当 Claude 完成任务且会话正常结束时，话题在 30 分钟后自动关闭。
- **已死亡的会话**（`--autoclose-dead`，默认：10 分钟）—— 当 Claude 进程崩溃或 tmux 窗口被外部杀掉时，话题在 10 分钟后自动关闭。

设为 `0` 可禁用：

```bash
ccgram --autoclose-done 0 --autoclose-dead 0
```

<a id="multi-instance-setup"></a>

## 多实例部署

在同一台机器上运行多个 ccgram 实例，每个实例负责一个不同的 Telegram 群组。所有实例可共用同一个机器人 token。

<a id="example-work--personal-instances"></a>

### 示例：工作 + 个人双实例

实例 1（`~/.ccgram-work/.env`）：

```ini
TELEGRAM_BOT_TOKEN=same_token_for_both
ALLOWED_USERS=123456789
CCGRAM_GROUP_ID=-1001111111111
CCGRAM_INSTANCE_NAME=work
CCGRAM_DIR=~/.ccgram-work
TMUX_SESSION_NAME=ccgram-work
```

实例 2（`~/.ccgram-personal/.env`）：

```ini
TELEGRAM_BOT_TOKEN=same_token_for_both
ALLOWED_USERS=123456789
CCGRAM_GROUP_ID=-1002222222222
CCGRAM_INSTANCE_NAME=personal
CCGRAM_DIR=~/.ccgram-personal
TMUX_SESSION_NAME=ccgram-personal
```

同时运行两个实例：

```bash
CCGRAM_DIR=~/.ccgram-work ccgram &
CCGRAM_DIR=~/.ccgram-personal ccgram &
```

每个实例使用独立的 tmux 会话、配置目录和状态。设置了 `CCGRAM_GROUP_ID` 时，实例会静默忽略来自其他群组的更新。

不设置 `CCGRAM_GROUP_ID` 时，单个实例处理所有群组（默认行为）。

> 要查找群组的 chat ID，把 [@RawDataBot](https://t.me/RawDataBot) 加入群组 —— 它会回复 chat ID（一个负数，形如 `-1001234567890`）。

<a id="creating-sessions-from-the-terminal"></a>

## 从终端创建会话

除了通过 Telegram 话题创建会话，你也可以直接在终端复用器中创建窗口。

<a id="tmux-default"></a>

### tmux（默认）

```bash
# Attach to the ccgram tmux session
tmux attach -t ccgram

# Create a new window for your project
tmux new-window -n myproject -c ~/Code/myproject

# Start any supported agent CLI
claude     # or: codex, gemini, pi
```

窗口必须位于 ccgram 的 tmux 会话中（可通过 `TMUX_SESSION_NAME` 配置）。

<a id="herdr-ccgram_multiplexerherdr"></a>

### herdr（`CCGRAM_MULTIPLEXER=herdr`）

在相应的工作区中打开一个新的 herdr 标签页，然后启动任意受支持的代理 CLI。CCGram 会自动发现代理面板；纯 shell 面板不会成为话题（只有活跃的代理面板才会）。

<a id="both-backends"></a>

### 两种后端通用

对于 Claude，SessionStart hook 会自动注册会话。对于 Codex、Gemini 和 Pi，CCGram 从运行中的进程名自动检测提供方，并从磁盘上的转录文件发现会话。所有情况下，机器人都会创建对应的 Telegram 话题。

即使是没有任何话题绑定的全新实例（冷启动），此机制同样有效。

<a id="session-recovery"></a>

## 会话恢复

当代理会话退出或崩溃时，机器人会检测到死亡窗口，并通过内联按钮提供恢复选项：

- **Fresh（新建）** —— 杀掉旧窗口，在同一目录中新建一个
- **Continue（继续）** —— 恢复上一次对话（所有提供方均支持）
- **Resume（选择恢复）** —— 浏览并选择一个历史会话来恢复

显示的按钮会根据各提供方的能力自适应。Claude、Codex、Gemini 和 Pi 支持 Fresh、Continue 和 Resume。Shell 仅支持 Fresh（shell 会话是临时性的）。

<a id="manual-provider-override-agent"></a>

## 手动指定提供方（`/agent`）

`/agent`（别名 `/provider`）用于修正被错误标记的窗口。自动检测（`detect_provider_from_command` + 经复用器接缝的 JS 运行时前台进程回退）对 `ralphex` 之类的自定义封装工具返回空值，导致窗口保留旧的提供方标记 —— 于是 SessionMonitor 轮询过期的转录文件，`/last` 返回旧文本，工具调用和回复不再出现。

用法：

```
/agent              # show picker (current marked ✓, with (manual override) badge if set)
/agent shell        # switch to shell
/agent claude       # switch to Claude (also: codex, gemini, pi)
/agent auto         # clear manual override and re-run auto-detection
```

切换时，机器人会清空 `WindowState.transcript_path`，删除旧的 `session_map.json` 条目（让 SessionMonitor 停止读取错误的转录文件），并在切换到 shell 时通过 `shell_prompt_orchestrator.ensure_setup` 触发提示符标记设置。新提供方的下一个 `SessionStart` hook 会重新填充 `session_map`。

手动覆盖会设置 `WindowState.provider_manual_override=True`。`_detect_and_apply_provider` 中的周期性自动检测会跳过被覆盖的窗口，直到 `/agent auto` 清除该标志。

<a id="live-view"></a>

## 实时视图

通过 Telegram 中自动刷新的截图实时监控代理终端输出。

<a id="how-it-works-1"></a>

### 工作原理

1. 点击操作工具栏中的 **Live** 按钮（或 `/toolbar` → Live）
2. CCGram 将终端截为 PNG 并作为图片发送
3. 每 5 秒（可配置）重新截图并原位编辑该图片
4. 内容哈希门控：屏幕内容未变化时不产生 API 调用
5. 5 分钟后自动停止（可配置），或点击 **Stop** 停止

<a id="configuration-1"></a>

### 配置

| 设置项       | 环境变量                    | 默认值        |
| ------------ | --------------------------- | ------------- |
| 刷新间隔     | `CCGRAM_LIVE_VIEW_INTERVAL` | `5`（秒）     |
| 自动停止超时 | `CCGRAM_LIVE_VIEW_TIMEOUT`  | `300`（秒）   |

两个值的下限均为 1 秒。

<a id="screenshots"></a>

## 截图

`/screenshot`（或状态栏的 📷 按钮）将绑定的 tmux 面板当前视口截为一张带 ANSI 颜色、清晰可读的 PNG。

实时视图（自动刷新）使用同样的视口捕获，但字号更小以降低文件体积。

<a id="last-reply-last"></a>

## 最近回复（`/last`）

`/last`（或工具栏的 📄 **Last** 按钮）将最近一条助手回复重新发送到当前话题：

- **AI 提供方**（Claude、Codex、Gemini、Pi）—— 从会话转录中提取最后一条用户消息之后的连续助手文本块。找不到轮次边界时回退到最近的助手文本。
- **Shell** —— 捕获回滚缓冲区，提取提示符标记之间的最后一个命令+输出块。

超过 4096 字符的回复将以 `.txt` 文档附件形式发送，而不是文本消息。


<a id="git-diff-diff"></a>

## 查看改动(`/diff`)

`/diff` 把当前话题绑定窗口目录的未提交 git 改动发送到话题:

- 内联显示 `git status --short` + diffstat 摘要
- 完整 diff 较短时内联(```diff 代码块),超长时作为 `.diff` 文件发送
- 可选路径参数缩小范围:`/diff src/foo.py`
- 非 git 目录或工作区干净时给出友好提示

适合在手机上 review agent 刚改完的代码,再决定下一步指令。

<a id="token-usage-usage"></a>

## Token 用量(`/usage`)

`/usage` 解析当前会话的 transcript,统计 token 消耗:

- 输入 / 输出 / 缓存读取 / 缓存写入 tokens 与总计
- 用户 / 助手轮次数、使用的模型
- 仅 Claude Code transcript 携带用量数据;其他 provider 会得到友好提示

<a id="transcript-search-search"></a>

## 跨会话搜索(`/search`)

`/search <关键词>` 在所有 Claude 会话历史(`~/.claude/projects/`)中全文检索:

- 匹配用户与助手消息文本(不匹配工具调用内部数据),大小写不敏感
- 结果按时间倒序,每条附项目目录、时间、角色、会话 ID 与上下文片段
- 全局命令,任意位置可用,无需绑定话题
- 内置护栏:最多 10 条结果 / 300 个文件 / 8 秒扫描预算,超出时提示缩小范围

用于找回"上次在哪个会话里讨论过 X"之类的历史上下文,可配合 `/resume` 恢复对应会话。

<a id="file-delivery-send"></a>

## 文件发送（`/send`）

将绑定窗口工作目录中的文件发送到 Telegram。一条命令三种模式：

```bash
/send docs/arch.png   # exact path → immediate upload
/send *.png           # glob → pick if multiple
/send arch            # substring search → pick if multiple
/send                 # no args → interactive directory browser at CWD
```

安全策略（项目范围限定，默认拒绝）：

- 解析后的路径必须位于窗口 CWD 之内（阻止 `../` 穿越和符号链接逃逸）
- 隐藏文件/目录（`.` 开头）拒绝
- 秘密文件模式拒绝：`*.pem`、`*.key`、`*.p12`、`*credential*`、`*secret*`、`.env` 等
- 如果存在 `.gitleaks.toml`，会强制执行其 `[[rules]]` 中的路径正则
- gitignore 忽略的文件拒绝（优先 `git check-ignore`，非 git 目录回退到 `pathspec`）
- 50 MB 上限（Telegram bot API 限制）
- 排除目录永不显示：`node_modules`、`__pycache__`、`.venv`、`dist`、`build` 等

可调参数：`CCGRAM_SEND_SEARCH_DEPTH`（默认 5）、`CCGRAM_SEND_MAX_RESULTS`（默认 50）。

<a id="action-toolbar-toolbar"></a>

## 操作工具栏（`/toolbar`）

`/toolbar` 打开一个按提供方定制的 tmux 按键操作内联键盘。第 1 行通用：`[📷 Screen, ⏹ Ctrl-C, 📺 Live]`。第 2 行因提供方而异：Claude（Mode、Think、Esc）、Codex（Esc、Tab、Mode）、Gemini（Mode、YOLO、Esc）、Pi（Esc、Tab、π Model）、Shell（Enter、EOF、Suspend）。Claude/Codex/Gemini/Pi 额外有一个导航行（Up、Enter、Down）。最后一行是 `[📄 Last, Get File, Close]`；Shell 把 Esc 并入其中：`[📄 Last, Get File, Esc, Close]`。

切换类操作（Mode = Shift+Tab、Think = Tab、YOLO = Ctrl+Y）会在按键后约 250 ms 捕获面板内容，并在 toast 中报告结果模式行（例如 `auto-accept edits on`）。

<a id="custom-toolbar"></a>

### 自定义工具栏

将 TOML 文件放在 `~/.ccgram/toolbar.toml`（或设置 `CCGRAM_TOOLBAR_CONFIG=/path/to/file`）。完整注释示例见 `docs/examples/toolbar.toml`。格式如下：

```toml
[actions.clear]                # define a custom action
emoji = "🧹"
text  = "Clear"
type  = "text"
payload = "/clear"

[providers.claude]             # override Claude's default grid
style = "emoji_text"           # emoji | text | emoji_text
buttons = [
  ["screen", "ctrlc", "live"],
  ["mode",   "think", "clear"],
  ["send",   "enter", "close"],
]
```

操作类型：

- `key` —— 发送 tmux 按键序列（`"Tab"`、`"C-c"`、`'\x1b[Z'`）。原始字节序列需设置 `literal=true`（TOML 字面字符串 —— 单引号）。
- `text` —— 发送字面文本 + 回车（例如 `"/clear"`、提示词模板）。
- `builtin` —— 保留类型（`screen`、`ctrlc`、`live`、`getfile`、`last`、`close`）。用户不能定义新的 builtin。

操作名不得超过 24 个字符（callback_data 预算限制）。TOML 中未出现的提供方保持内置默认值。格式错误的条目会被记录日志并跳过 —— 加载器永不抛出异常。

<a id="picker-hints"></a>

### 选择器提示

当你转发的斜杠命令会在 TUI 内打开模态选择器时（例如 Claude 的 `/model`、`/login`、`/theme`；Codex/Gemini 的 `/model`；Pi 的 `/model`），话题回复会附带一条提示，指向 `/toolbar`，以便用方向键操作选择器。提示会适配你的工具栏 —— 如果你删除了 Up/Down/Enter/Esc 按键，提示会退化为"打开 /toolbar 操作选择器"。

<a id="git-worktree-topics"></a>

## Git Worktree 话题

当你创建新话题并选择的目录是一个**符合条件的 git 仓库**（位于工作树内、非 bare、处于具名分支、无进行中的 merge/rebase）时，目录确认与提供方选择之间会多出一步：

- **使用当前分支** —— 原有流程，不创建 worktree。
- **新建 worktree** —— 建议命名为 `ccg/<kebab(topic-title)>`（或 `ccg/agent-<n>`），并自动避开分支和 worktree 名冲突。一键确认，或发送文本回复来修改名称。

Worktree 通过 `git worktree add` 创建在 `<repo>.worktrees/<slug>`。代理以 worktree 路径为根目录启动。源仓库有未提交更改时允许继续，仅显示一行警告。分支名验证走 `git check-ref-format --branch`。失败时显示一行错误信息和取消按钮。

非 git 目录的流程不变 —— 无警告，无额外步骤。

<a id="completion-summaries-llm"></a>

## 完成摘要（LLM）

代理完成任务（Stop 事件）时，ccgram 最多等待约 3 秒，让配置的 LLM 生成一行工作成果摘要，然后将 Ready 消息原位编辑为 `Done — {summary}`。静态的增强版 Ready（任务清单 + 最后状态）会立即显示，因此你永远不会被 LLM 阻塞 —— 摘要到达后只是对其升级。

未配置 LLM（或超时）时，保留静态 Ready。

该 LLM 与 shell 命令生成使用同一后端（`CCGRAM_LLM_PROVIDER`）。

<a id="providers"></a>

## 提供方

CCGram 支持 Claude Code、Codex CLI、Gemini CLI、Pi 和 Shell。每个话题可以使用不同的提供方。各提供方的完整说明、会话模式、自定义启动命令、LLM 配置及提供方特有行为见 **[docs/providers.md](providers.md)**。

<a id="data-storage"></a>

## 数据存储

所有状态文件位于 `$CCGRAM_DIR`（默认 `~/.ccgram/`）：

| 文件                 | 说明                                                       |
| -------------------- | ---------------------------------------------------------- |
| `state.json`         | 话题绑定、窗口状态、显示名称、读取偏移量                   |
| `session_map.json`   | hook 生成的窗口 → 会话映射                                 |
| `events.jsonl`       | 追加式 hook 事件日志（监控器增量读取）                     |
| `monitor_state.json` | 各会话的字节偏移量（防止重复通知）                         |

会话转录从各提供方的专属位置只读读取：`~/.claude/projects/`（Claude）、`~/.codex/sessions/`（Codex）、`~/.gemini/tmp/`（Gemini）、`~/.pi/agent/sessions/`（Pi）。Shell 没有转录文件 —— 输出直接从 tmux 面板捕获。机器人永不写入代理的数据目录。

<a id="running-as-a-service"></a>

## 作为服务运行

如需长期运行，可将 ccgram 作为 systemd 服务或交由进程管理器运行：

```bash
# systemd user service (~/.config/systemd/user/ccgram.service)
[Unit]
Description=CCGram - Command & Control Bot for AI coding agents
After=network.target

[Service]
Type=notify
ExecStart=%h/.local/bin/ccgram
Restart=on-failure
RestartSec=5
WatchdogSec=90
Environment=CCGRAM_DIR=%h/.ccgram

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable ccgram
systemctl --user start ccgram
```

`Type=notify` + `WatchdogSec` 启用健康看门狗:bot 启动完成后向 systemd 发送 `READY=1`,之后每半个看门狗周期发送一次心跳——心跳与内部健康检查(会话监控循环、状态轮询循环存活)绑定,任一核心循环卡死或退出即停止心跳,systemd 会自动重启服务。改回 `Type=simple`(并删除 `WatchdogSec`)即可禁用,行为退化为普通崩溃重启。

在 macOS 上，可以使用 launchd plist，或直接在分离的 tmux 会话中运行：

```bash
tmux new-session -d -s ccgram-daemon 'ccgram'
```
