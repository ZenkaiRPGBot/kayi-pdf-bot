"""File streaming, delivery, aggregate limits, and ZIP creation."""

from __future__ import annotations

import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.errors import (
    DownloadError,
    FileValidationError,
    OutputLimitError,
    UploadError,
)
from app.services.files import (
    _LimitedWriter,
    check_aggregate_size,
    create_zip,
    deliver_document,
    download_file,
    safe_unlink,
)


def test_limited_writer_enforces_cap(tmp_path: Path) -> None:
    path = tmp_path / "out.bin"
    writer = _LimitedWriter(path, 5)
    try:
        writer.write(b"12345")
        with pytest.raises(FileValidationError, match="5 B"):
            writer.write(b"6")
    finally:
        writer.close()
    assert path.read_bytes() == b"12345"


class DownloadBot:
    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.calls: list[dict] = []

    async def download(self, file_id, destination, timeout, seek):
        self.calls.append(
            {
                "file_id": file_id,
                "timeout": timeout,
                "seek": seek,
                "destination": destination,
            }
        )
        for chunk in self.chunks:
            destination.write(chunk)
        if self.error is not None:
            raise self.error
        destination.flush()


@pytest.mark.asyncio
async def test_download_file_writes_and_renames(settings, tmp_path) -> None:
    bot = DownloadBot([b"%PDF-", b"content"])
    destination = tmp_path / "download.pdf"
    result = await download_file(bot, "file-id", destination, settings)
    assert result == destination
    assert destination.read_bytes() == b"%PDF-content"
    assert not list(tmp_path.glob("*.part"))
    assert bot.calls[0]["file_id"] == "file-id"
    assert bot.calls[0]["seek"] is False


@pytest.mark.asyncio
async def test_download_file_rejects_expected_large_size(settings, tmp_path) -> None:
    bot = DownloadBot([])
    with pytest.raises(FileValidationError, match="too large"):
        await download_file(
            bot,
            "file-id",
            tmp_path / "out.pdf",
            settings,
            expected_size=settings.max_file_size_bytes + 1,
        )
    assert bot.calls == []
    assert not (tmp_path / "out.pdf").exists()


@pytest.mark.asyncio
async def test_download_file_cleans_partial_on_streaming_limit(
    settings, tmp_path
) -> None:
    bot = DownloadBot([b"x" * 600_000, b"y" * 600_000])
    with pytest.raises(FileValidationError):
        await download_file(
            bot,
            "file-id",
            tmp_path / "out.pdf",
            replace(settings, max_file_size_mb=1),
        )
    assert not (tmp_path / "out.pdf").exists()
    assert not (tmp_path / "out.pdf.part").exists()


@pytest.mark.asyncio
async def test_download_file_cleans_partial_on_error(settings, tmp_path) -> None:
    bot = DownloadBot([b"partial"], error=RuntimeError("connection reset"))
    with pytest.raises(DownloadError):
        await download_file(bot, "file-id", tmp_path / "out.pdf", settings)
    assert not (tmp_path / "out.pdf").exists()
    assert not (tmp_path / "out.pdf.part").exists()


@pytest.mark.asyncio
async def test_download_file_rejects_empty_download(settings, tmp_path) -> None:
    bot = DownloadBot([])
    with pytest.raises(DownloadError, match="empty"):
        await download_file(bot, "file-id", tmp_path / "out.pdf", settings)
    assert not (tmp_path / "out.pdf").exists()


def test_aggregate_size_limit(settings) -> None:
    check_aggregate_size(settings.max_total_input_bytes - 1, 1, settings)
    with pytest.raises(FileValidationError, match="add up"):
        check_aggregate_size(settings.max_total_input_bytes, 1, settings)


def test_create_zip_contains_all_entries(tmp_path: Path, settings) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    archive = tmp_path / "result.zip"
    create_zip(
        [first, second],
        archive,
        settings,
        arcnames=["part_01.pdf", "part_02.pdf"],
    )
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.namelist() == ["part_01.pdf", "part_02.pdf"]
        assert bundle.read("part_01.pdf") == b"one"


def test_create_zip_rejects_too_many_or_missing(tmp_path: Path, settings) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    with pytest.raises(OutputLimitError, match="Too many"):
        create_zip(
            [first, second],
            tmp_path / "out.zip",
            replace(settings, max_zip_entries=1),
        )
    with pytest.raises(OutputLimitError, match="missing"):
        create_zip([first, tmp_path / "nope.pdf"], tmp_path / "out.zip", settings)
    with pytest.raises(OutputLimitError, match="no files"):
        create_zip([], tmp_path / "out.zip", settings)


def test_create_zip_enforces_combined_output_limit(tmp_path: Path, settings) -> None:
    first = tmp_path / "first.pdf"
    first.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(OutputLimitError, match="too large"):
        create_zip(
            [first],
            tmp_path / "out.zip",
            replace(settings, max_total_output_mb=1),
        )
    assert not (tmp_path / "out.zip").exists()


def test_create_zip_sanitizes_and_deduplicates_names(tmp_path: Path, settings) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    archive = tmp_path / "out.zip"
    create_zip(
        [first, second],
        archive,
        settings,
        arcnames=["../../same.pdf", "../../same.pdf"],
    )
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
    assert names == ["same.pdf", "same_2.pdf"]


def test_create_zip_rejects_mismatched_names(tmp_path: Path, settings) -> None:
    first = tmp_path / "first.pdf"
    first.write_bytes(b"one")
    with pytest.raises(OutputLimitError, match="invalid"):
        create_zip(
            [first],
            tmp_path / "out.zip",
            settings,
            arcnames=["a.pdf", "b.pdf"],
        )


class UploadBot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    async def send_document(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return "sent"


@pytest.mark.asyncio
async def test_deliver_document_sanitizes_name(tmp_path: Path, settings) -> None:
    path = tmp_path / "result.pdf"
    path.write_bytes(b"pdf")
    bot = UploadBot()
    result = await deliver_document(
        bot,
        chat_id=1,
        path=path,
        filename="../../My Result.pdf",
        caption="done",
        settings=settings,
        reply_markup="markup",
    )
    assert result == "sent"
    call = bot.calls[0]
    assert call["chat_id"] == 1
    assert call["document"].filename == "My Result.pdf"
    assert call["caption"] == "done"
    assert call["reply_markup"] == "markup"


@pytest.mark.asyncio
async def test_deliver_document_errors(tmp_path: Path, settings) -> None:
    with pytest.raises(UploadError, match="missing"):
        await deliver_document(
            UploadBot(),
            1,
            tmp_path / "missing.pdf",
            "missing.pdf",
            settings=settings,
        )
    path = tmp_path / "result.pdf"
    path.write_bytes(b"pdf")
    from aiogram.exceptions import TelegramNetworkError

    with pytest.raises(UploadError):
        await deliver_document(
            UploadBot(TelegramNetworkError(method=None, message="down")),
            1,
            path,
            "result.pdf",
            settings=settings,
        )


def test_safe_unlink(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"x")
    safe_unlink(path)
    assert not path.exists()
    safe_unlink(path)
