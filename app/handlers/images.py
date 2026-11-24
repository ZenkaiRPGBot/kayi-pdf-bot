"""Images-to-PDF workflow with ordering and removal controls."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.config import Settings
from app.handlers.shared import (
    DownloadedFile,
    download_image,
    item_path,
    remove_item,
    total_size,
)
from app.keyboards.main import back_to_menu_keyboard, images_queue_keyboard
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
from app.services.workspaces import WorkspaceManager
from app.states.workflows import ImagesStates
from app.texts import (
    cancelled_text,
    expired_text,
    images_result_text,
    images_text,
    need_more_files_text,
    status_text,
)
from app.utils.logging import get_logger
from app.utils.telegram import safe_delete, safe_edit_text
from app.utils.text import filename_stem

log = get_logger(__name__)
router = Router(name="images")


async def _state_data(state: FSMContext, user_id: int) -> dict | None:
    data = await state.get_data()
    if data.get("owner_id") != user_id or not isinstance(data.get("files"), list):
        return None
    return data


async def _render_queue(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    data: dict,
    settings: Settings,
) -> None:
    files = data["files"]
    text = images_text(files, settings)
    markup = images_queue_keyboard(len(files))
    message_id = data.get("queue_message_id")
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=text,
                reply_markup=markup,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
        except TelegramAPIError:
            pass
    try:
        sent = await bot.send_message(chat_id, text, reply_markup=markup)
    except TelegramAPIError as exc:
        log.warning("Could not send images queue: %s", type(exc).__name__)
        return
    await state.update_data(queue_message_id=sent.message_id)


async def _close_queue(bot: Bot, chat_id: int, data: dict) -> None:
    message_id = data.get("queue_message_id")
    if not message_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=int(message_id),
            text="✅ <b>PDF created.</b>\nUse /start for another task.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )
    except TelegramAPIError:
        pass


def _make_item(downloaded: DownloadedFile) -> dict:
    return {
        "name": downloaded.display_name,
        "stored_name": downloaded.path.name,
        "size": downloaded.size,
    }


async def add_image_to_images(
    message: Message,
    bot: Bot,
    downloaded: DownloadedFile,
    state: FSMContext,
    workspaces: WorkspaceManager,
    settings: Settings,
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
    files.append(_make_item(downloaded))
    await state.set_data(data)
    await _render_queue(bot, message.chat.id, state, data, settings)


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
    try:
        await add_image_to_images(
            message, bot, downloaded, state, workspaces, settings
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
async def images_text_hint(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    if message.from_user is not None and data.get("owner_id") != message.from_user.id:
        return
    files = data.get("files") if isinstance(data.get("files"), list) else []
    await message.answer(
        images_text(files, settings),
        reply_markup=images_queue_keyboard(len(files)),
    )


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
    if data is None:
        await callback.answer(expired_text(), show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action in {"up", "down", "remove"}:
        try:
            index = int(parts[2])
        except (ValueError, IndexError):
            await callback.answer(expired_text(), show_alert=True)
            return
        files = data["files"]
        if not 0 <= index < len(files):
            await callback.answer(expired_text(), show_alert=True)
            return
        await callback.answer()
        if action == "remove":
            workspace = await workspaces.get_or_create(user.id)
            await remove_item(workspace, files, index)
        elif action == "up" and index > 0:
            files[index - 1], files[index] = files[index], files[index - 1]
        elif action == "down" and index < len(files) - 1:
            files[index], files[index + 1] = files[index + 1], files[index]
        data["files"] = files
        await state.set_data(data)
        if callback.message is not None:
            await _render_queue(bot, callback.message.chat.id, state, data, settings)
        return
    if action == "go":
        if not data["files"]:
            await callback.answer(need_more_files_text(1), show_alert=True)
            return
        await callback.answer()
        await _process_images(callback, bot, state, data, settings, jobs, workspaces)
        return
    await callback.answer(expired_text(), show_alert=True)


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
        stem = filename_stem(files[0].get("name"), "images")
        output = await workspace.new_output_path(f"{stem}_images.pdf")
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
            output.name,
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
