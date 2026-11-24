"""Bot, dispatcher, middleware, and shared service construction."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from app.config import Settings
from app.handlers import (
    common,
    extract,
    fallback,
    images,
    menu,
    merge,
    rotate,
    split,
    start,
    uploads,
)
from app.middleware.fsub import SubscriptionMiddleware
from app.middleware.user_lock import UserLockManager, UserSerializationMiddleware
from app.services.errors import KayiPdfError
from app.services.fsub import PendingActionStore, SubscriptionService
from app.services.jobs import JobManager
from app.services.workspaces import WorkspaceManager
from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class AppServices:
    settings: Settings
    workspaces: WorkspaceManager
    jobs: JobManager
    subscription: SubscriptionService
    pending: PendingActionStore
    locks: UserLockManager


def build_services(settings: Settings) -> AppServices:
    return AppServices(
        settings=settings,
        workspaces=WorkspaceManager(
            settings.temp_root,
            max_workspaces=settings.max_workspaces,
            stale_ttl=settings.stale_workspace_ttl,
            idle_ttl=settings.workspace_idle_ttl,
        ),
        jobs=JobManager(
            max_active_jobs=settings.max_active_jobs,
            max_queued_jobs=settings.max_queued_jobs,
            queue_timeout=max(120.0, settings.processing_timeout * 2),
        ),
        subscription=SubscriptionService(
            settings.fsub_channel,
            cache_ttl=settings.fsub_cache_ttl,
            cache_max=settings.fsub_cache_max,
        ),
        pending=PendingActionStore(
            ttl=settings.pending_action_ttl,
            max_entries=settings.pending_action_max,
        ),
        locks=UserLockManager(),
    )


def create_bot(settings: Settings) -> Bot:
    session = AiohttpSession(timeout=settings.telegram_request_timeout)
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )


def create_dispatcher(settings: Settings, services: AppServices) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.workflow_data.update(
        {
            "settings": settings,
            "services": services,
            "workspaces": services.workspaces,
            "jobs": services.jobs,
            "subscription": services.subscription,
            "pending": services.pending,
            "locks": services.locks,
        }
    )

    subscription_middleware = SubscriptionMiddleware(
        services.subscription, services.pending, settings
    )
    lock_middleware = UserSerializationMiddleware(services.locks)
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(subscription_middleware)
        observer.outer_middleware(lock_middleware)

    dp.include_routers(
        start.router,
        common.router,
        menu.router,
        merge.router,
        split.router,
        extract.router,
        rotate.router,
        images.router,
        uploads.router,
        fallback.router,
    )

    async def on_error(event: ErrorEvent, bot: Bot) -> None:
        exception = event.exception
        if isinstance(exception, KayiPdfError):
            message = exception.user_message
        else:
            message = "Something went wrong. Please try again or use /cancel."
        log.error(
            "Unhandled update error: %s",
            type(exception).__name__,
            exc_info=exception,
        )
        update = event.update
        try:
            if update.callback_query is not None:
                await update.callback_query.answer(message[:190], show_alert=True)
            elif update.message is not None:
                await bot.send_message(update.message.chat.id, message)
        except Exception:
            pass

    dp.errors.register(on_error)
    return dp
