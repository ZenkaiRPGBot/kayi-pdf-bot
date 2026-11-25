"""Image preview navigation, ordering integrity, and media fallbacks."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetMe
from aiogram.types import CallbackQuery, Chat, FSInputFile, Message, User
from PIL import Image

import app.handlers.images as images_module
import app.services.previews as previews_module
from app.config import Settings
from app.handlers.common import cancel_handler
from app.handlers.images import (
    _process_images,
    add_image_to_images,
    images_control,
    images_rename_back,
    images_rename_text,
    render_images_queue,
)
from app.handlers.shared import DownloadedFile
from app.handlers.uploads import choose_operation
from app.keyboards.main import images_queue_keyboard
from app.middleware.user_lock import UserLockManager, UserSerializationMiddleware
from app.services.jobs import JobManager
from app.services.previews import (
    PREVIEW_CONCURRENCY,
    preview_path,
    write_preview,
    write_preview_async,
)
from app.services.workspaces import WorkspaceManager
from app.states.workflows import ImagesStates, UploadChoiceStates
from app.texts import images_intro_text


class FakeSent:
    def __init__(
        self,
        message_id: int,
        *,
        chat_id: int = 100,
        text: str | None = None,
        caption: str | None = None,
        photo_file_id: str | None = None,
    ) -> None:
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id)
        self.text = text
        self.caption = caption
        self.photo = (
            [SimpleNamespace(file_id=photo_file_id)] if photo_file_id else []
        )
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class FakeBot:
    def __init__(self) -> None:
        self.counter = 0
        self.sent_messages: list[dict] = []
        self.sent_photos: list[dict] = []
        self.edited_texts: list[dict] = []
        self.edited_media: list[dict] = []
        self.edited_captions: list[dict] = []
        self.deleted: list[tuple[int, int]] = []
        self.fail_edit_media: Exception | None = None
        self.fail_send_photo: Exception | None = None
        self.invalid_photo_values: set[str] = set()

    def _next_id(self) -> int:
        self.counter += 1
        return self.counter

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        sent = FakeSent(self._next_id(), chat_id=chat_id, text=text)
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                **kwargs,
                "sent": sent,
            }
        )
        return sent

    async def send_photo(
        self,
        chat_id,
        photo,
        caption=None,
        reply_markup=None,
        **kwargs,
    ):
        if self.fail_send_photo is not None:
            raise self.fail_send_photo
        if isinstance(photo, str) and photo in self.invalid_photo_values:
            raise TelegramBadRequest(
                GetMe(), "wrong file identifier/HTTP URL specified"
            )
        file_id = photo if isinstance(photo, str) else f"uploaded-{self.counter + 1}"
        sent = FakeSent(
            self._next_id(),
            chat_id=chat_id,
            caption=caption,
            photo_file_id=file_id,
        )
        self.sent_photos.append(
            {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "reply_markup": reply_markup,
                **kwargs,
                "sent": sent,
            }
        )
        return sent

    async def edit_message_text(
        self,
        chat_id,
        message_id,
        text,
        reply_markup=None,
        **kwargs,
    ):
        self.edited_texts.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
                **kwargs,
            }
        )
        return FakeSent(int(message_id), chat_id=chat_id, text=text)

    async def edit_message_media(
        self,
        chat_id,
        message_id,
        media,
        reply_markup=None,
        **kwargs,
    ):
        if self.fail_edit_media is not None:
            raise self.fail_edit_media
        raw = media.media
        if isinstance(raw, str) and raw in self.invalid_photo_values:
            raise TelegramBadRequest(
                GetMe(), "wrong file identifier/HTTP URL specified"
            )
        file_id = raw if isinstance(raw, str) else f"edited-{message_id}"
        sent = FakeSent(
            int(message_id),
            chat_id=chat_id,
            caption=media.caption,
            photo_file_id=file_id,
        )
        self.edited_media.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "media": media,
                "reply_markup": reply_markup,
                **kwargs,
                "sent": sent,
            }
        )
        return sent

    async def edit_message_caption(
        self,
        chat_id,
        message_id,
        caption,
        reply_markup=None,
        **kwargs,
    ):
        self.edited_captions.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": caption,
                "reply_markup": reply_markup,
                **kwargs,
            }
        )
        return FakeSent(int(message_id), chat_id=chat_id, caption=caption)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, int(message_id)))


class FakeCallback:
    def __init__(self, data: str, *, user_id: int = 7, message=None) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = message
        self.answers: list[dict] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append({"text": text, "show_alert": show_alert})


class FakeQueueMessage:
    def __init__(self, message_id: int, *, chat_id: int = 100) -> None:
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id)
        self.edits: list[dict] = []

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append({"text": text, "reply_markup": reply_markup, **kwargs})
        return self


class FakeUserMessage:
    def __init__(self, *, user_id: int = 7, chat_id: int = 100) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id)
        self.answers: list[dict] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append({"text": text, "reply_markup": reply_markup, **kwargs})
        return SimpleNamespace(message_id=len(self.answers))


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=100, user_id=7),
    )


@pytest.fixture
def workspaces(tmp_path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "preview-work", max_workspaces=5)


@pytest.fixture
def jobs() -> JobManager:
    return JobManager(max_active_jobs=1, max_queued_jobs=0)


def make_image(
    path: Path,
    *,
    size: tuple[int, int] = (400, 300),
    fmt: str = "JPEG",
    mode: str = "RGB",
    color=(180, 60, 40),
) -> Path:
    image = Image.new(mode, size, color)
    try:
        image.save(path, fmt)
    finally:
        image.close()
    return path


def make_item(
    item_id: str,
    *,
    name: str = "image.jpg",
    stored_name: str | None = None,
    size: int = 1234,
    photo_file_id: str | None = None,
) -> dict:
    item = {
        "id": item_id,
        "name": name,
        "stored_name": stored_name or f"input_{item_id}.jpg",
        "size": size,
    }
    if photo_file_id:
        item["photo_file_id"] = photo_file_id
    return item


async def set_images_state(
    state: FSMContext,
    files: list[dict],
    *,
    preview_id: str | None,
    queue_message_id: int | None = None,
    queue_kind: str | None = None,
    queue_item_id: str | None = None,
    preview_file_ids: dict[str, str] | None = None,
) -> dict:
    data = {
        "owner_id": 7,
        "files": files,
        "queue_message_id": queue_message_id,
        "queue_kind": queue_kind,
        "queue_item_id": queue_item_id,
        "preview_id": preview_id,
        "preview_file_ids": preview_file_ids or {},
        "preview_unavailable": [],
    }
    await state.set_state(ImagesStates.collecting)
    await state.set_data(data)
    return await state.get_data()


def button_texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


async def queue_message_id(state: FSMContext) -> int:
    return int((await state.get_data())["queue_message_id"])


@pytest.mark.asyncio
async def test_first_image_preview_uses_telegram_photo_file_id(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager
) -> None:
    workspace = await workspaces.get_or_create(7)
    path = make_image(workspace.path / "input_001.jpg")
    downloaded = DownloadedFile(
        path=path,
        display_name="image.jpg",
        size=path.stat().st_size,
    )
    await state.set_state(ImagesStates.collecting)
    await state.set_data(
        {
            "owner_id": 7,
            "files": [],
            "queue_message_id": None,
            "queue_kind": None,
            "preview_id": None,
            "preview_file_ids": {},
            "preview_unavailable": [],
        }
    )
    bot = FakeBot()

    await add_image_to_images(
        FakeUserMessage(),
        bot,
        downloaded,
        state,
        workspaces,
        settings,
        photo_file_id="photo-file-1",
    )

    data = await state.get_data()
    assert len(data["files"]) == 1
    assert data["preview_id"] == data["files"][0]["id"]
    assert data["queue_kind"] == "photo"
    assert bot.sent_photos
    assert bot.sent_photos[0]["photo"] == "photo-file-1"
    assert button_texts(bot.sent_photos[0]["reply_markup"])[0] == [
        "←",
        "1 / 1",
        "→",
    ]
    assert not bot.edited_media


@pytest.mark.asyncio
async def test_document_thumbnail_is_cached_across_navigation(
    monkeypatch,
    settings: Settings,
    state: FSMContext,
    workspaces: WorkspaceManager,
    jobs: JobManager,
) -> None:
    workspace = await workspaces.get_or_create(7)
    first = make_image(workspace.path / "input_001.jpg")
    second = make_image(workspace.path / "input_002.png", fmt="PNG")
    files = [
        make_item("aaaaaaaaaaaa", name="image.jpg", stored_name=first.name),
        make_item("bbbbbbbbbbbb", name="image.png", stored_name=second.name),
    ]
    data = await set_images_state(state, files, preview_id="aaaaaaaaaaaa")

    calls: list[str] = []
    real_write_preview = previews_module.write_preview

    def counting_write_preview(source, target, max_dimension=720):
        calls.append(source.name)
        return real_write_preview(source, target, max_dimension)

    monkeypatch.setattr(previews_module, "write_preview", counting_write_preview)
    bot = FakeBot()
    await render_images_queue(bot, 100, state, data, settings, workspaces)

    data = await state.get_data()
    assert calls == ["input_001.jpg"]
    assert bot.sent_photos
    assert isinstance(bot.sent_photos[0]["photo"], FSInputFile)
    assert preview_path(workspace.path, "aaaaaaaaaaaa").is_file()
    assert data["preview_file_ids"]["aaaaaaaaaaaa"].startswith("uploaded-")

    message = FakeQueueMessage(await queue_message_id(state))
    await images_control(
        FakeCallback("images:next", message=message),
        bot,
        state,
        settings,
        jobs,
        workspaces,
    )
    data = await state.get_data()
    assert calls == ["input_001.jpg", "input_002.png"]
    assert data["preview_id"] == "bbbbbbbbbbbb"
    assert bot.edited_media
    assert isinstance(bot.edited_media[-1]["media"].media, FSInputFile)
    assert data["preview_file_ids"]["bbbbbbbbbbbb"].startswith("edited-")
    assert "▶ 2. image.png" in bot.edited_media[-1]["media"].caption

    message = FakeQueueMessage(await queue_message_id(state))
    await images_control(
        FakeCallback("images:prev", message=message),
        bot,
        state,
        settings,
        jobs,
        workspaces,
    )
    data = await state.get_data()
    assert calls == ["input_001.jpg", "input_002.png"]
    assert data["preview_id"] == "aaaaaaaaaaaa"
    assert bot.edited_media[-1]["media"].media == data["preview_file_ids"]["aaaaaaaaaaaa"]

    message = FakeQueueMessage(await queue_message_id(state))
    await images_control(
        FakeCallback("images:next", message=message),
        bot,
        state,
        settings,
        jobs,
        workspaces,
    )
    data = await state.get_data()
    assert calls == ["input_001.jpg", "input_002.png"]
    assert bot.edited_media[-1]["media"].media == data["preview_file_ids"]["bbbbbbbbbbbb"]


@pytest.mark.asyncio
async def test_preview_boundaries_do_not_edit_or_wrap(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="a.jpg"),
        make_item("bbbbbbbbbbbb", name="b.jpg"),
    ]
    await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids={"aaaaaaaaaaaa": "file-a", "bbbbbbbbbbbb": "file-b"},
    )
    bot = FakeBot()
    callback = FakeCallback("images:prev", message=FakeQueueMessage(10))
    await images_control(callback, bot, state, settings, jobs, workspaces)
    assert callback.answers == [{"text": None, "show_alert": False}]
    assert not bot.edited_media and not bot.sent_photos and not bot.sent_messages
    assert (await state.get_data())["preview_id"] == "aaaaaaaaaaaa"

    data = await state.get_data()
    data["preview_id"] = "bbbbbbbbbbbb"
    await state.set_data(data)
    callback = FakeCallback("images:next", message=FakeQueueMessage(10))
    await images_control(callback, bot, state, settings, jobs, workspaces)
    assert callback.answers == [{"text": None, "show_alert": False}]
    assert not bot.edited_media and not bot.sent_photos and not bot.sent_messages
    assert (await state.get_data())["preview_id"] == "bbbbbbbbbbbb"


@pytest.mark.asyncio
async def test_position_button_is_informational_only(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="a.jpg"),
        make_item("bbbbbbbbbbbb", name="b.jpg"),
    ]
    await set_images_state(
        state,
        files,
        preview_id="bbbbbbbbbbbb",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids={"aaaaaaaaaaaa": "file-a", "bbbbbbbbbbbb": "file-b"},
    )
    bot = FakeBot()
    callback = FakeCallback("images:pos", message=FakeQueueMessage(10))
    await images_control(callback, bot, state, settings, jobs, workspaces)
    assert callback.answers == [{"text": "2 / 2", "show_alert": False}]
    assert (await state.get_data())["preview_id"] == "bbbbbbbbbbbb"
    assert not bot.edited_media and not bot.sent_photos and not bot.sent_messages


@pytest.mark.asyncio
async def test_numbered_button_previews_that_queue_position(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="a.jpg"),
        make_item("bbbbbbbbbbbb", name="b.jpg"),
        make_item("cccccccccccc", name="c.jpg"),
    ]
    await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        queue_item_id="aaaaaaaaaaaa",
        preview_file_ids={
            "aaaaaaaaaaaa": "file-a",
            "bbbbbbbbbbbb": "file-b",
            "cccccccccccc": "file-c",
        },
    )
    bot = FakeBot()
    callback = FakeCallback("images:select:2", message=FakeQueueMessage(10))
    await images_control(callback, bot, state, settings, jobs, workspaces)
    data = await state.get_data()
    assert data["preview_id"] == "cccccccccccc"
    assert data["files"] and [item["id"] for item in data["files"]] == [
        "aaaaaaaaaaaa",
        "bbbbbbbbbbbb",
        "cccccccccccc",
    ]
    assert bot.edited_media[-1]["media"].media == "file-c"
    assert "▶ 3. c.jpg" in bot.edited_media[-1]["media"].caption
    assert button_texts(bot.edited_media[-1]["reply_markup"])[0] == [
        "←",
        "3 / 3",
        "→",
    ]
    assert button_texts(bot.edited_media[-1]["reply_markup"])[1] == [
        "1",
        "2",
        "3",
    ]


@pytest.mark.asyncio
async def test_rename_flow_sets_custom_filename_without_media_upload(
    monkeypatch,
    settings: Settings,
    state: FSMContext,
    workspaces: WorkspaceManager,
    jobs: JobManager,
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="first.jpg"),
        make_item("bbbbbbbbbbbb", name="second.jpg"),
    ]
    await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        queue_item_id="aaaaaaaaaaaa",
        preview_file_ids={"aaaaaaaaaaaa": "file-a", "bbbbbbbbbbbb": "file-b"},
    )
    bot = FakeBot()
    callback = FakeCallback("images:rename", message=FakeQueueMessage(10))
    await images_control(callback, bot, state, settings, jobs, workspaces)

    data = await state.get_data()
    prompt_id = data["rename_message_id"]
    assert await state.get_state() == ImagesStates.renaming.state
    assert bot.sent_messages[-1]["text"].startswith("<b>Rename PDF</b>")
    assert "first_images.pdf" in bot.sent_messages[-1]["text"]
    assert not bot.edited_media and not bot.sent_photos

    message = FakeUserMessage()
    message.text = "Quarterly Report.PDF"
    await images_rename_text(message, bot, state, settings, workspaces)

    data = await state.get_data()
    assert await state.get_state() == ImagesStates.collecting.state
    assert data["output_name"] == "Quarterly Report.pdf"
    assert data["preview_id"] == "aaaaaaaaaaaa"
    assert (100, prompt_id) in bot.deleted
    assert not bot.edited_media and not bot.sent_photos
    assert "Output:</b> <code>Quarterly Report.pdf</code>" in bot.edited_captions[-1][
        "caption"
    ]

    captured: dict[str, object] = {}

    def fake_images_to_pdf(paths, output, settings, token):
        captured["paths"] = list(paths)
        output.write_bytes(b"%PDF-1.4\n")

    async def fake_deliver(bot, chat_id, path, filename, **kwargs):
        captured["filename"] = filename
        return FakeSent(999, chat_id=chat_id, text=filename)

    monkeypatch.setattr(images_module, "images_to_pdf", fake_images_to_pdf)
    monkeypatch.setattr(images_module, "deliver_document", fake_deliver)
    await _process_images(callback, bot, state, data, settings, jobs, workspaces)

    assert captured["filename"] == "Quarterly Report.pdf"
    assert [Path(path).name for path in captured["paths"]] == [
        "input_aaaaaaaaaaaa.jpg",
        "input_bbbbbbbbbbbb.jpg",
    ]
    assert await state.get_state() is None
    assert "output_name" not in await state.get_data()


@pytest.mark.asyncio
async def test_rename_back_leaves_existing_filename_unchanged(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [make_item("aaaaaaaaaaaa", name="a.jpg")]
    data = await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        queue_item_id="aaaaaaaaaaaa",
        preview_file_ids={"aaaaaaaaaaaa": "file-a"},
    )
    data["output_name"] = "Existing.pdf"
    await state.set_data(data)
    bot = FakeBot()
    await images_control(
        FakeCallback("images:rename", message=FakeQueueMessage(10)),
        bot,
        state,
        settings,
        jobs,
        workspaces,
    )
    data = await state.get_data()
    prompt_id = data["rename_message_id"]
    assert "Existing.pdf" in bot.sent_messages[-1]["text"]

    await images_rename_back(
        FakeCallback("images:rename:back", message=FakeQueueMessage(prompt_id)),
        bot,
        state,
    )
    data = await state.get_data()
    assert await state.get_state() == ImagesStates.collecting.state
    assert data["output_name"] == "Existing.pdf"
    assert (100, prompt_id) in bot.deleted
    assert not bot.edited_media and not bot.sent_photos


@pytest.mark.asyncio
async def test_invalid_rename_is_rejected_and_keeps_state(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager
) -> None:
    files = [make_item("aaaaaaaaaaaa", name="a.jpg")]
    data = await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        queue_item_id="aaaaaaaaaaaa",
        preview_file_ids={"aaaaaaaaaaaa": "file-a"},
    )
    data["output_name"] = "Existing.pdf"
    await state.set_data(data)
    await state.set_state(ImagesStates.renaming)
    bot = FakeBot()

    for invalid in ("", "   ", ".pdf", "///", ".."):
        message = FakeUserMessage()
        message.text = invalid
        await images_rename_text(message, bot, state, settings, workspaces)
        assert await state.get_state() == ImagesStates.renaming.state
        assert (await state.get_data())["output_name"] == "Existing.pdf"
        assert message.answers
        assert message.answers[-1]["text"].startswith("<b>Rename PDF</b>")
    assert not bot.edited_media and not bot.sent_photos


@pytest.mark.asyncio
async def test_identical_filenames_remain_distinguishable_by_id(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="image.png", stored_name="input_1.png"),
        make_item("bbbbbbbbbbbb", name="image.png", stored_name="input_2.png"),
        make_item("cccccccccccc", name="image.png", stored_name="input_3.png"),
    ]
    data = await set_images_state(
        state,
        files,
        preview_id="bbbbbbbbbbbb",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids={
            "aaaaaaaaaaaa": "file-a",
            "bbbbbbbbbbbb": "file-b",
            "cccccccccccc": "file-c",
        },
    )
    bot = FakeBot()
    await render_images_queue(bot, 100, state, data, settings, workspaces)
    caption = bot.edited_media[-1]["media"].caption
    assert caption.count("image.png") == 3
    assert "▶ 2. image.png" in caption
    assert "▶ 1. image.png" not in caption
    assert button_texts(bot.edited_media[-1]["reply_markup"])[0] == [
        "←",
        "2 / 3",
        "→",
    ]


@pytest.mark.asyncio
async def test_uploading_another_image_preserves_current_preview(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager
) -> None:
    workspace = await workspaces.get_or_create(7)
    first = make_image(workspace.path / "input_001.jpg")
    second = make_image(workspace.path / "input_002.jpg", color=(10, 120, 200))
    files = [
        make_item("aaaaaaaaaaaa", name="first.jpg", stored_name=first.name)
    ]
    data = await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids={"aaaaaaaaaaaa": "file-a"},
    )
    bot = FakeBot()
    await render_images_queue(bot, 100, state, data, settings, workspaces)
    assert bot.edited_media
    media_edits = len(bot.edited_media)

    downloaded = DownloadedFile(
        path=second,
        display_name="second.jpg",
        size=second.stat().st_size,
    )
    await add_image_to_images(
        FakeUserMessage(),
        bot,
        downloaded,
        state,
        workspaces,
        settings,
        photo_file_id="photo-file-2",
    )

    data = await state.get_data()
    assert len(data["files"]) == 2
    assert data["preview_id"] == "aaaaaaaaaaaa"
    assert len(bot.edited_media) == media_edits
    assert bot.edited_captions
    assert button_texts(bot.edited_captions[-1]["reply_markup"])[0] == [
        "←",
        "1 / 2",
        "→",
    ]
    assert "▶ 1. first.jpg" in bot.edited_captions[-1]["caption"]
    assert not bot.sent_photos


@pytest.mark.asyncio
async def test_reordering_previewed_image_keeps_selection(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="a.jpg"),
        make_item("bbbbbbbbbbbb", name="b.jpg"),
        make_item("cccccccccccc", name="c.jpg"),
    ]
    await set_images_state(
        state,
        files,
        preview_id="bbbbbbbbbbbb",
        queue_message_id=10,
        queue_kind="photo",
        queue_item_id="bbbbbbbbbbbb",
        preview_file_ids={
            "aaaaaaaaaaaa": "file-a",
            "bbbbbbbbbbbb": "file-b",
            "cccccccccccc": "file-c",
        },
    )
    bot = FakeBot()
    await images_control(
        FakeCallback("images:up", message=FakeQueueMessage(10)),
        bot,
        state,
        settings,
        jobs,
        workspaces,
    )
    data = await state.get_data()
    assert [item["id"] for item in data["files"]] == [
        "bbbbbbbbbbbb",
        "aaaaaaaaaaaa",
        "cccccccccccc",
    ]
    assert data["preview_id"] == "bbbbbbbbbbbb"
    assert data["preview_file_ids"]["bbbbbbbbbbbb"] == "file-b"
    assert not bot.edited_media
    assert not bot.sent_photos
    assert bot.edited_captions
    assert "▶ 1. b.jpg" in bot.edited_captions[-1]["caption"]
    assert button_texts(bot.edited_captions[-1]["reply_markup"])[0] == [
        "←",
        "1 / 3",
        "→",
    ]


@pytest.mark.asyncio
async def test_moving_selected_image_updates_position_and_reuses_media(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="a.jpg"),
        make_item("bbbbbbbbbbbb", name="b.jpg"),
        make_item("cccccccccccc", name="c.jpg"),
    ]
    await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        queue_item_id="aaaaaaaaaaaa",
        preview_file_ids={
            "aaaaaaaaaaaa": "file-a",
            "bbbbbbbbbbbb": "file-b",
            "cccccccccccc": "file-c",
        },
    )
    bot = FakeBot()
    await images_control(
        FakeCallback("images:down", message=FakeQueueMessage(10)),
        bot,
        state,
        settings,
        jobs,
        workspaces,
    )
    data = await state.get_data()
    assert [item["id"] for item in data["files"]] == [
        "bbbbbbbbbbbb",
        "aaaaaaaaaaaa",
        "cccccccccccc",
    ]
    assert data["preview_id"] == "aaaaaaaaaaaa"
    assert not bot.edited_media and not bot.sent_photos
    assert "▶ 2. a.jpg" in bot.edited_captions[-1]["caption"]
    assert button_texts(bot.edited_captions[-1]["reply_markup"])[0] == [
        "←",
        "2 / 3",
        "→",
    ]

    await images_control(
        FakeCallback("images:up", message=FakeQueueMessage(10)),
        bot,
        state,
        settings,
        jobs,
        workspaces,
    )
    data = await state.get_data()
    assert [item["id"] for item in data["files"]] == [
        "aaaaaaaaaaaa",
        "bbbbbbbbbbbb",
        "cccccccccccc",
    ]
    assert data["preview_id"] == "aaaaaaaaaaaa"
    assert not bot.edited_media and not bot.sent_photos
    assert "▶ 1. a.jpg" in bot.edited_captions[-1]["caption"]


@pytest.mark.asyncio
async def test_move_boundaries_acknowledge_without_editing(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="a.jpg"),
        make_item("bbbbbbbbbbbb", name="b.jpg"),
        make_item("cccccccccccc", name="c.jpg"),
    ]
    await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        queue_item_id="aaaaaaaaaaaa",
        preview_file_ids={
            "aaaaaaaaaaaa": "file-a",
            "bbbbbbbbbbbb": "file-b",
            "cccccccccccc": "file-c",
        },
    )
    bot = FakeBot()
    first = FakeCallback("images:up", message=FakeQueueMessage(10))
    await images_control(first, bot, state, settings, jobs, workspaces)
    assert first.answers == [{"text": None, "show_alert": False}]
    assert not bot.edited_media and not bot.edited_captions and not bot.sent_photos

    data = await state.get_data()
    data["preview_id"] = "cccccccccccc"
    data["queue_item_id"] = "cccccccccccc"
    await state.set_data(data)
    last = FakeCallback("images:down", message=FakeQueueMessage(10))
    await images_control(last, bot, state, settings, jobs, workspaces)
    assert last.answers == [{"text": None, "show_alert": False}]
    assert not bot.edited_media and not bot.edited_captions and not bot.sent_photos
    assert [item["id"] for item in (await state.get_data())["files"]] == [
        "aaaaaaaaaaaa",
        "bbbbbbbbbbbb",
        "cccccccccccc",
    ]


@pytest.mark.asyncio
async def test_removing_previewed_image_selects_next_then_previous(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="a.jpg"),
        make_item("bbbbbbbbbbbb", name="b.jpg"),
        make_item("cccccccccccc", name="c.jpg"),
    ]
    await set_images_state(
        state,
        files,
        preview_id="bbbbbbbbbbbb",
        queue_message_id=10,
        queue_kind="photo",
        queue_item_id="bbbbbbbbbbbb",
        preview_file_ids={
            "aaaaaaaaaaaa": "file-a",
            "bbbbbbbbbbbb": "file-b",
            "cccccccccccc": "file-c",
        },
    )
    bot = FakeBot()
    await images_control(
        FakeCallback("images:remove", message=FakeQueueMessage(10)),
        bot,
        state,
        settings,
        jobs,
        workspaces,
    )
    data = await state.get_data()
    assert [item["id"] for item in data["files"]] == ["aaaaaaaaaaaa", "cccccccccccc"]
    assert data["preview_id"] == "cccccccccccc"
    assert "▶ 2. c.jpg" in bot.edited_media[-1]["media"].caption

    await images_control(
        FakeCallback("images:remove", message=FakeQueueMessage(10)),
        bot,
        state,
        settings,
        jobs,
        workspaces,
    )
    data = await state.get_data()
    assert [item["id"] for item in data["files"]] == ["aaaaaaaaaaaa"]
    assert data["preview_id"] == "aaaaaaaaaaaa"
    assert "▶ 1. a.jpg" in bot.edited_media[-1]["media"].caption


@pytest.mark.asyncio
async def test_removing_selected_last_image_selects_previous(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="a.jpg"),
        make_item("bbbbbbbbbbbb", name="b.jpg"),
        make_item("cccccccccccc", name="c.jpg"),
    ]
    await set_images_state(
        state,
        files,
        preview_id="cccccccccccc",
        queue_message_id=10,
        queue_kind="photo",
        queue_item_id="cccccccccccc",
        preview_file_ids={
            "aaaaaaaaaaaa": "file-a",
            "bbbbbbbbbbbb": "file-b",
            "cccccccccccc": "file-c",
        },
    )
    bot = FakeBot()
    await images_control(
        FakeCallback("images:remove", message=FakeQueueMessage(10)),
        bot,
        state,
        settings,
        jobs,
        workspaces,
    )
    data = await state.get_data()
    assert [item["id"] for item in data["files"]] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    assert data["preview_id"] == "bbbbbbbbbbbb"
    assert "▶ 2. b.jpg" in bot.edited_media[-1]["media"].caption


@pytest.mark.asyncio
async def test_removing_final_image_restores_empty_text_workspace(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    workspace = await workspaces.get_or_create(7)
    source = make_image(workspace.path / "input_001.jpg")
    item = make_item(
        "aaaaaaaaaaaa",
        name="only.jpg",
        stored_name=source.name,
    )
    stale_preview = preview_path(workspace.path, "aaaaaaaaaaaa")
    stale_preview.write_bytes(b"thumb")
    await set_images_state(
        state,
        [item],
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids={"aaaaaaaaaaaa": "file-a"},
    )
    bot = FakeBot()
    callback = FakeCallback("images:remove", message=FakeQueueMessage(10))
    await images_control(callback, bot, state, settings, jobs, workspaces)

    data = await state.get_data()
    assert data["files"] == []
    assert data["preview_id"] is None
    assert data["queue_kind"] == "text"
    assert data["queue_message_id"] != 10
    assert bot.sent_messages[-1]["text"] == images_intro_text(settings)
    assert (100, 10) in bot.deleted
    assert not stale_preview.exists()
    assert button_texts(bot.sent_messages[-1]["reply_markup"]) == [
        ["← Main Menu"]
    ]


@pytest.mark.asyncio
async def test_preview_navigation_does_not_change_pdf_page_order(
    monkeypatch,
    settings: Settings,
    state: FSMContext,
    workspaces: WorkspaceManager,
    jobs: JobManager,
) -> None:
    workspace = await workspaces.get_or_create(7)
    files = []
    for index, item_id in enumerate(
        ("cccccccccccc", "aaaaaaaaaaaa", "bbbbbbbbbbbb"), start=1
    ):
        path = make_image(workspace.path / f"input_{index:03d}.jpg")
        files.append(
            make_item(
                item_id,
                name=f"{index}.jpg",
                stored_name=path.name,
            )
        )
    await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids={
            "aaaaaaaaaaaa": "file-a",
            "bbbbbbbbbbbb": "file-b",
            "cccccccccccc": "file-c",
        },
    )
    bot = FakeBot()
    for action in ("images:next", "images:next", "images:prev", "images:prev"):
        await images_control(
            FakeCallback(action, message=FakeQueueMessage(10)),
            bot,
            state,
            settings,
            jobs,
            workspaces,
        )
    data = await state.get_data()
    assert [item["stored_name"] for item in data["files"]] == [
        "input_001.jpg",
        "input_002.jpg",
        "input_003.jpg",
    ]

    captured: list[Path] = []

    def fake_images_to_pdf(paths, output, settings, token):
        captured.extend(paths)
        output.write_bytes(b"%PDF-1.4\n")

    async def fake_deliver(bot, chat_id, path, filename, **kwargs):
        return FakeSent(999, chat_id=chat_id, text=filename)

    monkeypatch.setattr(images_module, "images_to_pdf", fake_images_to_pdf)
    monkeypatch.setattr(images_module, "deliver_document", fake_deliver)
    callback = FakeCallback("images:go", message=FakeQueueMessage(10))
    await _process_images(callback, bot, state, data, settings, jobs, workspaces)

    assert [path.name for path in captured] == [
        "input_001.jpg",
        "input_002.jpg",
        "input_003.jpg",
    ]
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_stale_unauthorized_and_expired_preview_callbacks(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [make_item("aaaaaaaaaaaa", name="a.jpg")]
    await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids={"aaaaaaaaaaaa": "file-a"},
    )
    bot = FakeBot()

    unauthorized = FakeCallback(
        "images:next", user_id=8, message=FakeQueueMessage(10)
    )
    await images_control(unauthorized, bot, state, settings, jobs, workspaces)
    assert unauthorized.answers[-1]["show_alert"] is True

    stale = FakeCallback("images:next", message=FakeQueueMessage(9))
    await images_control(stale, bot, state, settings, jobs, workspaces)
    assert stale.answers[-1]["show_alert"] is True

    await state.clear()
    expired = FakeCallback("images:next", message=FakeQueueMessage(10))
    await images_control(expired, bot, state, settings, jobs, workspaces)
    assert expired.answers[-1]["show_alert"] is True
    assert not bot.edited_media and not bot.sent_photos and not bot.sent_messages


@pytest.mark.asyncio
async def test_failed_media_edit_resends_preview_without_crashing(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager, jobs: JobManager
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="a.jpg"),
        make_item("bbbbbbbbbbbb", name="b.jpg"),
    ]
    await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids={"aaaaaaaaaaaa": "file-a", "bbbbbbbbbbbb": "file-b"},
    )
    bot = FakeBot()
    bot.fail_edit_media = TelegramBadRequest(
        GetMe(), "message to edit not found"
    )
    callback = FakeCallback("images:next", message=FakeQueueMessage(10))
    await images_control(callback, bot, state, settings, jobs, workspaces)

    data = await state.get_data()
    assert callback.answers == [{"text": None, "show_alert": False}]
    assert data["preview_id"] == "bbbbbbbbbbbb"
    assert bot.sent_photos
    assert bot.sent_photos[-1]["photo"] == "file-b"
    assert (100, 10) in bot.deleted
    assert data["queue_message_id"] == bot.sent_photos[-1]["sent"].message_id


@pytest.mark.asyncio
async def test_preview_generation_failure_falls_back_to_text(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager
) -> None:
    workspace = await workspaces.get_or_create(7)
    files = [
        make_item(
            "aaaaaaaaaaaa",
            name="missing.jpg",
            stored_name="missing.jpg",
        )
    ]
    data = await set_images_state(state, files, preview_id="aaaaaaaaaaaa")
    bot = FakeBot()
    await render_images_queue(bot, 100, state, data, settings, workspaces)

    data = await state.get_data()
    assert not bot.sent_photos
    assert bot.sent_messages
    assert data["queue_kind"] == "text"
    assert data["preview_id"] == "aaaaaaaaaaaa"
    assert "missing.jpg" in bot.sent_messages[-1]["text"]
    assert button_texts(bot.sent_messages[-1]["reply_markup"])[0] == [
        "←",
        "1 / 1",
        "→",
    ]


@pytest.mark.asyncio
async def test_invalid_cached_preview_id_regenerates_once(
    monkeypatch,
    settings: Settings,
    state: FSMContext,
    workspaces: WorkspaceManager,
) -> None:
    workspace = await workspaces.get_or_create(7)
    source = make_image(workspace.path / "input_001.jpg")
    files = [
        make_item(
            "aaaaaaaaaaaa",
            name="image.jpg",
            stored_name=source.name,
            photo_file_id="stale-photo-id",
        )
    ]
    data = await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids={"aaaaaaaaaaaa": "stale-preview-id"},
    )

    calls: list[str] = []
    real_write_preview = previews_module.write_preview

    def counting_write_preview(source_path, target, max_dimension=720):
        calls.append(source_path.name)
        return real_write_preview(source_path, target, max_dimension)

    monkeypatch.setattr(previews_module, "write_preview", counting_write_preview)

    bot = FakeBot()
    bot.invalid_photo_values = {"stale-preview-id", "stale-photo-id"}
    await render_images_queue(bot, 100, state, data, settings, workspaces)

    data = await state.get_data()
    assert calls == ["input_001.jpg"]
    assert bot.edited_media
    edited = bot.edited_media[-1]
    assert isinstance(edited["media"].media, FSInputFile)
    assert "▶ 1. image.jpg" in edited["media"].caption
    assert button_texts(edited["reply_markup"])[0] == ["←", "1 / 1", "→"]
    assert data["preview_file_ids"]["aaaaaaaaaaaa"] == "edited-10"
    assert "photo_file_id" not in data["files"][0]
    assert preview_path(workspace.path, "aaaaaaaaaaaa").is_file()
    assert data["queue_message_id"] == 10
    assert not bot.sent_photos


@pytest.mark.asyncio
async def test_invalid_cached_preview_id_falls_back_after_one_failed_regeneration(
    monkeypatch,
    settings: Settings,
    state: FSMContext,
    workspaces: WorkspaceManager,
) -> None:
    await workspaces.get_or_create(7)
    files = [
        make_item(
            "aaaaaaaaaaaa",
            name="missing.jpg",
            stored_name="missing.jpg",
            photo_file_id="stale-photo-id",
        )
    ]
    data = await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids={"aaaaaaaaaaaa": "stale-preview-id"},
    )

    calls: list[str] = []
    real_write_preview = previews_module.write_preview

    def counting_write_preview(source_path, target, max_dimension=720):
        calls.append(source_path.name)
        return real_write_preview(source_path, target, max_dimension)

    monkeypatch.setattr(previews_module, "write_preview", counting_write_preview)

    bot = FakeBot()
    bot.invalid_photo_values = {"stale-preview-id", "stale-photo-id"}
    await render_images_queue(bot, 100, state, data, settings, workspaces)

    data = await state.get_data()
    assert calls == ["missing.jpg"]
    assert not bot.sent_photos
    assert not bot.edited_media
    assert bot.sent_messages
    assert data["queue_kind"] == "text"
    assert data["queue_message_id"] != 10
    assert data["preview_file_ids"] == {}
    assert data["preview_unavailable"] == ["aaaaaaaaaaaa"]
    assert "photo_file_id" not in data["files"][0]
    assert "missing.jpg" in bot.sent_messages[-1]["text"]
    assert button_texts(bot.sent_messages[-1]["reply_markup"])[0] == [
        "←",
        "1 / 1",
        "→",
    ]
    assert (100, 10) in bot.deleted


@pytest.mark.asyncio
async def test_rapid_navigation_keeps_display_and_state_consistent(
    monkeypatch,
    settings: Settings,
    state: FSMContext,
    workspaces: WorkspaceManager,
    jobs: JobManager,
) -> None:
    files = [
        make_item("aaaaaaaaaaaa", name="a.jpg"),
        make_item("bbbbbbbbbbbb", name="b.jpg"),
        make_item("cccccccccccc", name="c.jpg"),
        make_item("dddddddddddd", name="d.jpg"),
    ]
    cached = {
        "aaaaaaaaaaaa": "file-a",
        "bbbbbbbbbbbb": "file-b",
        "cccccccccccc": "file-c",
        "dddddddddddd": "file-d",
    }
    await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
        preview_file_ids=cached,
    )
    original_ids = [item["id"] for item in files]
    answers: list[str | None] = []

    async def fake_answer(self, text=None, show_alert=False, **kwargs):
        answers.append(text)

    monkeypatch.setattr(CallbackQuery, "answer", fake_answer)

    bot = FakeBot()
    user = User(id=7, is_bot=False, first_name="Test")
    message = Message.model_construct(
        message_id=10,
        date=datetime.now(timezone.utc),
        chat=Chat(id=100, type="private"),
        from_user=user,
    )

    def callback(action: str) -> CallbackQuery:
        return CallbackQuery.model_construct(
            id=f"cb-{len(answers)}",
            from_user=user,
            chat_instance="chat-1",
            data=action,
            message=message,
        )

    middleware = UserSerializationMiddleware(UserLockManager())

    async def handler(event, _data):
        await images_control(event, bot, state, settings, jobs, workspaces)

    await asyncio.gather(
        *(middleware(handler, callback("images:next"), {}) for _ in range(5))
    )

    data = await state.get_data()
    assert [item["id"] for item in data["files"]] == original_ids
    assert bot.edited_media

    for entry in bot.edited_media:
        markers = re.findall(r"^▶ (\d+)\.", entry["media"].caption, re.MULTILINE)
        assert len(markers) == 1
        index = int(markers[0]) - 1
        assert entry["media"].media == cached[original_ids[index]]
        expected = images_queue_keyboard(len(files), active_index=index)
        assert entry["reply_markup"].model_dump() == expected.model_dump()

    final_markers = re.findall(
        r"^▶ (\d+)\.",
        bot.edited_media[-1]["media"].caption,
        re.MULTILINE,
    )
    final_index = int(final_markers[0]) - 1
    assert data["preview_id"] == original_ids[final_index]
    assert data["queue_message_id"] == 10
    assert data["queue_kind"] == "photo"
    assert final_index == len(files) - 1


@pytest.mark.asyncio
async def test_direct_image_upload_choice_opens_preview(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager
) -> None:
    workspace = await workspaces.get_or_create(7)
    path = make_image(workspace.path / "input_001.jpg")
    await state.set_state(UploadChoiceStates.choosing)
    await state.set_data(
        {
            "owner_id": 7,
            "kind": "image",
            "name": "photo.jpg",
            "stored_name": path.name,
            "size": path.stat().st_size,
            "photo_file_id": "photo-file-direct",
        }
    )
    bot = FakeBot()
    message = FakeQueueMessage(10)
    callback = FakeCallback("upload:choose:images", message=message)
    await choose_operation(
        callback=callback,
        bot=bot,
        state=state,
        settings=settings,
        workspaces=workspaces,
    )

    data = await state.get_data()
    assert await state.get_state() == ImagesStates.collecting.state
    assert len(data["files"]) == 1
    assert data["files"][0]["id"]
    assert data["preview_id"] == data["files"][0]["id"]
    assert data["queue_kind"] == "photo"
    assert bot.sent_photos
    assert bot.sent_photos[-1]["photo"] == "photo-file-direct"


def test_preview_callback_data_fits_telegram_limit() -> None:
    from app.keyboards.main import images_queue_keyboard

    markup = images_queue_keyboard(10, active_index=9)
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data is not None:
                assert len(button.callback_data.encode("utf-8")) <= 64


def test_thumbnail_respects_dimensions_orientation_and_transparency(
    tmp_path: Path,
) -> None:
    large = make_image(tmp_path / "large.jpg", size=(1600, 800))
    large_out = tmp_path / "large-preview.jpg"
    write_preview(large, large_out)
    with Image.open(large_out) as image:
        assert image.size == (720, 360)
        assert image.mode == "RGB"

    small = make_image(tmp_path / "small.jpg", size=(100, 50))
    small_out = tmp_path / "small-preview.jpg"
    write_preview(small, small_out)
    with Image.open(small_out) as image:
        assert image.size == (100, 50)

    transparent = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
    try:
        transparent_path = tmp_path / "transparent.png"
        transparent.save(transparent_path, "PNG")
    finally:
        transparent.close()
    transparent_out = tmp_path / "transparent-preview.jpg"
    write_preview(transparent_path, transparent_out)
    with Image.open(transparent_out) as image:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (255, 255, 255)

    rotated = Image.new("RGB", (800, 400), (10, 100, 200))
    try:
        exif = rotated.getexif()
        exif[0x0112] = 6
        rotated_path = tmp_path / "rotated.jpg"
        rotated.save(rotated_path, "JPEG", exif=exif)
    finally:
        rotated.close()
    rotated_out = tmp_path / "rotated-preview.jpg"
    write_preview(rotated_path, rotated_out)
    with Image.open(rotated_out) as image:
        assert image.size == (360, 720)


@pytest.mark.asyncio
async def test_thumbnail_generation_concurrency_is_bounded(
    monkeypatch, tmp_path: Path
) -> None:
    guard = threading.Lock()
    counters = {"active": 0, "peak": 0, "calls": 0}

    def blocking_write_preview(source, target, max_dimension=720):
        with guard:
            counters["active"] += 1
            counters["calls"] += 1
            counters["peak"] = max(counters["peak"], counters["active"])
        try:
            time.sleep(0.05)
            return target
        finally:
            with guard:
                counters["active"] -= 1

    monkeypatch.setattr(previews_module, "write_preview", blocking_write_preview)

    targets = [tmp_path / f"preview_{index}.jpg" for index in range(4)]
    results = await asyncio.gather(
        *(
            write_preview_async(tmp_path / f"source_{index}.jpg", target)
            for index, target in enumerate(targets)
        )
    )

    assert counters["calls"] == len(targets)
    assert 1 <= counters["peak"] <= PREVIEW_CONCURRENCY
    assert results == targets


@pytest.mark.asyncio
async def test_workspace_cleanup_removes_preview_files(
    settings: Settings, state: FSMContext, workspaces: WorkspaceManager
) -> None:
    workspace = await workspaces.get_or_create(7)
    source = make_image(workspace.path / "input_001.jpg")
    files = [
        make_item(
            "aaaaaaaaaaaa",
            name="image.jpg",
            stored_name=source.name,
        )
    ]
    data = await set_images_state(state, files, preview_id="aaaaaaaaaaaa")
    bot = FakeBot()
    await render_images_queue(bot, 100, state, data, settings, workspaces)
    thumb = preview_path(workspace.path, "aaaaaaaaaaaa")
    assert thumb.is_file()

    await workspaces.cleanup_user(7)
    assert not thumb.exists()
    assert not workspace.path.exists()


@pytest.mark.asyncio
async def test_cancel_invalidates_preview_state_and_callbacks(
    settings: Settings,
    state: FSMContext,
    workspaces: WorkspaceManager,
    jobs: JobManager,
) -> None:
    workspace = await workspaces.get_or_create(7)
    source = make_image(workspace.path / "input_001.jpg")
    files = [
        make_item(
            "aaaaaaaaaaaa",
            name="image.jpg",
            stored_name=source.name,
        )
    ]
    data = await set_images_state(
        state,
        files,
        preview_id="aaaaaaaaaaaa",
        queue_message_id=10,
        queue_kind="photo",
    )
    bot = FakeBot()
    await render_images_queue(bot, 100, state, data, settings, workspaces)
    preview_file = preview_path(workspace.path, "aaaaaaaaaaaa")
    assert preview_file.is_file()
    edits_before = len(bot.edited_media)

    await cancel_handler(
        message=FakeUserMessage(),
        state=state,
        settings=settings,
        jobs=jobs,
        workspaces=workspaces,
    )

    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert not workspace.path.exists()
    assert not preview_file.exists()
    callback = FakeCallback("images:next", message=FakeQueueMessage(10))
    await images_control(callback, bot, state, settings, jobs, workspaces)
    assert callback.answers[-1]["show_alert"] is True
    assert len(bot.edited_media) == edits_before
