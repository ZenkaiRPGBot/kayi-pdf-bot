"""JPG/PNG to PDF conversion with bounded memory use."""

from __future__ import annotations

import os
import warnings
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import img2pdf
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter

from app.config import Settings
from app.services.cancellation import CancelToken
from app.services.errors import ImageValidationError, JobCancelled, OutputLimitError
from app.utils.logging import get_logger
from app.utils.text import format_bytes

log = get_logger(__name__)

# Pillow's own hard bomb guard is intentionally higher than our configurable
# limit; explicit checks below produce a useful user-facing message first.
Image.MAX_IMAGE_PIXELS = 50_000_000
_ALLOWED_FORMATS = {"JPEG", "PNG"}
_METADATA = {"/Producer": "Kayi PDF Bot", "/Creator": "Kayi PDF Bot"}
_PDF_DPI = 150

# JPEGs using these modes need no colour conversion before embedding.  CMYK
# and exotic modes keep using the Pillow path so their output stays unchanged.
_EMBED_MODES = {"RGB", "L"}
_ORIENTATION_TAG = 0x0112
_JPEG_SOI = b"\xff\xd8"
_JPEG_SOS = 0xDA
_JPEG_EOI = 0xD9
_JPEG_APP0 = 0xE0
_JPEG_APP14 = 0xEE
_JPEG_COM = 0xFE


@dataclass(frozen=True, slots=True)
class ImageInfo:
    path: Path
    width: int
    height: int
    format: str
    size: int
    mode: str = "RGB"
    orientation: int = 1


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


def _read_orientation(image: Image.Image) -> int:
    """Return the EXIF orientation, or 0 when it cannot be read reliably.

    Parse the block already loaded by ``Image.open`` instead of calling
    ``getexif()``: on PNG files that call consumes the stream, and
    ``verify()`` must run directly after opening.
    """

    raw = image.info.get("exif")
    if not raw:
        return 1
    try:
        exif = Image.Exif()
        exif.load(raw)
        value = exif.get(_ORIENTATION_TAG, 1)
        return int(value) if value else 1
    except Exception:
        return 0


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
                mode = (image.mode or "").upper()
                orientation = _read_orientation(image)
                # verify() checks the file structure without decoding all pixels.
                image.verify()
        return ImageInfo(
            path=path,
            width=width,
            height=height,
            format=image_format,
            size=size,
            mode=mode,
            orientation=orientation,
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
    ``exif_transpose`` decodes the whole image even when the orientation is
    already normal, so skip it in that common case.
    """

    if _read_orientation(image) == 1:
        oriented = image
        owned = False
    else:
        oriented = ImageOps.exif_transpose(image)
        owned = oriented is not image
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
        # Hand ownership of the (possibly transposed) image to the caller.
        owned = False
        return oriented
    finally:
        if owned:
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


def _strip_jpeg_metadata(data: bytes) -> bytes | None:
    """Drop EXIF/XMP/ICC/comment segments without touching the JPEG scan.

    Photos often carry GPS coordinates and other personal metadata.  The old
    Pillow path dropped it by re-encoding; direct embedding must strip it
    explicitly.  Only header segments are rewritten, so pixels are untouched
    and the operation is a fast linear copy.  ``None`` means the structure is
    unusual and the caller must fall back to the re-encoding path rather than
    risk embedding unscrubbed metadata.
    """

    if len(data) < 4 or data[:2] != _JPEG_SOI or data[-2:] != b"\xff\xd9":
        return None
    output = bytearray(_JPEG_SOI)
    position = 2
    changed = False
    size = len(data)
    while position + 1 < size:
        if data[position] != 0xFF:
            return None
        marker = data[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker == _JPEG_SOS:
            # Entropy-coded data follows the scan header; copy it verbatim.
            output += data[position:]
            break
        if marker == _JPEG_EOI:
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            output += data[position : position + 2]
            position += 2
            continue
        if position + 4 > size:
            return None
        length = int.from_bytes(data[position + 2 : position + 4], "big")
        end = position + 2 + length
        if length < 2 or end > size:
            return None
        segment = data[position:end]
        payload = segment[4:]
        keep = False
        if marker == _JPEG_APP0:
            # Keep the JFIF header; JFXX thumbnails carry no useful data.
            keep = payload.startswith(b"JFIF\x00")
        elif marker == _JPEG_APP14:
            keep = payload.startswith(b"Adobe")
        elif not (0xE0 <= marker <= 0xEF):
            keep = marker != _JPEG_COM
        if keep:
            output += segment
        else:
            changed = True
        position = end
    else:
        return None
    return bytes(output) if changed else data


def _can_embed_jpeg(info: ImageInfo, settings: Settings) -> bool:
    """Direct embedding needs an upright, RGB/gray JPEG within size limits."""

    return (
        info.format == "JPEG"
        and info.mode in _EMBED_MODES
        and info.orientation == 1
        and max(info.width, info.height) <= settings.max_image_dimension
    )


def _embed_jpegs(
    infos: list[ImageInfo],
    target: Path,
    token: CancelToken | None,
) -> int:
    """Write JPEG scans straight into a PDF, avoiding decode/re-encode."""

    payloads: list[bytes] = []
    try:
        for info in infos:
            _check_cancel(token)
            payload = _strip_jpeg_metadata(info.path.read_bytes())
            if payload is None:
                raise ValueError("JPEG structure could not be scrubbed safely")
            payloads.append(payload)
        _check_cancel(token)
        layout = img2pdf.get_fixed_dpi_layout_fun((_PDF_DPI, _PDF_DPI))
        with target.open("wb") as stream:
            img2pdf.convert(
                payloads,
                outputstream=stream,
                layout_fun=layout,
                producer="Kayi PDF Bot",
                creator="Kayi PDF Bot",
                nodate=True,
            )
        _check_cancel(token)
        return target.stat().st_size
    finally:
        payloads.clear()


def _reencode_images(
    image_paths: list[Path],
    target: Path,
    settings: Settings,
    token: CancelToken | None,
) -> int:
    """Portable Pillow path used for PNGs, rotated or CMYK images, and when
    the embedded output would exceed the size limit."""

    page_pdfs: list[Path] = []
    writer = PdfWriter()
    try:
        for index, image_path in enumerate(image_paths, start=1):
            _check_cancel(token)
            page_pdf = target.parent / f"page_{index:02d}.pdf"
            page_pdfs.append(page_pdf)
            _write_single_page_pdf(image_path, page_pdf, settings)
        with ExitStack() as stack:
            for page_pdf in page_pdfs:
                _check_cancel(token)
                stream = stack.enter_context(page_pdf.open("rb"))
                reader = PdfReader(stream, strict=False)
                for page in reader.pages:
                    writer.add_page(page)
        writer.add_metadata(_METADATA)
        with target.open("wb") as stream:
            writer.write(stream)
        return target.stat().st_size
    finally:
        try:
            writer.close()
        except Exception:
            pass
        for page_pdf in page_pdfs:
            page_pdf.unlink(missing_ok=True)


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
    infos: list[ImageInfo] = []
    for path in image_paths:
        _check_cancel(token)
        infos.append(inspect_image(path, settings))

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".part")
    temp.unlink(missing_ok=True)
    try:
        size: int | None = None
        embedded = False
        if all(_can_embed_jpeg(info, settings) for info in infos):
            try:
                candidate = _embed_jpegs(infos, temp, token)
                if 0 < candidate <= settings.max_output_size_bytes:
                    size = candidate
                    embedded = True
                else:
                    log.info(
                        "Embedded JPEG output is %s; recompressing to fit %d MB",
                        format_bytes(candidate),
                        settings.max_output_size_mb,
                    )
            except (JobCancelled, MemoryError):
                raise
            except Exception as exc:
                log.warning(
                    "Direct JPEG embedding failed (%s); recompressing",
                    type(exc).__name__,
                )
            if size is None:
                temp.unlink(missing_ok=True)
        if size is None:
            size = _reencode_images(image_paths, temp, settings, token)
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
            "Converted %d images into %s (%s%s)",
            len(image_paths),
            output.name,
            format_bytes(size),
            ", direct JPEG embed" if embedded else "",
        )
        return output
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)
