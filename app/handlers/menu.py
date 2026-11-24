"""Main-menu tool callbacks."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.config import Settings
from app.services.navigation import open_tool
from app.services.workspaces import WorkspaceManager
from app.texts import wrong_tool_input_text
from app.utils.telegram import safe_answer_callback

router = Router(name="menu")

_TOOL_BY_DATA = {
    "menu:merge": "merge",
    "menu:split": "split",
    "menu:extract": "extract",
    "menu:rotate": "rotate",
    "menu:images": "images",
}


@router.callback_query(F.data.in_(set(_TOOL_BY_DATA)))
async def open_tool_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    workspaces: WorkspaceManager,
) -> None:
    user = callback.from_user
    if user is None:
        return
    tool = _TOOL_BY_DATA.get(callback.data or "")
    if tool is None:
        await safe_answer_callback(callback)
        return
    current = await state.get_state()
    if current is not None:
        await safe_answer_callback(
            callback,
            wrong_tool_input_text("active"),
            show_alert=True,
        )
        return
    await callback.answer()
    chat_id = callback.message.chat.id if callback.message else user.id
    await open_tool(bot, chat_id, user.id, tool, state, workspaces, settings)
