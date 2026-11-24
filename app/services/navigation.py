"""Menu opening and post-subscription destination restoration."""

from __future__ import annotations

from aiogram import Bot

from app.config import Settings
from app.keyboards.main import (
    back_to_menu_keyboard,
    help_main_keyboard,
    help_topic_keyboard,
    images_queue_keyboard,
    merge_queue_keyboard,
    workflow_keyboard,
)
from app.services.workspaces import WorkspaceManager
from app.services.welcome import send_welcome
from app.states.workflows import (
    ExtractStates,
    ImagesStates,
    MergeStates,
    RotateStates,
    SplitStates,
)
from app.texts import (
    HELP_MAIN_TEXT,
    extract_intro_text,
    help_topic_text,
    images_intro_text,
    merge_text,
    resend_after_verify_text,
    rotate_intro_text,
    split_intro_text,
)

TOOLS = {"merge", "split", "extract", "rotate", "images"}


async def clear_workflow(state, workspaces: WorkspaceManager, user_id: int) -> None:
    await state.clear()
    await workspaces.cleanup_user(user_id)


async def open_tool(
    bot: Bot,
    chat_id: int,
    user_id: int,
    tool: str,
    state,
    workspaces: WorkspaceManager,
    settings: Settings,
) -> None:
    await clear_workflow(state, workspaces, user_id)
    if tool == "merge":
        await state.set_state(MergeStates.collecting)
        await state.set_data({"owner_id": user_id, "files": [], "queue_message_id": None})
        await bot.send_message(
            chat_id,
            merge_text([], settings),
            reply_markup=merge_queue_keyboard(0),
        )
    elif tool == "split":
        await state.set_state(SplitStates.waiting_file)
        await state.set_data({"owner_id": user_id})
        await bot.send_message(
            chat_id,
            split_intro_text(),
            reply_markup=workflow_keyboard(),
        )
    elif tool == "extract":
        await state.set_state(ExtractStates.waiting_file)
        await state.set_data({"owner_id": user_id})
        await bot.send_message(
            chat_id,
            extract_intro_text(),
            reply_markup=workflow_keyboard(),
        )
    elif tool == "rotate":
        await state.set_state(RotateStates.waiting_file)
        await state.set_data({"owner_id": user_id})
        await bot.send_message(
            chat_id,
            rotate_intro_text(),
            reply_markup=workflow_keyboard(),
        )
    elif tool == "images":
        await state.set_state(ImagesStates.collecting)
        await state.set_data({"owner_id": user_id, "files": [], "queue_message_id": None})
        await bot.send_message(
            chat_id,
            images_intro_text(settings),
            reply_markup=images_queue_keyboard(0),
        )
    else:
        raise ValueError(f"Unknown tool: {tool}")


async def show_help(
    bot: Bot,
    chat_id: int,
    topic: str | None = None,
    settings: Settings | None = None,
) -> None:
    if topic:
        await bot.send_message(
            chat_id,
            help_topic_text(topic, settings),
            reply_markup=help_topic_keyboard(),
        )
    else:
        await bot.send_message(
            chat_id,
            HELP_MAIN_TEXT,
            reply_markup=help_main_keyboard(),
        )


async def restore_pending_action(
    bot: Bot,
    chat_id: int,
    user_id: int,
    action: str,
    state,
    workspaces: WorkspaceManager,
    settings: Settings,
) -> None:
    """Re-open the destination the user was trying to reach before FSub."""

    if action.startswith("help_topic:"):
        topic = action.split(":", 1)[1]
        await clear_workflow(state, workspaces, user_id)
        await show_help(
            bot,
            chat_id,
            topic if topic in {"start", "tools", "images", "limits"} else None,
            settings,
        )
        return
    if action == "help":
        await clear_workflow(state, workspaces, user_id)
        await show_help(bot, chat_id, settings=settings)
        return
    if action in TOOLS:
        await open_tool(bot, chat_id, user_id, action, state, workspaces, settings)
        return
    if action == "resend":
        await clear_workflow(state, workspaces, user_id)
        await bot.send_message(
            chat_id,
            resend_after_verify_text(),
            reply_markup=back_to_menu_keyboard(),
        )
        return
    if action == "start":
        await clear_workflow(state, workspaces, user_id)
        await send_welcome(bot, chat_id, settings, use_video=True)
        return
    # A plain main-menu destination is safer than replaying a stale action.
    await clear_workflow(state, workspaces, user_id)
    await send_welcome(bot, chat_id, settings, use_video=False)
