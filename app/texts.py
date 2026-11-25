"""User-facing Telegram HTML copy.

Dynamic values are escaped here.  Exception messages that already contain
trusted HTML are sent as-is by the handlers.
"""

from __future__ import annotations

from app.config import Settings
from app.services.fsub import MembershipStatus
from app.services.page_ranges import format_page_selection
from app.utils.text import escape, format_bytes, paginate_text, truncate

WELCOME_TEXT = (
    "<b>Welcome to Kayi PDF</b>\n\n"
    "Your everyday PDF tools, right inside Telegram.\n\n"
    "✦ Merge multiple PDFs\n"
    "✦ Split and extract pages\n"
    "✦ Rotate PDF pages\n"
    "✦ Convert images to PDF\n\n"
    "<i>Choose a tool below or send a PDF to begin. Use /help for instructions.</i>"
)

HELP_MAIN_TEXT = (
    "<b>Kayi PDF — Help Center</b>\n\n"
    "Edit and organize PDF documents directly in Telegram.\n\n"
    "Choose a topic below to learn more."
)

HELP_TOPICS: dict[str, str] = {
    "start": (
        "<b>Getting Started</b>\n\n"
        "Send a PDF directly and choose an operation, or pick a tool from the "
        "main menu.\n\n"
        "✦ Use /start to return to the menu\n"
        "✦ Use /cancel to stop the current task\n"
        "✦ Results are sent back as a new file"
    ),
    "tools": (
        "<b>PDF Tools</b>\n\n"
        "<b>Merge</b>\n"
        "Send 2–10 PDFs, reorder or remove files, then tap Merge PDFs.\n\n"
        "<b>Split</b>\n"
        "One comma-separated range per output. Example: "
        "<code>1-3,4-6,7-10</code>.\n\n"
        "<b>Extract</b>\n"
        "Keep selected pages in one PDF. Example: <code>1,3,5-8</code>.\n\n"
        "<b>Rotate</b>\n"
        "Rotate all pages or selected pages by 90°, 180°, or 270° clockwise."
    ),
    "images": (
        "<b>Images to PDF</b>\n\n"
        "Send 1–10 JPG or PNG images. Telegram photos are supported too.\n\n"
        "✦ Images keep the order shown\n"
        "✦ Use ← → to preview, ↑ ↓ to reorder\n"
        "✦ Reorder or remove images before converting\n"
        "✦ Transparent areas become white\n"
        "✦ Images are not enlarged"
    ),
    "limits": (
        "<b>Limits & Privacy</b>\n\n"
        "✦ Up to 20 MB per file\n"
        "✦ Up to 200 pages per PDF\n"
        "✦ Up to 10 PDFs per merge or images per conversion\n"
        "✦ Up to 45 MB per result\n"
        "✦ One PDF job runs at a time\n\n"
        "Files are temporary and deleted after the task ends. I do not offer "
        "end-to-end encryption or permanent storage. On a free hosting plan, "
        "a restart can cancel an active task."
    ),
}

TOPIC_TITLES = {
    "start": "Getting Started",
    "tools": "PDF Tools",
    "images": "Images to PDF",
    "limits": "Limits & Privacy",
}


def _limits_text(settings: Settings) -> str:
    return (
        "<b>Limits & Privacy</b>\n\n"
        f"✦ Up to {settings.max_file_size_mb} MB per file\n"
        f"✦ Up to {settings.max_pages} pages per PDF\n"
        f"✦ Up to {settings.max_files_merge} PDFs per merge or "
        f"{settings.max_images} images per conversion\n"
        f"✦ Up to {settings.max_output_size_mb} MB per result\n"
        f"✦ {settings.max_active_jobs} PDF job"
        f"{'s' if settings.max_active_jobs != 1 else ''} at a time\n\n"
        "Files are temporary and deleted after the task ends. I do not offer "
        "end-to-end encryption or permanent storage. On a free hosting plan, "
        "a restart can cancel an active task."
    )


def _tools_text(settings: Settings) -> str:
    return (
        "<b>PDF Tools</b>\n\n"
        "<b>Merge</b>\n"
        f"Send 2–{settings.max_files_merge} PDFs, reorder or remove files, "
        "then tap Merge PDFs.\n\n"
        "<b>Split</b>\n"
        "One comma-separated range per output. Example: "
        "<code>1-3,4-6,7-10</code>.\n\n"
        "<b>Extract</b>\n"
        "Keep selected pages in one PDF. Example: <code>1,3,5-8</code>.\n\n"
        "<b>Rotate</b>\n"
        "Rotate all pages or selected pages by 90°, 180°, or 270° clockwise."
    )


def _images_help_text(settings: Settings) -> str:
    return (
        "<b>Images to PDF</b>\n\n"
        f"Send 1–{settings.max_images} JPG or PNG images. "
        "Telegram photos are supported too.\n\n"
        "✦ Images keep the order shown\n"
        "✦ Use ← → to preview, ↑ ↓ to reorder\n"
        "✦ Reorder or remove images before converting\n"
        "✦ Transparent areas become white\n"
        "✦ Images are not enlarged"
    )


def help_topic_text(topic: str, settings: Settings | None = None) -> str:
    if settings is not None:
        if topic == "limits":
            return _limits_text(settings)
        if topic == "tools":
            return _tools_text(settings)
        if topic == "images":
            return _images_help_text(settings)
    return HELP_TOPICS.get(topic, HELP_MAIN_TEXT)


def fsub_text(status: MembershipStatus) -> str:
    if status is MembershipStatus.UNAVAILABLE:
        return (
            "<b>Membership Check Unavailable</b>\n\n"
            "I couldn't confirm your channel membership right now.\n\n"
            "Please try again in a moment. If this keeps happening, the "
            "channel administrator may need to check the bot's access."
        )
    return (
        "<b>Join Kayi Bots to Continue</b>\n\n"
        "To use Kayi PDF, join our official channel first.\n\n"
        "After joining, tap <b>I've Joined</b>."
    )


def verified_text() -> str:
    return "✅ <b>Verified</b>\n\nYou now have access to Kayi PDF."


def fsub_verify_failed_text() -> str:
    return (
        "<b>Join Kayi Bots to Continue</b>\n\n"
        "I still can't confirm your membership.\n\n"
        "Make sure you joined the channel, then tap <b>I've Joined</b> again."
    )


def resend_after_verify_text() -> str:
    return (
        "✅ <b>Verified</b>\n\n"
        "Please send your file again to continue.\n\n"
        "Use /help if you need instructions."
    )


def _file_line(index: int, item: dict, *, pages: bool = True, active: bool = False) -> str:
    name = truncate(escape(str(item.get("name", "file"))), 52)
    suffix = ""
    if pages and item.get("page_count"):
        count = int(item["page_count"])
        suffix = f" — {count} page{'s' if count != 1 else ''}"
    elif item.get("size"):
        suffix = f" — {format_bytes(int(item['size']))}"
    marker = "▶ " if active else ""
    return f"{marker}{index}. {name}{suffix}"


def merge_text(files: list[dict], settings: Settings) -> str:
    if not files:
        return (
            "<b>Merge PDFs</b>\n\n"
            f"Send 2–{settings.max_files_merge} PDF files, one at a time. "
            "They will be merged in the order shown.\n\n"
            "✦ Use the arrows to reorder\n"
            "✦ Use ✖ to remove a file\n"
            "✦ Tap Merge PDFs when ready"
        )
    lines = [
        "<b>Merge PDFs</b>",
        "",
        f"<b>{len(files)}</b> of {settings.max_files_merge} PDFs ready.",
        "",
    ]
    lines.extend(_file_line(index, item) for index, item in enumerate(files, start=1))
    lines.extend(
        [
            "",
            "Use the arrows to reorder, ✖ to remove, then tap Merge PDFs.",
        ]
    )
    return paginate_text(lines)


def split_intro_text() -> str:
    return (
        "<b>Split PDF</b>\n\n"
        "Send one PDF. After I check it, enter page ranges.\n\n"
        "✦ Each comma-separated range becomes a separate PDF\n"
        "✦ Example: <code>1-3,4-6,7-10</code>"
    )


def split_ready_text(name: str, page_count: int, settings: Settings) -> str:
    return (
        "<b>Split PDF</b>\n\n"
        f"{escape(truncate(name, 60))} — <b>{page_count}</b> "
        f"page{'s' if page_count != 1 else ''}\n\n"
        "Send the page ranges. Each comma-separated range becomes a separate PDF.\n\n"
        f"Example: <code>1-3,4-6</code>\n"
        f"Maximum {settings.max_split_outputs} output documents."
    )


def extract_intro_text() -> str:
    return (
        "<b>Extract Pages</b>\n\n"
        "Send one PDF. After I check it, choose the pages to keep."
    )


def extract_ready_text(name: str, page_count: int) -> str:
    return (
        "<b>Extract Pages</b>\n\n"
        f"{escape(truncate(name, 60))} — <b>{page_count}</b> "
        f"page{'s' if page_count != 1 else ''}\n\n"
        "Send the pages to keep, in the order you want them.\n\n"
        "Example: <code>1,3,5-8</code>"
    )


def rotate_intro_text() -> str:
    return (
        "<b>Rotate Pages</b>\n\n"
        "Send one PDF. Then rotate all pages or selected pages."
    )


def rotate_ready_text(name: str, page_count: int, selected_pages: list[int] | None) -> str:
    if selected_pages is None:
        scope = "All pages"
    else:
        scope = f"Pages {format_page_selection(selected_pages)}"
    return (
        "<b>Rotate Pages</b>\n\n"
        f"{escape(truncate(name, 60))} — <b>{page_count}</b> "
        f"page{'s' if page_count != 1 else ''}\n\n"
        f"Scope: <b>{escape(scope)}</b>\n\n"
        "Choose a rotation angle. Rotation is clockwise."
    )


def rotate_pages_prompt_text(name: str, page_count: int) -> str:
    return (
        "<b>Rotate Pages</b>\n\n"
        f"{escape(truncate(name, 60))} — <b>{page_count}</b> "
        f"page{'s' if page_count != 1 else ''}\n\n"
        "Send the pages to rotate.\n\n"
        "Example: <code>1,3-5</code>"
    )


def images_intro_text(settings: Settings) -> str:
    return (
        "<b>Images to PDF</b>\n\n"
        f"Send 1–{settings.max_images} JPG or PNG images. "
        "They will be added in the order received.\n\n"
        "✦ Use ← → to preview images\n"
        "✦ Use ↑ ↓ to reorder\n"
        "✦ Use ✖ to remove an image\n"
        "✦ Tap Convert to PDF when ready"
    )


def images_text(
    items: list[dict],
    settings: Settings,
    *,
    active_id: str | None = None,
    limit: int = 3800,
) -> str:
    if not items:
        return images_intro_text(settings)
    lines = [
        "<b>Images to PDF</b>",
        "",
        f"<b>{len(items)}</b> of {settings.max_images} images ready.",
        "",
    ]
    lines.extend(
        _file_line(
            index,
            item,
            pages=False,
            active=active_id is not None and item.get("id") == active_id,
        )
        for index, item in enumerate(items, start=1)
    )
    lines.extend(["", "Use the arrows to reorder, ✖ to remove, then tap Convert to PDF."])
    return paginate_text(lines, limit=limit)


def upload_choice_text(media_kind: str, name: str, page_count: int | None = None) -> str:
    if media_kind == "image":
        return (
            "<b>Image Received</b>\n\n"
            f"{escape(truncate(name, 60))}\n\n"
            "Convert this image to PDF?"
        )
    pages = (
        f" — <b>{page_count}</b> page{'s' if page_count != 1 else ''}"
        if page_count
        else ""
    )
    return (
        "<b>PDF Received</b>\n\n"
        f"{escape(truncate(name, 60))}{pages}\n\n"
        "What would you like to do with this PDF?"
    )


def status_text(action: str) -> str:
    return f"⏳ {escape(action)}…"


def merge_result_text(count: int) -> str:
    return f"✅ <b>Merge complete</b>\n\n{count} PDFs merged in the order shown."


def split_result_text(count: int, pages: int, zipped: bool) -> str:
    if zipped:
        return (
            "✅ <b>Split complete</b>\n\n"
            f"{count} PDFs packed into a ZIP archive ({pages} pages)."
        )
    if count == 1:
        return f"✅ <b>Split complete</b>\n\nCreated one PDF from {pages} page{'s' if pages != 1 else ''}."
    return f"✅ <b>Split complete</b>\n\n{count} PDFs created from {pages} pages."


def extract_result_text(pages: int) -> str:
    return (
        "✅ <b>Extract complete</b>\n\n"
        f"{pages} page{'s' if pages != 1 else ''} saved as a new PDF."
    )


def rotate_result_text(pages: int | None, angle: int) -> str:
    scope = "All pages" if pages is None else f"{pages} selected page{'s' if pages != 1 else ''}"
    return f"✅ <b>Rotation complete</b>\n\n{scope} rotated {angle}° clockwise."


def images_result_text(count: int) -> str:
    return f"✅ <b>PDF created</b>\n\n{count} image{'s' if count != 1 else ''} converted into one PDF."


def cancelled_text() -> str:
    return "Task cancelled. Temporary files were removed."


def expired_text() -> str:
    return "This menu is no longer active. Use /start to begin again."


def need_more_files_text(minimum: int = 2) -> str:
    return f"Add at least {minimum} files before continuing."


def busy_text() -> str:
    return "The bot is handling another job right now. Please try again in a moment."


def unsupported_file_text() -> str:
    return "Please send a PDF or a JPG/PNG image."


def wrong_tool_input_text(tool: str) -> str:
    return f"Finish or /cancel your current {tool} task first."


def no_active_task_text() -> str:
    return "There is no active task to cancel. Use /start to choose a tool."


def retry_text() -> str:
    return "Please try again, or use /help for instructions."
