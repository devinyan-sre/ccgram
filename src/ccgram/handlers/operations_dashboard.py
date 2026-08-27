"""Callback controls for the persistent operations dashboard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update

from ..i18n import t
from ..operations_dashboard import (
    CB_DASHBOARD_ERRORS,
    CB_DASHBOARD_QUEUE,
    CB_DASHBOARD_REFRESH,
    CB_DASHBOARD_REFRESH_ALL,
    operations_dashboard_status,
    refresh_all_operations_dashboards,
    refresh_operations_dashboard,
)
from .callback_helpers import get_thread_id
from .callback_registry import register

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


@register(CB_DASHBOARD_REFRESH)
async def handle_dashboard_refresh(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    thread_id = get_thread_id(update) or 1
    refreshed = await refresh_operations_dashboard(
        query.message.chat.id, thread_id
    )
    await query.answer(t("Refreshed") if refreshed else t("Dashboard unavailable"))


@register(CB_DASHBOARD_REFRESH_ALL)
async def handle_dashboard_refresh_all(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    refreshed = await refresh_all_operations_dashboards()
    await query.answer(t("Refreshed") if refreshed else t("Dashboard unavailable"))


@register(CB_DASHBOARD_ERRORS)
async def handle_dashboard_errors(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer(
            operations_dashboard_status("errors"), show_alert=True
        )


@register(CB_DASHBOARD_QUEUE)
async def handle_dashboard_queue(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer(
            operations_dashboard_status("queue"), show_alert=True
        )


__all__ = ["handle_dashboard_refresh"]
