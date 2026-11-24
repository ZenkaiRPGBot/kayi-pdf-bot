"""Cancellation, main-menu navigation, and stale callback fallback."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.keyboards.main import main_menu_keyboard
from app.services.jobs import JobManager
from app.services.navigation import clear_workflow
from app.services.welcome import send_welcome
from app.services.workspaces import WorkspaceManager
from app.texts import cancelled_text, no_active_task_text

router = Router(name="common")


@router.message(Command("cancel"))
async def cancel_handler(
    message: Message,
    state: FSMContext,
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> None:
    user = message.from_user
    if user is None:
        return
    if jobs.cancel(user.id):
        await message.answer("⏳ <b>Cancelling…</b>\n\nThe current task will stop shortly.")
        return
    current = await state.get_state()
    await clear_workflow(state, workspaces, user.id)
    if current is None:
        await message.answer(
            no_active_task_text(),
            reply_markup=main_menu_keyboard(settings),
        )
    else:
        await message.answer(
            cancelled_text(),
            reply_markup=main_menu_keyboard(settings),
        )


@router.callback_query(F.data.in_({"menu:home", "workflow:cancel"}))
async def home_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    jobs: JobManager,
    workspaces: WorkspaceManager,
) -> None:
    user = callback.from_user
    await callback.answer()
    if user is None:
        return
    if jobs.is_active(user.id):
        await bot.send_message(
            callback.message.chat.id if callback.message else user.id,
            "A task is still running. Use /cancel to stop it.",
        )
        return
    await clear_workflow(state, workspaces, user.id)
    chat_id = callback.message.chat.id if callback.message else user.id
    await send_welcome(bot, chat_id, settings, use_video=False)
