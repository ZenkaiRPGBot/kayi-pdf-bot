"""Kayi Bots startup notification.

The layout and behaviour mirror the established implementation in
``Temp Mail Bot``: a branded HTML message with the latest change bullets,
owner/log-channel delivery, an optional ``restart_reason.txt`` override, and
duplicate suppression for the same deployed commit.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from aiogram import Bot
from aiogram.enums import ParseMode

from app.config import BRAND_NAME, Settings
from app.utils.logging import get_logger

log = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_CHANGES = ["Bug fixes and performance improvements"]
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COMMIT_PREFIXES = (
    "feat:",
    "fix:",
    "chore:",
    "refactor:",
    "perf:",
    "docs:",
    "style:",
    "test:",
)


def project_root() -> Path:
    return _PROJECT_ROOT


def current_commit(root: Path | None = None) -> str:
    """Return Render's deployed commit or the local git HEAD."""

    render_commit = os.getenv("RENDER_GIT_COMMIT", "").strip()
    if render_commit:
        return render_commit
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root or _PROJECT_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).strip()
    except Exception:
        return ""


def latest_changes(root: Path | None = None, reason_path: Path | None = None) -> list[str]:
    """Read ``restart_reason.txt`` first, then the latest git commit."""

    base = Path(root or _PROJECT_ROOT)
    reason = Path(reason_path) if reason_path is not None else base / "restart_reason.txt"
    if reason.exists():
        try:
            content = reason.read_text(encoding="utf-8").strip()
            reason.unlink(missing_ok=True)
            if content:
                return [
                    line.strip("-•* ").strip()
                    for line in content.splitlines()
                    if line.strip()
                ]
        except Exception:
            pass

    try:
        raw = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%s%n%b"],
            cwd=str(base),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).strip()
        lines = [line.strip("-•* ").strip() for line in raw.splitlines() if line.strip()]
        cleaned: list[str] = []
        for line in lines:
            for prefix in _COMMIT_PREFIXES:
                if line.lower().startswith(prefix):
                    line = line[len(prefix) :].strip()
                    break
            if (
                line
                and len(line) > 3
                and not line.startswith("Co-authored-by:")
                and not line.startswith("Signed-off-by:")
            ):
                cleaned.append(line[0].upper() + line[1:])
            if len(cleaned) >= 3:
                break
        if cleaned:
            return cleaned
    except Exception:
        pass

    return list(DEFAULT_CHANGES)


def _chat_id(value: str | int) -> int | str | None:
    if isinstance(value, int):
        return value or None
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        if raw.startswith("@") and len(raw) > 1:
            return raw
        log.warning("LOG_CHANNEL_ID is not a numeric ID or @username")
        return None


class RestartNotifier:
    """Send one branded startup message per process and deployed commit."""

    def __init__(
        self,
        bot: Bot,
        *,
        owner_id: int = 0,
        log_channel_id: str = "",
        brand_name: str = BRAND_NAME,
        state_path: Path | None = None,
        root: Path | None = None,
        reason_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bot = bot
        self.owner_id = int(owner_id or 0)
        self.log_channel_id = log_channel_id
        self.brand_name = brand_name
        self.root = Path(root or _PROJECT_ROOT)
        self.state_path = Path(state_path) if state_path else (
            Path(os.getenv("TEMP", ".")) / "kayi-pdf-bot" / "restart_state.json"
        )
        self.reason_path = (
            Path(reason_path) if reason_path is not None else self.root / "restart_reason.txt"
        )
        self.clock = clock or (lambda: datetime.now(IST))
        self._lock = asyncio.Lock()
        self._attempted = False

    def targets(self) -> list[int | str]:
        targets: list[int | str] = []
        seen: set[str] = set()
        if self.owner_id:
            targets.append(self.owner_id)
            seen.add(str(self.owner_id))
        channel = _chat_id(self.log_channel_id)
        if channel is not None and str(channel) not in seen:
            targets.append(channel)
        return targets

    def _read_last_commit(self) -> str:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        value = payload.get("last_notified_commit") if isinstance(payload, dict) else ""
        return value if isinstance(value, str) else ""

    def _write_last_commit(self, commit: str) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(self.state_path.name + ".tmp")
            temporary.write_text(
                json.dumps({"last_notified_commit": commit}),
                encoding="utf-8",
            )
            temporary.replace(self.state_path)
        except OSError as exc:
            log.warning("Could not save restart state: %s", type(exc).__name__)

    async def notify_once(self) -> bool:
        """Send the notification at most once for this process."""

        async with self._lock:
            if self._attempted:
                return False
            self._attempted = True
            try:
                return await self._notify()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Restart notification failed")
                return False

    async def _notify(self) -> bool:
        targets = self.targets()
        if not targets:
            log.info("Restart notification skipped: OWNER_ID/LOG_CHANNEL_ID not set")
            return False

        has_reason = self.reason_path.exists()
        commit = current_commit(self.root)
        if not has_reason and commit and self._read_last_commit() == commit:
            log.info("Restart notification skipped: commit %s already notified", commit[:7])
            return False

        changes = latest_changes(self.root, self.reason_path)
        formatted = "\n".join(f"• {html.escape(change)}" for change in changes)
        now = self.clock().astimezone(IST).strftime("%I:%M %p IST · %b %d")
        content = (
            f"<b>{html.escape(self.brand_name)} Online 🟢</b>\n\n"
            "<b>Changes in this Update:</b>\n"
            f"{formatted}\n\n"
            f"<b>Time:</b> <code>{now}</code>"
        )

        delivered = 0
        for chat_id in targets:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=content,
                    parse_mode=ParseMode.HTML,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "Could not send restart notification to %s: %s",
                    chat_id,
                    type(exc).__name__,
                )
            else:
                delivered += 1
                log.info("Restart notification sent to %s", chat_id)

        if delivered and commit:
            self._write_last_commit(commit)
        return delivered > 0


def build_restart_notifier(bot: Bot, settings: Settings) -> RestartNotifier:
    return RestartNotifier(
        bot,
        owner_id=settings.owner_id,
        log_channel_id=settings.log_channel_id,
        state_path=settings.temp_root / "restart_state.json",
    )
