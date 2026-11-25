"""Lightweight, privacy-safe thumbnails for the Images to PDF workspace.

Previews are only used inside Telegram.  Final PDFs always use the original
downloaded image, so preview compression can never affect output quality.
"""

from __future__ import annotations

import asyncio
import re
import uuid
import warnings
import weakref
from pathlib import Path

from PIL import Image, ImageOps

from app.utils.logging import get_logger

log = get_logger(__name__)

PREVIEW_MAX_DIMENSION = 720
# Decoding a large source image can briefly use a lot of memory.  Preview
# generation is serialised so several users cannot exhaust a small host at
# the same time; PDF processing still uses the original files.
PREVIEW_CONCURRENCY = 1
_PREVIEW_QUALITY = 82
_ALLOWED_FORMATS = {"JPEG", "PNG"}
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")
_preview_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def new_item_id() -> str:
    """Return a short, stable identifier for one queued image."""

    return uuid.uuid4().hex[:12]


def preview_path(directory: Path, item_id: str) -> Path:
    """Return the temporary preview path for an image inside a workspace."""

    safe = _SAFE_ID.sub("", item_id)[:32] or "image"
    return Path(directory) / f"preview_{safe}.jpg"


def write_preview(
    source: Path,
    target: Path,
    max_dimension: int = PREVIEW_MAX_DIMENSION,
) -> Path:
    """Write a JPEG thumbnail no larger than ``max_dimension`` pixels.

    EXIF orientation is applied, transparency is flattened onto white, and
    small images are never enlarged.  Only one decoded image is held at a time.
    """

    limit = max(64, min(int(max_dimension), 2048))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                image_format = (image.format or "").upper()
                if image_format not in _ALLOWED_FORMATS:
                    raise ValueError("Unsupported preview image format")
                oriented = ImageOps.exif_transpose(image)
                oriented_owned = oriented is not image
                try:
                    if oriented.mode in {"RGBA", "LA"} or (
                        oriented.mode == "P" and "transparency" in oriented.info
                    ):
                        rgba = oriented.convert("RGBA")
                        try:
                            rgb = Image.new("RGB", rgba.size, (255, 255, 255))
                            rgb.paste(rgba, mask=rgba.getchannel("A"))
                        finally:
                            rgba.close()
                    elif oriented.mode != "RGB":
                        rgb = oriented.convert("RGB")
                    else:
                        rgb = oriented
                    try:
                        # thumbnail() only shrinks, preserving the aspect ratio.
                        rgb.thumbnail((limit, limit), Image.Resampling.LANCZOS)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        rgb.save(
                            target,
                            "JPEG",
                            quality=_PREVIEW_QUALITY,
                            optimize=False,
                        )
                    finally:
                        if rgb is not oriented:
                            rgb.close()
                finally:
                    if oriented_owned:
                        oriented.close()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def _preview_semaphore() -> asyncio.Semaphore:
    """Return the preview limiter for the current event loop.

    Tests create a fresh loop per test, so the limiter is kept per loop rather
    than binding a module-level primitive to whichever loop used it first.
    """

    loop = asyncio.get_running_loop()
    semaphore = _preview_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(PREVIEW_CONCURRENCY)
        _preview_semaphores[loop] = semaphore
    return semaphore


async def write_preview_async(
    source: Path,
    target: Path,
    max_dimension: int = PREVIEW_MAX_DIMENSION,
) -> Path:
    """Build one thumbnail off the event loop with bounded concurrency."""

    async with _preview_semaphore():
        return await asyncio.to_thread(write_preview, source, target, max_dimension)
