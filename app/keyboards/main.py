"""Shared inline keyboards."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings


def _row(*buttons: InlineKeyboardButton) -> list[InlineKeyboardButton]:
    return list(buttons)


def _button(
    text: str,
    callback_data: str,
    *,
    style: str | None = None,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)


def _url_button(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def main_menu_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(_button("Merge PDFs", "menu:merge"), _button("Split PDF", "menu:split")),
            _row(
                _button("Extract Pages", "menu:extract"),
                _button("Rotate Pages", "menu:rotate"),
            ),
            _row(_button("Images to PDF", "menu:images")),
            _row(
                _url_button("📢 Updates", "https://t.me/KayiBots"),
                _url_button("👨‍💻 Developer", "https://t.me/qouba"),
            ),
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_row(_button("← Main Menu", "menu:home"))])


def subscribe_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(_url_button(f"📢 Join {settings.fsub_channel}", settings.fsub_invite_url)),
            _row(_button("✅ I've Joined", "fsub:verify")),
        ]
    )


def help_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(_button("1. Getting Started", "help:topic:start")),
            _row(_button("2. PDF Tools", "help:topic:tools")),
            _row(_button("3. Images to PDF", "help:topic:images")),
            _row(_button("4. Limits & Privacy", "help:topic:limits")),
            _row(_button("← Main Menu", "menu:home")),
        ]
    )


def help_topic_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[_row(_button("← Back to Help", "help:main"))]
    )


def workflow_keyboard(*, with_go: str | None = None, go_label: str = "") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if with_go:
        rows.append(_row(_button(go_label or "Continue", with_go)))
    rows.append(_row(_button("← Main Menu", "menu:home")))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rotate_keyboard(selected_pages: list[int] | None) -> InlineKeyboardMarkup:
    all_label = "All Pages" if selected_pages is not None else "✓ All Pages"
    selected_label = "✓ Selected Pages" if selected_pages is not None else "Select Pages"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(
                _button(all_label, "rotate:scope:all"),
                _button(selected_label, "rotate:scope:select"),
            ),
            _row(
                _button("90°", "rotate:angle:90"),
                _button("180°", "rotate:angle:180"),
                _button("270°", "rotate:angle:270"),
            ),
            _row(_button("← Main Menu", "menu:home")),
        ]
    )


def merge_queue_keyboard(file_count: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(file_count):
        controls: list[InlineKeyboardButton] = []
        if index > 0:
            controls.append(_button(f"↑ {index + 1}", f"merge:up:{index}"))
        if index < file_count - 1:
            controls.append(_button(f"↓ {index + 1}", f"merge:down:{index}"))
        controls.append(_button(f"✖ {index + 1}", f"merge:remove:{index}"))
        rows.append(controls)
    if file_count:
        rows.append(_row(_button("✅ Merge PDFs", "merge:go")))
    rows.append(_row(_button("← Main Menu", "menu:home")))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def images_queue_keyboard(
    image_count: int,
    *,
    active_index: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if image_count:
        current = 0 if active_index is None else active_index
        current = max(0, min(current, image_count - 1))
        rows.append(
            _row(
                _button("←", "images:prev", style="primary"),
                _button(f"{current + 1} / {image_count}", "images:pos"),
                _button("→", "images:next", style="primary"),
            )
        )
    for index in range(image_count):
        controls: list[InlineKeyboardButton] = []
        if index > 0:
            controls.append(_button(f"↑ {index + 1}", f"images:up:{index}"))
        if index < image_count - 1:
            controls.append(_button(f"↓ {index + 1}", f"images:down:{index}"))
        controls.append(
            _button(f"✖ {index + 1}", f"images:remove:{index}", style="danger")
        )
        rows.append(controls)
    if image_count:
        rows.append(
            _row(_button("✅ Convert to PDF", "images:go", style="success"))
        )
    rows.append(_row(_button("← Main Menu", "menu:home")))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def upload_choice_keyboard(media_kind: str) -> InlineKeyboardMarkup:
    if media_kind == "image":
        rows = [
            _row(_button("Images to PDF", "upload:choose:images")),
            _row(_button("← Main Menu", "menu:home")),
        ]
    else:
        rows = [
            _row(_button("Merge PDFs", "upload:choose:merge"), _button("Split PDF", "upload:choose:split")),
            _row(
                _button("Extract Pages", "upload:choose:extract"),
                _button("Rotate Pages", "upload:choose:rotate"),
            ),
            _row(_button("← Main Menu", "menu:home")),
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
