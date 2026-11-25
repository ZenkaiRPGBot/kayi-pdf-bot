"""Keyboard layouts, callback names, and user-facing copy."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import Settings
from app.keyboards.main import (
    back_to_menu_keyboard,
    help_main_keyboard,
    help_topic_keyboard,
    images_queue_keyboard,
    main_menu_keyboard,
    merge_queue_keyboard,
    rotate_keyboard,
    subscribe_keyboard,
    upload_choice_keyboard,
    workflow_keyboard,
)
from app.services.fsub import MembershipStatus
from app.services.page_ranges import format_page_selection
from app.texts import (
    HELP_MAIN_TEXT,
    WELCOME_TEXT,
    extract_ready_text,
    fsub_text,
    fsub_verify_failed_text,
    help_topic_text,
    images_result_text,
    images_text,
    merge_result_text,
    merge_text,
    need_more_files_text,
    resend_after_verify_text,
    rotate_ready_text,
    rotate_result_text,
    split_ready_text,
    split_result_text,
    status_text,
    unsupported_file_text,
    upload_choice_text,
    verified_text,
    wrong_tool_input_text,
)


def callback_rows(markup) -> list[list[tuple[str, str | None]]]:
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def test_main_menu_has_exactly_the_expected_rows() -> None:
    settings = Settings()
    markup = main_menu_keyboard(settings)
    rows = callback_rows(markup)
    assert rows == [
        [("Merge PDFs", "menu:merge"), ("Split PDF", "menu:split")],
        [("Extract Pages", "menu:extract"), ("Rotate Pages", "menu:rotate")],
        [("Images to PDF", "menu:images")],
        [("📢 Updates", None), ("👨‍💻 Developer", None)],
    ]
    urls = [
        (button.text, button.url)
        for row in markup.inline_keyboard
        for button in row
        if button.url
    ]
    assert urls == [
        ("📢 Updates", "https://t.me/KayiBots"),
        ("👨‍💻 Developer", "https://t.me/qouba"),
    ]
    assert all("help" not in (data or "") for row in rows for _, data in row)


def test_subscribe_keyboard_uses_channel_url_and_verify() -> None:
    settings = Settings()
    rows = callback_rows(subscribe_keyboard(settings))
    assert rows == [
        [(f"📢 Join {settings.fsub_channel}", None)],
        [("✅ I've Joined", "fsub:verify")],
    ]
    join = subscribe_keyboard(settings).inline_keyboard[0][0]
    assert join.url == settings.fsub_invite_url == "https://t.me/KayiBots"


def test_help_keyboards() -> None:
    rows = callback_rows(help_main_keyboard())
    assert [data for row in rows[:-1] for _, data in row] == [
        "help:topic:start",
        "help:topic:tools",
        "help:topic:images",
        "help:topic:limits",
    ]
    assert rows[-1] == [("← Main Menu", "menu:home")]
    assert callback_rows(help_topic_keyboard()) == [[("← Back to Help", "help:main")]]


def test_back_and_workflow_keyboards() -> None:
    assert callback_rows(back_to_menu_keyboard()) == [[("← Main Menu", "menu:home")]]
    assert callback_rows(workflow_keyboard()) == [[("← Main Menu", "menu:home")]]
    assert callback_rows(workflow_keyboard(with_go="merge:go", go_label="Go")) == [
        [("Go", "merge:go")],
        [("← Main Menu", "menu:home")],
    ]


def test_merge_queue_keyboard_controls() -> None:
    empty = callback_rows(merge_queue_keyboard(0))
    assert empty == [[("← Main Menu", "menu:home")]]
    one = callback_rows(merge_queue_keyboard(1))
    assert one[0] == [("✖ 1", "merge:remove:0")]
    assert one[1] == [("✅ Merge PDFs", "merge:go")]
    three = callback_rows(merge_queue_keyboard(3))
    assert three[0] == [("↓ 1", "merge:down:0"), ("✖ 1", "merge:remove:0")]
    assert three[1] == [
        ("↑ 2", "merge:up:1"),
        ("↓ 2", "merge:down:1"),
        ("✖ 2", "merge:remove:1"),
    ]
    assert three[2] == [("↑ 3", "merge:up:2"), ("✖ 3", "merge:remove:2")]


def test_images_queue_keyboard_controls() -> None:
    rows = callback_rows(images_queue_keyboard(2))
    assert rows[0] == [("←", "images:prev"), ("1 / 2", "images:pos"), ("→", "images:next")]
    assert rows[1] == [("↓ 1", "images:down:0"), ("✖ 1", "images:remove:0")]
    assert rows[2] == [("↑ 2", "images:up:1"), ("✖ 2", "images:remove:1")]
    assert rows[3] == [("✅ Convert to PDF", "images:go")]
    assert rows[4] == [("← Main Menu", "menu:home")]

    positioned = callback_rows(images_queue_keyboard(3, active_index=2))
    assert positioned[0] == [("←", "images:prev"), ("3 / 3", "images:pos"), ("→", "images:next")]
    assert callback_rows(images_queue_keyboard(0)) == [[("← Main Menu", "menu:home")]]


def test_images_queue_keyboard_native_styles() -> None:
    markup = images_queue_keyboard(2, active_index=0)
    payload = markup.model_dump(exclude_none=True)
    preview = payload["inline_keyboard"][0]
    assert preview[0]["style"] == "primary"
    assert "style" not in preview[1]
    assert preview[2]["style"] == "primary"
    assert payload["inline_keyboard"][1][1]["style"] == "danger"
    assert payload["inline_keyboard"][3][0]["style"] == "success"
    assert "style" not in payload["inline_keyboard"][4][0]


def test_rotate_keyboard_reflects_scope() -> None:
    all_rows = callback_rows(rotate_keyboard(None))
    assert all_rows[0] == [
        ("✓ All Pages", "rotate:scope:all"),
        ("Select Pages", "rotate:scope:select"),
    ]
    selected_rows = callback_rows(rotate_keyboard([1, 2]))
    assert selected_rows[0] == [
        ("All Pages", "rotate:scope:all"),
        ("✓ Selected Pages", "rotate:scope:select"),
    ]
    assert selected_rows[1] == [
        ("90°", "rotate:angle:90"),
        ("180°", "rotate:angle:180"),
        ("270°", "rotate:angle:270"),
    ]


def test_upload_choice_keyboards() -> None:
    pdf_rows = callback_rows(upload_choice_keyboard("pdf"))
    assert pdf_rows[0] == [
        ("Merge PDFs", "upload:choose:merge"),
        ("Split PDF", "upload:choose:split"),
    ]
    assert pdf_rows[1] == [
        ("Extract Pages", "upload:choose:extract"),
        ("Rotate Pages", "upload:choose:rotate"),
    ]
    image_rows = callback_rows(upload_choice_keyboard("image"))
    assert image_rows[0] == [("Images to PDF", "upload:choose:images")]


def test_help_topics_follow_configured_limits() -> None:
    settings = Settings(max_files_merge=5, max_images=4, max_pages=50)
    tools = help_topic_text("tools", settings)
    images = help_topic_text("images", settings)
    limits = help_topic_text("limits", settings)
    assert "2–5 PDFs" in tools
    assert "1–4 JPG" in images
    assert "50 pages per PDF" in limits


def test_fsub_copy_headings() -> None:
    assert "<b>Join Kayi Bots to Continue</b>" in fsub_text(MembershipStatus.NOT_MEMBER)
    assert "<b>Join Kayi Bots to Continue</b>" in fsub_verify_failed_text()
    unavailable = fsub_text(MembershipStatus.UNAVAILABLE)
    assert "<b>Membership Check Unavailable</b>" in unavailable
    assert "Join Kayi Bots to Continue" not in unavailable
    assert "Verified" in verified_text()
    assert "send your file again" in resend_after_verify_text()


def test_merge_copy_and_empty_state() -> None:
    settings = Settings()
    empty = merge_text([], settings)
    assert "Send 2–10" in empty
    filled = merge_text(
        [
            {"name": "one.pdf", "page_count": 2},
            {"name": "two.pdf", "page_count": 1},
        ],
        settings,
    )
    assert "one.pdf" in filled and "2 pages" in filled
    assert "two.pdf" in filled and "1 page" in filled
    assert "✅ Merge PDFs" not in filled


def test_merge_copy_is_bounded_for_many_long_names() -> None:
    settings = Settings(max_files_merge=10)
    files = [{"name": "x" * 200 + f"{index}.pdf", "page_count": 3} for index in range(10)]
    text = merge_text(files, settings)
    assert len(text) <= 3801


def test_ready_prompts_include_examples_and_limits() -> None:
    settings = Settings()
    assert "1-3,4-6" in split_ready_text("x.pdf", 10, settings)
    assert "Maximum 10 output documents" in split_ready_text("x.pdf", 10, settings)
    assert "1,3,5-8" in extract_ready_text("x.pdf", 10)
    assert "1, 3-5" in rotate_ready_text("x.pdf", 5, [1, 3, 4, 5])
    assert "All pages" in rotate_ready_text("x.pdf", 3, None)


def test_rotate_scope_is_compacted_for_large_selections() -> None:
    pages = list(range(1, 201))
    text = rotate_ready_text("x.pdf", 200, pages)
    assert "Pages 1-200" in text
    assert len(text) < 4096
    assert format_page_selection(pages) == "1-200"


def test_images_copy() -> None:
    settings = Settings()
    assert "1–10" in images_text([], settings)
    items = [
        {"id": "first", "name": "a.jpg", "size": 2048},
        {"id": "second", "name": "b.png", "size": 1024},
    ]
    text = images_text(items, settings)
    assert "a.jpg" in text and "2 KB" in text
    assert "b.png" in text
    assert "Convert to PDF" in text
    marked = images_text(items, settings, active_id="second")
    assert "▶ 2. b.png" in marked
    assert "▶ 1. a.jpg" not in marked
    assert len(images_text(items, settings, limit=100)) <= 101


def test_result_and_error_copy() -> None:
    assert "Merge complete" in merge_result_text(3)
    assert "one PDF" in split_result_text(1, 3, False)
    assert "3 PDFs" in split_result_text(3, 3, False)
    assert "ZIP" in split_result_text(3, 3, True)
    assert "2 images" in images_result_text(2)
    assert "All pages" in rotate_result_text(None, 90)
    assert "2 selected pages" in rotate_result_text(2, 180)
    assert "&lt;b&gt;" in status_text("<b>working</b>")
    assert "at least 2" in need_more_files_text(2)
    assert "PDF or a JPG/PNG" in unsupported_file_text()
    assert "current merge task" in wrong_tool_input_text("merge")
    assert "What would you like" in upload_choice_text("pdf", "doc.pdf", 2)
    assert "Convert this image" in upload_choice_text("image", "photo.jpg")
    assert "Welcome to Kayi PDF" in WELCOME_TEXT
    assert "Help Center" in HELP_MAIN_TEXT
