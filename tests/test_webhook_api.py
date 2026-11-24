"""FastAPI health endpoints and authenticated webhook endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.webhook import IngressStatus

SECRET = "webhook-secret"
HEADER = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


class FakeRuntime:
    def __init__(
        self, status: IngressStatus = IngressStatus.ACCEPTED, ready: bool = True
    ) -> None:
        self.status = status
        self.ready = ready
        self.started = False
        self.stopped = False
        self.payloads: list[object] = []
        self.health_calls = 0

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def ingest(self, payload: object) -> IngressStatus:
        self.payloads.append(payload)
        return self.status

    def health(self) -> dict[str, Any]:
        self.health_calls += 1
        return {
            "status": "ok",
            "queue": {"size": 0, "capacity": 10},
        }


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="123:test",
        webhook_secret=SECRET,
        temp_root=tmp_path / "temp",
    )


def make_client(settings: Settings, runtime: FakeRuntime) -> TestClient:
    return TestClient(create_app(settings, runtime=runtime))


def test_health_and_ping_do_not_touch_telegram(api_settings) -> None:
    runtime = FakeRuntime()
    with make_client(api_settings, runtime) as client:
        assert runtime.started is True
        root = client.get("/")
        assert root.status_code == 200
        assert root.json()["service"] == "kayi-pdf-bot"
        ping = client.get("/ping")
        assert ping.status_code == 200
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert runtime.health_calls == 1
        assert health.headers["X-Content-Type-Options"] == "nosniff"
        assert health.headers["Cache-Control"] == "no-store"
    assert runtime.stopped is True


def test_health_unavailable_before_runtime_is_ready(api_settings) -> None:
    runtime = FakeRuntime(ready=False)
    with make_client(api_settings, runtime) as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"
        assert runtime.health_calls == 0


def test_webhook_rejects_missing_or_wrong_secret(api_settings) -> None:
    runtime = FakeRuntime()
    with make_client(api_settings, runtime) as client:
        assert client.post("/telegram/webhook", json={"update_id": 1}).status_code == 401
        assert (
            client.post(
                "/telegram/webhook",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            ).status_code
            == 401
        )
        assert runtime.payloads == []


def test_webhook_rejects_non_ascii_secret_header(api_settings) -> None:
    runtime = FakeRuntime()
    with make_client(api_settings, runtime) as client:
        response = client.post(
            "/telegram/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": b"bad-\xff-value"},
        )
        assert response.status_code == 401
        assert runtime.payloads == []


def test_webhook_accepts_valid_payload(api_settings) -> None:
    runtime = FakeRuntime()
    with make_client(api_settings, runtime) as client:
        response = client.post(
            "/telegram/webhook", json={"update_id": 1}, headers=HEADER
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert runtime.payloads == [{"update_id": 1}]


def test_webhook_duplicate_is_acknowledged(api_settings) -> None:
    runtime = FakeRuntime(status=IngressStatus.DUPLICATE)
    with make_client(api_settings, runtime) as client:
        response = client.post(
            "/telegram/webhook", json={"update_id": 1}, headers=HEADER
        )
        assert response.status_code == 200


def test_webhook_full_queue_asks_for_retry(api_settings) -> None:
    runtime = FakeRuntime(status=IngressStatus.FULL)
    with make_client(api_settings, runtime) as client:
        response = client.post(
            "/telegram/webhook", json={"update_id": 1}, headers=HEADER
        )
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"


def test_webhook_invalid_status_is_rejected(api_settings) -> None:
    runtime = FakeRuntime(status=IngressStatus.INVALID)
    with make_client(api_settings, runtime) as client:
        response = client.post(
            "/telegram/webhook", json={"update_id": 1}, headers=HEADER
        )
        assert response.status_code == 400


def test_webhook_rejects_bad_json(api_settings) -> None:
    runtime = FakeRuntime()
    with make_client(api_settings, runtime) as client:
        response = client.post(
            "/telegram/webhook",
            content=b"{not json",
            headers={**HEADER, "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert runtime.payloads == []


def test_webhook_rejects_non_object_json(api_settings) -> None:
    runtime = FakeRuntime()
    with make_client(api_settings, runtime) as client:
        response = client.post("/telegram/webhook", json=[1, 2], headers=HEADER)
        assert response.status_code == 400


def test_webhook_rejects_oversized_body(api_settings) -> None:
    runtime = FakeRuntime()
    with make_client(api_settings, runtime) as client:
        response = client.post(
            "/telegram/webhook",
            content=b"x" * 1_000_001,
            headers={**HEADER, "Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert runtime.payloads == []


def test_webhook_unavailable_without_configured_secret(tmp_path: Path) -> None:
    settings = Settings(
        bot_token="123:test",
        webhook_secret="",
        temp_root=tmp_path / "temp",
    )
    runtime = FakeRuntime()
    with make_client(settings, runtime) as client:
        response = client.post("/telegram/webhook", json={"update_id": 1})
        assert response.status_code == 503
        assert runtime.payloads == []
