"""Webhook runtime lifecycle, bounded ingress, and health state."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.bot import build_services
from app.config import APP_NAME, APP_VERSION, Settings
from app.runtime import TelegramRuntime
from app.services.webhook import IngressStatus


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeFSM:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.webhooks: list[dict] = []
        self.id = 1

    async def set_webhook(self, **kwargs):
        self.webhooks.append(kwargs)
        return True


class FakeDispatcher:
    def __init__(self) -> None:
        self.fsm = FakeFSM()
        self.fed: list = []

    async def feed_update(self, bot, update):
        self.fed.append(update)
        return True


def make_runtime(monkeypatch, settings: Settings):
    bot = FakeBot()
    dispatcher = FakeDispatcher()
    monkeypatch.setattr("app.runtime.create_bot", lambda value: bot)
    monkeypatch.setattr(
        "app.runtime.create_dispatcher", lambda value, services: dispatcher
    )
    runtime = TelegramRuntime(settings, services=build_services(settings))
    return runtime, bot, dispatcher


def update_payload(update_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 7, "is_bot": False, "first_name": "Test"},
            "text": "hello",
        },
    }


@pytest.mark.asyncio
async def test_runtime_starts_processes_and_stops(monkeypatch, tmp_path) -> None:
    settings = Settings(
        bot_token="123:test",
        webhook_secret="secret",
        temp_root=tmp_path / "runtime",
        webhook_auto_set=False,
    )
    runtime, bot, dispatcher = make_runtime(monkeypatch, settings)
    assert runtime.health()["status"] == "starting"
    assert await runtime.ingest(update_payload(1)) is IngressStatus.FULL

    await runtime.start()
    try:
        health = runtime.health()
        assert health["status"] == "ok"
        assert health["service"] == APP_NAME
        assert health["version"] == APP_VERSION
        assert health["queue"] == {"size": 0, "capacity": settings.webhook_queue_size}
        assert await runtime.ingest(update_payload(1)) is IngressStatus.ACCEPTED
        assert await runtime.ingest(update_payload(1)) is IngressStatus.DUPLICATE
        for _ in range(100):
            if dispatcher.fed:
                break
            await asyncio.sleep(0.01)
        assert len(dispatcher.fed) == 1
        assert dispatcher.fed[0].update_id == 1
        assert await runtime.ingest({"update_id": "bad"}) is IngressStatus.INVALID
    finally:
        await runtime.stop()
    assert runtime.ready is False
    assert bot.session.closed is True
    assert dispatcher.fsm.closed is True
    assert await runtime.ingest(update_payload(2)) is IngressStatus.FULL


@pytest.mark.asyncio
async def test_runtime_registers_webhook(monkeypatch, tmp_path) -> None:
    settings = Settings(
        bot_token="123:test",
        webhook_secret="secret",
        public_base_url="https://kayi.example",
        temp_root=tmp_path / "runtime",
        webhook_auto_set=True,
    )
    runtime, bot, _ = make_runtime(monkeypatch, settings)
    await runtime.start()
    try:
        for _ in range(100):
            if bot.webhooks:
                break
            await asyncio.sleep(0.01)
        assert bot.webhooks == [
            {
                "url": "https://kayi.example/telegram/webhook",
                "secret_token": "secret",
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
            }
        ]
        assert runtime.webhook_registered is True
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_without_public_url_skips_registration(monkeypatch, tmp_path) -> None:
    settings = Settings(
        bot_token="123:test",
        webhook_secret="secret",
        temp_root=tmp_path / "runtime",
        webhook_auto_set=True,
    )
    runtime, bot, _ = make_runtime(monkeypatch, settings)
    await runtime.start()
    try:
        await asyncio.sleep(0.05)
        assert bot.webhooks == []
        assert runtime.webhook_registered is False
        assert runtime.health()["status"] == "ok"
    finally:
        await runtime.stop()
