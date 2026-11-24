"""Merge workflow: collect, reorder, remove, then merge in one CPU job."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.config import Settings
from app.handlers.shared import (
    DownloadedFile,
    download_pdf,
    item_path,
    remove_item,
    total_size,
)
from app.keyboards.main import back_to_menu_keyboard, merge_queue_keyboard
from app.services.errors import (
    FileValidationError,
    JobAlreadyActiveError,
    JobCancelled,
    JobCapacityError,
    KayiPdfError,
    ProcessingTimeout,
)
from app.services.files import check_aggregate_size, deliver_document
from app.services.jobs import JobManager, JobSlot, run_cpu
from app.services.pdf_ops import merge_pdfs
from app.services.workspaces import WorkspaceManager
from app.states.workflows import MergeStates
from app.texts import (
    cancelled_text,
    expired_text,
    merge_result_text,
    merge_text,
    need_more_files_text,
    status_text,
)
from app.utils.logging import get_logger
from app.utils.telegram import safe_delete, safe_edit_text
from app.utils.text import filename_stem

log = get_logger(__name__)
router = Router(name="merge")


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
    text = merge_text(files, settings)
    markup = merge_queue_keyboard(len(files))
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
        log.warning("Could not send merge queue: %s", type(exc).__name__)
        return
    await state.update_data(queue_message_id=sent.message_id)


async def _close_queue(bot: Bot, chat_id: int, data: dict, text: str) -> None:
    message_id = data.get("queue_message_id")
    if not message_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=int(message_id),
            text=text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )
    except TelegramAPIError:
        pass


def _make_item(downloaded: DownloadedFile) -> dict:
    return {
        "name": downloaded.display_name,
        "stored_name": downloaded.path.name,
        "size": downloaded.size,
        "page_count": downloaded.page_count or 0,
        "digest": downloaded.digest,
    }


async def add_pdf_to_merge(
    message: Message,
    bot: Bot,
    downloaded: DownloadedFile,
    state: FSMContext,
    workspaces: WorkspaceManager,
    settings: Settings,
) -> None:
    """Append an already-downloaded PDF (also used by direct uploads)."""

    user = message.from_user
    if user is None:
        return
    data = await _state_data(state, user.id)
    if data is None:
        await message.answer(expired_text(), reply_markup=back_to_menu_keyboard())
        await workspaces.cleanup_user(user.id)
        return
    files = data["files"]
    if len(files) >= settings.max_files_merge:
        await message.answer(
            f"Merge accepts up to {settings.max_files_merge} PDFs.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    if downloaded.digest and any(
        item.get("digest") == downloaded.digest for item in files
    ):
        raise FileValidationError("That PDF is already in the merge list.")
    check_aggregate_size(total_size(files), downloaded.size, settings)
    files.append(_make_item(downloaded))
    await state.set_data(data)
    await _render_queue(bot, message.chat.id, state, data, settings)


@router.message(MergeStates.collecting, F.document | F.photo)
async def merge_upload(
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
    if message.photo or not message.document:
        await message.answer(
            "Please send a PDF file. Photos cannot be merged as documents.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    files = data["files"]
    if len(files) >= settings.max_files_merge:
        await message.answer(
            f"Merge accepts up to {settings.max_files_merge} PDFs. "
            "Remove one before adding another.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    expected = message.document.file_size
    if expected:
        try:
            check_aggregate_size(total_size(files), expected, settings)
        except KayiPdfError as exc:
            await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())
            return
    downloaded = None
    try:
        workspace = await workspaces.get_or_create(user.id)
        downloaded = await download_pdf(message, bot, workspace, settings)
        check_aggregate_size(total_size(files), downloaded.size, settings)
    except KayiPdfError as exc:
        if downloaded is not None:
            downloaded.path.unlink(missing_ok=True)
        if not files:
            await workspaces.cleanup_user(user.id)
        await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())
        return
    except Exception:
        log.exception("Unexpected error while adding a merge file")
        if downloaded is not None:
            downloaded.path.unlink(missing_ok=True)
        if not files:
            await workspaces.cleanup_user(user.id)
        await message.answer(
            "I couldn't add that PDF. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    try:
        await add_pdf_to_merge(message, bot, downloaded, state, workspaces, settings)
    except KayiPdfError as exc:
        try:
            downloaded.path.unlink(missing_ok=True)
        except OSError:
            pass
        await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())


@router.message(
    MergeStates.collecting,
    F.text & ~F.text.startswith("/"),
)
async def merge_text_hint(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    if message.from_user is not None and data.get("owner_id") != message.from_user.id:
        return
    files = data.get("files") if isinstance(data.get("files"), list) else []
    await message.answer(
        merge_text(files, settings),
        reply_markup=merge_queue_keyboard(len(files)),
    )


@router.callback_query(MergeStates.collecting, F.data.startswith("merge:"))
async def merge_control(
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
        if len(data["files"]) < 2:
            await callback.answer(need_more_files_text(2), show_alert=True)
            return
        await callback.answer()
        await _process_merge(callback, bot, state, data, settings, jobs, workspaces)
        return
    await callback.answer(expired_text(), show_alert=True)


async def _process_merge(
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
        chat_id, status_text(f"Merging {len(files)} PDFs"), parse_mode="HTML"
    )
    slot: JobSlot | None = None
    workspace = None
    cleanup_now = True
    try:
        slot = await jobs.acquire(user.id)
        workspace = await workspaces.get_or_create(user.id)
        paths = [item_path(workspace, item) for item in files]
        first_stem = filename_stem(files[0].get("name"), "merged")
        output = await workspace.new_output_path(f"merged_{first_stem}.pdf")
        await run_cpu(
            merge_pdfs,
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
            caption=merge_result_text(len(files)),
            settings=settings,
            reply_markup=back_to_menu_keyboard(),
        )
        await safe_delete(status)
        await _close_queue(
            bot,
            chat_id,
            data,
            "✅ <b>Merge complete.</b>\nUse /start for another task.",
        )
        await state.clear()
        await workspaces.cleanup_user(user.id)
    except ProcessingTimeout as exc:
        cleanup_now = not exc.cleanup_deferred
        await safe_edit_text(
            status, exc.user_message, reply_markup=back_to_menu_keyboard()
        )
        await state.clear()
        if workspace is not None:
            workspaces.detach(workspace)
        if cleanup_now:
            await workspaces.cleanup_user(user.id)
    except JobCancelled:
        await safe_edit_text(
            status, cancelled_text(), reply_markup=back_to_menu_keyboard()
        )
        await _close_queue(bot, chat_id, data, "Task cancelled.")
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
        log.exception("Unexpected merge failure")
        await safe_edit_text(
            status,
            "Something went wrong while merging. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        await state.clear()
        await workspaces.cleanup_user(user.id)
    finally:
        if slot is not None:
            await slot.release()
