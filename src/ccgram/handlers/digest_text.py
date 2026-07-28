"""Pure text analysis for the daily digest — turn classification + keywords.

Split out from ``daily_digest`` so the parsing rules and the keyword
extractor are testable without a bot, a job queue, or a transcript on disk.
No I/O, no Telegram, no config.

Two jobs:

1. **Tell conversation apart from tool traffic.** A transcript's ``user``
   entries are not all prompts — a tool result is fed back as a ``user``
   entry too, and slash commands expand into synthetic ``user`` text. On a
   real working day those outnumber genuine prompts ~20:1, so counting raw
   entry types reports tool traffic, not activity anyone recognises.
2. **Summarise what a topic was about.** Keyword extraction is deliberately
   dependency-free: Chinese has no word boundaries, and pulling in a
   segmenter for one daily message is not worth it. Character n-grams plus a
   function-character filter get close enough for a digest line.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Synthetic user turns injected by the CLI or the harness, not typed by anyone.
_SYNTHETIC_PREFIXES = (
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<user-prompt-submit-hook>",
    "<system-reminder>",
    "<task-notification>",
    "caveat: the messages below",
)

# The telegram channel plugin (claude-ops) wraps a real message in a <channel>
# envelope carrying chat/user metadata. The prompt is the payload, not the tag —
# without unwrapping, a digest reports "channel · chat_id · plugin" as themes.
_CHANNEL_ENVELOPE = re.compile(r"<channel\b[^>]*>(?P<body>.*?)</channel>", re.S | re.I)

# ccgram's own reply-quote feature (text_handler._compose_with_quote) prepends
# the quoted message. That text is *previous* output being echoed back, so
# counting it would let yesterday's words dominate today's themes.
_REPLY_QUOTE = re.compile(
    r'^\[Replying to this earlier message:\]\s*"""\s*.*?\s*"""\s*', re.S
)

# Noise stripped before tokenising: fenced code, inline code, URLs, paths,
# and long hex/uuid blobs. All of it is either unreadable as a keyword or
# would dominate the frequency count of a technical conversation.
_STRIP_PATTERNS = (
    re.compile(r"```.*?```", re.S),
    re.compile(r"`[^`]*`"),
    re.compile(r"https?://\S+"),
    re.compile(r"[~/]?(?:[\w.-]+/){2,}[\w.-]*"),
    re.compile(r"\b[0-9a-f]{8,}\b", re.I),
)

_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{1,}")
_CJK_RUN = re.compile(r"[一-鿿]+")

# Characters that carry grammar rather than meaning. An n-gram built only
# from these is structural filler ("这个", "是否", "可以"), so it is dropped
# without maintaining an exhaustive stopword list.
_FUNCTION_CHARS = set(
    "的了是在我你他她它们有和与就不这那个些也都要会能到对以可么什怎样"
    "吗呢吧啊被把给让从向还很太更再又只等而且或并之于否"
)

# A content term does not begin with a particle or a measure word, nor end
# with one. Without this, "10个群" yields the term "个群" — segmentation
# debris that makes the digest look careless.
_CANNOT_START = set("的了吗呢吧啊个些们之而且或并也都就还很太更再又只被把给让从向以么")
_CANNOT_END = set(
    "的了在和与就不也都要会能把被给让从向而且或并之很太更再又只我你他这那个些们"
)

# High-frequency fillers that survive the character rules because they are
# built from content characters ("一下", "时候"). An explicit list beats
# widening _CANNOT_END: banning a trailing 下 would also kill 下载 and 线下.
_CJK_STOPWORDS = frozenset(
    [
        "一下",
        "一些",
        "一样",
        "一点",
        "一直",
        "一起",
        "这么",
        "那么",
        "什么",
        "怎么",
        "时候",
        "可能",
        "应该",
        "已经",
        "现在",
        "然后",
        "因为",
        "所以",
        "但是",
        "如果",
        "就是",
        "不是",
        "没有",
        "还是",
        "这里",
        "那里",
        "目前",
        "当前",
        "这种",
        "那种",
        "可以",
        "是否",
        "需要",
        "我们",
        "你们",
        "他们",
        "自己",
        "这样",
        "那样",
        "之后",
        "之前",
        "的话",
        "而言",
        "方面",
        "情况",
    ]
)

_ASCII_STOPWORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "doing",
        "have",
        "has",
        "had",
        "having",
        "will",
        "would",
        "shall",
        "should",
        "can",
        "could",
        "may",
        "might",
        "must",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "into",
        "out",
        "up",
        "down",
        "over",
        "under",
        "again",
        "further",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "too",
        "very",
        "just",
        "now",
        "also",
        "get",
        "got",
        "make",
        "made",
        "use",
        "used",
        "using",
        "like",
        "want",
        "need",
        "see",
        "look",
        "know",
        "think",
        "say",
        "said",
        "one",
        "two",
        "three",
        "yes",
        "ok",
        "okay",
        "please",
        "thanks",
        "let",
        "its",
        "it",
        "http",
        "https",
        "www",
        "com",
        "cn",
        "org",
        "net",
        "html",
        "json",
        "yaml",
        "toml",
    ]
)

# Minimum times a term must appear before it counts as a theme rather than
# a one-off mention.
_MIN_COUNT = 2
_MAX_NGRAM = 4
_MIN_NGRAM = 2


@dataclass(frozen=True)
class DigestStats:
    """What one topic did over the digest window."""

    prompts: int = 0
    replies: int = 0
    errors: int = 0
    tools: tuple[tuple[str, int], ...] = ()
    keywords: tuple[str, ...] = field(default=())

    @property
    def is_empty(self) -> bool:
        """True when nothing worth reporting happened."""
        return not (self.prompts or self.replies)


def _is_error_flag(value: Any) -> bool:
    """Truthiness for ``is_error``, which arrives as a bool *or* the string 'True'."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


def _blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Content blocks of an entry, or an empty list for string/absent content."""
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def analyze(
    entries: Iterable[dict[str, Any]], *, keyword_limit: int = 5
) -> DigestStats:
    """Summarise transcript entries already filtered to the digest window.

    Pure: callers do the file reading and the timestamp filtering, so this
    stays testable with a handful of literal dicts.
    """
    prompts: list[str] = []
    replies = errors = 0
    tools: Counter[str] = Counter()

    for entry in entries:
        prompt = extract_user_prompt(entry)
        if prompt is not None:
            prompts.append(prompt)
        elif is_text_reply(entry):
            replies += 1
        for block in _blocks(entry):
            kind = block.get("type")
            if kind == "tool_use":
                name = block.get("name")
                if isinstance(name, str) and name:
                    tools[name] += 1
            elif kind == "tool_result" and _is_error_flag(block.get("is_error")):
                errors += 1

    return DigestStats(
        prompts=len(prompts),
        replies=replies,
        errors=errors,
        tools=tuple(tools.most_common()),
        keywords=tuple(extract_keywords(prompts, keyword_limit)),
    )


def extract_user_prompt(entry: dict[str, Any]) -> str | None:
    """Return the text a human actually typed, or None for anything else.

    A ``user`` entry whose content is a list is a tool result being fed back,
    not a prompt. A string starting with one of the CLI's synthetic markers is
    slash-command plumbing.
    """
    if entry.get("type") != "user":
        return None
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.lower().startswith(_SYNTHETIC_PREFIXES):
        return None

    envelope = _CHANNEL_ENVELOPE.search(text)
    if envelope is not None:
        text = envelope.group("body").strip()
    text = _REPLY_QUOTE.sub("", text).strip()
    return text or None


def is_text_reply(entry: dict[str, Any]) -> bool:
    """True for an assistant turn that said something, not just called a tool.

    A turn carrying only ``tool_use`` blocks is machinery: it has no content a
    person read, so counting it as a "reply" inflates the number severalfold.
    """
    if entry.get("type") != "assistant":
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text", "")).strip():
            return True
    return False


def _clean(text: str) -> str:
    """Strip code, URLs, paths and hex blobs before tokenising."""
    for pattern in _STRIP_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def _is_filler(term: str) -> bool:
    """True when a CJK n-gram is grammar rather than a term.

    Two rejections: an n-gram made only of function characters ("这个",
    "是否"), and one whose boundary betrays a bad cut ("个群" from "10个群").
    """
    if term in _CJK_STOPWORDS:
        return True
    if all(ch in _FUNCTION_CHARS for ch in term):
        return True
    return term[0] in _CANNOT_START or term[-1] in _CANNOT_END


def _count_terms(texts: list[str]) -> Counter[str]:
    """Frequency-count ASCII words and CJK n-grams across *texts*."""
    counts: Counter[str] = Counter()
    for raw in texts:
        text = _clean(raw)
        for match in _ASCII_TOKEN.finditer(text):
            token = match.group().lower().strip(".-+_")
            if len(token) > 1 and token not in _ASCII_STOPWORDS:
                counts[token] += 1
        for run in _CJK_RUN.findall(text):
            for size in range(_MIN_NGRAM, _MAX_NGRAM + 1):
                for i in range(len(run) - size + 1):
                    term = run[i : i + size]
                    if not _is_filler(term):
                        counts[term] += 1
    return counts


def _drop_subsumed(terms: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Drop short n-grams that only ever occur inside a longer kept one.

    "熔断" and "熔断器" both surface from the same sentences; reporting both
    wastes a slot in a five-item list. The longer term is kept when the
    shorter one adds no occurrences of its own.
    """
    kept: list[tuple[str, int]] = []
    for term, count in terms:
        subsumed = any(
            term != longer and term in longer and count <= longer_count
            for longer, longer_count in terms
            if len(longer) > len(term)
        )
        if not subsumed:
            kept.append((term, count))
    return kept


def extract_keywords(texts: list[str], limit: int = 5) -> list[str]:
    """Top recurring terms across *texts*, most distinctive first.

    Returns an empty list when nothing recurs — a digest line with no themes
    is better than one padded with single mentions.
    """
    if not texts:
        return []
    counts = _count_terms(texts)
    candidates = [(term, n) for term, n in counts.items() if n >= _MIN_COUNT]
    if not candidates:
        return []
    # Sort before subsumption so the longer-term preference sees a stable order:
    # by count, then by length (a more specific term wins a tie).
    candidates.sort(key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    return [term for term, _ in _drop_subsumed(candidates)[:limit]]
