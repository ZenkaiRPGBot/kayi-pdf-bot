"""Extract workflow: keep selected pages in one new PDF."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.config import Settings
from app.handlers.shared import download_pdf, item_path
from app.keyboards.main import back_to_menu_keyboard
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
from app.services.pdf_ops import extract_pages
from app.services.workspaces import WorkspaceManager
from app.states.workflows import ExtractStates
from app.texts import (
    cancelled_text,
    expired_text,
    extract_ready_text,
    extract_result_text,
    status_text,
)
from app.utils.logging import get_logger
from app.utils.telegram import safe_delete, safe_edit_text
from app.utils.text import filename_stem

log = get_logger(__name__)
router = Router(name="extract")


async def _state_data(state: FSMContext, user_id: int) -> dict | None:
    data = await state.get_data()
    if data.get("owner_id") != user_id:
        return None
    return data


@router.message(ExtractStates.waiting_file, F.document | F.photo)
async def extract_upload(
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
        log.exception("Unexpected error while adding an extract source")
        await workspaces.cleanup_user(user.id)
        await message.answer(
            "I couldn't process that PDF. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    await state.set_state(ExtractStates.waiting_pages)
    await state.set_data(
        {
            "owner_id": user.id,
            "name": downloaded.display_name,
            "stored_name": downloaded.path.name,
            "size": downloaded.size,
            "page_count": downloaded.page_count,
        }
    )
    await message.answer(
        extract_ready_text(downloaded.display_name, downloaded.page_count or 0),
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(ExtractStates.waiting_file, F.text & ~F.text.startswith("/"))
async def extract_waiting_file_text(message: Message) -> None:
    await message.answer(
        "Send one PDF file first.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(ExtractStates.waiting_pages, F.document | F.photo)
async def extract_second_upload(message: Message) -> None:
    await message.answer(
        "You already sent a PDF. Send the pages to keep, for example "
        "<code>1,3,5-8</code>.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(ExtractStates.waiting_pages, F.text & ~F.text.startswith("/"))
async def extract_pages_input(
    message: Message,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> None:
    user = message.from_user
    if user is None:
        return
    data = await _state_data(state, user.id)
    if data is None or not data.get("stored_name"):
        await message.answer(expired_text(), reply_markup=back_to_menu_keyboard())
        return
    page_count = int(data.get("page_count") or 0)
    try:
        pages = parse_page_selection(
            message.text or "",
            page_count,
            max_pages=settings.max_pages,
            allow_duplicates=False,
            example="1,3,5-8",
        )
    except KayiPdfError as exc:
        await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())
        return
    await _process_extract(
        message, bot, state, data, pages, settings, jobs, workspaces
    )


async def _process_extract(
    message: Message,
    bot: Bot,
    state: FSMContext,
    data: dict,
    pages: list[int],
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> None:
    user = message.from_user
    if user is None:
        return
    chat_id = message.chat.id
    status = await bot.send_message(chat_id, status_text("Extracting pages"))
    slot: JobSlot | None = None
    workspace = None
    try:
        slot = await jobs.acquire(user.id)
        workspace = await workspaces.get_or_create(user.id)
        source = item_path(workspace, data)
        stem = filename_stem(data.get("name"), "document")
        output = await workspace.new_output_path(f"{stem}_extracted.pdf")
        await run_cpu(
            extract_pages,
            source,
            pages,
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
            caption=extract_result_text(len(pages)),
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
        log.exception("Unexpected extract failure")
        await safe_edit_text(
            status,
            "Something went wrong while extracting pages. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        await state.clear()
        await workspaces.cleanup_user(user.id)
    finally:
        if slot is not None:
            await slot.release()
