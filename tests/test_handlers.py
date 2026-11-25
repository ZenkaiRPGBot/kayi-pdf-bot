"""Handler-level behaviour for start, cancel, menu, and verification."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import build_services, create_dispatcher
from app.handlers.common import cancel_handler, home_callback
from app.handlers.fallback import stale_callback_fallback
from app.handlers.merge import add_pdf_to_merge
from app.handlers.menu import open_tool_callback
from app.handlers.shared import DownloadedFile
from app.handlers.start import (
    privacy_handler,
    start_handler,
    verify_subscription_handler,
)
from app.handlers.uploads import choose_operation, direct_upload
from app.services.errors import FileValidationError
from app.services.fsub import MembershipResult, MembershipStatus, PendingActionStore
from app.services.jobs import JobManager
from app.services.workspaces import WorkspaceManager
from app.states.workflows import MergeStates, RotateStates, UploadChoiceStates
from app.texts import (
    HELP_MAIN_TEXT,
    WELCOME_TEXT,
    cancelled_text,
    fsub_text,
    fsub_verify_failed_text,
    help_topic_text,
    no_active_task_text,
    verified_text,
)


class FakeMessage:
    def __init__(self, text: str | None = None, user_id: int = 7) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=100)
        self.text = text
        self.answers: list[dict] = []
        self.edits: list[dict] = []
        self.deleted = False

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append({"text": text, "reply_markup": reply_markup, **kwargs})
        return SimpleNamespace(message_id=len(self.answers))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append({"text": text, "reply_markup": reply_markup, **kwargs})
        return self

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, data: str, user_id: int = 7, message=None) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = message
        self.answers: list[dict] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append({"text": text, "show_alert": show_alert})


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


class FakeSubscription:
    def __init__(self, status: MembershipStatus = MembershipStatus.MEMBER) -> None:
        self.status = status
        self.calls: list[tuple[int, bool]] = []

    async def check(self, bot, user_id, *, force=False):
        self.calls.append((user_id, force))
        return MembershipResult(self.status)


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=100, user_id=7),
    )


@pytest.fixture
def workspaces(tmp_path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "handler-work", max_workspaces=5)


@pytest.fixture
def jobs() -> JobManager:
    return JobManager(max_active_jobs=1, max_queued_jobs=0)


@pytest.mark.asyncio
async def test_start_help_for_member_never_sends_video(
    settings, state, workspaces
) -> None:
    message = FakeMessage(text="/start help")
    bot = FakeBot()
    subscription = FakeSubscription(MembershipStatus.MEMBER)
    pending = PendingActionStore()
    await start_handler(
        message=message,
        command=CommandObject(args="help"),
        bot=bot,
        state=state,
        settings=settings,
        subscription=subscription,
        pending=pending,
        workspaces=workspaces,
    )
    assert message.answers[-1]["text"] == HELP_MAIN_TEXT
    assert bot.videos == []
    assert bot.messages == []


@pytest.mark.asyncio
async def test_privacy_command_reuses_the_limits_topic(settings) -> None:
    message = FakeMessage(text="/privacy")
    await privacy_handler(message, settings)
    assert message.answers[-1]["text"] == help_topic_text("limits", settings)
    assert message.answers[-1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_start_for_member_sends_welcome(settings, state, workspaces) -> None:
    message = FakeMessage(text="/start")
    bot = FakeBot()
    await start_handler(
        message=message,
        command=CommandObject(args=None),
        bot=bot,
        state=state,
        settings=settings,
        subscription=FakeSubscription(MembershipStatus.MEMBER),
        pending=PendingActionStore(),
        workspaces=workspaces,
    )
    assert bot.messages[-1]["text"] == WELCOME_TEXT


@pytest.mark.asyncio
async def test_start_for_non_member_prompts_and_remembers_help(
    settings, state, workspaces
) -> None:
    message = FakeMessage(text="/start help")
    bot = FakeBot()
    pending = PendingActionStore()
    await start_handler(
        message=message,
        command=CommandObject(args="help"),
        bot=bot,
        state=state,
        settings=settings,
        subscription=FakeSubscription(MembershipStatus.NOT_MEMBER),
        pending=pending,
        workspaces=workspaces,
    )
    assert message.answers[-1]["text"] == fsub_text(MembershipStatus.NOT_MEMBER)
    assert pending.get(7) == "help"


@pytest.mark.asyncio
async def test_verify_restores_help(settings, state, workspaces, jobs) -> None:
    message = FakeMessage()
    callback = FakeCallback("fsub:verify", message=message)
    bot = FakeBot()
    pending = PendingActionStore()
    pending.set(7, "help")
    await verify_subscription_handler(
        callback=callback,
        bot=bot,
        state=state,
        settings=settings,
        subscription=FakeSubscription(MembershipStatus.MEMBER),
        pending=pending,
        workspaces=workspaces,
        jobs=jobs,
    )
    assert callback.answers
    assert message.edits[-1]["text"] == verified_text()
    assert bot.messages[-1]["text"] == HELP_MAIN_TEXT


@pytest.mark.asyncio
async def test_verify_does_not_disturb_running_job(
    settings, state, workspaces, jobs
) -> None:
    message = FakeMessage()
    callback = FakeCallback("fsub:verify", message=message)
    bot = FakeBot()
    slot = await jobs.acquire(7)
    try:
        await verify_subscription_handler(
            callback=callback,
            bot=bot,
            state=state,
            settings=settings,
            subscription=FakeSubscription(MembershipStatus.MEMBER),
            pending=PendingActionStore(),
            workspaces=workspaces,
            jobs=jobs,
        )
    finally:
        await slot.release()
    assert "still running" in bot.messages[-1]["text"]


@pytest.mark.asyncio
async def test_verify_failure_keeps_subscription_prompt(
    settings, state, workspaces, jobs
) -> None:
    message = FakeMessage()
    callback = FakeCallback("fsub:verify", message=message)
    await verify_subscription_handler(
        callback=callback,
        bot=FakeBot(),
        state=state,
        settings=settings,
        subscription=FakeSubscription(MembershipStatus.NOT_MEMBER),
        pending=PendingActionStore(),
        workspaces=workspaces,
        jobs=jobs,
    )
    assert message.edits[-1]["text"] == fsub_verify_failed_text()


@pytest.mark.asyncio
async def test_cancel_without_task(settings, state, workspaces, jobs) -> None:
    message = FakeMessage("/cancel")
    await cancel_handler(
        message=message,
        state=state,
        settings=settings,
        jobs=jobs,
        workspaces=workspaces,
    )
    assert message.answers[-1]["text"] == no_active_task_text()


@pytest.mark.asyncio
async def test_cancel_active_job(settings, state, workspaces, jobs) -> None:
    message = FakeMessage("/cancel")
    slot = await jobs.acquire(7)
    try:
        await cancel_handler(
            message=message,
            state=state,
            settings=settings,
            jobs=jobs,
            workspaces=workspaces,
        )
        assert "Cancelling" in message.answers[-1]["text"]
        assert slot.token.cancelled is True
    finally:
        await slot.release()


@pytest.mark.asyncio
async def test_cancel_workflow_state(settings, state, workspaces, jobs) -> None:
    message = FakeMessage("/cancel")
    await state.set_state(MergeStates.collecting)
    await cancel_handler(
        message=message,
        state=state,
        settings=settings,
        jobs=jobs,
        workspaces=workspaces,
    )
    assert message.answers[-1]["text"] == cancelled_text()
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_home_while_job_active(settings, state, workspaces, jobs) -> None:
    callback = FakeCallback("menu:home")
    bot = FakeBot()
    slot = await jobs.acquire(7)
    try:
        await home_callback(
            callback=callback,
            bot=bot,
            state=state,
            settings=settings,
            jobs=jobs,
            workspaces=workspaces,
        )
    finally:
        await slot.release()
    assert "still running" in bot.messages[-1]["text"]


@pytest.mark.asyncio
async def test_home_returns_to_welcome(settings, state, workspaces, jobs) -> None:
    callback = FakeCallback("menu:home")
    bot = FakeBot()
    await home_callback(
        callback=callback,
        bot=bot,
        state=state,
        settings=settings,
        jobs=jobs,
        workspaces=workspaces,
    )
    assert bot.messages[-1]["text"] == WELCOME_TEXT


@pytest.mark.asyncio
async def test_menu_opens_tool_when_idle(settings, state, workspaces) -> None:
    callback = FakeCallback("menu:merge")
    bot = FakeBot()
    await open_tool_callback(
        callback=callback,
        bot=bot,
        state=state,
        settings=settings,
        workspaces=workspaces,
    )
    assert await state.get_state() == MergeStates.collecting.state
    assert bot.messages


@pytest.mark.asyncio
async def test_menu_refuses_while_workflow_is_active(settings, state, workspaces) -> None:
    callback = FakeCallback("menu:merge")
    bot = FakeBot()
    await state.set_state(MergeStates.collecting)
    await open_tool_callback(
        callback=callback,
        bot=bot,
        state=state,
        settings=settings,
        workspaces=workspaces,
    )
    assert callback.answers[-1]["show_alert"] is True
    assert bot.messages == []


@pytest.mark.asyncio
async def test_stale_callback_is_answered() -> None:
    callback = FakeCallback("unknown:data")
    await stale_callback_fallback(callback)
    assert callback.answers[-1]["show_alert"] is True


def test_dispatcher_wiring(settings) -> None:
    dispatcher = create_dispatcher(settings, build_services(settings))
    assert [router.name for router in dispatcher.sub_routers] == [
        "start",
        "common",
        "usage",
        "menu",
        "merge",
        "split",
        "extract",
        "rotate",
        "images",
        "uploads",
        "fallback",
    ]
    assert len(dispatcher.message.outer_middleware) == 2
    assert len(dispatcher.callback_query.outer_middleware) == 2


@pytest.mark.asyncio
async def test_direct_upload_stores_operation_choice(
    monkeypatch, settings, state, workspaces
) -> None:
    async def fake_download(message, bot, workspace, settings):
        return DownloadedFile(
            path=await workspace.new_input_path(".pdf"),
            display_name="doc.pdf",
            size=100,
            page_count=3,
            digest="direct-digest",
        )

    monkeypatch.setattr("app.handlers.uploads.download_pdf", fake_download)
    message = FakeMessage()
    message.document = SimpleNamespace(
        file_id="file",
        file_name="doc.pdf",
        mime_type="application/pdf",
        file_size=100,
    )
    message.photo = None
    await direct_upload(
        message=message,
        bot=FakeBot(),
        state=state,
        settings=settings,
        workspaces=workspaces,
    )
    assert await state.get_state() == UploadChoiceStates.choosing.state
    data = await state.get_data()
    assert data["owner_id"] == 7
    assert data["kind"] == "pdf"
    assert data["digest"] == "direct-digest"
    assert "PDF Received" in message.answers[-1]["text"]


@pytest.mark.asyncio
async def test_choose_operation_opens_rotate(settings, state, workspaces) -> None:
    await workspaces.get_or_create(7)
    await state.set_state(UploadChoiceStates.choosing)
    await state.set_data(
        {
            "owner_id": 7,
            "kind": "pdf",
            "name": "doc.pdf",
            "stored_name": "input_001.pdf",
            "size": 100,
            "page_count": 3,
            "digest": "direct-digest",
        }
    )
    callback = FakeCallback("upload:choose:rotate", message=FakeMessage())
    bot = FakeBot()
    await choose_operation(
        callback=callback,
        bot=bot,
        state=state,
        settings=settings,
        workspaces=workspaces,
    )
    assert await state.get_state() == RotateStates.choosing.state
    data = await state.get_data()
    assert data["owner_id"] == 7
    assert data["page_count"] == 3
    assert "Rotate Pages" in bot.messages[-1]["text"]


@pytest.mark.asyncio
async def test_choose_operation_rejects_kind_mismatch(
    settings, state, workspaces
) -> None:
    await workspaces.get_or_create(7)
    await state.set_state(UploadChoiceStates.choosing)
    await state.set_data(
        {
            "owner_id": 7,
            "kind": "image",
            "name": "photo.jpg",
            "stored_name": "input_001.jpg",
            "size": 100,
        }
    )
    callback = FakeCallback("upload:choose:merge", message=FakeMessage())
    await choose_operation(
        callback=callback,
        bot=FakeBot(),
        state=state,
        settings=settings,
        workspaces=workspaces,
    )
    assert callback.answers[-1]["show_alert"] is True
    assert await state.get_state() == UploadChoiceStates.choosing.state


@pytest.mark.asyncio
async def test_merge_rejects_identical_content(
    settings, state, workspaces, tmp_path
) -> None:
    message = FakeMessage()
    bot = FakeBot()
    workspace = await workspaces.get_or_create(7)
    await state.set_state(MergeStates.collecting)
    await state.set_data(
        {
            "owner_id": 7,
            "files": [
                {
                    "name": "first.pdf",
                    "stored_name": "input_001.pdf",
                    "size": 100,
                    "page_count": 1,
                    "digest": "same-digest",
                }
            ],
            "queue_message_id": None,
        }
    )
    duplicate = DownloadedFile(
        path=workspace.path / "input_002.pdf",
        display_name="copy.pdf",
        size=100,
        page_count=1,
        digest="same-digest",
    )
    with pytest.raises(FileValidationError, match="already in the merge list"):
        await add_pdf_to_merge(
            message, bot, duplicate, state, workspaces, settings
        )
    assert len((await state.get_data())["files"]) == 1


@pytest.mark.asyncio
async def test_merge_accepts_distinct_content(
    settings, state, workspaces
) -> None:
    message = FakeMessage()
    bot = FakeBot()
    workspace = await workspaces.get_or_create(7)
    await state.set_state(MergeStates.collecting)
    await state.set_data(
        {
            "owner_id": 7,
            "files": [
                {
                    "name": "first.pdf",
                    "stored_name": "input_001.pdf",
                    "size": 100,
                    "page_count": 1,
                    "digest": "first-digest",
                }
            ],
            "queue_message_id": None,
        }
    )
    second = DownloadedFile(
        path=workspace.path / "input_002.pdf",
        display_name="second.pdf",
        size=120,
        page_count=2,
        digest="second-digest",
    )
    await add_pdf_to_merge(message, bot, second, state, workspaces, settings)
    data = await state.get_data()
    assert len(data["files"]) == 2
    assert data["files"][1]["digest"] == "second-digest"
