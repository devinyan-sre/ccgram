"""Fitness gate: i18n catalog coverage.

Every user-facing string routed through ``t("...")`` must have a Simplified
Chinese entry in ``i18n._ZH``.  The house rule is "new user-visible strings
must be wrapped in t()" — this gate closes the other half: a wrapped string
whose translation was forgotten no longer slips through review.

Three checks:
1. Every *literal* ``t("...")`` argument in ``src/ccgram/**`` is a key in
   ``_ZH``.
2. Non-literal ``t(expr)`` calls (dynamic strings can't be checked here) are
   pinned to an explicit allowlist so new ones get a conscious review.
3. Every ``_ZH`` translation preserves the exact ``{placeholder}`` set of its
   English key — callers ``.format()`` the returned string, so a renamed or
   dropped placeholder in the translation raises ``KeyError`` at runtime.
"""

from __future__ import annotations

import ast
import string
from pathlib import Path

from ccgram.i18n import _ZH

SRC = Path(__file__).resolve().parents[2] / "src" / "ccgram"


def _is_t_call(node: ast.Call) -> bool:
    """Match ``t(...)`` (imported from i18n) and ``i18n.t(...)`` calls."""
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id == "t"
    if isinstance(fn, ast.Attribute):
        return fn.attr == "t" and (
            isinstance(fn.value, ast.Name) and fn.value.id == "i18n"
        )
    return False


def _imports_t(tree: ast.Module) -> bool:
    """True if the module imports ``t`` from the i18n module (any depth)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (
                module == "i18n" or module.endswith(".i18n") or module == "ccgram"
            ) and any(alias.name in ("t", "i18n") for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name.endswith("i18n") for alias in node.names):
                return True
    return False


def _iter_t_calls():
    """Yield (relpath, call node) for every t() call in the source tree."""
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not _imports_t(tree):
            continue
        rel = path.relative_to(SRC)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_t_call(node) and node.args:
                yield rel, node


def _placeholders(text: str) -> set[str]:
    """Named ``{placeholder}`` fields in a format string (ignores ``{{``)."""
    fields = set()
    for _, field, _, _ in string.Formatter().parse(text):
        if field:  # skip literal-only chunks and positional ''
            fields.add(field)
    return fields


def test_every_literal_t_string_has_zh_entry() -> None:
    missing: list[str] = []
    for rel, node in _iter_t_calls():
        arg = node.args[0]
        if (
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and arg.value not in _ZH
        ):
            missing.append(f"{rel}:{node.lineno}: {arg.value!r}")
    assert not missing, (
        "t() strings without a Simplified Chinese entry in i18n._ZH "
        "(add translations for each):\n" + "\n".join(missing)
    )


def test_dynamic_t_calls_are_allowlisted() -> None:
    """t(<non-literal>) can't be coverage-checked — keep the set reviewed."""
    dynamic = sorted(
        f"{rel}:{node.lineno}"
        for rel, node in _iter_t_calls()
        if not (
            isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        )
    )
    # Each allowlisted site passes a module-level constant / dict value into
    # t(); test_dynamic_site_values_are_translated verifies those values.
    allowlist = [
        "handlers/interactive/interactive_callbacks.py:137",
        "handlers/interactive/interactive_ui.py:102",
        "handlers/live/pane_callbacks.py:188",
        "handlers/sync_command.py:82",
        "handlers/text/text_handler.py:304",
        "handlers/text/text_handler.py:323",
    ]
    unexpected = [loc for loc in dynamic if loc not in allowlist]
    assert not unexpected, (
        "New dynamic t(expr) call sites found — their strings bypass the "
        "coverage gate. Prefer t('literal'); if dynamic is unavoidable, make "
        "sure every possible value is in _ZH and add the site to the "
        "allowlist in this test:\n" + "\n".join(unexpected)
    )


def test_dynamic_site_values_are_translated() -> None:
    """The constants/dict values behind allowlisted dynamic t() sites."""
    # Lazy: handler imports are heavyweight; keep module import light.
    from ccgram.handlers.interactive.interactive_callbacks import (
        INTERACTIVE_KEY_LABELS,
    )
    from ccgram.handlers.interactive.interactive_ui import (
        INTERACTIVE_INSTRUCTION_LINE,
    )
    from ccgram.handlers.live.pane_callbacks import _RENAME_PROMPT
    from ccgram.handlers.sync_command import _CATEGORY_LABELS
    from ccgram.handlers.text.text_handler import PENDING_DELIVERY_NOTICE

    values = [
        INTERACTIVE_INSTRUCTION_LINE,
        _RENAME_PROMPT,
        PENDING_DELIVERY_NOTICE,
        *INTERACTIVE_KEY_LABELS.values(),
        *_CATEGORY_LABELS.values(),
    ]
    missing = [v for v in values if v not in _ZH]
    assert not missing, (
        "Values reaching t() via allowlisted dynamic call sites lack a "
        "Simplified Chinese entry in i18n._ZH:\n" + "\n".join(repr(v) for v in missing)
    )


def test_zh_translations_preserve_placeholders() -> None:
    broken: list[str] = []
    for en, zh in _ZH.items():
        en_fields = _placeholders(en)
        zh_fields = _placeholders(zh)
        if en_fields != zh_fields:
            broken.append(f"{en!r}: en={sorted(en_fields)} zh={sorted(zh_fields)}")
    assert not broken, (
        "_ZH translations whose {placeholders} differ from the English key "
        "(callers .format() the returned string):\n" + "\n".join(broken)
    )
