"""Split workflow: download one PDF, parse ranges, return PDFs or a ZIP."""

from __future__ import annotations

import uuid

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
from app.services.files import create_zip, deliver_document
from app.services.jobs import JobManager, JobSlot, run_cpu
from app.services.page_ranges import parse_split_ranges
from app.services.pdf_ops import split_pdf
from app.services.workspaces import WorkspaceManager
from app.states.workflows import SplitStates
from app.texts import (
    cancelled_text,
    expired_text,
    split_ready_text,
    split_result_text,
    status_text,
)
from app.utils.logging import get_logger
from app.utils.telegram import safe_delete, safe_edit_text
from app.utils.text import filename_stem

log = get_logger(__name__)
router = Router(name="split")


async def _state_data(state: FSMContext, user_id: int) -> dict | None:
    data = await state.get_data()
    if data.get("owner_id") != user_id:
        return None
    return data


@router.message(SplitStates.waiting_file, F.document | F.photo)
async def split_upload(
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
        log.exception("Unexpected error while adding a split source")
        await workspaces.cleanup_user(user.id)
        await message.answer(
            "I couldn't process that PDF. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    await state.set_state(SplitStates.waiting_ranges)
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
        split_ready_text(downloaded.display_name, downloaded.page_count or 0, settings),
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(SplitStates.waiting_file, F.text & ~F.text.startswith("/"))
async def split_waiting_file_text(message: Message) -> None:
    await message.answer(
        "Send one PDF file first.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(SplitStates.waiting_ranges, F.document | F.photo)
async def split_second_upload(message: Message) -> None:
    await message.answer(
        "You already sent a PDF. Send page ranges, for example "
        "<code>1-3,4-6</code>, or tap Main Menu to cancel.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(SplitStates.waiting_ranges, F.text & ~F.text.startswith("/"))
async def split_ranges(
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
    if data is None:
        await message.answer(expired_text(), reply_markup=back_to_menu_keyboard())
        return
    page_count = int(data.get("page_count") or 0)
    try:
        ranges = parse_split_ranges(
            message.text or "",
            page_count,
            max_outputs=settings.max_split_outputs,
            max_total_pages=settings.max_split_total_pages,
        )
    except KayiPdfError as exc:
        await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())
        return
    await _process_split(
        message,
        bot,
        state,
        data,
        ranges,
        settings,
        jobs,
        workspaces,
    )


async def _process_split(
    message: Message,
    bot: Bot,
    state: FSMContext,
    data: dict,
    ranges: list[list[int]],
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> None:
    user = message.from_user
    if user is None:
        return
    chat_id = message.chat.id
    page_total = sum(len(pages) for pages in ranges)
    status = await bot.send_message(
        chat_id,
        status_text("Splitting your PDF"),
    )
    slot: JobSlot | None = None
    workspace = None
    try:
        slot = await jobs.acquire(user.id)
        workspace = await workspaces.get_or_create(user.id)
        source = item_path(workspace, data)
        output_dir = workspace.path / f"split_{uuid.uuid4().hex[:10]}"
        outputs = await run_cpu(
            split_pdf,
            source,
            ranges,
            output_dir,
            settings,
            slot.token,
            timeout=settings.processing_timeout,
            token=slot.token,
            cleanup_on_timeout=lambda ws=workspace: workspaces.cleanup_workspace_sync(ws),
        )
        stem = filename_stem(data.get("name"), "document")
        if len(outputs) == 1:
            await deliver_document(
                bot,
                chat_id,
                outputs[0],
                f"{stem}_split.pdf",
                caption=split_result_text(1, page_total, False),
                settings=settings,
                reply_markup=back_to_menu_keyboard(),
            )
        elif len(outputs) <= settings.split_individual_max:
            for index, output in enumerate(outputs, start=1):
                await deliver_document(
                    bot,
                    chat_id,
                    output,
                    f"{stem}_part_{index:02d}.pdf",
                    caption=split_result_text(len(outputs), page_total, False)
                    if index == 1
                    else None,
                    settings=settings,
                    reply_markup=back_to_menu_keyboard() if index == 1 else None,
                )
        else:
            zip_path = await workspace.new_output_path(f"{stem}_split.zip")
            arcnames = [
                f"{stem}_part_{index:02d}.pdf"
                for index in range(1, len(outputs) + 1)
            ]
            await run_cpu(
                create_zip,
                outputs,
                zip_path,
                settings,
                timeout=settings.processing_timeout,
                token=slot.token,
                arcnames=arcnames,
                cleanup_on_timeout=lambda ws=workspace: workspaces.cleanup_workspace_sync(ws),
            )
            await deliver_document(
                bot,
                chat_id,
                zip_path,
                zip_path.name,
                caption=split_result_text(len(outputs), page_total, True),
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
        log.exception("Unexpected split failure")
        await safe_edit_text(
            status,
            "Something went wrong while splitting. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        await state.clear()
        await workspaces.cleanup_user(user.id)
    finally:
        if slot is not None:
            await slot.release()
