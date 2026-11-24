"""Telegram file transfer helpers, aggregate limits, and ZIP creation."""

from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path
from typing import BinaryIO

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import FSInputFile, Message

from app.config import Settings
from app.services.errors import (
    DownloadError,
    FileValidationError,
    OutputLimitError,
    UploadError,
)
from app.utils.logging import get_logger
from app.utils.text import format_bytes, sanitize_filename

log = get_logger(__name__)


class _LimitedWriter:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.written = 0
        self._stream = path.open("wb")

    def write(self, data: bytes) -> int:
        self.written += len(data)
        if self.written > self.max_bytes:
            raise FileValidationError(
                f"That file is larger than the <b>{format_bytes(self.max_bytes)}</b> limit."
            )
        return self._stream.write(data)

    def flush(self) -> None:
        self._stream.flush()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)

    def close(self) -> None:
        try:
            self._stream.close()
        except Exception:
            pass

    def __enter__(self) -> "_LimitedWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


async def download_file(
    bot: Bot,
    file_id: str,
    destination: Path,
    settings: Settings,
    *,
    expected_size: int | None = None,
) -> Path:
    """Download a Telegram file to disk with a hard streaming size cap."""

    if expected_size is not None and expected_size > settings.max_file_size_bytes:
        raise FileValidationError(
            f"That file is too large. The limit is "
            f"<b>{settings.max_file_size_mb} MB</b> per file."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    temp = destination.with_name(destination.name + ".part")
    temp.unlink(missing_ok=True)
    writer = _LimitedWriter(temp, settings.max_file_size_bytes)
    try:
        await bot.download(
            file_id,
            destination=writer,  # type: ignore[arg-type]
            timeout=max(1, int(settings.download_timeout)),
            seek=False,
        )
        writer.close()
        if writer.written <= 0 or not temp.is_file() or temp.stat().st_size <= 0:
            raise DownloadError("That file is empty. Please send a different file.")
        if temp.stat().st_size > settings.max_file_size_bytes:
            raise FileValidationError(
                f"That file is too large. The limit is "
                f"<b>{settings.max_file_size_mb} MB</b> per file."
            )
        os.replace(temp, destination)
        return destination
    except (FileValidationError, DownloadError):
        writer.close()
        temp.unlink(missing_ok=True)
        raise
    except (TelegramRetryAfter, TelegramNetworkError, TelegramAPIError) as exc:
        writer.close()
        temp.unlink(missing_ok=True)
        log.warning("Telegram download failed: %s", type(exc).__name__)
        raise DownloadError() from exc
    except asyncio.TimeoutError as exc:
        writer.close()
        temp.unlink(missing_ok=True)
        raise DownloadError(
            "The download timed out. Please send the file again."
        ) from exc
    except OSError as exc:
        writer.close()
        temp.unlink(missing_ok=True)
        log.warning("Disk error while downloading: %s", type(exc).__name__)
        raise DownloadError("I couldn't save that file. Please try again.") from exc
    except Exception as exc:
        writer.close()
        temp.unlink(missing_ok=True)
        log.warning("Download failed unexpectedly: %s", type(exc).__name__)
        raise DownloadError() from exc


async def deliver_document(
    bot: Bot,
    chat_id: int,
    path: Path,
    filename: str,
    *,
    caption: str | None = None,
    settings: Settings,
    reply_markup=None,
) -> Message:
    if not path.is_file():
        raise UploadError("The result file is missing. Please try again.")
    safe_name = sanitize_filename(filename, "kayi_result.pdf")
    try:
        return await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(path, filename=safe_name),
            caption=caption,
            reply_markup=reply_markup,
            request_timeout=max(1, int(settings.telegram_request_timeout)),
        )
    except (TelegramRetryAfter, TelegramNetworkError, TelegramAPIError) as exc:
        log.warning("Telegram upload failed: %s", type(exc).__name__)
        raise UploadError() from exc
    except asyncio.TimeoutError as exc:
        raise UploadError(
            "Sending the result timed out. Please try again."
        ) from exc
    except OSError as exc:
        log.warning("Disk error while uploading: %s", type(exc).__name__)
        raise UploadError() from exc


def check_aggregate_size(existing_sizes: int, new_size: int, settings: Settings) -> None:
    if existing_sizes + new_size > settings.max_total_input_bytes:
        raise FileValidationError(
            f"These files would add up to more than "
            f"<b>{settings.max_total_input_mb} MB</b>. "
            "Please use fewer or smaller files."
        )


def create_zip(
    files: list[Path],
    zip_path: Path,
    settings: Settings,
    *,
    arcnames: list[str] | None = None,
) -> Path:
    if not files:
        raise OutputLimitError("There are no files to pack.")
    if len(files) > settings.max_zip_entries:
        raise OutputLimitError(
            f"Too many files for one archive (limit {settings.max_zip_entries})."
        )
    total = 0
    for path in files:
        if not path.is_file():
            raise OutputLimitError("One of the result files is missing.")
        total += path.stat().st_size
        if total > settings.max_total_output_bytes:
            raise OutputLimitError("The results are too large to pack into one archive.")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temp = zip_path.with_name(zip_path.name + ".part")
    temp.unlink(missing_ok=True)
    names = arcnames or [f"part_{index:02d}.pdf" for index in range(1, len(files) + 1)]
    if len(names) != len(files):
        raise OutputLimitError("The archive file names are invalid.")
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED) as archive:
            used: set[str] = set()
            for path, name in zip(files, names):
                safe = sanitize_filename(name, "part.pdf")
                if safe in used:
                    stem = Path(safe).stem
                    suffix = Path(safe).suffix or ".pdf"
                    index = 2
                    while f"{stem}_{index}{suffix}" in used:
                        index += 1
                    safe = f"{stem}_{index}{suffix}"
                used.add(safe)
                archive.write(path, arcname=safe)
        size = temp.stat().st_size
        if size <= 0:
            raise OutputLimitError("The archive could not be created.")
        if size > settings.max_output_size_bytes:
            raise OutputLimitError(
                f"The archive is <b>{format_bytes(size)}</b>, above the "
                f"<b>{settings.max_output_size_mb} MB</b> limit. "
                "Try fewer ranges."
            )
        os.replace(temp, zip_path)
        return zip_path
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
