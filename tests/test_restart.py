"""Owner restart reason handling, Render deploy hooks, and local shutdown."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

import app.services.restart as restart_module
from app.services.restart import (
    DEFAULT_REASON,
    MAX_REASON_LENGTH,
    MODE_DEPLOY,
    MODE_SHUTDOWN,
    clear_restart_reason,
    deploy_hook_is_configured,
    deploy_hook_url,
    normalize_reason,
    project_root,
    request_shutdown,
    restart_is_supported,
    restart_mode,
    schedule_restart,
    trigger_deploy,
    write_restart_reason,
)

HOOK = "https://api.render.com/deploy/srv-example?key=secret"


def test_normalize_reason_defaults_and_collapses_whitespace() -> None:
    assert normalize_reason(None) == DEFAULT_REASON
    assert normalize_reason("   ") == DEFAULT_REASON
    assert normalize_reason("  planned\n\tmaintenance  ") == "planned maintenance"


def test_normalize_reason_is_bounded() -> None:
    reason = normalize_reason("x" * (MAX_REASON_LENGTH + 40))
    assert len(reason) == MAX_REASON_LENGTH
    assert reason == "x" * MAX_REASON_LENGTH


def test_write_restart_reason_is_atomic_and_readable(tmp_path: Path) -> None:
    target = write_restart_reason("  deploy\nmaintenance ", root=tmp_path)
    assert target == tmp_path / "restart_reason.txt"
    assert target is not None
    assert target.read_text(encoding="utf-8") == "deploy maintenance"
    assert not (tmp_path / "restart_reason.txt.tmp").exists()


def test_write_restart_reason_reports_write_failure(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    assert write_restart_reason("reason", root=blocker) is None


def test_clear_restart_reason_removes_the_file(tmp_path: Path) -> None:
    write_restart_reason("reason", root=tmp_path)
    clear_restart_reason(root=tmp_path)
    assert not (tmp_path / "restart_reason.txt").exists()
    clear_restart_reason(root=tmp_path)


def test_project_root_is_the_repository_root() -> None:
    assert (project_root() / "app").is_dir()


def test_deploy_hook_requires_render_origin(monkeypatch) -> None:
    monkeypatch.delenv("RENDER_DEPLOY_HOOK_URL", raising=False)
    assert deploy_hook_url() == ""
    assert deploy_hook_is_configured() is False

    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", "https://example.com/deploy")
    assert deploy_hook_is_configured() is False

    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    assert deploy_hook_is_configured() is True


def test_render_uses_the_deploy_hook_even_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(restart_module.sys, "platform", "win32")
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    assert restart_mode() == MODE_DEPLOY
    assert restart_is_supported() is True


def test_render_without_a_hook_refuses_to_self_terminate(monkeypatch) -> None:
    monkeypatch.setattr(restart_module.sys, "platform", "linux")
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("RENDER_DEPLOY_HOOK_URL", raising=False)
    assert restart_mode() is None
    assert restart_is_supported() is False


def test_local_windows_shutdown_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(restart_module.sys, "platform", "win32")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_DEPLOY_HOOK_URL", raising=False)
    monkeypatch.setattr(
        restart_module.sys, "argv", ["uvicorn", "app.main:app", "--port", "8080"]
    )
    assert restart_mode() is None


def test_local_asgi_server_uses_graceful_shutdown(monkeypatch) -> None:
    monkeypatch.setattr(restart_module.sys, "platform", "linux")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_DEPLOY_HOOK_URL", raising=False)
    monkeypatch.setattr(
        restart_module.sys, "argv", ["uvicorn", "app.main:app", "--port", "8080"]
    )
    assert restart_mode() == MODE_SHUTDOWN

    monkeypatch.setattr(restart_module.sys, "argv", ["python", "-m", "pytest"])
    assert restart_mode() is None


@pytest.mark.asyncio
async def test_trigger_deploy_rejects_a_missing_or_foreign_hook(monkeypatch) -> None:
    calls: list[tuple] = []

    async def inline(function, *args):
        calls.append(args)
        return function(*args)

    monkeypatch.setattr(restart_module.asyncio, "to_thread", inline)
    monkeypatch.delenv("RENDER_DEPLOY_HOOK_URL", raising=False)
    assert await trigger_deploy() is False
    assert calls == []

    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", "https://example.com/deploy")
    assert await trigger_deploy() is False
    assert calls == []


@pytest.mark.asyncio
async def test_trigger_deploy_posts_to_the_hook(monkeypatch) -> None:
    calls: list[tuple[str, float]] = []

    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(restart_module.asyncio, "to_thread", inline)
    monkeypatch.setattr(
        restart_module, "_post_deploy_hook", lambda url, timeout: calls.append((url, timeout))
    )
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)

    assert await trigger_deploy(timeout=3.0) is True
    assert calls == [(HOOK, 3.0)]


@pytest.mark.asyncio
async def test_trigger_deploy_swallows_network_failures(monkeypatch) -> None:
    async def inline(function, *args):
        return function(*args)

    def failing(url, timeout):
        raise OSError("network down")

    monkeypatch.setattr(restart_module.asyncio, "to_thread", inline)
    monkeypatch.setattr(restart_module, "_post_deploy_hook", failing)
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)

    assert await trigger_deploy() is False


def test_post_deploy_hook_uses_post_and_checks_status(monkeypatch) -> None:
    requests: list[object] = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(restart_module.urllib.request, "urlopen", fake_urlopen)
    restart_module._post_deploy_hook(HOOK, 5.0)

    request, timeout = requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == HOOK
    assert timeout == 5.0


def test_post_deploy_hook_rejects_an_error_status(monkeypatch) -> None:
    class FakeResponse:
        status = 500

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        restart_module.urllib.request, "urlopen", lambda request, timeout: FakeResponse()
    )
    with pytest.raises(RuntimeError):
        restart_module._post_deploy_hook(HOOK, 5.0)


def test_request_shutdown_sends_sigterm(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(restart_module.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    request_shutdown()
    assert calls == [(restart_module.os.getpid(), signal.SIGTERM)]


def test_schedule_restart_uses_the_running_loop(monkeypatch) -> None:
    monkeypatch.setattr(restart_module, "restart_mode", lambda: MODE_SHUTDOWN)

    class FakeLoop:
        def __init__(self) -> None:
            self.calls: list[tuple[float, object]] = []

        def call_later(self, delay, callback):
            self.calls.append((delay, callback))

    loop = FakeLoop()
    monkeypatch.setattr(restart_module.asyncio, "get_running_loop", lambda: loop)

    assert schedule_restart(0.25) is True
    assert loop.calls == [(0.25, restart_module.request_shutdown)]


def test_schedule_restart_refuses_in_deploy_mode(monkeypatch) -> None:
    monkeypatch.setattr(restart_module, "restart_mode", lambda: MODE_DEPLOY)

    class ExplodingLoop:
        def call_later(self, delay, callback):
            raise AssertionError("must not schedule")

    monkeypatch.setattr(
        restart_module.asyncio, "get_running_loop", lambda: ExplodingLoop()
    )
    assert schedule_restart() is False


def test_schedule_restart_handles_a_missing_event_loop(monkeypatch) -> None:
    monkeypatch.setattr(restart_module, "restart_mode", lambda: MODE_SHUTDOWN)

    def no_loop():
        raise RuntimeError("no running loop")

    monkeypatch.setattr(restart_module.asyncio, "get_running_loop", no_loop)
    assert schedule_restart() is False
