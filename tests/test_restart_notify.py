"""Restart notification recipients, content, and duplicate suppression."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import app.services.restart_notify as restart_module
from app.config import ConfigError, Settings, load_settings
from app.services.restart_notify import IST, RestartNotifier


class FakeBot:
    def __init__(self, *, failing: set[object] | None = None) -> None:
        self.sent: list[dict] = []
        self.failing = set(failing or ())

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        if chat_id in self.failing:
            raise RuntimeError("blocked")
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})


def make_notifier(tmp_path: Path, bot: FakeBot, **kwargs) -> RestartNotifier:
    return RestartNotifier(
        bot,
        owner_id=kwargs.pop("owner_id", 111),
        log_channel_id=kwargs.pop("log_channel_id", ""),
        state_path=tmp_path / "state.json",
        root=tmp_path,
        reason_path=tmp_path / "restart_reason.txt",
        clock=lambda: datetime(2026, 11, 25, 6, 45, tzinfo=IST),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_notification_uses_branded_layout_and_both_recipients(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(restart_module, "current_commit", lambda root=None: "abc123")
    (tmp_path / "restart_reason.txt").write_text(
        "- Added numbered image selector\n- Fixed <preview> reuse\n",
        encoding="utf-8",
    )
    bot = FakeBot()
    notifier = make_notifier(tmp_path, bot, log_channel_id="-100123")

    assert await notifier.notify_once() is True

    assert [entry["chat_id"] for entry in bot.sent] == [111, -100123]
    text = bot.sent[0]["text"]
    assert text.startswith("<b>Kayi PDF Online 🟢</b>")
    assert "<b>Changes in this Update:</b>" in text
    assert "• Added numbered image selector" in text
    assert "• Fixed &lt;preview&gt; reuse" in text
    assert "<b>Time:</b> <code>06:45 AM IST · Nov 25</code>" in text
    assert bot.sent[0]["parse_mode"] == "HTML"
    assert not (tmp_path / "restart_reason.txt").exists()
    assert "abc123" in (tmp_path / "state.json").read_text(encoding="utf-8")

    # A second startup hook in the same process must not send again.
    assert await notifier.notify_once() is False
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_same_commit_is_suppressed_across_processes(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(restart_module, "current_commit", lambda root=None: "abc123")
    (tmp_path / "state.json").write_text(
        '{"last_notified_commit": "abc123"}', encoding="utf-8"
    )
    bot = FakeBot()
    notifier = make_notifier(tmp_path, bot)

    assert await notifier.notify_once() is False
    assert bot.sent == []


@pytest.mark.asyncio
async def test_new_commit_notifies_and_updates_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(restart_module, "current_commit", lambda root=None: "def456")
    (tmp_path / "state.json").write_text(
        '{"last_notified_commit": "abc123"}', encoding="utf-8"
    )
    bot = FakeBot()
    notifier = make_notifier(tmp_path, bot)

    assert await notifier.notify_once() is True
    assert len(bot.sent) == 1
    assert "def456" in (tmp_path / "state.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_reason_file_forces_notification_for_same_commit(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(restart_module, "current_commit", lambda root=None: "abc123")
    (tmp_path / "state.json").write_text(
        '{"last_notified_commit": "abc123"}', encoding="utf-8"
    )
    (tmp_path / "restart_reason.txt").write_text(
        "Manual owner restart", encoding="utf-8"
    )
    bot = FakeBot()
    notifier = make_notifier(tmp_path, bot)

    assert await notifier.notify_once() is True
    assert len(bot.sent) == 1
    assert "Manual owner restart" in bot.sent[0]["text"]
    assert not (tmp_path / "restart_reason.txt").exists()


@pytest.mark.asyncio
async def test_delivery_failure_is_non_blocking_and_other_recipient_receives(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(restart_module, "current_commit", lambda root=None: "abc123")
    bot = FakeBot(failing={111})
    notifier = make_notifier(tmp_path, bot, log_channel_id=-100123)

    assert await notifier.notify_once() is True
    assert [entry["chat_id"] for entry in bot.sent] == [-100123]
    assert "abc123" in (tmp_path / "state.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_all_delivery_failures_do_not_raise_or_mark_commit(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(restart_module, "current_commit", lambda root=None: "abc123")
    bot = FakeBot(failing={111})
    notifier = make_notifier(tmp_path, bot)

    assert await notifier.notify_once() is False
    assert bot.sent == []
    assert not (tmp_path / "state.json").exists()


@pytest.mark.asyncio
async def test_no_recipients_does_nothing(tmp_path) -> None:
    bot = FakeBot()
    notifier = make_notifier(tmp_path, bot, owner_id=0, log_channel_id="")
    assert await notifier.notify_once() is False
    assert bot.sent == []


def test_recipients_are_deduplicated_and_usernames_are_supported(tmp_path) -> None:
    bot = FakeBot()
    notifier = make_notifier(
        tmp_path, bot, owner_id=111, log_channel_id="@kayibots"
    )
    assert notifier.targets() == [111, "@kayibots"]

    numeric = make_notifier(tmp_path, bot, owner_id=111, log_channel_id="111")
    assert numeric.targets() == [111]


def test_restart_recipient_configuration(monkeypatch) -> None:
    monkeypatch.setenv("OWNER_ID", "12345")
    monkeypatch.setenv("LOG_CHANNEL_ID", "-100999")
    settings = load_settings(require_token=False)
    assert settings.owner_id == 12345
    assert settings.log_channel_id == "-100999"

    monkeypatch.setenv("OWNER_ID", "not-a-number")
    with pytest.raises(ConfigError):
        load_settings(require_token=False)


def test_settings_default_restart_recipients_are_empty() -> None:
    settings = Settings()
    assert settings.owner_id == 0
    assert settings.log_channel_id == ""


@pytest.mark.asyncio
async def test_render_git_commit_and_log_channel_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RENDER_GIT_COMMIT", "render999abc")
    assert restart_module.current_commit(tmp_path) == "render999abc"

    bot = FakeBot()
    notifier = make_notifier(tmp_path, bot, owner_id=0, log_channel_id="-1004495645007")
    assert await notifier.notify_once() is True
    assert [entry["chat_id"] for entry in bot.sent] == [-1004495645007]
    assert "render999abc" in (tmp_path / "state.json").read_text(encoding="utf-8")


def test_git_log_prefix_stripping_and_fallback(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        restart_module.subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "feat: add restart alerts\n"
            "- fix: deduplicate same commit\n"
            "perf: optimize startup\n"
            "Co-authored-by: someone\n"
            "Signed-off-by: someone\n"
        ),
    )
    changes = restart_module.latest_changes(tmp_path)
    assert changes == [
        "Add restart alerts",
        "Deduplicate same commit",
        "Optimize startup",
    ]

    def fail(*args, **kwargs):
        raise RuntimeError("no git")

    monkeypatch.setattr(restart_module.subprocess, "check_output", fail)
    assert restart_module.latest_changes(tmp_path) == ["Bug fixes and performance improvements"]
