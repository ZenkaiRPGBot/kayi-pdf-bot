"""Shared upload classification and download helpers for handlers."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.types import Document, Message, PhotoSize

from app.config import Settings
from app.services.errors import FileValidationError, ImageValidationError
from app.services.files import download_file
from app.services.image_ops import ImageInfo, inspect_image
from app.services.pdf_ops import PdfInfo, inspect_pdf
from app.services.workspaces import JobWorkspace, WorkspaceManager
from app.utils.text import sanitize_filename


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    path: Path
    display_name: str
    size: int
    page_count: int | None = None
    image_format: str | None = None
    digest: str | None = None


def document_is_image(document: Document) -> bool:
    mime = (document.mime_type or "").lower()
    name = (document.file_name or "").lower()
    return mime in {"image/jpeg", "image/png"} or name.endswith(
        (".jpg", ".jpeg", ".png")
    )


def document_is_pdf(document: Document) -> bool:
    mime = (document.mime_type or "").lower()
    name = (document.file_name or "").lower()
    return mime == "application/pdf" or name.endswith(".pdf")


def _document_suffix(document: Document) -> str:
    name = (document.file_name or "").lower()
    if name.endswith(".pdf"):
        return ".pdf"
    if name.endswith(".jpg") or name.endswith(".jpeg"):
        return ".jpg"
    if name.endswith(".png"):
        return ".png"
    mime = (document.mime_type or "").lower()
    if mime == "application/pdf":
        return ".pdf"
    if mime == "image/png":
        return ".png"
    if mime == "image/jpeg":
        return ".jpg"
    return ".bin"


def _photo(message: Message) -> PhotoSize | None:
    if not message.photo:
        return None
    return message.photo[-1]


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def download_pdf(
    message: Message,
    bot: Bot,
    workspace: JobWorkspace,
    settings: Settings,
) -> DownloadedFile:
    document = message.document
    if document is None:
        if message.photo:
            raise FileValidationError("Please send a PDF file, not an image.")
        raise FileValidationError("Please send a PDF file.")
    if document_is_image(document):
        raise FileValidationError("That looks like an image. Please send a PDF file.")
    display_name = sanitize_filename(document.file_name, "document.pdf")
    target = await workspace.new_input_path(".pdf")
    downloaded = await download_file(
        bot,
        document.file_id,
        target,
        settings,
        expected_size=document.file_size,
    )
    try:
        info: PdfInfo = await asyncio.to_thread(inspect_pdf, downloaded, settings)
        digest = await asyncio.to_thread(_file_digest, downloaded)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return DownloadedFile(
        path=downloaded,
        display_name=display_name,
        size=info.size,
        page_count=info.page_count,
        digest=digest,
    )


async def download_image(
    message: Message,
    bot: Bot,
    workspace: JobWorkspace,
    settings: Settings,
) -> DownloadedFile:
    document = message.document
    photo = _photo(message)
    if document is None and photo is None:
        raise ImageValidationError("Please send a JPG or PNG image.")
    if document is not None:
        if document_is_pdf(document):
            raise ImageValidationError("That is a PDF. Please send an image.")
        if not document_is_image(document):
            raise ImageValidationError("Please send JPG or PNG images only.")
        display_name = sanitize_filename(document.file_name, "image.jpg")
        file_id = document.file_id
        expected_size = document.file_size
        suffix = _document_suffix(document)
    else:
        assert photo is not None
        display_name = "photo.jpg"
        file_id = photo.file_id
        expected_size = getattr(photo, "file_size", None)
        suffix = ".jpg"
    target = await workspace.new_input_path(suffix)
    downloaded = await download_file(
        bot,
        file_id,
        target,
        settings,
        expected_size=expected_size,
    )
    try:
        info: ImageInfo = await asyncio.to_thread(inspect_image, downloaded, settings)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return DownloadedFile(
        path=downloaded,
        display_name=display_name,
        size=info.size,
        image_format=info.format,
    )


def item_path(workspace: JobWorkspace, item: dict) -> Path:
    return workspace.resolve(str(item["stored_name"]))


def total_size(items: list[dict]) -> int:
    return sum(int(item.get("size", 0)) for item in items)


async def remove_item(
    workspace: JobWorkspace, items: list[dict], index: int
) -> list[dict]:
    if 0 <= index < len(items):
        item = items.pop(index)
        try:
            item_path(workspace, item).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    return items


async def cleanup_workspace_for_user(
    workspaces: WorkspaceManager, user_id: int
) -> None:
    await workspaces.cleanup_user(user_id)
