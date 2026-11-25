"""Images-to-PDF workflow with id-based previews and ordering controls."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramRetryAfter,
)
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from app.config import Settings
from app.handlers.shared import (
    DownloadedFile,
    download_image,
    item_path,
    remove_item,
    total_size,
)
from app.keyboards.main import (
    back_to_menu_keyboard,
    images_queue_keyboard,
    rename_pdf_keyboard,
)
from app.services.errors import (
    JobAlreadyActiveError,
    JobCancelled,
    JobCapacityError,
    KayiPdfError,
    ProcessingTimeout,
)
from app.services.files import check_aggregate_size, deliver_document
from app.services.image_ops import images_to_pdf
from app.services.jobs import JobManager, JobSlot, run_cpu
from app.services.previews import (
    PREVIEW_MAX_DIMENSION,
    new_item_id,
    preview_path,
    write_preview_async,
)
from app.services.workspaces import JobWorkspace, WorkspaceManager
from app.states.workflows import ImagesStates
from app.texts import (
    cancelled_text,
    expired_text,
    images_result_text,
    images_text,
    need_more_files_text,
    rename_pdf_invalid_text,
    rename_pdf_text,
    status_text,
)
from app.utils.logging import get_logger
from app.utils.telegram import safe_delete, safe_edit_text
from app.utils.text import filename_stem, parse_pdf_filename, sanitize_pdf_filename

log = get_logger(__name__)
router = Router(name="images")


async def _state_data(state: FSMContext, user_id: int) -> dict | None:
    data = await state.get_data()
    if data.get("owner_id") != user_id or not isinstance(data.get("files"), list):
        return None
    return data


def _item_id(item: dict) -> str:
    value = item.get("id")
    if not isinstance(value, str) or not value:
        value = new_item_id()
        item["id"] = value
    return value


def _default_output_name(files: list[dict]) -> str:
    stem = filename_stem(files[0].get("name"), "images") if files else "images"
    return sanitize_pdf_filename(f"{stem}_images.pdf")


def _output_name(data: dict) -> str:
    custom = data.get("output_name")
    if isinstance(custom, str) and custom:
        safe = parse_pdf_filename(custom)
        if safe is not None:
            return safe
    files = data.get("files")
    return _default_output_name(files if isinstance(files, list) else [])


def _preview_index(files: list[dict], preview_id: str | None) -> int | None:
    if preview_id is None:
        return None
    for index, item in enumerate(files):
        if item.get("id") == preview_id:
            return index
    return None


def _normalise_preview(data: dict) -> tuple[list[dict], int | None]:
    files = data.get("files")
    if not isinstance(files, list):
        files = []
        data["files"] = files
    for item in files:
        _item_id(item)

    preview_id = data.get("preview_id")
    if not isinstance(preview_id, str) or not preview_id:
        preview_id = None
    index = _preview_index(files, preview_id)
    if index is None and files:
        preview_id = _item_id(files[0])
        index = 0
    if not files:
        preview_id = None
        index = None
    data["preview_id"] = preview_id

    ids = {str(item.get("id")) for item in files}
    cache = data.get("preview_file_ids")
    if not isinstance(cache, dict):
        cache = {}
    data["preview_file_ids"] = {
        key: value
        for key, value in cache.items()
        if key in ids and isinstance(value, str) and value
    }
    unavailable = data.get("preview_unavailable")
    if not isinstance(unavailable, list):
        unavailable = []
    data["preview_unavailable"] = [
        value for value in unavailable if isinstance(value, str) and value in ids
    ]
    queue_item_id = data.get("queue_item_id")
    if not isinstance(queue_item_id, str) or queue_item_id not in ids:
        data["queue_item_id"] = None
    output_name = data.get("output_name")
    if output_name is not None:
        safe_name = (
            parse_pdf_filename(output_name) if isinstance(output_name, str) else None
        )
        if safe_name is None:
            data.pop("output_name", None)
        else:
            data["output_name"] = safe_name
    return files, index


def _drop_preview_assets(workspace: JobWorkspace, data: dict, item_id: str) -> None:
    try:
        preview_path(workspace.path, item_id).unlink(missing_ok=True)
    except OSError:
        pass
    cache = data.get("preview_file_ids")
    if isinstance(cache, dict):
        cache.pop(item_id, None)
    unavailable = data.get("preview_unavailable")
    if isinstance(unavailable, list) and item_id in unavailable:
        unavailable.remove(item_id)


def _cache_preview_file_id(data: dict, item_id: str, sent) -> None:
    photos = getattr(sent, "photo", None) or []
    if not photos:
        return
    file_id = getattr(photos[-1], "file_id", None)
    if not isinstance(file_id, str) or not file_id:
        return
    cache = data.setdefault("preview_file_ids", {})
    if isinstance(cache, dict):
        cache[item_id] = file_id


def _invalidate_preview_source(
    workspace: JobWorkspace, data: dict, item: dict
) -> None:
    """Forget Telegram IDs and the local thumbnail rejected by Telegram."""

    _drop_preview_assets(workspace, data, _item_id(item))
    item.pop("photo_file_id", None)


async def _preview_media(
    workspace: JobWorkspace,
    data: dict,
    item: dict,
) -> str | FSInputFile | None:
    """Return a reusable file ID or a temporary thumbnail for one image."""

    item_id = _item_id(item)
    cache = data.get("preview_file_ids")
    if isinstance(cache, dict):
        cached = cache.get(item_id)
        if isinstance(cached, str) and cached:
            return cached

    # The file ID of a Telegram *photo* is safe to reuse.  Document file IDs
    # are not, so only explicitly recorded photo IDs are used here.
    photo_file_id = item.get("photo_file_id")
    if isinstance(photo_file_id, str) and photo_file_id:
        return photo_file_id

    unavailable = data.setdefault("preview_unavailable", [])
    if item_id in unavailable:
        return None
    target = preview_path(workspace.path, item_id)
    if not target.is_file():
        try:
            await write_preview_async(
                item_path(workspace, item),
                target,
                PREVIEW_MAX_DIMENSION,
            )
        except Exception as exc:
            log.warning(
                "Could not build an image preview: %s", type(exc).__name__
            )
            unavailable.append(item_id)
            return None
    return FSInputFile(target)


async def _delete_message(bot: Bot, chat_id: int, message_id: int | str) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=int(message_id))
    except (TelegramAPIError, TypeError, ValueError):
        pass


async def _render_photo_queue(
    bot: Bot,
    chat_id: int,
    data: dict,
    text: str,
    caption: str,
    markup: InlineKeyboardMarkup,
    media: str | FSInputFile,
    item: dict,
    workspace: JobWorkspace | None = None,
) -> None:
    item_id = _item_id(item)
    message_id = data.get("queue_message_id")
    kind = data.get("queue_kind")

    # The selected image has not changed (for example after reordering or
    # renaming): update only the caption and keyboard.  This avoids touching
    # the media and any Telegram upload path.
    if message_id and kind == "photo" and data.get("queue_item_id") == item_id:
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=int(message_id),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except TelegramRetryAfter as exc:
            log.warning(
                "Telegram flood control while editing a preview caption; retry in %ss",
                exc.retry_after,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            log.debug("Could not edit preview caption: %s", type(exc).__name__)
        except TelegramAPIError as exc:
            log.debug("Could not edit preview caption: %s", type(exc).__name__)
        else:
            return

    for attempt in range(2):
        if message_id and kind == "photo":
            try:
                sent = await bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    media=InputMediaPhoto(
                        media=media,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    ),
                    reply_markup=markup,
                )
            except TelegramRetryAfter as exc:
                log.warning(
                    "Telegram flood control while editing a preview; retry in %ss",
                    exc.retry_after,
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
                log.debug("Could not edit image preview: %s", type(exc).__name__)
            except TelegramAPIError as exc:
                log.debug("Could not edit image preview: %s", type(exc).__name__)
            else:
                _cache_preview_file_id(data, item_id, sent)
                data["queue_item_id"] = item_id
                return

        try:
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=media,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except TelegramRetryAfter as exc:
            log.warning(
                "Telegram flood control while sending a preview; retry in %ss",
                exc.retry_after,
            )
            return
        except TelegramBadRequest as exc:
            if (
                attempt == 0
                and isinstance(media, str)
                and workspace is not None
            ):
                # The cached Telegram file ID is no longer accepted.  Drop it
                # (and the original photo ID), rebuild one thumbnail, and retry
                # exactly once before degrading to the text workspace.
                log.info(
                    "Cached preview file ID was rejected; rebuilding one thumbnail"
                )
                _invalidate_preview_source(workspace, data, item)
                regenerated = await _preview_media(workspace, data, item)
                if regenerated is not None:
                    media = regenerated
                    continue
            log.warning("Could not send image preview: %s", type(exc).__name__)
            # Preview media must never block the existing workflow.
            await _render_text_queue(bot, chat_id, data, text, markup)
            return
        except TelegramAPIError as exc:
            log.warning("Could not send image preview: %s", type(exc).__name__)
            # Preview media must never block the existing workflow.
            await _render_text_queue(bot, chat_id, data, text, markup)
            return

        old_message_id = message_id
        _cache_preview_file_id(data, item_id, sent)
        data["queue_message_id"] = sent.message_id
        data["queue_kind"] = "photo"
        data["queue_item_id"] = item_id
        if old_message_id and int(old_message_id) != int(sent.message_id):
            await _delete_message(bot, chat_id, old_message_id)
        return


async def _render_text_queue(
    bot: Bot,
    chat_id: int,
    data: dict,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    message_id = data.get("queue_message_id")
    kind = data.get("queue_kind")

    if message_id and kind in {None, "text"}:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=text,
                reply_markup=markup,
            )
        except TelegramRetryAfter as exc:
            log.warning(
                "Telegram flood control while editing the image queue; retry in %ss",
                exc.retry_after,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            log.debug("Could not edit image queue: %s", type(exc).__name__)
        except TelegramAPIError as exc:
            log.debug("Could not edit image queue: %s", type(exc).__name__)
        else:
            return

    try:
        sent = await bot.send_message(chat_id, text, reply_markup=markup)
    except TelegramRetryAfter as exc:
        log.warning(
            "Telegram flood control while sending the image queue; retry in %ss",
            exc.retry_after,
        )
        return
    except TelegramAPIError as exc:
        log.warning("Could not send images queue: %s", type(exc).__name__)
        return

    old_message_id = message_id
    data["queue_message_id"] = sent.message_id
    data["queue_kind"] = "text"
    data["queue_item_id"] = None
    if old_message_id and int(old_message_id) != int(sent.message_id):
        await _delete_message(bot, chat_id, old_message_id)


async def render_images_queue(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    data: dict,
    settings: Settings,
    workspaces: WorkspaceManager,
) -> None:
    """Render the existing workspace as a photo when a preview is available."""

    files, active_index = _normalise_preview(data)
    active_id = data.get("preview_id")
    output_name = _output_name(data)
    text = images_text(
        files, settings, active_id=active_id, output_name=output_name
    )
    markup = images_queue_keyboard(len(files), active_index=active_index)

    media: str | FSInputFile | None = None
    item: dict | None = None
    workspace: JobWorkspace | None = None
    if files and active_index is not None:
        owner_id = data.get("owner_id")
        if isinstance(owner_id, int):
            try:
                workspace = await workspaces.get_or_create(owner_id)
            except KayiPdfError as exc:
                log.warning(
                    "Could not open a workspace for an image preview: %s",
                    type(exc).__name__,
                )
            except Exception:
                log.exception("Could not open a workspace for an image preview")
            else:
                item = files[active_index]
                media = await _preview_media(workspace, data, item)

    if media is not None and item is not None:
        caption = images_text(
            files,
            settings,
            active_id=active_id,
            output_name=output_name,
            limit=1000,
        )
        await _render_photo_queue(
            bot,
            chat_id,
            data,
            text,
            caption,
            markup,
            media,
            item,
            workspace,
        )
    else:
        await _render_text_queue(bot, chat_id, data, text, markup)
    await state.set_data(data)


async def _close_queue(bot: Bot, chat_id: int, data: dict) -> None:
    message_id = data.get("queue_message_id")
    if not message_id:
        return
    empty = InlineKeyboardMarkup(inline_keyboard=[])
    text = "✅ <b>PDF created.</b>\nUse /start for another task."
    try:
        if data.get("queue_kind") == "photo":
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=int(message_id),
                caption=text,
                reply_markup=empty,
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=text,
                reply_markup=empty,
            )
    except TelegramAPIError:
        pass


def _make_item(
    downloaded: DownloadedFile,
    *,
    photo_file_id: str | None = None,
) -> dict:
    item = {
        "id": new_item_id(),
        "name": downloaded.display_name,
        "stored_name": downloaded.path.name,
        "size": downloaded.size,
    }
    if photo_file_id:
        item["photo_file_id"] = photo_file_id
    return item


async def add_image_to_images(
    message: Message,
    bot: Bot,
    downloaded: DownloadedFile,
    state: FSMContext,
    workspaces: WorkspaceManager,
    settings: Settings,
    *,
    photo_file_id: str | None = None,
) -> None:
    user = message.from_user
    if user is None:
        return
    data = await _state_data(state, user.id)
    if data is None:
        await message.answer(expired_text(), reply_markup=back_to_menu_keyboard())
        await workspaces.cleanup_user(user.id)
        return
    files = data["files"]
    if len(files) >= settings.max_images:
        await message.answer(
            f"Images to PDF accepts up to {settings.max_images} images.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    check_aggregate_size(total_size(files), downloaded.size, settings)
    item = _make_item(downloaded, photo_file_id=photo_file_id)
    files.append(item)
    if not data.get("preview_id"):
        data["preview_id"] = item["id"]
    await state.set_data(data)
    await render_images_queue(bot, message.chat.id, state, data, settings, workspaces)


@router.message(ImagesStates.collecting, F.document | F.photo)
async def images_upload(
    message: Message,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    workspaces: WorkspaceManager,
) -> None:
    user = message.from_user
    if user is None:
        return
    data = await _state_data(state, user.id)
    if data is None:
        await message.answer(expired_text(), reply_markup=back_to_menu_keyboard())
        return
    files = data["files"]
    if len(files) >= settings.max_images:
        await message.answer(
            f"Images to PDF accepts up to {settings.max_images} images. "
            "Remove one before adding another.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    expected = None
    if message.document is not None:
        expected = message.document.file_size
    elif message.photo:
        expected = getattr(message.photo[-1], "file_size", None)
    if expected:
        try:
            check_aggregate_size(total_size(files), expected, settings)
        except KayiPdfError as exc:
            await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())
            return
    downloaded = None
    try:
        workspace = await workspaces.get_or_create(user.id)
        downloaded = await download_image(message, bot, workspace, settings)
        check_aggregate_size(total_size(files), downloaded.size, settings)
    except KayiPdfError as exc:
        if downloaded is not None:
            downloaded.path.unlink(missing_ok=True)
        if not files:
            await workspaces.cleanup_user(user.id)
        await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())
        return
    except Exception:
        log.exception("Unexpected error while adding an image")
        if downloaded is not None:
            downloaded.path.unlink(missing_ok=True)
        if not files:
            await workspaces.cleanup_user(user.id)
        await message.answer(
            "I couldn't add that image. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    photo_file_id = message.photo[-1].file_id if message.photo else None
    try:
        await add_image_to_images(
            message,
            bot,
            downloaded,
            state,
            workspaces,
            settings,
            photo_file_id=photo_file_id,
        )
    except KayiPdfError as exc:
        try:
            downloaded.path.unlink(missing_ok=True)
        except OSError:
            pass
        await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())


@router.message(
    ImagesStates.collecting,
    F.text & ~F.text.startswith("/"),
)
async def images_text_hint(
    message: Message,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    workspaces: WorkspaceManager,
) -> None:
    data = await state.get_data()
    if message.from_user is not None and data.get("owner_id") != message.from_user.id:
        return
    if not isinstance(data.get("files"), list):
        data["files"] = []
    await render_images_queue(bot, message.chat.id, state, data, settings, workspaces)


def _callback_matches_queue(callback: CallbackQuery, data: dict) -> bool:
    message = callback.message
    if message is None:
        return True
    current = data.get("queue_message_id")
    if not current:
        return True
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return True
    try:
        return int(message_id) == int(current)
    except (TypeError, ValueError):
        return True


@router.callback_query(ImagesStates.collecting, F.data.startswith("images:"))
async def images_control(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> None:
    user = callback.from_user
    if user is None:
        return
    data = await _state_data(state, user.id)
    if data is None or not _callback_matches_queue(callback, data):
        await callback.answer(expired_text(), show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    files, current = _normalise_preview(data)

    async def rerender() -> None:
        if callback.message is not None:
            await render_images_queue(
                bot,
                callback.message.chat.id,
                state,
                data,
                settings,
                workspaces,
            )

    if action in {"prev", "next"}:
        if not files or current is None:
            await callback.answer(expired_text(), show_alert=True)
            return
        target = current - 1 if action == "prev" else current + 1
        if not 0 <= target < len(files):
            # Boundary: acknowledge without editing the workspace message.
            await callback.answer()
            return
        data["preview_id"] = _item_id(files[target])
        await state.set_data(data)
        await callback.answer()
        await rerender()
        return

    if action == "select":
        try:
            target = int(parts[2])
        except (ValueError, IndexError):
            await callback.answer(expired_text(), show_alert=True)
            return
        if not files or not 0 <= target < len(files):
            await callback.answer(expired_text(), show_alert=True)
            return
        if target == current:
            await callback.answer()
            return
        data["preview_id"] = _item_id(files[target])
        await state.set_data(data)
        await callback.answer()
        await rerender()
        return

    if action in {"up", "down"}:
        if not files or current is None:
            await callback.answer(expired_text(), show_alert=True)
            return
        target = current - 1 if action == "up" else current + 1
        if not 0 <= target < len(files):
            # Boundary: acknowledge without changing the queue.
            await callback.answer()
            return
        files[current], files[target] = files[target], files[current]
        data["files"] = files
        await state.set_data(data)
        await callback.answer()
        await rerender()
        return

    if action == "remove":
        if not files or current is None:
            await callback.answer(expired_text(), show_alert=True)
            return
        workspace = await workspaces.get_or_create(user.id)
        removed = files[current]
        removed_id = _item_id(removed)
        await remove_item(workspace, files, current)
        _drop_preview_assets(workspace, data, removed_id)
        if files:
            data["preview_id"] = _item_id(
                files[current] if current < len(files) else files[current - 1]
            )
        else:
            data["preview_id"] = None
            data.pop("output_name", None)
        data["files"] = files
        await state.set_data(data)
        await callback.answer()
        await rerender()
        return

    if action == "pos":
        if not files or current is None:
            await callback.answer(expired_text(), show_alert=True)
            return
        await callback.answer(f"{current + 1} / {len(files)}")
        return

    if action == "rename":
        if not files:
            await callback.answer(need_more_files_text(1), show_alert=True)
            return
        await callback.answer()
        await state.set_state(ImagesStates.renaming)
        sent = await bot.send_message(
            callback.message.chat.id if callback.message else user.id,
            rename_pdf_text(_output_name(data)),
            reply_markup=rename_pdf_keyboard(),
        )
        await state.update_data(rename_message_id=sent.message_id)
        return

    if action == "go":
        if not files:
            await callback.answer(need_more_files_text(1), show_alert=True)
            return
        await callback.answer()
        await _process_images(callback, bot, state, data, settings, jobs, workspaces)
        return

    await callback.answer(expired_text(), show_alert=True)


@router.callback_query(ImagesStates.renaming, F.data == "images:rename:back")
async def images_rename_back(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
) -> None:
    user = callback.from_user
    if user is None:
        return
    data = await _state_data(state, user.id)
    if data is None:
        await callback.answer(expired_text(), show_alert=True)
        return
    await callback.answer()
    prompt_id = data.pop("rename_message_id", None)
    await state.set_state(ImagesStates.collecting)
    await state.set_data(data)
    if isinstance(prompt_id, int):
        chat_id = callback.message.chat.id if callback.message else user.id
        await _delete_message(bot, chat_id, prompt_id)


@router.message(ImagesStates.renaming, F.text & ~F.text.startswith("/"))
async def images_rename_text(
    message: Message,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    workspaces: WorkspaceManager,
) -> None:
    user = message.from_user
    if user is None:
        return
    data = await _state_data(state, user.id)
    if data is None:
        await message.answer(expired_text(), reply_markup=back_to_menu_keyboard())
        await workspaces.cleanup_user(user.id)
        return
    filename = parse_pdf_filename(message.text)
    if filename is None:
        await message.answer(
            rename_pdf_invalid_text(_output_name(data)),
            reply_markup=rename_pdf_keyboard(),
        )
        return
    data["output_name"] = filename
    prompt_id = data.pop("rename_message_id", None)
    await state.set_state(ImagesStates.collecting)
    await state.set_data(data)
    if isinstance(prompt_id, int):
        await _delete_message(bot, message.chat.id, prompt_id)
    await render_images_queue(
        bot,
        message.chat.id,
        state,
        data,
        settings,
        workspaces,
    )


@router.message(ImagesStates.renaming, F.document | F.photo)
async def images_rename_media_hint(message: Message) -> None:
    await message.answer(
        "Send a filename for the PDF, or tap Back to return to the images.",
        reply_markup=rename_pdf_keyboard(),
    )


@router.message(ImagesStates.renaming)
async def images_rename_hint(message: Message) -> None:
    await message.answer(
        "Send a filename for the PDF, or tap Back to return to the images.",
        reply_markup=rename_pdf_keyboard(),
    )


async def _process_images(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    data: dict,
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> None:
    user = callback.from_user
    if user is None or callback.message is None:
        return
    chat_id = callback.message.chat.id
    files = list(data["files"])
    status = await bot.send_message(
        chat_id,
        status_text(f"Creating a PDF from {len(files)} images"),
    )
    slot: JobSlot | None = None
    workspace = None
    try:
        slot = await jobs.acquire(user.id)
        workspace = await workspaces.get_or_create(user.id)
        paths = [item_path(workspace, item) for item in files]
        display_name = _output_name(data)
        output = await workspace.new_output_path("output.pdf")
        await run_cpu(
            images_to_pdf,
            paths,
            output,
            settings,
            slot.token,
            timeout=settings.processing_timeout,
            token=slot.token,
            cleanup_on_timeout=lambda ws=workspace: workspaces.cleanup_workspace_sync(ws),
        )
        await deliver_document(
            bot,
            chat_id,
            output,
            display_name,
            caption=images_result_text(len(files)),
            settings=settings,
            reply_markup=back_to_menu_keyboard(),
        )
        await safe_delete(status)
        await _close_queue(bot, chat_id, data)
        await state.clear()
        await workspaces.cleanup_user(user.id)
    except ProcessingTimeout as exc:
        await safe_edit_text(
            status, exc.user_message, reply_markup=back_to_menu_keyboard()
        )
        await state.clear()
        if workspace is not None:
            workspaces.detach(workspace)
        if not exc.cleanup_deferred:
            await workspaces.cleanup_user(user.id)
    except JobCancelled:
        await safe_edit_text(
            status, cancelled_text(), reply_markup=back_to_menu_keyboard()
        )
        await _close_queue(bot, chat_id, data)
        await state.clear()
        await workspaces.cleanup_user(user.id)
    except (JobCapacityError, JobAlreadyActiveError) as exc:
        await safe_edit_text(
            status, exc.user_message, reply_markup=back_to_menu_keyboard()
        )
    except KayiPdfError as exc:
        await safe_edit_text(
            status, exc.user_message, reply_markup=back_to_menu_keyboard()
        )
        await state.clear()
        await workspaces.cleanup_user(user.id)
    except Exception:
        log.exception("Unexpected images-to-PDF failure")
        await safe_edit_text(
            status,
            "Something went wrong while creating the PDF. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        await state.clear()
        await workspaces.cleanup_user(user.id)
    finally:
        if slot is not None:
            await slot.release()
