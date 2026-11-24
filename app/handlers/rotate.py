"""Rotate workflow: all pages or a validated page selection."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.handlers.shared import download_pdf, item_path
from app.keyboards.main import back_to_menu_keyboard, rotate_keyboard
from app.services.errors import (
    JobAlreadyActiveError,
    JobCancelled,
    JobCapacityError,
    KayiPdfError,
    ProcessingTimeout,
)
from app.services.files import deliver_document
from app.services.jobs import JobManager, JobSlot, run_cpu
from app.services.page_ranges import parse_page_selection
from app.services.pdf_ops import rotate_pdf
from app.services.workspaces import WorkspaceManager
from app.states.workflows import RotateStates
from app.texts import (
    cancelled_text,
    expired_text,
    rotate_pages_prompt_text,
    rotate_ready_text,
    rotate_result_text,
    status_text,
)
from app.utils.logging import get_logger
from app.utils.telegram import safe_delete, safe_edit_text
from app.utils.text import filename_stem

log = get_logger(__name__)
router = Router(name="rotate")


async def _state_data(state: FSMContext, user_id: int) -> dict | None:
    data = await state.get_data()
    if data.get("owner_id") != user_id:
        return None
    return data


async def _show_rotate_menu(
    bot: Bot,
    chat_id: int,
    message_id: int | None,
    data: dict,
) -> None:
    text = rotate_ready_text(
        str(data.get("name", "document.pdf")),
        int(data.get("page_count") or 0),
        data.get("selected_pages"),
    )
    markup = rotate_keyboard(data.get("selected_pages"))
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=markup,
            )
            return
        except TelegramAPIError:
            pass
    await bot.send_message(chat_id, text, reply_markup=markup)


@router.message(RotateStates.waiting_file, F.document | F.photo)
async def rotate_upload(
    message: Message,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    workspaces: WorkspaceManager,
) -> None:
    user = message.from_user
    if user is None:
        return
    if message.photo or not message.document:
        await message.answer(
            "Please send a PDF file, not an image.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    try:
        workspace = await workspaces.get_or_create(user.id)
        downloaded = await download_pdf(message, bot, workspace, settings)
    except KayiPdfError as exc:
        await workspaces.cleanup_user(user.id)
        await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())
        return
    except Exception:
        log.exception("Unexpected error while adding a rotate source")
        await workspaces.cleanup_user(user.id)
        await message.answer(
            "I couldn't process that PDF. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    await state.set_state(RotateStates.choosing)
    await state.set_data(
        {
            "owner_id": user.id,
            "name": downloaded.display_name,
            "stored_name": downloaded.path.name,
            "size": downloaded.size,
            "page_count": downloaded.page_count,
            "selected_pages": None,
            "scope_message_id": None,
        }
    )
    await message.answer(
        rotate_ready_text(
            downloaded.display_name, downloaded.page_count or 0, None
        ),
        reply_markup=rotate_keyboard(None),
    )


@router.message(RotateStates.waiting_file, F.text & ~F.text.startswith("/"))
async def rotate_waiting_file_text(message: Message) -> None:
    await message.answer(
        "Send one PDF file first.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(RotateStates.choosing, F.document | F.photo)
@router.message(RotateStates.waiting_pages, F.document | F.photo)
async def rotate_second_upload(message: Message) -> None:
    await message.answer(
        "You already sent a PDF. Choose a rotation or use /cancel to start over.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.callback_query(
    RotateStates.choosing,
    F.data.in_({"rotate:scope:all", "rotate:scope:select"}),
)
async def rotate_scope(
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
    if callback.data == "rotate:scope:select":
        await callback.answer()
        await state.set_state(RotateStates.waiting_pages)
        await state.update_data(scope_message_id=callback.message.message_id if callback.message else None)
        text = rotate_pages_prompt_text(
            str(data.get("name", "document.pdf")), int(data.get("page_count") or 0)
        )
        if callback.message is not None:
            await safe_edit_text(callback.message, text, reply_markup=back_to_menu_keyboard())
        return
    await callback.answer()
    data["selected_pages"] = None
    await state.set_data(data)
    if callback.message is not None:
        await _show_rotate_menu(
            bot, callback.message.chat.id, callback.message.message_id, data
        )


@router.message(RotateStates.waiting_pages, F.text & ~F.text.startswith("/"))
async def rotate_pages_input(
    message: Message,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
) -> None:
    user = message.from_user
    if user is None:
        return
    data = await _state_data(state, user.id)
    if data is None or not data.get("stored_name"):
        await message.answer(expired_text(), reply_markup=back_to_menu_keyboard())
        return
    try:
        pages = parse_page_selection(
            message.text or "",
            int(data.get("page_count") or 0),
            max_pages=settings.max_pages,
            allow_duplicates=False,
            example="1,3-5",
        )
    except KayiPdfError as exc:
        await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())
        return
    data["selected_pages"] = pages
    await state.set_data(data)
    await state.set_state(RotateStates.choosing)
    message_id = data.get("scope_message_id")
    if message_id:
        await _show_rotate_menu(bot, message.chat.id, int(message_id), data)
    else:
        await _show_rotate_menu(bot, message.chat.id, None, data)


@router.callback_query(RotateStates.choosing, F.data.startswith("rotate:angle:"))
async def rotate_angle(
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
    try:
        angle = int((callback.data or "").rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer(expired_text(), show_alert=True)
        return
    if angle not in {90, 180, 270}:
        await callback.answer(expired_text(), show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    await callback.answer()
    await _process_rotate(
        callback,
        bot,
        state,
        data,
        angle,
        settings,
        jobs,
        workspaces,
    )


@router.message(RotateStates.choosing, F.text & ~F.text.startswith("/"))
async def rotate_choosing_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.answer(
        rotate_ready_text(
            str(data.get("name", "document.pdf")),
            int(data.get("page_count") or 0),
            data.get("selected_pages"),
        ),
        reply_markup=rotate_keyboard(data.get("selected_pages")),
    )


async def _process_rotate(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    data: dict,
    angle: int,
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> None:
    user = callback.from_user
    if user is None or callback.message is None:
        return
    chat_id = callback.message.chat.id
    selected = data.get("selected_pages")
    status = await bot.send_message(
        chat_id,
        status_text(f"Rotating pages by {angle}°"),
    )
    slot: JobSlot | None = None
    workspace = None
    try:
        slot = await jobs.acquire(user.id)
        workspace = await workspaces.get_or_create(user.id)
        source = item_path(workspace, data)
        stem = filename_stem(data.get("name"), "document")
        output = await workspace.new_output_path(f"{stem}_rotated.pdf")
        await run_cpu(
            rotate_pdf,
            source,
            selected,
            angle,
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
            caption=rotate_result_text(len(selected) if selected else None, angle),
            settings=settings,
            reply_markup=back_to_menu_keyboard(),
        )
        await safe_delete(status)
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
        log.exception("Unexpected rotate failure")
        await safe_edit_text(
            status,
            "Something went wrong while rotating. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        await state.clear()
        await workspaces.cleanup_user(user.id)
    finally:
        if slot is not None:
            await slot.release()
