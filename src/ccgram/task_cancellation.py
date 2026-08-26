"""Provider-neutral graceful and forced task cancellation orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os

from . import window_query
from .config import config
from .inbound_store import inbound_store
from .multiplexer import multiplexer
from .providers.shell_infra import KNOWN_SHELLS
from .request_context import clear_window
from .task_audit import record_task_audit
from .task_scheduler import CancelRequest, task_scheduler


@dataclass(frozen=True, slots=True)
class CancellationResult:
    status: str
    task_id: str = ""
    window_id: str = ""
    owner_user_id: int = 0


def _audit(
    action: str,
    request: CancelRequest,
    *,
    requester_user_id: int,
    chat_id: int,
    thread_id: int,
    detail: str = "",
) -> None:
    record_task_audit(
        action,
        task_id=request.task_id,
        requester_user_id=requester_user_id,
        owner_user_id=request.user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=request.window_id,
        detail=detail,
    )


async def _cli_is_idle(window_id: str) -> bool:
    window = await multiplexer.find_window_by_id(window_id)
    if window is None:
        return True
    if multiplexer.capabilities.native_agent_status:
        status = await multiplexer.agent_status(window_id)
        if status is not None:
            return status.state in ("idle", "done")
    provider = window_query.get_window_provider(window_id) or ""
    if provider != "shell":
        return False
    foreground = await multiplexer.foreground(window_id)
    if foreground is None or len(foreground.argv) != 1:
        return False
    return os.path.basename(foreground.argv[0]).lstrip("-") in KNOWN_SHELLS


async def graceful_cancel(
    *,
    chat_id: int,
    thread_id: int,
    requester_user_id: int,
    task_id: str | None = None,
    allow_any: bool = False,
) -> CancellationResult:
    """Request Ctrl+C and release only after an observable stop signal."""
    request = await task_scheduler.request_cancel(
        chat_id=chat_id,
        thread_id=thread_id,
        requester_user_id=requester_user_id,
        task_id=task_id,
        allow_any=allow_any,
    )
    if request.status in ("not_found", "forbidden"):
        _audit(
            f"cancel_{request.status}",
            request,
            requester_user_id=requester_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return CancellationResult(request.status, request.task_id)
    if request.status == "queued":
        _audit(
            "queued_cancelled",
            request,
            requester_user_id=requester_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return CancellationResult(
            "queued_cancelled", request.task_id, owner_user_id=request.user_id
        )

    _audit(
        "cancel_requested",
        request,
        requester_user_id=requester_user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        detail="retry" if request.status == "already_cancelling" else "",
    )
    sent = await multiplexer.send_keys(
        request.window_id, "C-c", enter=False, literal=False
    )
    if not sent:
        _audit(
            "interrupt_failed",
            request,
            requester_user_id=requester_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return CancellationResult(
            "interrupt_failed",
            request.task_id,
            request.window_id,
            request.user_id,
        )

    deadline = asyncio.get_running_loop().time() + config.task_cancel_confirm_seconds
    while asyncio.get_running_loop().time() < deadline:
        current = next(
            (
                view
                for view in task_scheduler.views()
                if view.task_id == request.task_id
            ),
            None,
        )
        if current is None or await _cli_is_idle(request.window_id):
            if current is not None:
                await task_scheduler.confirm_cancel(request.task_id)
            inbound_store.mark_window_done(request.window_id, failed=True)
            clear_window(request.window_id)
            _audit(
                "cancel_confirmed",
                request,
                requester_user_id=requester_user_id,
                chat_id=chat_id,
                thread_id=thread_id,
            )
            return CancellationResult(
                "cancel_confirmed",
                request.task_id,
                request.window_id,
                request.user_id,
            )
        await asyncio.sleep(0.5)

    _audit(
        "cancel_timeout",
        request,
        requester_user_id=requester_user_id,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    return CancellationResult(
        "cancel_timeout", request.task_id, request.window_id, request.user_id
    )


async def force_cancel(
    *,
    chat_id: int,
    thread_id: int,
    requester_user_id: int,
    task_id: str,
) -> CancellationResult:
    """Admin-only controller: kill the CLI window but preserve its binding."""
    request = await task_scheduler.request_cancel(
        chat_id=chat_id,
        thread_id=thread_id,
        requester_user_id=requester_user_id,
        task_id=task_id,
        allow_any=True,
    )
    if request.status == "not_found":
        _audit(
            "force_cancel_not_found",
            request,
            requester_user_id=requester_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return CancellationResult("not_found")
    if request.status == "queued":
        _audit(
            "queued_force_cancelled",
            request,
            requester_user_id=requester_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return CancellationResult(
            "queued_force_cancelled", request.task_id, owner_user_id=request.user_id
        )
    _audit(
        "force_cancel_requested",
        request,
        requester_user_id=requester_user_id,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    window = await multiplexer.find_window_by_id(request.window_id)
    if window is not None and not await multiplexer.kill_window(request.window_id):
        _audit(
            "force_cancel_failed",
            request,
            requester_user_id=requester_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return CancellationResult(
            "force_cancel_failed",
            request.task_id,
            request.window_id,
            request.user_id,
        )
    await task_scheduler.confirm_cancel(request.task_id, forced=True)
    inbound_store.mark_window_done(request.window_id, failed=True)
    clear_window(request.window_id)
    _audit(
        "force_cancelled",
        request,
        requester_user_id=requester_user_id,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    return CancellationResult(
        "force_cancelled", request.task_id, request.window_id, request.user_id
    )


__all__ = ["CancellationResult", "force_cancel", "graceful_cancel"]
