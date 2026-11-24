"""Menu navigation and post-subscription destination restoration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.services.navigation import (
    clear_workflow,
    open_tool,
    restore_pending_action,
    show_help,
)
from app.services.workspaces import WorkspaceManager
from app.states.workflows import (
    ExtractStates,
    ImagesStates,
    MergeStates,
    RotateStates,
    SplitStates,
)
from app.texts import (
    HELP_MAIN_TEXT,
    HELP_TOPICS,
    WELCOME_TEXT,
    extract_intro_text,
    images_intro_text,
    merge_text,
    resend_after_verify_text,
    rotate_intro_text,
    split_intro_text,
)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.videos: list[dict] = []
        self.counter = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                **kwargs,
            }
        )
        self.counter += 1
        return SimpleNamespace(message_id=self.counter)

    async def send_video(self, **kwargs):
        self.videos.append(kwargs)
        return None


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=100, user_id=7),
    )


@pytest.fixture
def workspaces(tmp_path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "nav-work", max_workspaces=5)


@pytest.mark.asyncio
async def test_clear_workflow_removes_state_and_files(
    state: FSMContext, workspaces: WorkspaceManager
) -> None:
    workspace = await workspaces.get_or_create(7)
    await state.set_state(MergeStates.collecting)
    await state.set_data({"owner_id": 7})
    await clear_workflow(state, workspaces, 7)
    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert not workspace.path.exists()
    assert workspaces.get(7) is None


@pytest.mark.asyncio
async def test_open_merge(settings, state: FSMContext, workspaces: WorkspaceManager) -> None:
    bot = FakeBot()
    await open_tool(bot, 100, 7, "merge", state, workspaces, settings)
    assert await state.get_state() == MergeStates.collecting.state
    data = await state.get_data()
    assert data["owner_id"] == 7
    assert data["files"] == []
    assert bot.messages[-1]["text"] == merge_text([], settings)
    assert bot.messages[-1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_open_split(settings, state: FSMContext, workspaces: WorkspaceManager) -> None:
    bot = FakeBot()
    await open_tool(bot, 100, 7, "split", state, workspaces, settings)
    assert await state.get_state() == SplitStates.waiting_file.state
    assert (await state.get_data())["owner_id"] == 7
    assert bot.messages[-1]["text"] == split_intro_text()


@pytest.mark.asyncio
async def test_open_extract(settings, state: FSMContext, workspaces: WorkspaceManager) -> None:
    bot = FakeBot()
    await open_tool(bot, 100, 7, "extract", state, workspaces, settings)
    assert await state.get_state() == ExtractStates.waiting_file.state
    assert bot.messages[-1]["text"] == extract_intro_text()


@pytest.mark.asyncio
async def test_open_rotate(settings, state: FSMContext, workspaces: WorkspaceManager) -> None:
    bot = FakeBot()
    await open_tool(bot, 100, 7, "rotate", state, workspaces, settings)
    assert await state.get_state() == RotateStates.waiting_file.state
    assert bot.messages[-1]["text"] == rotate_intro_text()


@pytest.mark.asyncio
async def test_open_images(settings, state: FSMContext, workspaces: WorkspaceManager) -> None:
    bot = FakeBot()
    await open_tool(bot, 100, 7, "images", state, workspaces, settings)
    assert await state.get_state() == ImagesStates.collecting.state
    assert (await state.get_data())["files"] == []
    assert bot.messages[-1]["text"] == images_intro_text(settings)


@pytest.mark.asyncio
async def test_open_unknown_tool(settings, state: FSMContext, workspaces: WorkspaceManager) -> None:
    with pytest.raises(ValueError):
        await open_tool(FakeBot(), 100, 7, "nope", state, workspaces, settings)


@pytest.mark.asyncio
async def test_show_help(settings, state: FSMContext) -> None:
    bot = FakeBot()
    await show_help(bot, 100)
    assert bot.messages[-1]["text"] == HELP_MAIN_TEXT
    await show_help(bot, 100, "tools")
    assert bot.messages[-1]["text"] == HELP_TOPICS["tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("help", HELP_MAIN_TEXT),
        ("help_topic:start", HELP_TOPICS["start"]),
        ("help_topic:images", HELP_TOPICS["images"]),
        ("help_topic:unknown", HELP_MAIN_TEXT),
        ("resend", resend_after_verify_text()),
        ("welcome", WELCOME_TEXT),
        ("start", WELCOME_TEXT),
        ("something-stale", WELCOME_TEXT),
    ],
)
async def test_restore_pending_action(
    action, expected, settings, state: FSMContext, workspaces: WorkspaceManager
) -> None:
    bot = FakeBot()
    workspace = await workspaces.get_or_create(7)
    await state.set_state(MergeStates.collecting)
    await restore_pending_action(bot, 100, 7, action, state, workspaces, settings)
    assert bot.messages[-1]["text"] == expected
    assert not workspace.path.exists()
    if action != "help" and not action.startswith("help_topic:"):
        assert await state.get_state() is None


@pytest.mark.asyncio
async def test_restore_tool_destination(settings, state: FSMContext, workspaces: WorkspaceManager) -> None:
    bot = FakeBot()
    await restore_pending_action(bot, 100, 7, "split", state, workspaces, settings)
    assert await state.get_state() == SplitStates.waiting_file.state
    assert bot.messages[-1]["text"] == split_intro_text()


@pytest.mark.asyncio
async def test_restore_start_uses_video_when_configured(
    settings, state: FSMContext, workspaces: WorkspaceManager
) -> None:
    from dataclasses import replace

    bot = FakeBot()
    settings = replace(settings, welcome_video_file_id="video-id")
    await restore_pending_action(bot, 100, 7, "start", state, workspaces, settings)
    assert bot.videos and bot.videos[0]["video"] == "video-id"
    assert bot.messages == []
