"""JPG/PNG to PDF conversion with bounded memory use."""

from __future__ import annotations

import os
import warnings
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter

from app.config import Settings
from app.services.cancellation import CancelToken
from app.services.errors import ImageValidationError, OutputLimitError
from app.utils.logging import get_logger
from app.utils.text import format_bytes

log = get_logger(__name__)

# Pillow's own hard bomb guard is intentionally higher than our configurable
# limit; explicit checks below produce a useful user-facing message first.
Image.MAX_IMAGE_PIXELS = 50_000_000
_ALLOWED_FORMATS = {"JPEG", "PNG"}
_METADATA = {"/Producer": "Kayi PDF Bot", "/Creator": "Kayi PDF Bot"}
_PDF_DPI = 150


@dataclass(frozen=True, slots=True)
class ImageInfo:
    path: Path
    width: int
    height: int
    format: str
    size: int


def _check_cancel(token: CancelToken | None) -> None:
    if token is not None:
        token.raise_if_cancelled()


def _check_size(path: Path, settings: Settings) -> int:
    if not path.is_file():
        raise ImageValidationError("That image is missing. Please send it again.")
    size = path.stat().st_size
    if size <= 0:
        raise ImageValidationError("That image is empty. Please send a different one.")
    if size > settings.max_file_size_bytes:
        raise ImageValidationError(
            f"That image is too large. The limit is "
            f"<b>{settings.max_file_size_mb} MB</b> per file."
        )
    return size


def inspect_image(path: Path, settings: Settings) -> ImageInfo:
    size = _check_size(path, settings)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = (image.format or "").upper()
                if image_format not in _ALLOWED_FORMATS:
                    raise ImageValidationError(
                        "Please send JPG or PNG images only."
                    )
                width, height = image.size
                if width < 1 or height < 1:
                    raise ImageValidationError("That image has no usable dimensions.")
                if width * height > settings.max_image_pixels:
                    raise ImageValidationError(
                        "That image has too many pixels. "
                        f"The limit is <b>{settings.max_image_pixels:,}</b>."
                    )
                # verify() checks the file structure without decoding all pixels.
                image.verify()
        return ImageInfo(
            path=path,
            width=width,
            height=height,
            format=image_format,
            size=size,
        )
    except ImageValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageValidationError(
            "That image is too large to process safely. Send a smaller image."
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        log.warning("Image validation failed: %s", type(exc).__name__)
        raise ImageValidationError(
            "I couldn't read this image. Send a valid JPG or PNG file."
        ) from exc


def _to_rgb(image: Image.Image) -> Image.Image:
    """Apply EXIF orientation and flatten transparency onto white.

    The returned image is owned by the caller.  Intermediate copies are
    released here so Pillow does not hold decoded pixel buffers open.
    """

    oriented = ImageOps.exif_transpose(image)
    keep_oriented = False
    try:
        if oriented.mode in {"RGBA", "LA"} or (
            oriented.mode == "P" and "transparency" in oriented.info
        ):
            rgba = oriented.convert("RGBA")
            try:
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                return background
            finally:
                rgba.close()
        if oriented.mode != "RGB":
            return oriented.convert("RGB")
        keep_oriented = True
        return oriented
    finally:
        if not keep_oriented:
            oriented.close()


def _write_single_page_pdf(image_path: Path, target: Path, settings: Settings) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(image_path) as source:
                image_format = (source.format or "").upper()
                if image_format not in _ALLOWED_FORMATS:
                    raise ImageValidationError("Please send JPG or PNG images only.")
                width, height = source.size
                if width * height > settings.max_image_pixels:
                    raise ImageValidationError(
                        "That image has too many pixels to process safely."
                    )
                source.seek(0)
                rgb = _to_rgb(source)
                try:
                    if max(rgb.size) > settings.max_image_dimension:
                        rgb.thumbnail(
                            (settings.max_image_dimension, settings.max_image_dimension),
                            Image.Resampling.LANCZOS,
                        )
                    rgb.save(target, "PDF", resolution=_PDF_DPI)
                finally:
                    if rgb is not source:
                        rgb.close()
    except ImageValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageValidationError(
            "That image is too large to process safely. Send a smaller image."
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        log.warning("Image conversion failed: %s", type(exc).__name__)
        raise ImageValidationError(
            "One of the images could not be processed. Try another copy."
        ) from exc


def images_to_pdf(
    image_paths: list[Path],
    output: Path,
    settings: Settings,
    token: CancelToken | None = None,
) -> Path:
    if not 1 <= len(image_paths) <= settings.max_images:
        raise ImageValidationError(
            f"Send between 1 and {settings.max_images} images."
        )
    total_input = sum(path.stat().st_size for path in image_paths if path.is_file())
    if total_input > settings.max_total_input_bytes:
        raise ImageValidationError(
            f"The images add up to more than <b>{settings.max_total_input_mb} MB</b>. "
            "Please use fewer or smaller images."
        )
    for path in image_paths:
        _check_cancel(token)
        inspect_image(path, settings)

    output.parent.mkdir(parents=True, exist_ok=True)
    page_pdfs: list[Path] = []
    writer = PdfWriter()
    try:
        for index, image_path in enumerate(image_paths, start=1):
            _check_cancel(token)
            page_pdf = output.parent / f"page_{index:02d}.pdf"
            page_pdfs.append(page_pdf)
            _write_single_page_pdf(image_path, page_pdf, settings)
        with ExitStack() as stack:
            for page_pdf in page_pdfs:
                _check_cancel(token)
                stream = stack.enter_context(page_pdf.open("rb"))
                reader = PdfReader(stream, strict=False)
                for page in reader.pages:
                    writer.add_page(page)
        temp = output.with_name(output.name + ".part")
        temp.unlink(missing_ok=True)
        writer.add_metadata(_METADATA)
        with temp.open("wb") as stream:
            writer.write(stream)
        size = temp.stat().st_size
        if size <= 0:
            raise OutputLimitError("The PDF could not be created. Please try again.")
        if size > settings.max_output_size_bytes:
            raise OutputLimitError(
                f"The resulting PDF is <b>{format_bytes(size)}</b>, which is larger "
                f"than the <b>{settings.max_output_size_mb} MB</b> limit. "
                "Try fewer or smaller images."
            )
        os.replace(temp, output)
        log.info(
            "Converted %d images into %s (%s)",
            len(image_paths),
            output.name,
            format_bytes(size),
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
        for page_pdf in page_pdfs:
            page_pdf.unlink(missing_ok=True)
