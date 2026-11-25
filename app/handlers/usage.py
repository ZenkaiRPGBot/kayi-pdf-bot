"""System commands (/ping, /usage) and Telegram command menu registration."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import BRAND_NAME, Settings
from app.services.jobs import JobManager
from app.services.workspaces import WorkspaceManager
from app.utils.logging import get_logger
from app.utils.telegram import safe_answer_callback, safe_edit_text
from app.utils.text import escape

log = get_logger(__name__)
router = Router(name="usage")

BOT_START_TIME = datetime.now(timezone.utc)
_START_MONO = time.monotonic()


def _cpu_seconds() -> float:
    try:
        t = os.times()
        return float(t.user + t.system)
    except Exception:
        return 0.0


_CPU_SAMPLE: tuple[float, float] = (_START_MONO, _cpu_seconds())


def is_owner(user_id: int | None, settings: Settings) -> bool:
    """Fail-closed owner authorization check."""
    return bool(settings.owner_id and user_id and user_id == settings.owner_id)


def get_uptime_string(started_at: float | None = None) -> str:
    elapsed = max(
        0,
        int(time.monotonic() - (started_at if started_at is not None else _START_MONO)),
    )
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def get_process_memory_mb() -> float | None:
    """Return actual process RSS memory in MB using stdlib / OS interfaces."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / (1024.0 * 1024.0)
        except Exception:
            pass
    else:
        try:
            import resource

            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if sys.platform == "darwin":
                return rss_kb / (1024.0 * 1024.0)
            return rss_kb / 1024.0
        except Exception:
            pass

    return None


def get_process_cpu_display() -> str:
    """Return real process CPU usage on demand without blocking the event loop."""
    global _CPU_SAMPLE
    now_mono = time.monotonic()
    now_cpu = _cpu_seconds()
    prev_mono, prev_cpu = _CPU_SAMPLE
    wall_delta = now_mono - prev_mono
    cpu_delta = max(0.0, now_cpu - prev_cpu)
    _CPU_SAMPLE = (now_mono, now_cpu)
    if wall_delta >= 0.05:
        pct = min(100.0, (cpu_delta / wall_delta) * 100.0)
        return f"{pct:.1f}% ({now_cpu:.1f}s total)"
    return f"{now_cpu:.1f}s total"


async def measure_gateway_latency(bot: Bot) -> float | None:
    t0 = time.perf_counter()
    try:
        await bot.get_me()
    except Exception:
        return None
    return (time.perf_counter() - t0) * 1000.0


def _build_ping_message(bot_ms: float, api_ms: float | None) -> tuple[str, InlineKeyboardMarkup]:
    uptime = get_uptime_string()
    api_str = f"{api_ms:.1f} ms" if api_ms is not None else "Unavailable"
    text = (
        "<b>Pong!</b> ⚡\n\n"
        f"<b>Bot Responsiveness:</b> <code>{bot_ms:.2f} ms</code>\n"
        f"<b>Telegram API Latency:</b> <code>{api_str}</code>\n\n"
        f"<b>Uptime:</b> <code>{uptime}</code>"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Check Ping",
                    callback_data="ping:refresh",
                    style="primary",
                )
            ]
        ]
    )
    return text, markup


async def build_usage_card_text(
    bot: Bot,
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> tuple[str, InlineKeyboardMarkup]:
    uptime_str = get_uptime_string()
    session_hours = max(0.0, (time.monotonic() - _START_MONO) / 3600.0)
    total_used_hours = min(750.0, settings.render_used_hours + session_hours)
    remaining_hours = max(0.0, 750.0 - total_used_hours)
    used_pct = min(100.0, (total_used_hours / 750.0) * 100.0)
    days_left_capacity = remaining_hours / 24.0

    mem_mb = get_process_memory_mb()
    if mem_mb is not None:
        mem_pct = min(100.0, (mem_mb / 512.0) * 100.0)
        ram_display = f"{mem_mb:.1f} MB / 512 MB ({mem_pct:.1f}%)"
    else:
        ram_display = "Unavailable"

    cpu_display = get_process_cpu_display()
    temp_bytes, temp_files = workspaces.disk_usage()
    temp_mb = temp_bytes / (1024.0 * 1024.0)

    api_ms = await measure_gateway_latency(bot)
    ping_display = f"{api_ms:.1f} ms" if api_ms is not None else "Unavailable"

    job_stats = jobs.stats
    email_display = settings.render_email or "Not Set"

    text = (
        f"<b>{escape(BRAND_NAME)} — Host &amp; Service Metrics</b>\n\n"
        "<b>Render Instance &amp; Account:</b>\n"
        "• <b>Cloud:</b> Render (Frankfurt, EU-Central)\n"
        f"• <b>Account Email:</b> <code>{escape(email_display)}</code>\n"
        f"• <b>Service Name:</b> <code>{escape(settings.render_service_name)}</code>\n"
        f"• <b>Repository:</b> <code>{escape(settings.render_repo)}</code>\n"
        f"• <b>Bot Uptime:</b> <code>{uptime_str}</code>\n"
        f"• <b>Monthly Hours (750h):</b> <code>{total_used_hours:.1f}h / 750h ({used_pct:.1f}%)</code>\n"
        f"• <b>Remaining Free:</b> <code>{remaining_hours:.1f}h (~{days_left_capacity:.1f} days)</code>\n\n"
        "<b>Process Resources (On-Demand):</b>\n"
        f"• <b>Process CPU:</b> <code>{escape(cpu_display)}</code>\n"
        f"• <b>Process RAM:</b> <code>{escape(ram_display)}</code>\n"
        f"• <b>Temp Storage:</b> <code>{temp_mb:.2f} MB ({temp_files} files)</code>\n"
        f"• <b>Workspaces:</b> <code>{workspaces.count} / {settings.max_workspaces}</code>\n\n"
        "<b>PDF Engine &amp; Limits:</b>\n"
        f"• <b>Active Jobs:</b> <code>{job_stats['active']} / {settings.max_active_jobs}</code>\n"
        f"• <b>Queued Jobs:</b> <code>{job_stats['queued']} / {settings.max_queued_jobs}</code>\n"
        f"• <b>File / Page Cap:</b> <code>{settings.max_file_size_mb} MB / {settings.max_pages} pages</code>\n"
        f"• <b>Telegram API Ping:</b> <code>{ping_display}</code>"
    )

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Refresh Metrics",
                    callback_data="usage:refresh",
                    style="primary",
                )
            ]
        ]
    )
    return text, markup


@router.message(Command("ping"))
async def ping_handler(message: Message, bot: Bot) -> None:
    t0 = time.perf_counter()
    api_ms = await measure_gateway_latency(bot)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    bot_ms = max(0.01, elapsed_ms - (api_ms or elapsed_ms))
    text, markup = _build_ping_message(bot_ms, api_ms)
    await message.reply(text, reply_markup=markup)


@router.callback_query(F.data == "ping:refresh")
async def ping_refresh_callback(callback: CallbackQuery, bot: Bot) -> None:
    t0 = time.perf_counter()
    api_ms = await measure_gateway_latency(bot)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    bot_ms = max(0.01, elapsed_ms - (api_ms or elapsed_ms))
    text, markup = _build_ping_message(bot_ms, api_ms)
    if callback.message is not None:
        await safe_edit_text(callback.message, text, reply_markup=markup)
    await safe_answer_callback(callback, "Ping refreshed")


@router.message(Command("usage"))
@router.message(Command("render"))
async def usage_handler(
    message: Message,
    bot: Bot,
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_owner(user_id, settings):
        await message.reply("Access restricted to bot owner.")
        return
    text, markup = await build_usage_card_text(bot, settings, jobs, workspaces)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "usage:refresh")
async def usage_refresh_callback(
    callback: CallbackQuery,
    bot: Bot,
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    if not is_owner(user_id, settings):
        await safe_answer_callback(
            callback, "Access restricted to bot owner.", show_alert=True
        )
        return
    text, markup = await build_usage_card_text(bot, settings, jobs, workspaces)
    if callback.message is not None:
        await safe_edit_text(callback.message, text, reply_markup=markup)
    await safe_answer_callback(callback, "Metrics refreshed")


async def register_bot_commands(bot: Bot, settings: Settings) -> None:
    """Register public slash commands globally and owner-only commands cleanly."""
    if not settings.bot_token or not callable(getattr(bot, "set_my_commands", None)):
        return
    public_commands = [
        BotCommand(command="start", description="Start bot & PDF tools menu"),
        BotCommand(command="help", description="How to use PDF tools & limits"),
        BotCommand(command="ping", description="Check bot latency and uptime"),
        BotCommand(command="cancel", description="Cancel the current PDF task"),
    ]
    owner_commands = public_commands + [
        BotCommand(
            command="usage",
            description="Render hosting & service usage (Owner)",
        )
    ]
    try:
        await bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
        if settings.owner_id:
            try:
                await bot.set_my_commands(
                    owner_commands,
                    scope=BotCommandScopeChat(chat_id=settings.owner_id),
                )
            except Exception as exc:
                log.warning(
                    "Could not register owner commands for %s: %s",
                    settings.owner_id,
                    type(exc).__name__,
                )
        log.info(
            "Registered Telegram bot commands (/start, /help, /ping, /cancel%s)",
            ", /usage" if settings.owner_id else "",
        )
    except Exception as exc:
        log.warning("Could not register default bot commands: %s", type(exc).__name__)
