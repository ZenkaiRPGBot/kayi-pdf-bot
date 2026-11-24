"""Shared fixtures for the Kayi PDF Bot test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter

from app.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="123456:TEST-TOKEN",
        webhook_secret="test-secret",
        temp_root=tmp_path / "temp",
        max_workspaces=20,
    )


@pytest.fixture
def pdf_factory(tmp_path: Path):
    """Create a PDF with blank pages of distinct sizes."""

    counter = 0

    def make_pdf(
        page_count: int = 3,
        name: str | None = None,
        size: tuple[float, float] = (200.0, 300.0),
    ) -> Path:
        nonlocal counter
        counter += 1
        writer = PdfWriter()
        for index in range(page_count):
            width = size[0] + index
            height = size[1] + index
            writer.add_blank_page(width=width, height=height)
        writer.add_metadata({"/Title": f"Sample {counter}"})
        path = tmp_path / (name or f"sample_{counter}.pdf")
        with path.open("wb") as stream:
            writer.write(stream)
        writer.close()
        return path

    return make_pdf


@pytest.fixture
def image_factory(tmp_path: Path):
    counter = 0

    def make_image(
        size: tuple[int, int] = (120, 80),
        mode: str = "RGB",
        name: str | None = None,
        fmt: str = "JPEG",
        color=(200, 30, 30),
        exif_orientation: int | None = None,
        **save_kwargs,
    ) -> Path:
        nonlocal counter
        counter += 1
        suffix = ".png" if fmt.upper() == "PNG" else ".jpg"
        path = tmp_path / (name or f"image_{counter}{suffix}")
        image = Image.new(mode, size, color)
        try:
            if exif_orientation is not None:
                exif = image.getexif()
                exif[0x0112] = exif_orientation
                save_kwargs["exif"] = exif
            image.save(path, fmt, **save_kwargs)
        finally:
            image.close()
        return path

    return make_image
