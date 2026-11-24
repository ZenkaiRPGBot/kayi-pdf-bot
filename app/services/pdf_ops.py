"""PDF validation and manipulation built on pypdf.

All functions in this module are synchronous and CPU-bound.  Call them from
``asyncio.to_thread`` (or the job runner) rather than directly from a handler.
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from app.config import Settings
from app.services.cancellation import CancelToken
from app.services.errors import (
    OutputLimitError,
    PageRangeError,
    PdfEncryptedError,
    PdfValidationError,
)
from app.services.page_ranges import format_page_selection
from app.utils.logging import get_logger
from app.utils.text import format_bytes

log = get_logger(__name__)

_METADATA = {"/Producer": "Kayi PDF Bot", "/Creator": "Kayi PDF Bot"}


@dataclass(frozen=True, slots=True)
class PdfInfo:
    path: Path
    page_count: int
    size: int


def _check_cancel(token: CancelToken | None) -> None:
    if token is not None:
        token.raise_if_cancelled()


def _open_reader(path: Path, settings: Settings) -> PdfReader:
    if not path.is_file():
        raise PdfValidationError("The PDF file is missing. Please send it again.")
    size = path.stat().st_size
    if size <= 0:
        raise PdfValidationError("That PDF is empty. Please send a different file.")
    if size > settings.max_file_size_bytes:
        raise PdfValidationError(
            f"That PDF is too large. The limit is "
            f"<b>{settings.max_file_size_mb} MB</b> per file."
        )
    try:
        stream = path.open("rb")
    except OSError as exc:
        log.warning("PDF could not be opened: %s", type(exc).__name__)
        raise PdfValidationError(
            "I couldn't open this PDF. Please send it again."
        ) from exc
    try:
        header = stream.read(1024)
        if b"%PDF-" not in header:
            raise PdfValidationError(
                "That file does not look like a PDF. Please send a valid PDF."
            )
        reader = PdfReader(stream, strict=False)
        if reader.is_encrypted:
            raise PdfEncryptedError()
        page_count = len(reader.pages)
        if page_count < 1:
            raise PdfValidationError("That PDF does not contain any pages.")
        if page_count > settings.max_pages:
            raise PdfValidationError(
                f"That PDF has <b>{page_count}</b> pages. The limit is "
                f"<b>{settings.max_pages}</b> pages."
            )
        # Touch the page tree now so malformed documents fail during
        # validation instead of halfway through an operation.  The stream
        # stays open: pypdf lazily reads the header and page data when pages
        # are copied into a writer.
        _ = reader.pages[0]
        return reader
    except (PdfEncryptedError, PdfValidationError):
        stream.close()
        raise
    except MemoryError:
        stream.close()
        raise
    except Exception as exc:  # pypdf raises several parser-specific types
        stream.close()
        log.warning("PDF validation failed: %s", type(exc).__name__)
        raise PdfValidationError(
            "I couldn't read this PDF. It may be damaged or use an "
            "unsupported format. Try another copy."
        ) from exc


def inspect_pdf(path: Path, settings: Settings) -> PdfInfo:
    """Validate a PDF and return its page count and size."""

    reader = _open_reader(path, settings)
    try:
        page_count = len(reader.pages)
    finally:
        # PdfReader keeps the stream alive for lazy page reads; the caller
        # reopens for processing, so closing here is safe.
        try:
            reader.stream.close()
        except Exception:
            pass
    return PdfInfo(path=path, page_count=page_count, size=path.stat().st_size)


def _check_output(path: Path, settings: Settings, *, total: int = 0) -> int:
    if not path.is_file():
        raise OutputLimitError("The result could not be created.")
    size = path.stat().st_size
    if size <= 0:
        raise OutputLimitError("The result is empty. Please try again.")
    if size > settings.max_output_size_bytes:
        path.unlink(missing_ok=True)
        raise OutputLimitError(
            f"The result is <b>{format_bytes(size)}</b>, which is larger than "
            f"the <b>{settings.max_output_size_mb} MB</b> output limit. "
            "Try fewer pages or a smaller file."
        )
    if total and total > settings.max_total_output_bytes:
        path.unlink(missing_ok=True)
        raise OutputLimitError(
            "The combined results are too large to send. Try fewer ranges."
        )
    return size


def _finalise(temp: Path, output: Path) -> None:
    os.replace(temp, output)


def merge_pdfs(
    inputs: list[Path],
    output: Path,
    settings: Settings,
    token: CancelToken | None = None,
) -> Path:
    if not 2 <= len(inputs) <= settings.max_files_merge:
        raise PdfValidationError(
            f"Send between 2 and {settings.max_files_merge} PDFs to merge."
        )
    total_input = sum(path.stat().st_size for path in inputs if path.is_file())
    if total_input > settings.max_total_input_bytes:
        raise PdfValidationError(
            f"The files add up to more than <b>{settings.max_total_input_mb} MB</b>. "
            "Please merge fewer or smaller PDFs."
        )
    infos: list[PdfInfo] = []
    for path in inputs:
        _check_cancel(token)
        infos.append(inspect_pdf(path, settings))
    total_pages = sum(info.page_count for info in infos)
    if total_pages > settings.max_total_pages_per_job:
        raise PdfValidationError(
            f"These PDFs contain <b>{total_pages}</b> pages in total. The limit "
            f"for one merge is <b>{settings.max_total_pages_per_job}</b> pages."
        )
    if len({info.path.resolve() for info in infos}) != len(infos):
        raise PdfValidationError("The same PDF was added more than once.")

    writer = PdfWriter()
    try:
        with ExitStack() as stack:
            for path in inputs:
                _check_cancel(token)
                reader = _open_reader(path, settings)
                stack.callback(_close_reader, reader)
                for page in reader.pages:
                    _check_cancel(token)
                    writer.add_page(page)
        temp = output.with_name(output.name + ".part")
        temp.unlink(missing_ok=True)
        writer.add_metadata(_METADATA)
        with temp.open("wb") as stream:
            writer.write(stream)
        size = _check_output(temp, settings)
        _finalise(temp, output)
        log.info("Merged %d PDFs into %s (%s)", len(inputs), output.name, format_bytes(size))
        return output
    except Exception:
        output.with_name(output.name + ".part").unlink(missing_ok=True)
        raise
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _close_reader(reader: PdfReader) -> None:
    try:
        reader.stream.close()
    except Exception:
        pass


def split_pdf(
    input_path: Path,
    ranges: list[list[int]],
    output_dir: Path,
    settings: Settings,
    token: CancelToken | None = None,
) -> list[Path]:
    if not ranges:
        raise PageRangeError("Send at least one page range.")
    if len(ranges) > settings.max_split_outputs:
        raise PageRangeError(
            f"Create no more than {settings.max_split_outputs} documents at a time."
        )
    info = inspect_pdf(input_path, settings)
    seen_pages = 0
    for pages in ranges:
        if not pages:
            raise PageRangeError("One of the ranges is empty.")
        if any(page < 1 or page > info.page_count for page in pages):
            raise PageRangeError(
                f"This PDF has <b>{info.page_count}</b> pages. "
                "One of the ranges is out of bounds."
            )
        seen_pages += len(pages)
    if seen_pages > settings.max_split_total_pages:
        raise PageRangeError(
            f"That would produce <b>{seen_pages}</b> pages in total. "
            f"The limit is <b>{settings.max_split_total_pages}</b>."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    total_size = 0
    try:
        reader = _open_reader(input_path, settings)
        try:
            for index, pages in enumerate(ranges, start=1):
                _check_cancel(token)
                writer = PdfWriter()
                target = output_dir / f"part_{index:02d}.pdf"
                temp = target.with_name(target.name + ".part")
                try:
                    for page_number in pages:
                        _check_cancel(token)
                        writer.add_page(reader.pages[page_number - 1])
                    writer.add_metadata(_METADATA)
                    temp.unlink(missing_ok=True)
                    with temp.open("wb") as stream:
                        writer.write(stream)
                    size = _check_output(temp, settings)
                    total_size += size
                    if total_size > settings.max_total_output_bytes:
                        raise OutputLimitError(
                            "The split results are too large to send. "
                            "Try fewer or shorter ranges."
                        )
                    _finalise(temp, target)
                    outputs.append(target)
                except Exception:
                    temp.unlink(missing_ok=True)
                    raise
                finally:
                    try:
                        writer.close()
                    except Exception:
                        pass
        finally:
            _close_reader(reader)
        log.info(
            "Split %s into %d PDFs (total %s)",
            input_path.name,
            len(outputs),
            format_bytes(total_size),
        )
        return outputs
    except Exception:
        for path in outputs:
            path.unlink(missing_ok=True)
        raise


def extract_pages(
    input_path: Path,
    pages: list[int],
    output: Path,
    settings: Settings,
    token: CancelToken | None = None,
) -> Path:
    info = inspect_pdf(input_path, settings)
    if not pages:
        raise PageRangeError("Select at least one page to extract.")
    if len(pages) > settings.max_pages:
        raise PageRangeError(
            f"Select no more than <b>{settings.max_pages}</b> pages at a time."
        )
    if len(set(pages)) != len(pages):
        raise PageRangeError("Pages can't be repeated in an extraction.")
    if any(page < 1 or page > info.page_count for page in pages):
        raise PageRangeError(
            f"This PDF has <b>{info.page_count}</b> pages. "
            "One of the selected pages is out of range."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    reader = _open_reader(input_path, settings)
    writer = PdfWriter()
    try:
        for page_number in pages:
            _check_cancel(token)
            writer.add_page(reader.pages[page_number - 1])
        temp = output.with_name(output.name + ".part")
        temp.unlink(missing_ok=True)
        writer.add_metadata(_METADATA)
        with temp.open("wb") as stream:
            writer.write(stream)
        size = _check_output(temp, settings)
        _finalise(temp, output)
        log.info(
            "Extracted %d pages (%s) from %s",
            len(pages),
            format_page_selection(pages),
            input_path.name,
        )
        log.debug("Extracted output size: %s", format_bytes(size))
        return output
    except Exception:
        output.with_name(output.name + ".part").unlink(missing_ok=True)
        raise
    finally:
        try:
            writer.close()
        except Exception:
            pass
        _close_reader(reader)


def rotate_pdf(
    input_path: Path,
    pages: list[int] | None,
    angle: int,
    output: Path,
    settings: Settings,
    token: CancelToken | None = None,
) -> Path:
    if angle not in (90, 180, 270):
        raise PdfValidationError("Choose a rotation of 90, 180, or 270 degrees.")
    info = inspect_pdf(input_path, settings)
    if pages is not None:
        if not pages:
            raise PageRangeError("Select at least one page to rotate.")
        if len(set(pages)) != len(pages):
            raise PageRangeError("Pages can't be repeated in a rotation.")
        if any(page < 1 or page > info.page_count for page in pages):
            raise PageRangeError(
                f"This PDF has <b>{info.page_count}</b> pages. "
                "One of the selected pages is out of range."
            )
        selected = set(pages)
    else:
        selected = None
    output.parent.mkdir(parents=True, exist_ok=True)
    reader = _open_reader(input_path, settings)
    writer = PdfWriter()
    try:
        for index, page in enumerate(reader.pages, start=1):
            _check_cancel(token)
            if selected is None or index in selected:
                page.rotate(angle)
            writer.add_page(page)
        temp = output.with_name(output.name + ".part")
        temp.unlink(missing_ok=True)
        writer.add_metadata(_METADATA)
        with temp.open("wb") as stream:
            writer.write(stream)
        _check_output(temp, settings)
        _finalise(temp, output)
        log.info(
            "Rotated %s by %d degrees",
            "all pages" if selected is None else f"{len(selected)} page(s)",
            angle,
        )
        return output
    except Exception:
        output.with_name(output.name + ".part").unlink(missing_ok=True)
        raise
    finally:
        try:
            writer.close()
        except Exception:
            pass
        _close_reader(reader)
