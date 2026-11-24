"""Shared handler helpers: digesting and upload validation."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.handlers.shared import download_pdf
from app.services.errors import PdfValidationError
from app.services.workspaces import WorkspaceManager


class FakeBot:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def download(self, file_id, destination=None, **kwargs):
        self.calls.append({"file_id": file_id, **kwargs})
        destination.write(self.payload)
        destination.flush()
        return destination


class FakeMessage:
    def __init__(self, payload_size: int, name: str = "source.pdf") -> None:
        self.document = SimpleNamespace(
            file_id="telegram-file",
            file_name=name,
            file_size=payload_size,
            mime_type="application/pdf",
        )
        self.photo = None
        self.from_user = SimpleNamespace(id=7)
        self.chat = SimpleNamespace(id=100)


@pytest.mark.asyncio
async def test_download_pdf_records_content_digest(
    settings: Settings, tmp_path, pdf_factory
) -> None:
    source = pdf_factory(page_count=2)
    payload = source.read_bytes()
    bot = FakeBot(payload)
    message = FakeMessage(len(payload))
    workspaces = WorkspaceManager(tmp_path / "shared", max_workspaces=2)
    workspace = await workspaces.get_or_create(7)
    downloaded = await download_pdf(message, bot, workspace, settings)
    try:
        assert downloaded.page_count == 2
        assert downloaded.size == len(payload)
        assert downloaded.digest == hashlib.sha256(payload).hexdigest()
        assert downloaded.path.is_file()
        assert bot.calls[0]["file_id"] == "telegram-file"
    finally:
        await workspaces.cleanup_user(7)


@pytest.mark.asyncio
async def test_download_pdf_removes_invalid_upload(
    settings: Settings, tmp_path
) -> None:
    bot = FakeBot(b"this is not a pdf")
    message = FakeMessage(18, name="fake.pdf")
    workspaces = WorkspaceManager(tmp_path / "shared-bad", max_workspaces=2)
    workspace = await workspaces.get_or_create(7)
    try:
        with pytest.raises(PdfValidationError):
            await download_pdf(message, bot, workspace, settings)
        assert list(workspace.path.iterdir()) == []
    finally:
        await workspaces.cleanup_user(7)
