"""Workspace isolation, bounded capacity, and cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.errors import WorkspaceCapacityError
from app.services.workspaces import WorkspaceManager


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def manager(tmp_path: Path):
    clock = FakeClock()
    instance = WorkspaceManager(
        tmp_path / "workspaces",
        max_workspaces=3,
        stale_ttl=1000.0,
        idle_ttl=500.0,
        clock=clock,
    )
    return instance, clock


@pytest.mark.asyncio
async def test_workspace_is_created_and_reused(manager) -> None:
    workspaces, _ = manager
    first = await workspaces.get_or_create(1)
    second = await workspaces.get_or_create(1)
    assert first is second
    assert first.path.is_dir()
    assert workspaces.count == 1
    assert workspaces.get(1) is first
    await workspaces.cleanup_user(1)
    assert not first.path.exists()
    assert workspaces.get(1) is None


@pytest.mark.asyncio
async def test_input_and_output_names_are_generated(manager, tmp_path) -> None:
    workspaces, _ = manager
    workspace = await workspaces.get_or_create(2)
    pdf_path = await workspace.new_input_path(".PDF")
    bin_path = await workspace.new_input_path("../../evil")
    output = await workspace.new_output_path("../../outside.pdf")
    collision = await workspace.new_output_path("../../outside.pdf")
    assert pdf_path.name == "input_001.pdf"
    assert bin_path.name == "input_002.bin"
    assert output.name == "outside.pdf"
    assert output.parent == workspace.path
    assert collision.name == "outside_004.pdf"
    assert collision.parent == workspace.path


@pytest.mark.asyncio
async def test_resolve_blocks_path_escape(manager) -> None:
    workspaces, _ = manager
    workspace = await workspaces.get_or_create(3)
    target = workspace.path / "file.pdf"
    target.write_bytes(b"x")
    assert workspace.resolve("file.pdf") == target.resolve()
    for bad in ("../file.pdf", "sub/../../file.pdf", "."):
        with pytest.raises(ValueError):
            workspace.resolve(bad)


@pytest.mark.asyncio
async def test_capacity_rejects_new_workspace(manager) -> None:
    workspaces, clock = manager
    for user_id in (1, 2, 3):
        await workspaces.get_or_create(user_id)
    clock.advance(1)
    with pytest.raises(WorkspaceCapacityError):
        await workspaces.get_or_create(4)
    assert workspaces.count == 3


@pytest.mark.asyncio
async def test_idle_workspace_is_evicted_to_make_room(manager) -> None:
    workspaces, clock = manager
    old = await workspaces.get_or_create(1)
    clock.advance(501)
    await workspaces.get_or_create(2)
    clock.advance(1)
    await workspaces.get_or_create(3)
    clock.advance(1)
    new = await workspaces.get_or_create(4)
    assert new.path.is_dir()
    assert not old.path.exists()
    assert workspaces.get(1) is None


@pytest.mark.asyncio
async def test_stale_pruning(manager) -> None:
    workspaces, clock = manager
    workspace = await workspaces.get_or_create(5)
    clock.advance(1001)
    assert workspaces.prune_stale() == 1
    assert workspaces.count == 0
    assert not workspace.path.exists()


@pytest.mark.asyncio
async def test_cleanup_all(manager) -> None:
    workspaces, _ = manager
    first = await workspaces.get_or_create(1)
    second = await workspaces.get_or_create(2)
    await workspaces.cleanup_all()
    assert workspaces.count == 0
    assert not first.path.exists()
    assert not second.path.exists()


@pytest.mark.asyncio
async def test_cleanup_user_sync(manager) -> None:
    workspaces, _ = manager
    workspace = await workspaces.get_or_create(9)
    workspaces.cleanup_user_sync(9)
    assert not workspace.path.exists()
    assert workspaces.count == 0


@pytest.mark.asyncio
async def test_detached_workspace_can_be_replaced_without_losing_new_job(manager) -> None:
    workspaces, _ = manager
    old = await workspaces.get_or_create(4)
    workspaces.detach(old)
    assert workspaces.get(4) is None
    new = await workspaces.get_or_create(4)
    assert new is not old
    assert new.path.is_dir()
    workspaces.cleanup_workspace_sync(old)
    assert not old.path.exists()
    assert new.path.is_dir()
    await workspaces.cleanup_user(4)


@pytest.mark.asyncio
async def test_orphan_cleanup_keeps_unrelated_directories(manager) -> None:
    workspaces, _ = manager
    base = workspaces.base_dir
    orphan = base / "job_orphan"
    unrelated = base / "notes"
    orphan.mkdir()
    unrelated.mkdir()
    active = await workspaces.get_or_create(1)
    removed = workspaces.cleanup_orphans(max_age_seconds=0)
    assert removed == 1
    assert not orphan.exists()
    assert unrelated.exists()
    assert active.path.exists()


@pytest.mark.asyncio
async def test_externally_deleted_workspace_is_forgotten(manager) -> None:
    workspaces, _ = manager
    workspace = await workspaces.get_or_create(1)
    import shutil

    shutil.rmtree(workspace.path)
    assert workspaces.get(1) is None
    assert workspaces.count == 0
