"""/start, /help, help navigation, and subscription verification."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.config import Settings
from app.keyboards.main import (
    help_main_keyboard,
    help_topic_keyboard,
    subscribe_keyboard,
)
from app.services.fsub import MembershipStatus, PendingActionStore, SubscriptionService
from app.services.jobs import JobManager
from app.services.navigation import clear_workflow, restore_pending_action
from app.services.welcome import send_welcome
from app.services.workspaces import WorkspaceManager
from app.texts import (
    HELP_MAIN_TEXT,
    fsub_text,
    fsub_verify_failed_text,
    help_topic_text,
    verified_text,
)
from app.utils.telegram import safe_edit_text

router = Router(name="start")


@router.message(CommandStart())
async def start_handler(
    message: Message,
    command: CommandObject,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    subscription: SubscriptionService,
    pending: PendingActionStore,
    workspaces: WorkspaceManager,
) -> None:
    user = message.from_user
    if user is None:
        return
    args = (command.args or "").strip().lower()
    wants_help = args == "help"
    await clear_workflow(state, workspaces, user.id)
    result = await subscription.check(bot, user.id)
    if result.status is MembershipStatus.MEMBER:
        if wants_help:
            await message.answer(HELP_MAIN_TEXT, reply_markup=help_main_keyboard())
        else:
            await send_welcome(bot, message.chat.id, settings, use_video=True)
        return
    pending.set(user.id, "help" if wants_help else "start")
    await message.answer(
        fsub_text(result.status),
        reply_markup=subscribe_keyboard(settings),
    )


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(HELP_MAIN_TEXT, reply_markup=help_main_keyboard())


@router.message(Command("privacy"))
async def privacy_handler(message: Message, settings: Settings) -> None:
    await message.answer(
        help_topic_text("limits", settings),
        reply_markup=help_topic_keyboard(),
    )


@router.callback_query(F.data == "help:main")
async def help_main_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await safe_edit_text(
            callback.message,
            HELP_MAIN_TEXT,
            reply_markup=help_main_keyboard(),
        )


@router.callback_query(F.data.startswith("help:topic:"))
async def help_topic_callback(callback: CallbackQuery, settings: Settings) -> None:
    await callback.answer()
    topic = (callback.data or "").split(":", 2)[-1]
    if topic not in {"start", "tools", "images", "limits"}:
        topic = ""
    if callback.message is not None:
        await safe_edit_text(
            callback.message,
            help_topic_text(topic, settings) if topic else HELP_MAIN_TEXT,
            reply_markup=help_topic_keyboard() if topic else help_main_keyboard(),
        )


@router.callback_query(F.data == "fsub:verify")
async def verify_subscription_handler(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    subscription: SubscriptionService,
    pending: PendingActionStore,
    workspaces: WorkspaceManager,
    jobs: JobManager,
) -> None:
    user = callback.from_user
    await callback.answer()
    if user is None:
        return
    result = await subscription.check(bot, user.id, force=True)
    chat_id = callback.message.chat.id if callback.message is not None else user.id
    if result.status is MembershipStatus.MEMBER:
        action = pending.pop(user.id) or "welcome"
        if callback.message is not None:
            await safe_edit_text(
                callback.message,
                verified_text(),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            )
        if jobs.is_active(user.id):
            await bot.send_message(
                chat_id,
                "✅ <b>Verified</b>\n\nYour current task is still running.",
            )
            return
        await restore_pending_action(
            bot,
            chat_id,
            user.id,
            action,
            state,
            workspaces,
            settings,
        )
        return
    if result.status is MembershipStatus.NOT_MEMBER:
        text = fsub_verify_failed_text()
    else:
        text = fsub_text(MembershipStatus.UNAVAILABLE)
    if callback.message is not None:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=subscribe_keyboard(settings),
        )
    else:
        await bot.send_message(
            chat_id,
            text,
            reply_markup=subscribe_keyboard(settings),
        )
