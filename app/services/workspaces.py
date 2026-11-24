"""Isolated, short-lived workspaces for one user's active job."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.services.errors import WorkspaceCapacityError
from app.utils.logging import get_logger
from app.utils.text import sanitize_filename

log = get_logger(__name__)
_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


@dataclass
class JobWorkspace:
    user_id: int
    path: Path
    created_at: float
    last_used: float
    cleaned: bool = False
    _counter: int = field(default=0, repr=False)
    _reserved: set[str] = field(default_factory=set, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def touch(self) -> None:
        self.last_used = time.monotonic()

    async def new_input_path(self, suffix: str = ".bin") -> Path:
        safe_suffix = suffix.lower() if _SUFFIX_RE.match(suffix) else ".bin"
        async with self._lock:
            self._counter += 1
            self.touch()
            return self.path / f"input_{self._counter:03d}{safe_suffix}"

    async def new_output_path(self, filename: str, default: str = "result.pdf") -> Path:
        safe = sanitize_filename(filename, default)
        async with self._lock:
            self._counter += 1
            self.touch()
            candidate = self.path / safe
            if candidate.exists() or candidate.name in self._reserved:
                stem = candidate.stem
                suffix = candidate.suffix
                index = self._counter
                while True:
                    name = f"{stem}_{index:03d}{suffix}"
                    candidate = self.path / name
                    if not candidate.exists() and name not in self._reserved:
                        break
                    index += 1
            self._reserved.add(candidate.name)
            return candidate

    def resolve(self, name: str) -> Path:
        """Resolve a stored relative name inside this workspace only."""

        base = self.path.resolve()
        candidate = (base / name).resolve()
        if candidate == base or base not in candidate.parents:
            raise ValueError("Path escapes the job workspace")
        return candidate

    def cleanup(self) -> None:
        if self.cleaned:
            return
        self.cleaned = True
        self._reserved.clear()
        shutil.rmtree(self.path, ignore_errors=True)


class WorkspaceManager:
    """One active workspace per user, with bounded disk usage."""

    def __init__(
        self,
        base_dir: Path,
        *,
        max_workspaces: int = 20,
        stale_ttl: float = 7200.0,
        idle_ttl: float = 900.0,
        clock=time.monotonic,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.max_workspaces = max_workspaces
        self.stale_ttl = stale_ttl
        self.idle_ttl = idle_ttl
        self._clock = clock
        self._workspaces: dict[int, JobWorkspace] = {}
        self._lock = asyncio.Lock()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.base_dir, 0o700)
        except OSError:
            pass

    @property
    def count(self) -> int:
        return len(self._workspaces)

    def get(self, user_id: int) -> JobWorkspace | None:
        workspace = self._workspaces.get(user_id)
        if workspace is None:
            return None
        if workspace.cleaned or not workspace.path.exists():
            self._workspaces.pop(user_id, None)
            return None
        workspace.touch()
        return workspace

    async def get_or_create(self, user_id: int) -> JobWorkspace:
        existing = self.get(user_id)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self.get(user_id)
            if existing is not None:
                return existing
            self._prune_locked()
            if len(self._workspaces) >= self.max_workspaces:
                self._evict_idle_locked()
            if len(self._workspaces) >= self.max_workspaces:
                raise WorkspaceCapacityError()
            path = Path(tempfile.mkdtemp(prefix="job_", dir=self.base_dir))
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
            now = self._clock()
            workspace = JobWorkspace(
                user_id=user_id, path=path, created_at=now, last_used=now
            )
            self._workspaces[user_id] = workspace
            return workspace

    async def cleanup_user(self, user_id: int) -> None:
        workspace = self._workspaces.pop(user_id, None)
        if workspace is not None:
            workspace.cleanup()

    def cleanup_user_sync(self, user_id: int) -> None:
        workspace = self._workspaces.pop(user_id, None)
        if workspace is not None:
            workspace.cleanup()

    def detach(self, workspace: JobWorkspace) -> None:
        """Forget a workspace so a new job can get a fresh directory."""

        if self._workspaces.get(workspace.user_id) is workspace:
            self._workspaces.pop(workspace.user_id, None)

    def cleanup_workspace_sync(self, workspace: JobWorkspace) -> None:
        """Clean one specific workspace, never a newer replacement."""

        self.detach(workspace)
        workspace.cleanup()

    async def cleanup_all(self) -> None:
        workspaces = list(self._workspaces.values())
        self._workspaces.clear()
        for workspace in workspaces:
            workspace.cleanup()

    def cleanup_all_sync(self) -> None:
        workspaces = list(self._workspaces.values())
        self._workspaces.clear()
        for workspace in workspaces:
            workspace.cleanup()

    def prune_stale(self) -> int:
        now = self._clock()
        removed = 0
        for user_id, workspace in list(self._workspaces.items()):
            if (
                workspace.cleaned
                or not workspace.path.exists()
                or now - workspace.last_used > self.stale_ttl
            ):
                self._workspaces.pop(user_id, None)
                workspace.cleanup()
                removed += 1
        return removed

    def _prune_locked(self) -> None:
        now = self._clock()
        for user_id, workspace in list(self._workspaces.items()):
            if (
                workspace.cleaned
                or not workspace.path.exists()
                or now - workspace.last_used > self.stale_ttl
            ):
                self._workspaces.pop(user_id, None)
                workspace.cleanup()

    def _evict_idle_locked(self) -> None:
        now = self._clock()
        candidates = sorted(
            self._workspaces.values(), key=lambda workspace: workspace.last_used
        )
        for workspace in candidates:
            if now - workspace.last_used >= self.idle_ttl:
                self._workspaces.pop(workspace.user_id, None)
                workspace.cleanup()
                if len(self._workspaces) < self.max_workspaces:
                    return

    def cleanup_orphans(self, max_age_seconds: float = 0.0) -> int:
        """Remove abandoned ``job_*`` directories left by an earlier process."""

        now = time.time()
        removed = 0
        try:
            entries = list(self.base_dir.iterdir())
        except OSError:
            return 0
        active_paths = {workspace.path.resolve() for workspace in self._workspaces.values()}
        for entry in entries:
            if not entry.is_dir() or not entry.name.startswith("job_"):
                continue
            try:
                if entry.resolve() in active_paths:
                    continue
                age = now - entry.stat().st_mtime
                if max_age_seconds <= 0 or age >= max_age_seconds:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                log.warning("Could not inspect stale workspace")
        return removed
