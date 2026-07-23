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
}
