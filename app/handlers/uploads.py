"""Direct PDF/image uploads and the operation-choice screen."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.config import Settings
from app.handlers.shared import (
    document_is_image,
    document_is_pdf,
    download_image,
    download_pdf,
)
from app.keyboards.main import (
    back_to_menu_keyboard,
    images_queue_keyboard,
    merge_queue_keyboard,
    rotate_keyboard,
    upload_choice_keyboard,
)
from app.services.errors import KayiPdfError
from app.services.workspaces import WorkspaceManager
from app.states.workflows import (
    ExtractStates,
    ImagesStates,
    MergeStates,
    RotateStates,
    SplitStates,
    UploadChoiceStates,
)
from app.texts import (
    expired_text,
    extract_ready_text,
    images_text,
    merge_text,
    rotate_ready_text,
    split_ready_text,
    unsupported_file_text,
    upload_choice_text,
)
from app.utils.logging import get_logger
from app.utils.telegram import safe_answer_callback, safe_edit_text

log = get_logger(__name__)
router = Router(name="uploads")

_EMPTY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[])


async def _store_choice(
    message: Message,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    workspaces: WorkspaceManager,
) -> None:
    user = message.from_user
    if user is None:
        return
    document = message.document
    if document is not None:
        if document_is_image(document):
            kind = "image"
        elif document_is_pdf(document):
            kind = "pdf"
        else:
            await message.answer(
                unsupported_file_text(),
                reply_markup=back_to_menu_keyboard(),
            )
            return
    elif message.photo:
        kind = "image"
    else:
        await message.answer(
            unsupported_file_text(),
            reply_markup=back_to_menu_keyboard(),
        )
        return
    try:
        workspace = await workspaces.get_or_create(user.id)
        if kind == "image":
            downloaded = await download_image(message, bot, workspace, settings)
        else:
            downloaded = await download_pdf(message, bot, workspace, settings)
    except KayiPdfError as exc:
        await workspaces.cleanup_user(user.id)
        await message.answer(exc.user_message, reply_markup=back_to_menu_keyboard())
        return
    except Exception:
        log.exception("Unexpected error while preparing a direct upload")
        await workspaces.cleanup_user(user.id)
        await message.answer(
            "I couldn't process that file. Please try again.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    await state.set_state(UploadChoiceStates.choosing)
    data = {
        "owner_id": user.id,
        "kind": kind,
        "name": downloaded.display_name,
        "stored_name": downloaded.path.name,
        "size": downloaded.size,
        "page_count": downloaded.page_count,
        "digest": downloaded.digest,
    }
    await state.set_data(data)
    sent = await message.answer(
        upload_choice_text(kind, downloaded.display_name, downloaded.page_count),
        reply_markup=upload_choice_keyboard(kind),
    )
    await state.update_data(choice_message_id=sent.message_id)


@router.message(UploadChoiceStates.choosing, F.document | F.photo)
async def second_upload_while_choosing(message: Message) -> None:
    await message.answer(
        "Choose an operation for the file above first, or use /cancel to start over.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(UploadChoiceStates.choosing, F.text & ~F.text.startswith("/"))
async def text_while_choosing(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    kind = str(data.get("kind") or "pdf")
    await message.answer(
        upload_choice_text(
            kind,
            str(data.get("name") or "file"),
            data.get("page_count"),
        ),
        reply_markup=upload_choice_keyboard(kind),
    )


@router.message(UploadChoiceStates.choosing)
async def unsupported_while_choosing(message: Message) -> None:
    await message.answer(
        "Choose an operation above, or use /cancel to start over.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(F.document | F.photo)
async def direct_upload(
    message: Message,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    workspaces: WorkspaceManager,
) -> None:
    await _store_choice(message, bot, state, settings, workspaces)


@router.callback_query(UploadChoiceStates.choosing, F.data.startswith("upload:choose:"))
async def choose_operation(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    workspaces: WorkspaceManager,
) -> None:
    user = callback.from_user
    if user is None:
        return
    data = await state.get_data()
    if data.get("owner_id") != user.id or not data.get("stored_name"):
        await callback.answer(expired_text(), show_alert=True)
        return
    choice = (callback.data or "").split(":")[-1]
    kind = str(data.get("kind") or "pdf")
    if kind == "image" and choice != "images":
        await callback.answer("This is an image. Choose Images to PDF.", show_alert=True)
        return
    if kind == "pdf" and choice == "images":
        await callback.answer("This is a PDF. Choose a PDF operation.", show_alert=True)
        return
    if choice not in {"merge", "split", "extract", "rotate", "images"}:
        await callback.answer(expired_text(), show_alert=True)
        return
    await callback.answer()
    chat_id = callback.message.chat.id if callback.message else user.id
    if callback.message is not None:
        await safe_edit_text(callback.message, "Opening…", reply_markup=_EMPTY_KEYBOARD)
    item = {
        "name": data.get("name", "document.pdf"),
        "stored_name": data["stored_name"],
        "size": int(data.get("size") or 0),
        "page_count": int(data.get("page_count") or 0),
        "digest": data.get("digest"),
    }
    if choice == "merge":
        await state.clear()
        await state.set_state(MergeStates.collecting)
        await state.set_data(
            {"owner_id": user.id, "files": [item], "queue_message_id": None}
        )
        sent = await bot.send_message(
            chat_id,
            merge_text([item], settings),
            reply_markup=merge_queue_keyboard(1),
        )
        await state.update_data(queue_message_id=sent.message_id)
        return
    if choice == "images":
        image_item = {key: item[key] for key in ("name", "stored_name", "size")}
        await state.clear()
        await state.set_state(ImagesStates.collecting)
        await state.set_data(
            {"owner_id": user.id, "files": [image_item], "queue_message_id": None}
        )
        sent = await bot.send_message(
            chat_id,
            images_text([image_item], settings),
            reply_markup=images_queue_keyboard(1),
        )
        await state.update_data(queue_message_id=sent.message_id)
        return
    base = {
        "owner_id": user.id,
        "name": item["name"],
        "stored_name": item["stored_name"],
        "size": item["size"],
        "page_count": item["page_count"],
    }
    if choice == "split":
        await state.clear()
        await state.set_state(SplitStates.waiting_ranges)
        await state.set_data(base)
        await bot.send_message(
            chat_id,
            split_ready_text(item["name"], item["page_count"], settings),
            reply_markup=back_to_menu_keyboard(),
        )
        return
    if choice == "extract":
        await state.clear()
        await state.set_state(ExtractStates.waiting_pages)
        await state.set_data(base)
        await bot.send_message(
            chat_id,
            extract_ready_text(item["name"], item["page_count"]),
            reply_markup=back_to_menu_keyboard(),
        )
        return
    await state.clear()
    await state.set_state(RotateStates.choosing)
    await state.set_data(
        {
            **base,
            "selected_pages": None,
            "scope_message_id": None,
        }
    )
    await bot.send_message(
        chat_id,
        rotate_ready_text(item["name"], item["page_count"], None),
        reply_markup=rotate_keyboard(None),
    )
