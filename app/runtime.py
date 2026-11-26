"""Telegram webhook runtime: bounded ingress, workers, and lifecycle tasks."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import ClientSession, ClientTimeout

from app.bot import AppServices, build_services, create_bot, create_dispatcher
from app.config import APP_NAME, APP_VERSION, Settings
from app.handlers.usage import register_bot_commands
from app.services.restart_notify import build_restart_notifier
from app.services.webhook import IngressStatus, UpdateIngress
from app.utils.logging import get_logger

log = get_logger(__name__)

_ALLOWED_UPDATES = ["message", "callback_query"]
_WEBHOOK_RETRY_MAX = 300.0
_WEBHOOK_RETRY_START = 30.0


class TelegramRuntime:
    def __init__(self, settings: Settings, services: AppServices | None = None) -> None:
        self.settings = settings
        self.services = services or build_services(settings)
        self.bot: Bot = create_bot(settings)
        self.dispatcher: Dispatcher = create_dispatcher(settings, self.services)
        self.ingress = UpdateIngress(
            queue_size=settings.webhook_queue_size,
            dedup_ttl=settings.update_dedup_ttl,
            dedup_max=settings.update_dedup_max,
        )
        self.started_at = time.monotonic()
        self.ready = False
        self.webhook_registered = False
        self.restart_notifier = build_restart_notifier(self.bot, settings)
        self._stopping = asyncio.Event()
        self._update_semaphore = asyncio.Semaphore(settings.max_concurrent_updates)
        self._update_tasks: set[asyncio.Task[Any]] = set()
        self._background: list[asyncio.Task[Any]] = []

    async def start(self) -> None:
        if not self.settings.bot_token:
            raise RuntimeError("BOT_TOKEN is required to start Kayi PDF Bot")
        removed = self.services.workspaces.cleanup_orphans(max_age_seconds=0)
        if removed:
            log.info("Removed %d stale workspace(s) from a previous run", removed)
        self.started_at = time.monotonic()
        self.ready = True
        self._background.append(
            asyncio.create_task(self._consume_updates(), name="kayi-update-consumer")
        )
        self._background.append(
            asyncio.create_task(
                self._workspace_cleanup_loop(), name="kayi-workspace-cleanup"
            )
        )
        self._background.append(
            asyncio.create_task(
                self._notify_restart(), name="kayi-restart-notification"
            )
        )
        self._background.append(
            asyncio.create_task(
                self._register_commands(), name="kayi-command-register"
            )
        )
        if self.settings.webhook_auto_set and self.settings.public_base_url:
            self._background.append(
                asyncio.create_task(
                    self._register_webhook_loop(), name="kayi-webhook-register"
                )
            )
        elif self.settings.webhook_auto_set:
            log.warning("PUBLIC_BASE_URL is not set; Telegram webhook was not registered")
        if self.settings.enable_self_ping:
            ping_url = self.settings.ping_url
            if not ping_url:
                render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
                if render_url:
                    ping_url = render_url.rstrip("/") + "/ping"
                    log.info("PING_URL was empty; using RENDER_EXTERNAL_URL fallback")
            if ping_url:
                self.settings = self.settings.with_ping_url(ping_url) if hasattr(self.settings, "with_ping_url") else self.settings
                self._ping_url = ping_url
                self._background.append(
                    asyncio.create_task(self._self_ping_loop(), name="kayi-self-ping")
                )
            else:
                log.warning("ENABLE_SELF_PING is true but PING_URL and RENDER_EXTERNAL_URL are empty")
        log.info(
            "Kayi PDF Bot runtime started (queue=%d, active_jobs=%d)",
            self.ingress.capacity,
            self.services.jobs.max_active_jobs,
        )

    async def stop(self) -> None:
        if not self.ready:
            return
        self.ready = False
        self._stopping.set()
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        self._background.clear()
        tasks = list(self._update_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._update_tasks.clear()
        try:
            await self.services.workspaces.cleanup_all()
        except Exception:
            log.warning("Workspace cleanup during shutdown failed")
        try:
            await self.dispatcher.fsm.close()
        except Exception:
            pass
        try:
            await self.bot.session.close()
        except Exception:
            pass
        log.info("Kayi PDF Bot runtime stopped")

    async def ingest(self, payload: object) -> IngressStatus:
        if self._stopping.is_set() or not self.ready:
            return IngressStatus.FULL
        return self.ingress.ingest(payload)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.ready else "starting",
            "service": APP_NAME,
            "version": APP_VERSION,
            "uptime_seconds": int(max(0, time.monotonic() - self.started_at)),
            "webhook_registered": self.webhook_registered,
            "queue": {
                "size": self.ingress.size,
                "capacity": self.ingress.capacity,
            },
            "jobs": self.services.jobs.stats,
        }

    async def _consume_updates(self) -> None:
        while not self._stopping.is_set():
            try:
                payload = await self.ingress.queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._update_semaphore.acquire()
            except asyncio.CancelledError:
                self.ingress.queue.task_done()
                return
            try:
                task = asyncio.create_task(self._process_payload(payload))
            except Exception:
                self._update_semaphore.release()
                self.ingress.queue.task_done()
                raise
            self._update_tasks.add(task)
            task.add_done_callback(self._update_tasks.discard)
            self.ingress.queue.task_done()

    async def _process_payload(self, payload: dict) -> None:
        try:
            update = Update.model_validate(payload, context={"bot": self.bot})
            await self.dispatcher.feed_update(self.bot, update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "Failed to process a webhook update: %s",
                type(exc).__name__,
                exc_info=exc,
            )
        finally:
            self._update_semaphore.release()

    async def _notify_restart(self) -> None:
        try:
            await self.restart_notifier.notify_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Restart notification task failed")

    async def _register_commands(self) -> None:
        try:
            await register_bot_commands(self.bot, self.settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Command menu registration task failed")

    async def _workspace_cleanup_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(self.settings.workspace_cleanup_interval)
            except asyncio.CancelledError:
                return
            try:
                pruned = self.services.workspaces.prune_stale()
                orphans = self.services.workspaces.cleanup_orphans(
                    self.settings.stale_workspace_ttl
                )
                if pruned or orphans:
                    log.info(
                        "Workspace cleanup removed %d active and %d orphan director(ies)",
                        pruned,
                        orphans,
                    )
            except Exception as exc:
                log.warning("Workspace cleanup failed: %s", type(exc).__name__)

    async def _register_webhook_loop(self) -> None:
        if not self.settings.webhook_url or not self.settings.webhook_secret:
            log.warning("Webhook URL or secret is not configured; registration skipped")
            return
        delay = _WEBHOOK_RETRY_START
        while not self._stopping.is_set():
            try:
                await self.bot.set_webhook(
                    url=self.settings.webhook_url,
                    secret_token=self.settings.webhook_secret,
                    allowed_updates=_ALLOWED_UPDATES,
                    drop_pending_updates=self.settings.drop_pending_updates,
                )
                self.webhook_registered = True
                log.info("Telegram webhook registered")
                return
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning(
                    "Webhook registration failed (%s); retrying in %.0fs",
                    type(exc).__name__,
                    delay,
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                delay = min(delay * 2, _WEBHOOK_RETRY_MAX)

    async def _self_ping_loop(self) -> None:
        try:
            await asyncio.sleep(self.settings.self_ping_initial_delay)
        except asyncio.CancelledError:
            return
        timeout = ClientTimeout(total=10)
        ping_url = getattr(self, "_ping_url", self.settings.ping_url)
        async with ClientSession(timeout=timeout) as session:
            while not self._stopping.is_set():
                try:
                    async with session.get(ping_url) as response:
                        log.info("Self-ping returned HTTP %d", response.status)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    log.warning("Self-ping failed: %s", type(exc).__name__)
                try:
                    await asyncio.sleep(self.settings.self_ping_interval)
                except asyncio.CancelledError:
                    return
