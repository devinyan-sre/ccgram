"""Minimal user-facing string localization.

``t("English text")`` returns the localized string for the active language,
falling back to the input unchanged. The language comes from ``CCGRAM_LANG``
(default ``"en"`` — a pure passthrough, so tests and English installs see
zero behavior change). ``load_dotenv`` in ``config.py`` populates the env,
so ``CCGRAM_LANG=zh`` in ``~/.ccgram/.env`` is enough.

Design notes:
- Catalog keys are the exact English source strings (gettext style), so
  call sites stay readable and untranslated strings degrade gracefully.
- Strings with ``{placeholders}`` keep them verbatim in the translation;
  callers ``.format()`` the *returned* string.
- Only user-visible Telegram strings go through ``t()`` — log messages,
  internal errors, and CLI output stay English.

Key function: t(). Catalog: _ZH (Simplified Chinese).
"""

from __future__ import annotations

import os

_LANG: str | None = None


def _language() -> str:
    """Resolve the active language once (env is stable after config load)."""
    global _LANG
    if _LANG is None:
        _LANG = os.environ.get("CCGRAM_LANG", "en").strip().lower() or "en"
    return _LANG


def _reset_language_for_testing() -> None:
    global _LANG
    _LANG = None


def t(text: str) -> str:
    """Translate a user-facing string for the active language.

    Unknown strings (or ``CCGRAM_LANG`` unset/``en``) pass through unchanged.
    """
    if _language().startswith("zh"):
        return _ZH.get(text, text)
    return text


# ── Simplified Chinese catalog ─────────────────────────────────────────
# Keys must match the English source strings byte-for-byte (including
# placeholders and trailing punctuation). Keep alphabetized-by-feature
# groups so additions stay reviewable.

_ZH: dict[str, str] = {
    # Shared topic guards
    "❌ Use this command inside a topic.": "❌ 请在话题内使用此命令。",
    "❌ This topic is not bound to any session.": "❌ 此话题尚未绑定任何会话。",
    "❌ Window no longer exists.": "❌ 窗口已不存在。",
    "Use this command inside a topic.": "请在话题内使用此命令。",
    "No session bound to this topic.": "此话题尚未绑定会话。",
    "You are not authorized to use this bot.": "你没有权限使用此机器人。",
    "Please include a question after the mention.": "请在 @机器人 后写上问题内容。",
    "{emoji} Uploaded `{path}`": "{emoji} 已上传 `{path}`",
    "📚 Uploaded album: {count} files": "📚 已合并上传相册：{count} 个文件",
    "❌ File saved but failed to notify the agent: {error}": (
        "❌ 文件已保存，但通知 CLI 失败：{error}"
    ),
    "⚠ Topic not bound — send a text message first to select a workspace, then resend the media.": (
        "⚠ 此话题尚未绑定会话，请先发送文字消息选择工作目录，再重新发送媒体。"
    ),
    "⏳ Task queued (position {position}).": "⏳ 任务已排队（第 {position} 位）。",
    "⏳ Task {task_id} queued (position {position}, estimated ≤{eta}s).": (
        "⏳ 任务 {task_id} 已排队（位置 {position}，预计 ≤{eta} 秒）。"
    ),
    "➕ Added to your current task.": "➕ 已补充到你的当前任务。",
    "✅ Received · task {task_id} · analyzing": ("✅ 已收到 · 任务 {task_id} · 分析中"),
    "✅ Task finished": "✅ 任务已结束",
    # Telegram command menu descriptions
    "Show ccgram welcome message": "显示 ccgram 欢迎信息",
    "List commands for this topic provider": "列出当前话题 Provider 的命令",
    "Message history for this topic": "查看当前话题的消息历史",
    "Sessions dashboard": "查看会话总览",
    "Browse and resume past sessions": "浏览并恢复历史会话",
    "Capture terminal screenshot": "截取终端画面",
    "Open auto-refreshing terminal view": "打开自动刷新的终端视图",
    "List panes in this window": "列出当前窗口的 Pane",
    "Recover a dead topic": "恢复已停止的话题",
    "Audit and fix state": "检查并修复状态",
    "Unbind this topic": "解除当前话题绑定",
    "Recall recent commands": "找回最近命令",
    "Search across session transcripts": "搜索所有会话记录",
    "Show action toolbar": "显示操作工具栏",
    "Toggle tool call batching": "切换工具调用合并模式",
    "Upgrade ccgram and restart": "升级并重启 ccgram",
    "Replace provider while keeping this topic": "保留话题并切换 Provider",
    "Apply directory-provider-number topic name": "按目录-Provider-序号命名话题",
    "Stop session but keep topic history": "停止会话并保留话题历史",
    "Restart a parked topic": "重新启动已挂起话题",
    "Diagnose topic and delivery state": "诊断话题与消息投递状态",
    "Replay recent assistant replies": "重新发送最近的助手回复",
    "Show delivery and queue health": "查看投递与队列健康状态",
    "Run safe CCGram wiring checks": "运行安全的 CCGram 链路自检",
    "Show active and queued topic tasks": "查看运行中和排队任务",
    "Supplement your current task": "补充当前任务",
    "End current task and start a boundary": "结束当前任务并创建新边界",
    "Cancel your active or queued task": "取消运行中或排队任务",
    "Retry a stuck task submission": "重试未确认的任务提交",
    "Admin: cancel all topic tasks": "管理员：取消话题全部任务",
    "Admin: force-stop a stuck task": "管理员：强制停止卡住的任务",
    "Inspect or manage your isolated workspace": "查看或管理隔离工作区",
    # Codex command descriptions (unknown/custom descriptions stay English)
    "Clear conversation history": "清空对话历史",
    "Summarize history and free up context": "压缩历史并释放上下文",
    "Copy last response as Markdown": "复制上一条回复为 Markdown",
    "Send logs to maintainers": "向维护者发送日志",
    "Branch chat into a new thread": "将对话分支到新线程",
    "Create AGENTS.md project guidance": "创建 AGENTS.md 项目说明",
    "List configured MCP tools": "列出已配置的 MCP 工具",
    "Attach files to conversation": "向对话附加文件",
    "Switch approval mode (suggest/auto-edit/full-auto)": (
        "切换审批模式（建议/自动编辑/全自动）"
    ),
    "Switch model and reasoning effort": "切换模型与推理强度",
    "Manage confirmation rules": "管理确认规则",
    "Customize Codex communication style": "设置 Codex 沟通风格",
    "Enter plan mode": "进入计划模式",
    "Rename current thread": "重命名当前线程",
    "Review current changes": "审查当前改动",
    "Start a side conversation in a temporary fork": "在临时分支中开始旁路对话",
    "List available skills": "列出可用 Skills",
    "Configure status line items": "配置状态栏项目",
    "Show model, approvals, token usage": "显示模型、审批和 Token 用量",
    # Persistent operations dashboard
    "🛰 CCGram operations overview": "🛰 CCGram 全局运行总览",
    "🛰 Topic operations overview": "🛰 话题运行总览",
    "Concurrency {used}/{limit} · queued {queued} · cancelling {cancelling}": (
        "并发 {used}/{limit} · 排队 {queued} · 取消中 {cancelling}"
    ),
    "⚪ No active or queued tasks": "⚪ 当前没有运行或排队任务",
    "🟢 processing": "🟢 处理中",
    "🔵 analyzing": "🔵 分析中",
    "🛠 using tools": "🛠 工具执行中",
    "🟣 waiting": "🟣 等待确认",
    "🟢 generating reply": "🟢 生成回复中",
    "📨 delivering": "📨 投递中",
    "🟡 queued #{position}": "🟡 排队第 {position} 位",
    "🟠 cancelling": "🟠 取消中",
    " · ETA ≤{seconds}s": " · 预计 ≤{seconds} 秒",
    " · +{count} supplements": " · +{count} 条补充",
    "… and {count} more": "… 另有 {count} 项",
    "Updated {time} · one operator is serial, operators are parallel": (
        "更新于 {time} · 同一成员串行，不同成员并行"
    ),
    "Beijing Time": "北京时间",
    "cancelled": "已取消",
    "ended": "已结束",
    "member-{code}": "成员-{code}",
    "topic {thread_id}": "话题 {thread_id}",
    "{seconds}s": "{seconds}秒",
    "{minutes}m": "{minutes}分",
    "{hours}h{minutes}m": "{hours}时{minutes}分",
    "Dashboard unavailable": "总览暂不可用",
    "🧪 CCGram self-test (no CLI request sent)": "🧪 CCGram 自检（不会向 CLI 发送请求）",
    "{mark} Topic routing: {detail}": "{mark} 话题路由：{detail}",
    "not bound": "未绑定",
    "{mark} Request correlation: chat/member/topic/window": (
        "{mark} 请求关联：群组/成员/话题/窗口"
    ),
    "{mark} Scheduler: active {active} · queued {queued}": (
        "{mark} 调度器：运行 {active} · 排队 {queued}"
    ),
    "{mark} Delivery: pending {pending} · retrying {retrying}": (
        "{mark} 投递：待发送 {pending} · 重试 {retrying}"
    ),
    "{mark} Topic queues: {topics} · waiting {queued} · unfinished {unfinished}": (
        "{mark} 话题队列：{topics} · 等待 {queued} · 未完成 {unfinished}"
    ),
    "{mark} Topic + General dashboard wiring": "{mark} 话题与 General 总览链路",
    "Result: {result}": "结果：{result}",
    "healthy": "健康",
    "check warnings": "请检查警告项",
    "✅ No dashboard errors.": "✅ 总览当前没有异常。",
    "⚠️ Dashboard errors: {count}": "⚠️ 总览异常：{count} 项",
    "isolated": "已隔离",
    "retrying": "重试中",
    "⏳ Queue: {queued} · running: {active}": "⏳ 排队：{queued} · 运行：{active}",
    "🔄 Refresh all": "🔄 全部刷新",
    "⚠️ Errors": "⚠️ 异常",
    "⏳ Queue": "⏳ 排队",
    "Open topic {thread_id}": "打开话题 {thread_id}",
    "Dashboard sync {age}s ago · isolated topics {count}": (
        "总览同步 {age} 秒前 · 已隔离话题 {count} 个"
    ),
    "❌ This task has too many supplements. Use /task_new to start a new task.": (
        "❌ 当前任务的补充消息过多，请使用 /task_new 开始新任务。"
    ),
    "⏸ Task {task_id} is still cancelling. Wait for confirmation or ask an admin to force-cancel it.": (
        "⏸ 任务 {task_id} 仍在取消确认中。请等待确认，或让管理员强制取消。"
    ),
    "⚠️ ccgram delivery stalled\nsession: `{session_id}`\npending: {lag} bytes · {duration}s\nUse /ops and /diag for details.": (
        "⚠️ ccgram 投递暂时停滞\n"
        "会话：`{session_id}`\n"
        "待投递：{lag} 字节 · 已持续 {duration} 秒\n"
        "请使用 /ops 和 /diag 查看详情。"
    ),
    "⚠️ *CCGram task queue stalled*\n\ntopic `{thread_id}` · user `{user_id}` · waiting `{age}s` · position `{position}`": (
        "⚠️ *CCGram 任务排队停滞*\n\n"
        "话题 `{thread_id}` · 用户 `{user_id}` · "
        "已等待 `{age}` 秒 · 队列位置 `{position}`"
    ),
    "❌ Could not start your isolated workspace lane: {error}": (
        "❌ 无法启动你的隔离工作通道：{error}"
    ),
    "Not authorized.": "未授权。",
    # Shared toasts / buttons
    "Not your session": "不是你的会话",
    "Not your window": "不是你的窗口",
    "Use in a topic": "请在话题内使用",
    "Invalid data": "无效数据",
    "Invalid callback data": "无效的回调数据",
    "Cancel": "取消",
    "Cancelled": "已取消",
    "✖ Cancel": "✖ 取消",
    "✕ Dismiss": "✕ 关闭",
    "Refreshed": "已刷新",
    "Window not found": "未找到窗口",
    "Window no longer exists": "窗口已不存在",
    "Working directory not available": "工作目录不可用",
    "Working directory not available.": "工作目录不可用。",
    "State error": "状态错误",
    "(unknown)": "(未知)",
    "unknown": "未知",
    "; ": "；",
    # /ops and /diag
    "✅ healthy": "✅ 运行正常",
    "⚠ attention needed": "⚠ 需要关注",
    "ccgram ops · {health}": "ccgram 运维状态 · {health}",
    "Sessions: {tracked} tracked · {stalled} stalled": (
        "会话：跟踪 {tracked} 个 · 停滞 {stalled} 个"
    ),
    "Delivery: {lag} bytes lag · {pending} durable pending": (
        "投递：积压 {lag} 字节 · 持久化待投递 {pending} 条"
    ),
    "Queues: {topics} topics · {queued} queued · {active} active": (
        "队列：{topics} 个话题 · 排队 {queued} 条 · 处理中 {active} 条"
    ),
    "Provider tasks: {active} active · {queued} queued · {cancelling} cancelling": (
        "CLI 任务：运行 {active} 个 · 排队 {queued} 个 · 取消确认中 {cancelling} 个"
    ),
    "Task timing: avg {average}s · oldest wait {oldest}s": (
        "任务耗时：平均 {average} 秒 · 最久等待 {oldest} 秒"
    ),
    "Cancels (24h): {confirmed} confirmed · {timed_out} timed out · {forced} forced": (
        "取消统计（24 小时）：确认 {confirmed} 个 · 超时 {timed_out} 个 · 强制 {forced} 个"
    ),
    "Retries: {retrying}": "重试中：{retrying} 条",
    "✅ consistent": "✅ 状态一致",
    "window is parked/dead": "窗口已挂起或退出",
    "foreground provider differs from state": "前台 CLI 与记录的 provider 不一致",
    "transcript provider differs from state": "转录文件 provider 与记录状态不一致",
    "no session binding": "没有会话绑定",
    "session is not tracked by the delivery monitor": "投递监控未跟踪此会话",
    "parked": "已挂起",
    "pid {pid} · {command}": "进程 {pid} · {command}",
    "Topic diagnostic · `{window_id}`": "话题诊断 · `{window_id}`",
    "Health: {health}": "健康状态：{health}",
    "Lifecycle: {lifecycle}": "生命周期：{lifecycle}",
    "Provider: state `{state}` · detected `{detected}`": (
        "Provider：记录 `{state}` · 检测 `{detected}`"
    ),
    "Process: {process}": "进程：{process}",
    "Session: `{session_id}`": "会话：`{session_id}`",
    "Transcript: `{transcript}`": "转录文件：`{transcript}`",
    "Delivery: file {file_size} bytes · committed {delivered} · lag {lag}": (
        "投递：文件 {file_size} 字节 · 已提交 {delivered} · 积压 {lag}"
    ),
    "CWD: `{cwd}`": "工作目录：`{cwd}`",
    "window `{window_id}`": "窗口 `{window_id}`",
    "topic `{thread_id}`": "话题 `{thread_id}`",
    # /diff
    "❌ Not a git repository: {cwd}": "❌ 不是 git 仓库:{cwd}",
    "❌ git failed: {error}": "❌ git 执行失败:{error}",
    "✅ Working tree clean — no uncommitted changes.": "✅ 工作区干净,没有未提交的改动。",
    "📋 Status:": "📋 文件状态:",
    # /usage
    "📊 Session token usage": "📊 会话 token 用量",
    "Model: {models}": "模型:{models}",
    "Turns: {user} user / {assistant} assistant": "轮次:用户 {user} / 助手 {assistant}",
    "Input tokens: {n}": "输入 tokens:{n}",
    "Output tokens: {n}": "输出 tokens:{n}",
    "Cache read: {n}": "缓存读取:{n}",
    "Cache write: {n}": "缓存写入:{n}",
    "Total: {n}": "总计:{n}",
    "❌ No transcript for this session yet (usage data unavailable).": (
        "❌ 此会话还没有 transcript(暂无用量数据)。"
    ),
    "❌ Could not read the transcript file.": "❌ 无法读取 transcript 文件。",
    "❌ This provider's transcript has no token usage data.": (
        "❌ 该 provider 的 transcript 不包含 token 用量数据。"
    ),
    # Token/context watch
    "⚠️ Context is {pct}% full ({used} / {limit} tokens) — "
    "consider /compact or a fresh session.": (
        "⚠️ 上下文已使用 {pct}%({used} / {limit} tokens),建议执行 /compact 或开启新会话。"
    ),
    "⚠️ This session has consumed {total} tokens (warning threshold: {threshold}).": (
        "⚠️ 本会话已消耗 {total} tokens(预警阈值:{threshold})。"
    ),
    # /search
    "Usage: /search <keyword> — search across session transcripts": (
        "用法:/search <关键词> — 跨会话搜索历史对话"
    ),
    "🔍 No matches for “{query}”.": "🔍 未找到与“{query}”匹配的内容。",
    "🔍 {count} match(es) for “{query}”:": "🔍 “{query}”共 {count} 条结果:",
    "_(results truncated — refine your query)_": "_(结果已截断,请缩小搜索范围)_",
    # Text handler
    "\U0001f4ac Will deliver once the agent starts.": "\U0001f4ac agent 启动后将自动送达。",
    "Please use the window picker above, or tap Cancel.": (
        "请使用上方的窗口选择器,或点击取消。"
    ),
    "Please use the directory browser above, or tap Cancel.": (
        "请使用上方的目录浏览器,或点击取消。"
    ),
    "❌ Worktree state lost. Start over with a new message.": (
        "❌ Worktree 状态丢失,请发送新消息重新开始。"
    ),
    "❌ Invalid branch name; try again or tap Cancel.": (
        "❌ 分支名无效;请重试或点击取消。"
    ),
    "❌ Please use a named topic. Create a new topic to start a session.": (
        "❌ 请使用具名话题。创建一个新话题以开始会话。"
    ),
    (
        "❌ {provider} is not ready. Your message was not sent to the terminal; "
        "send it again to retry. Details: {error}"
    ): (
        "❌ {provider} 尚未就绪。消息未发送到终端，请重新发送以安全重试。详情：{error}"
    ),
    # /start welcome
    "\U0001f916 *CCGram*\n\nEach topic is a session. Create a new topic to start.": (
        "\U0001f916 *CCGram*\n\n每个话题即一个会话。创建新话题即可开始。"
    ),
    # Window picker
    "*Bind to Existing Window*": "*绑定到现有窗口*",
    "These windows are running but not bound to any topic.": (
        "以下窗口正在运行但尚未绑定任何话题。"
    ),
    "Pick one to attach it here, or start a new session.": (
        "选择一个绑定到此话题,或开始新会话。"
    ),
    "➕ New Session": "➕ 新建会话",
    # Directory browser
    "*Select Working Directory*\n\nCurrent: `{path}`\n\n_(No subdirectories)_": (
        "*选择工作目录*\n\n当前:`{path}`\n\n_(没有子目录)_"
    ),
    (
        "*Select Working Directory*\n\nCurrent: `{path}`\n\n"
        "Tap a folder to enter, or select current directory"
    ): "*选择工作目录*\n\n当前:`{path}`\n\n点击文件夹进入,或选择当前目录",
    "Select": "选择",
    "⭐ Starred": "⭐ 已收藏",
    "☆ Unstarred": "☆ 已取消收藏",
    "Stale browser (flow reset)": "浏览器已失效(流程已重置)",
    "Stale browser (topic mismatch)": "浏览器已失效(话题不匹配)",
    "Favorite not found": "未找到收藏",
    "Directory no longer exists": "目录已不存在",
    "Directory list changed, please refresh": "目录列表已变化,请刷新",
    "Directory not found": "未找到目录",
    "✅ Already bound to window {name}.": "✅ 已绑定到窗口 {name}。",
    # Provider / mode picker
    "*Select Provider*\n\nDirectory: `{path}`\n\nWhich agent CLI to use?": (
        "*选择 Provider*\n\n目录:`{path}`\n\n使用哪个 agent CLI?"
    ),
    " (default)": "(默认)",
    (
        "*Select Session Mode*\n\n"
        "Directory: `{path}`\n"
        "Provider: {provider}\n\n"
        "Choose how many approvals you want for this session."
    ): "*选择会话模式*\n\n目录:`{path}`\nProvider:{provider}\n\n选择此会话需要多少确认。",
    "✅ Standard": "✅ 标准",
    "Unknown provider": "未知 provider",
    "Invalid mode": "无效模式",
    "Unknown mode": "未知模式",
    (
        "{provider} did not become ready, so your pending message was not sent. "
        "Send it again to retry safely. Details: {error}"
    ): ("{provider} 未能就绪，因此待发消息未送出。请重新发送以安全重试。详情：{error}"),
    (
        "{provider} started, but session registration timed out. Your pending "
        "message was not sent; send it again to retry safely."
    ): ("{provider} 已启动，但会话注册超时。待发消息未送出；请重新发送以安全重试。"),
    "❌ Selection expired. Tap Cancel and retry.": "❌ 选择已过期。请点击取消后重试。",
    "⏳ Creating window, please wait…": "⏳ 窗口创建中,请稍候…",
    # Workspace picker (herdr)
    (
        "*Select Workspace*\n\nDirectory: `{path}`\n\n"
        "Pick an existing workspace or let ccgram resolve one from the folder."
    ): (
        "*选择 Workspace*\n\n目录:`{path}`\n\n"
        "选择现有 workspace,或让 ccgram 根据文件夹自动解析。"
    ),
    "🔍 Auto-resolve from folder": "🔍 根据文件夹自动解析",
    "❌ Invalid workspace selection. Tap Cancel and retry.": (
        "❌ 无效的 workspace 选择。请点击取消后重试。"
    ),
    "❌ Workspace list changed. Tap Cancel and retry.": (
        "❌ Workspace 列表已变化。请点击取消后重试。"
    ),
    # Worktree flow
    (
        "*Git Worktree*\n\n"
        "Repo: `{path}`\n"
        "Current branch: `{branch}`\n\n"
        "Work on the current branch, or create an isolated worktree "
        "on a new branch?"
    ): (
        "*Git Worktree*\n\n仓库:`{path}`\n当前分支:`{branch}`\n\n"
        "在当前分支上工作,还是在新分支上创建隔离的 worktree?"
    ),
    "🌿 Use current ({branch})": "🌿 使用当前分支({branch})",
    "➕ New worktree": "➕ 新建 worktree",
    "*New Worktree*": "*新建 Worktree*",
    "Repo: `{path}`": "仓库:`{path}`",
    "Branch: `{branch}`": "分支:`{branch}`",
    "Worktree: `{path}`": "Worktree:`{path}`",
    (
        "⚠️ The source repo has uncommitted changes. The worktree "
        "starts from HEAD; uncommitted work stays where it is."
    ): "⚠️ 源仓库有未提交的改动。Worktree 将从 HEAD 开始;未提交的工作保留在原处。",
    "✅ Use this": "✅ 就用这个",
    "✏️ Edit name": "✏️ 编辑名称",
    "Creating worktree…": "正在创建 worktree…",
    "❌ Could not create worktree: {error}": "❌ 无法创建 worktree:{error}",
    "❌ Worktree state lost. Tap Cancel and retry.": (
        "❌ Worktree 状态丢失。请点击取消后重试。"
    ),
    "✏️ Send the branch name as a message, or tap Cancel.": (
        "✏️ 以消息形式发送分支名,或点击取消。"
    ),
    # Window launch
    "Bound to this topic. Send messages here.": "已绑定到此话题。请在这里发送消息。",
    "❌ Failed to send pending message: {error}": "❌ 发送待处理消息失败:{error}",
    # Recovery banner
    "\U0001f504 Restore `{name}`.": "\U0001f504 恢复 `{name}`。",
    "Choose how to continue.": "选择如何继续。",
    "⏪ Resume `{name}`.": "⏪ 恢复会话 `{name}`。",
    "Pick a session below or use the menu.": "在下方选择会话或使用菜单。",
    "⚠ Session `{name}` ended.": "⚠ 会话 `{name}` 已结束。",
    "Tap a button or send a message to recover.": "点击按钮或发送消息以恢复。",
    "Start fresh": "全新开始",
    "Continue last session": "继续上次会话",
    "Resume from list": "从列表恢复",
    "\U0001f195 Fresh": "\U0001f195 全新",
    "▶ Continue": "▶ 继续",
    "⏪ Resume": "⏪ 恢复",
    "Session started.": "会话已启动。",
    "Fresh session started.": "全新会话已启动。",
    "Continuing previous session.": "正在继续上次会话。",
    "Resuming session: {summary}": "正在恢复会话:{summary}",
    "Failed": "失败",
    "Failed to send": "发送失败",
    "Created": "已创建",
    "Chat unavailable": "聊天不可用",
    "Stale recovery (topic mismatch)": "恢复已失效(话题不匹配)",
    "❌ Directory no longer exists.": "❌ 目录已不存在。",
    "Project gone": "项目已不存在",
    "⏪ Select a session to resume:\n(`{cwd}`)": "⏪ 选择要恢复的会话:\n(`{cwd}`)",
    "⚠ No sessions in this folder yet.\n(`{cwd}`)": (
        "⚠ 此文件夹还没有会话。\n(`{cwd}`)"
    ),
    "⚠ No past sessions found in any project.": "⚠ 所有项目中都没有找到历史会话。",
    "Nothing to resume": "没有可恢复的会话",
    "⏪ Select a session to resume:": "⏪ 选择要恢复的会话:",
    "Cancelled. Send a message to try again.": "已取消。发送消息可重试。",
    "⬅ Back": "⬅ 返回",
    "\U0001f5c2 Browse other projects": "\U0001f5c2 浏览其他项目",
    "\U0001f195 Start fresh": "\U0001f195 全新开始",
    "Couldn't read selection": "无法读取所选项",
    "Session no longer in list": "会话已不在列表中",
    "Recovery menu expired": "恢复菜单已过期",
    # /resume
    "never": "从未",
    "today": "今天",
    "yesterday": "昨天",
    "{days}d ago": "{days} 天前",
    "{count} msgs": "{count} 条消息",
    "⬅ Prev": "⬅ 上一页",
    "Next ➡": "下一页 ➡",
    "❌ Please use /resume in a named topic.": "❌ 请在具名话题内使用 /resume。",
    "❌ Resume is not supported by the current provider.": (
        "❌ 当前 provider 不支持恢复会话。"
    ),
    "❌ No past sessions found.": "❌ 没有找到历史会话。",
    "❌ Project directory no longer exists.": "❌ 项目目录已不存在。",
    "Couldn't create window": "无法创建窗口",
    "✅ Resuming session: {summary}\n\U0001f4c2 `{cwd}`": (
        "✅ 正在恢复会话:{summary}\n\U0001f4c2 `{cwd}`"
    ),
    "Resumed": "已恢复",
    "Invalid page": "无效页码",
    "No sessions available": "没有可用会话",
    "Resume cancelled.": "已取消恢复。",
    # /restore
    "Window is still running — nothing to restore.": "窗口仍在运行——无需恢复。",
    "Directory no longer exists.": "目录已不存在。",
    # Status bubble
    "⎋ Esc": "⎋ Esc",
    "\U0001f4c4 Last": "\U0001f4c4 最新回复",
    "\U0001f4e5 Get File": "\U0001f4e5 获取文件",
    "idle": "空闲",
    "idle {minutes}m": "空闲 {minutes} 分钟",
    "idle {hours}h": "空闲 {hours} 小时",
    "active": "活跃",
    "blocked": "阻塞",
    "dead": "已退出",
    "{total} tasks ({done} done, {open} open)": (
        "{total} 个任务(已完成 {done},待办 {open})"
    ),
    "blocked by {tasks}": "被 {tasks} 阻塞",
    "+{count} more": "+{count} 更多",
    # Status bar actions
    "Stale status button": "状态按钮已失效",
    "Command not found": "未找到命令",
    "↩ Recalled": "↩ 已重发",
    "Failed to send command": "发送命令失败",
    "↩ Sent": "↩ 已发送",
    "⎋ Sent Escape": "⎋ 已发送 Esc",
    "Unknown key": "未知按键",
    "\U0001f4c4 Last reply": "\U0001f4c4 最新回复",
    "\U0001fa9f Dashboard": "\U0001fa9f 仪表盘",
    # Interactive UI
    "↑↓ select · Enter confirm · Esc cancel · type to enter text": (
        "↑↓ 选择 · Enter 确认 · Esc 取消 · 直接输入文字"
    ),
    "Pane": "窗格",
    "␣ Space": "␣ 空格",
    "⇥ Tab": "⇥ Tab",
    "⏎ Enter": "⏎ 回车",
    # Sessions dashboard
    "\U0001f504 Refresh": "\U0001f504 刷新",
    "No active sessions.\n\nCreate a new topic to start a session.": (
        "没有活跃会话。\n\n创建新话题即可开始会话。"
    ),
    "Sessions": "会话列表",
    "\U0001f5d1 Kill {name}": "\U0001f5d1 结束 {name}",
    "⚠ Confirm kill {name}": "⚠ 确认结束 {name}",
    "Kill session '{name}'?\n\nThis will terminate the Claude Code process.": (
        "结束会话 '{name}'?\n\n这将终止 Claude Code 进程。"
    ),
    "\U0001f5d1 Killed '{name}'": "\U0001f5d1 已结束 '{name}'",
    "Create a new topic to start a session.": "创建新话题即可开始会话。",
    "Killed": "已结束",
    # Context compaction
    "🗜 Context is full — auto-compacting now.": "🗜 上下文已满,正在自动压缩。",
    "🗜 Compacting context.": "🗜 正在压缩上下文。",
    (
        "Details will be summarised away; put anything you need to keep in a file."
    ): "细节会被摘要掉;需要保留的内容请先写进文件。",
    # Daily digest ("idle {hours}h" is already defined above and reused here)
    "idle {days}d": "空闲 {days} 天",
    "{count} tool errors": "{count} 次工具报错",
    "most discussed:": "聊得最多的：",
    "{minutes}m / {turns} turns": "{minutes} 分钟 / {turns} 轮",
    # Mass-death circuit breaker
    "🧯 *CCGram circuit breaker*": "🧯 *CCGram 熔断器*",
    "{count} windows died within {seconds}s — this looks like an": (
        "{seconds} 秒内有 {count} 个窗口死亡 —— 这看起来是一次"
    ),
    "infrastructure event, not something anyone asked for.": (
        "基础设施事件,而不是任何人主动要求的操作。"
    ),
    (
        "Automatic topic/window cleanup is suspended for {minutes} minutes. "
        "Nothing is lost — the timers stay armed and re-evaluate afterwards."
    ): (
        "自动话题/窗口清理已暂停 {minutes} 分钟。"
        "什么都不会丢 —— 计时器仍然武装,暂停结束后会重新判断。"
    ),
    # Destructive-action audit
    "🔻 *CCGram automatic action*": "🔻 *CCGram 自动操作*",
    "Nobody requested this — it was automatic cleanup. Check it was correct.": (
        "这不是任何人主动触发的,是自动清理。请确认它做得对。"
    ),
    "Topic retired by autoclose": "话题被自动关闭策略退休",
    "Topic removed by /sync (history destroyed)": "话题被 /sync 移除(聊天记录已销毁)",
    "Window killed — unbound past its TTL": "窗口被杀 —— 解绑后超过存活时限",
    "Window killed — its topic was deleted": "窗口被杀 —— 对应话题已被删除",
    "Window killed from the sessions dashboard": "窗口被杀 —— 来自会话面板操作",
    "Merged member worktree removed after retention": "保留期结束后已删除完成合并的成员工作区",
    "Deleted — the topic's whole message history is gone.": (
        "已删除 —— 该话题的全部聊天记录已随之销毁。"
    ),
    "Archived after the {state} timeout.": "在 {state} 状态超时后归档。",
    "The agent process and any unsaved work in it are gone.": (
        "该 agent 进程及其中未保存的工作已丢失。"
    ),
    # Topic autoclose
    (
        "🗄 Session ended — topic archived. History is kept; "
        "reopen the topic to start a new session here."
    ): ("🗄 会话已结束，话题已归档。历史记录已保留；重新打开话题即可在此开始新会话。"),
    # Voice
    "✓ Send to agent": "✓ 发送给 agent",
    "✗ Discard": "✗ 丢弃",
    "❌ Failed to download voice message.": "❌ 下载语音消息失败。",
    (
        "⚠️ Voice transcription is not configured. Set"
        " CCGRAM_WHISPER_PROVIDER to enable it.\n\nSupported providers:"
        " openai, groq"
    ): (
        "⚠️ 尚未配置语音转写。设置 CCGRAM_WHISPER_PROVIDER 以启用。\n\n"
        "支持的 provider:openai、groq"
    ),
    "🎤 Transcribed:\n\n{text}": "🎤 转写结果:\n\n{text}",
    (
        "⚠ Topic not bound — send a text message first to pick a "
        "directory, then re-record.\n"
        "\U0001f4ac Voice messages aren't queued."
    ): (
        "⚠ 话题尚未绑定——请先发送文字消息选择目录,然后重新录音。\n"
        "\U0001f4ac 语音消息不会排队。"
    ),
    "❌ Voice message too large ({size} MB). Maximum 25 MB.": (
        "❌ 语音消息过大({size} MB)。最大 25 MB。"
    ),
    "⚠️ Could not transcribe audio (empty result).": "⚠️ 无法转写音频(结果为空)。",
    "Message no longer available": "消息已不可用",
    "⚠️ Session expired, resend voice message": "⚠️ 会话已过期,请重新发送语音",
    "⚠️ No session bound.": "⚠️ 尚未绑定会话。",
    "❌ Failed to send": "❌ 发送失败",
    "Discarded": "已丢弃",
    # /verbose and /toolcalls
    "⚡ Tool calls will be *batched* into a single message.": (
        "⚡ 工具调用将*合并*为一条消息。"
    ),
    "🫧 Tool calls shown live, removed when the reply is ready (ephemeral).": (
        "🫧 工具调用实时显示,回复就绪后移除(临时模式)。"
    ),
    "💬 Tool calls will be sent *individually* (verbose mode).": (
        "💬 工具调用将*逐条*发送(详细模式)。"
    ),
    "⚡ Tool calls *shown* for this topic (overrides global default).": (
        "⚡ 此话题*显示*工具调用(覆盖全局默认)。"
    ),
    "🔇 Tool calls *hidden* for this topic (overrides global default).": (
        "🔇 此话题*隐藏*工具调用(覆盖全局默认)。"
    ),
    "🔄 Tool calls follow the global default (currently *{mode}*).": (
        "🔄 工具调用跟随全局默认(当前为 *{mode}*)。"
    ),
    # Live view + screenshots
    "⏹ Stop Live": "⏹ 停止实时",
    "Live view ended (timeout)": "实时视图已结束(超时)",
    "Live view ended (window closed)": "实时视图已结束(窗口已关闭)",
    "Live · {time}": "实时 · {time}",
    "\U0001f4fa Live": "\U0001f4fa 实时",
    "\U0001f4fa Live started": "\U0001f4fa 实时视图已开启",
    "\U0001f4fa Live view already running.": "\U0001f4fa 实时视图已在运行。",
    "Already live": "已在实时视图",
    "Failed to capture pane": "捕获窗格失败",
    "Failed to start live view": "启动实时视图失败",
    "Message lost": "消息丢失",
    "Screenshot": "截图",
    "⏹ Stopped": "⏹ 已停止",
    "\U0001f4f8 Pane {pane}": "\U0001f4f8 窗格 {pane}",
    "Failed to send screenshot": "发送截图失败",
    "Failed to refresh": "刷新失败",
    "Failed to capture": "捕获失败",
    "❌ Failed to capture terminal.": "❌ 捕获终端失败。",
    "❌ Failed to send screenshot.": "❌ 发送截图失败。",
    "❌ Failed to start live view.": "❌ 启动实时视图失败。",
    "\U0001f4d0 Single pane — no multi-pane layout detected.": (
        "\U0001f4d0 只有一个窗格——未检测到多窗格布局。"
    ),
    "\U0001f4d0 {count} panes in window": "\U0001f4d0 窗口中有 {count} 个窗格",
    "Pane {index} ({command})": "窗格 {index}({command})",
    "running": "运行中",
    "subscribed": "已订阅",
    # Pane actions
    (
        "✏️ Reply with a name for pane {pane_id} (max 32 chars). Send '-' to clear."
    ): "✏️ 回复窗格 {pane_id} 的名称(最多 32 个字符)。发送 '-' 清除。",
    "\U0001f515 Unsub": "\U0001f515 退订",
    "\U0001f514 Sub": "\U0001f514 订阅",
    "✏️ Rename": "✏️ 重命名",
    "\U0001f514 Lifecycle: on": "\U0001f514 生命周期通知:开",
    "\U0001f515 Lifecycle: off": "\U0001f515 生命周期通知:关",
    "Invalid pane": "无效窗格",
    "Pane lookup failed": "窗格查询失败",
    "Pane not found": "未找到窗格",
    "✓ Subscribed {pane}": "✓ 已订阅 {pane}",
    "✓ Unsubscribed {pane}": "✓ 已退订 {pane}",
    "Failed to open rename prompt": "打开重命名提示失败",
    "✓ Cleared name for {pane}": "✓ 已清除 {pane} 的名称",
    "❌ Name too long ({length} chars, max {max}).": (
        "❌ 名称过长({length} 个字符,最多 {max})。"
    ),
    "✓ Renamed {pane} → {name}": "✓ 已将 {pane} 重命名为 {name}",
    "Invalid window": "无效窗口",
    "✓ Pane lifecycle notifications on": "✓ 窗格生命周期通知已开启",
    "✓ Pane lifecycle notifications off": "✓ 窗格生命周期通知已关闭",
    # /split
    "❌ Could not split the window.": "❌ 无法拆分窗口。",
    "✅ Split into pane `{pane}` and ran `{command}`. Use /panes to view.": (
        "✅ 已拆分出窗格 `{pane}` 并运行 `{command}`。使用 /panes 查看。"
    ),
    "✅ Split into pane `{pane}`. Use /panes to view.": (
        "✅ 已拆分出窗格 `{pane}`。使用 /panes 查看。"
    ),
    # /sync
    "🔍 State audit": "🔍 状态审计",
    "🔍 State audit…": "🔍 状态审计…",
    "🔧 Fixing…": "🔧 修复中…",
    "Running fix...": "正在修复...",
    "Dismissed": "已关闭",
    "⚠ Multiplexer unavailable. No state changes were made.": (
        "⚠ 终端复用器不可用。未做任何状态更改。"
    ),
    "✅ Fixed {count} issue": "✅ 已修复 {count} 个问题",
    "✅ Fixed {count} issues": "✅ 已修复 {count} 个问题",
    "ℹ Removed {count} stale topic": "ℹ 已移除 {count} 个失效话题",
    "ℹ Removed {count} stale topics": "ℹ 已移除 {count} 个失效话题",
    "ℹ Recreated {count} topic": "ℹ 已重建 {count} 个话题",
    "ℹ Recreated {count} topics": "ℹ 已重建 {count} 个话题",
    (
        "⚠ {count} topic could not be closed automatically; safe to close manually"
    ): "⚠ {count} 个话题无法自动关闭;可放心手动关闭",
    (
        "⚠ {count} topics could not be closed automatically; safe to close manually"
    ): "⚠ {count} 个话题无法自动关闭;可放心手动关闭",
    "ℹ No topic bindings": "ℹ 没有话题绑定",
    "✓ {count} topics bound, all windows alive": (
        "✓ 已绑定 {count} 个话题,所有窗口均在运行"
    ),
    "⚠ {count} ghost binding(s) ({live}/{total} alive)": (
        "⚠ {count} 个幽灵绑定({live}/{total} 存活)"
    ),
    "⚠ {count} dead topic (deleted in Telegram)": (
        "⚠ {count} 个已删除话题(在 Telegram 中被删除)"
    ),
    "⚠ {count} dead topics (deleted in Telegram)": (
        "⚠ {count} 个已删除话题(在 Telegram 中被删除)"
    ),
    "\U0001f527 Fix {count} issue": "\U0001f527 修复 {count} 个问题",
    "\U0001f527 Fix {count} issues": "\U0001f527 修复 {count} 个问题",
    "✓ No orphaned entries": "✓ 没有孤立条目",
    "✓ Tmux display cache in sync": "✓ Tmux 显示缓存已同步",
    "ghost binding (dead window)": "幽灵绑定(窗口已死)",
    "dead topic (window alive, topic deleted)": "已删除话题(窗口存活,话题被删)",
    "orphaned display name": "孤立的显示名称",
    "orphaned group chat ID": "孤立的群组聊天 ID",
    "stale window state": "失效的窗口状态",
    "stale offset entry": "失效的偏移条目",
    "display name drift": "显示名称漂移",
    "unbound window (no topic)": "未绑定窗口(无话题)",
    # /agent
    "🔄 Auto": "🔄 自动",
    (
        "Current agent for `{window}`: **{current}**{badge}\n\n"
        "Pick a provider, or **Auto** to re-detect."
    ): (
        "窗口 `{window}` 当前的 agent:**{current}**{badge}\n\n"
        "选择一个 provider,或点 **自动** 重新检测。"
    ),
    " (manual override)": "(手动覆盖)",
    "Auto-detected: **{provider}**.": "自动检测结果:**{provider}**。",
    "Agent set to **{provider}** (manual override).": (
        "Agent 已设为 **{provider}**(手动覆盖)。"
    ),
    "Agent set to **shell**.": "Agent 已设为 **shell**。",
    "Prompt markers will install on next prompt.": (
        "提示符标记将在下一个提示符时安装。"
    ),
    "Launch the agent CLI in this pane; next SessionStart hook will track it.": (
        "在此窗格中启动 agent CLI;下一次 SessionStart hook 将开始跟踪。"
    ),
    "Use /agent inside a bound topic.": "请在已绑定的话题内使用 /agent。",
    "Unknown agent `{name}`. Use one of: {valid}.": (
        "未知 agent `{name}`。可用:{valid}。"
    ),
    "Cancelled. Agent still **{provider}**.": "已取消。Agent 仍为 **{provider}**。",
    "Bad callback": "无效回调",
    # Provider lifecycle
    "Codex + context": "Codex + 上下文",
    "Park topic": "休眠话题",
    " Context was transferred.": " 已交接上下文。",
    "✅ Switched this topic to {provider} ({window}).{context}": (
        "✅ 此话题已切换到 {provider}（{window}）。{context}"
    ),
    "❌ Use /handoff inside a bound topic.": "❌ 请在已绑定的话题内使用 /handoff。",
    "❌ Use /autoname inside a bound topic.": "❌ 请在已绑定的话题内使用 /autoname。",
    "❌ Could not apply the automatic name.": "❌ 无法应用自动话题名称。",
    "✅ Automatic topic name applied: {name}": "✅ 已应用自动话题名称：{name}",
    (
        "Choose the replacement provider. The old session is kept until "
        "the new one is ready."
    ): "选择替换 Provider。新会话就绪前会保留旧会话。",
    "❌ Unknown provider.": "❌ 未知 Provider。",
    "⏳ Starting {provider} and verifying it…": "⏳ 正在启动并验证 {provider}…",
    "Topic is already parked. Use /wake to restart it.": (
        "话题已经休眠，请使用 /wake 重新启动。"
    ),
    "Could not stop the session.": "无法停止会话。",
    "Topic parked. History and project binding were preserved.": (
        "话题已休眠，历史消息和项目绑定均已保留。"
    ),
    "❌ Use /park inside a bound topic.": "❌ 请在已绑定的话题内使用 /park。",
    "❌ Use /wake inside a bound topic.": "❌ 请在已绑定的话题内使用 /wake。",
    "⏳ Waking with {provider}…": "⏳ 正在使用 {provider} 恢复…",
    "✅ Topic woke and your message was sent.": "✅ 话题已唤醒，消息已发送。",
    "❌ No state exists for this window.": "❌ 此窗口没有可用状态。",
    "❌ Use /diag inside a bound topic.": "❌ 请在已绑定的话题内使用 /diag。",
    "❌ Use /replay inside a bound topic.": "❌ 请在已绑定的话题内使用 /replay。",
    "No assistant replies are available to replay.": "没有可重放的助手回复。",
    "🔁 Replay": "🔁 重放",
    "The original project directory is unavailable.": "原项目目录不可用。",
    "Unknown provider: {provider}": "未知 Provider：{provider}",
    (
        "{provider} did not become ready within {seconds} seconds. "
        "The old session was kept."
    ): "{provider} 未能在 {seconds} 秒内就绪，旧会话已保留。",
    "Switched to {provider}.": "已切换到 {provider}。",
    "Provider handoff failed. The old session was kept.": (
        "Provider 迁移失败，旧会话已保留。"
    ),
    (
        "The replacement started, but context could not be sent. "
        "The old session was kept."
    ): "新会话已启动，但上下文发送失败；旧会话已保留。",
    (
        "The old session could not be stopped, so the switch was rolled back."
    ): "旧会话无法停止，切换已回滚。",
    (
        "The replacement started, but its topic name could not be finalized. "
        "The old session was kept."
    ): "新会话已启动，但话题名称无法完成设置；旧会话已保留。",
    (
        "The provider may be expired or logged out. Switch provider or park this topic."
    ): "Provider 可能已过期或退出登录，请切换 Provider 或休眠此话题。",
    # /last
    "No command output found.": "没有找到命令输出。",
    "No reply yet.": "还没有回复。",
    # /send
    " for '{query}'": "(匹配 '{query}')",
    "🔍 {count}+ results": "🔍 {count}+ 个结果",
    "🔍 {count} result(s)": "🔍 {count} 个结果",
    "Upload failed: {error}": "上传失败:{error}",
    "Cannot send: {error}": "无法发送:{error}",
    "Cannot send: file is outside project directory": ("无法发送:文件在项目目录之外"),
    "Cannot send: file is in an excluded directory": (
        "无法发送:文件位于被排除的目录中"
    ),
    "No files found matching: {pattern}": "没有找到匹配的文件:{pattern}",
    # Daily digest
    "☀️ Daily digest — last 24h": "☀️ 每日摘要——过去 24 小时",
    "no transcript": "无 transcript",
    "no activity in 24h": "24 小时内无活动",
    "{users} prompts / {replies} replies": "{users} 条提问 / {replies} 条回复",
    # Operator alerts
    "⚠️ *CCGram startup check*": "⚠️ *CCGram 启动自检*",
    "The bot is missing permissions in chat `{chat_id}`:": "机器人在群 `{chat_id}` 缺少以下权限:",
    "• *Manage Topics* — required to auto-create and rename topics": "• *管理话题* —— 自动创建和重命名话题所必需",
    "Grant it in the group's admin settings, then it recovers automatically.": "请在群组管理员设置中授予,之后会自动恢复。",
    "🚨 *CCGram error alert*": "🚨 *CCGram 错误告警*",
    "`{count}×` in {window}s: {message}": "`{count}×` 于 {window} 秒内:{message}",
    "Can't DM the operator (`{chat_id}`): alerts are undeliverable.": "无法私信运营者(`{chat_id}`):告警无法送达。",
    "Have that account send the bot `/start`, or point "
    "`CCGRAM_OPERATOR_CHAT_ID` at a chat the bot can post to.": "请让该账号先私聊机器人发送 `/start`,或把 "
    "`CCGRAM_OPERATOR_CHAT_ID` 指向机器人可发言的会话。",
    "Until then, alerts are routed here (`{fallback}`).": "在此之前,告警将改投到这里(`{fallback}`)。",
}
