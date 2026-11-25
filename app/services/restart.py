"""Owner-requested restart support for an ASGI/Uvicorn deployment.

The sibling Kayi bots call ``os.execv`` because they own their polling loop.
Kayi PDF runs under Uvicorn, so replacing the interpreter in-place would leave
the ASGI server, its listening socket, and its lifespan tasks in an undefined
state.  Two safe paths are supported instead:

* **Render deploy hook (preferred).** A POST to the service's deploy hook asks
  Render to build and swap in a fresh instance with zero downtime.  The current
  process keeps serving until the new instance is healthy.
* **Local supervised ASGI shutdown.** Outside Render, a Uvicorn/Gunicorn/
  Hypercorn process may receive ``SIGTERM`` so its supervisor can start it
  again.  Windows is rejected because ``SIGTERM`` is not a graceful ASGI stop
  there.

In both cases the owner command writes ``restart_reason.txt`` first so the next
startup notification can explain why the service restarted.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import urllib.request
from pathlib import Path

from app.utils.logging import get_logger

log = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REASON = "Owner requested restart"
MAX_REASON_LENGTH = 160
DEPLOY_HOOK_PREFIX = "https://api.render.com/deploy/"

MODE_DEPLOY = "deploy"
MODE_SHUTDOWN = "shutdown"


def project_root() -> Path:
    """Return the repository root that owns ``restart_reason.txt``."""

    return _PROJECT_ROOT


def normalize_reason(reason: str | None) -> str:
    """Collapse whitespace, bound the length, and provide a safe default."""

    value = " ".join((reason or "").split())
    if not value:
        return DEFAULT_REASON
    return value[:MAX_REASON_LENGTH].rstrip()


def write_restart_reason(
    reason: str | None = None,
    *,
    root: Path | None = None,
) -> Path | None:
    """Persist the next startup reason, returning ``None`` on write failure."""

    target = Path(root or _PROJECT_ROOT) / "restart_reason.txt"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(normalize_reason(reason), encoding="utf-8")
        temporary.replace(target)
        return target
    except OSError as exc:
        log.warning("Could not write restart reason: %s", type(exc).__name__)
        return None


def clear_restart_reason(*, root: Path | None = None) -> None:
    """Remove a reason that could not be acted on, ignoring missing files."""

    target = Path(root or _PROJECT_ROOT) / "restart_reason.txt"
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not clear restart reason: %s", type(exc).__name__)


def deploy_hook_url() -> str:
    """Return the configured Render deploy hook, or an empty string."""

    return os.getenv("RENDER_DEPLOY_HOOK_URL", "").strip()


def deploy_hook_is_configured() -> bool:
    """Only accept Render's documented deploy-hook origin."""

    return deploy_hook_url().startswith(DEPLOY_HOOK_PREFIX)


def restart_mode() -> str | None:
    """Return the safe restart path for this process, if any.

    A Render process must use the deploy hook.  Self-terminating on Render is
    not safe because a clean exit is not a deploy: the instance can simply stay
    stopped.  Local ASGI servers may use ``SIGTERM`` when a supervisor is
    expected to bring the process back.
    """

    if deploy_hook_is_configured():
        return MODE_DEPLOY
    if sys.platform == "win32":
        return None
    if os.getenv("RENDER", "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    argv = " ".join(sys.argv).lower()
    if any(server in argv for server in ("uvicorn", "gunicorn", "hypercorn")):
        return MODE_SHUTDOWN
    return None


def restart_is_supported() -> bool:
    """Whether the owner restart command can act safely."""

    return restart_mode() is not None


def _post_deploy_hook(url: str, timeout: float) -> None:
    """Blocking POST executed in a worker thread."""

    request = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise RuntimeError(f"deploy hook returned {status}")


async def trigger_deploy(timeout: float = 15.0) -> bool:
    """Ask Render for a zero-downtime deploy without exposing the hook URL."""

    url = deploy_hook_url()
    if not url.startswith(DEPLOY_HOOK_PREFIX):
        log.warning("Render deploy hook is missing or has an unexpected origin")
        return False
    try:
        await asyncio.to_thread(_post_deploy_hook, url, timeout)
    except Exception as exc:
        log.warning("Render deploy hook failed: %s", type(exc).__name__)
        return False
    return True


def request_shutdown() -> None:
    """Ask a local ASGI server to stop so its supervisor can restart it."""

    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except (OSError, ValueError) as exc:
        log.warning("Could not request process shutdown: %s", type(exc).__name__)


def schedule_restart(delay: float = 1.0) -> bool:
    """Schedule a graceful local shutdown after the reply has been delivered."""

    if restart_mode() != MODE_SHUTDOWN:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("Restart requested outside a running event loop")
        return False
    loop.call_later(max(0.0, delay), request_shutdown)
    return True
