"""Image inspection and images-to-PDF conversion."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from app.services import image_ops
from app.services.cancellation import CancelToken
from app.services.errors import ImageValidationError, JobCancelled
from app.services.image_ops import images_to_pdf, inspect_image
from tests.helpers import pdf_metadata, pdf_page_count, pdf_page_sizes


def test_inspect_jpeg_and_png(image_factory, settings) -> None:
    jpeg = image_factory(size=(120, 80), fmt="JPEG")
    png = image_factory(size=(64, 32), fmt="PNG")
    jpeg_info = inspect_image(jpeg, settings)
    png_info = inspect_image(png, settings)
    assert (jpeg_info.width, jpeg_info.height, jpeg_info.format) == (120, 80, "JPEG")
    assert (png_info.width, png_info.height, png_info.format) == (64, 32, "PNG")
    assert jpeg_info.size == jpeg.stat().st_size
    assert (jpeg_info.mode, jpeg_info.orientation) == ("RGB", 1)


def test_inspect_rejects_unsupported_format(image_factory, settings) -> None:
    gif = image_factory(size=(20, 20), fmt="GIF")
    with pytest.raises(ImageValidationError, match="JPG or PNG"):
        inspect_image(gif, settings)


def test_inspect_rejects_missing_and_empty(tmp_path: Path, settings) -> None:
    with pytest.raises(ImageValidationError, match="missing"):
        inspect_image(tmp_path / "missing.jpg", settings)
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    with pytest.raises(ImageValidationError, match="empty"):
        inspect_image(empty, settings)


def test_inspect_rejects_corrupt_file(tmp_path: Path, settings) -> None:
    path = tmp_path / "corrupt.jpg"
    path.write_bytes(b"\xff\xd8not really a jpeg")
    with pytest.raises(ImageValidationError, match="couldn't read"):
        inspect_image(path, settings)


def test_inspect_rejects_too_many_pixels(image_factory, settings) -> None:
    path = image_factory(size=(40, 40), fmt="PNG")
    with pytest.raises(ImageValidationError, match="pixels"):
        inspect_image(path, replace(settings, max_image_pixels=100))


def test_inspect_rejects_oversized_file(image_factory, settings) -> None:
    path = image_factory(size=(40, 40), fmt="JPEG")
    with path.open("ab") as stream:
        stream.write(b"0" * (1024 * 1024 + 100))
    with pytest.raises(ImageValidationError, match="too large"):
        inspect_image(path, replace(settings, max_file_size_mb=1))


def test_images_to_pdf_combines_in_order(image_factory, settings, tmp_path) -> None:
    first = image_factory(size=(100, 50), fmt="JPEG")
    second = image_factory(size=(60, 30), fmt="PNG")
    output = tmp_path / "images.pdf"
    result = images_to_pdf([first, second], output, settings)
    assert result == output
    assert pdf_page_count(output) == 2
    widths = [round(width, 1) for width, _ in pdf_page_sizes(output)]
    assert widths == [48.0, 28.8]
    assert pdf_metadata(output).get("/Producer") == "Kayi PDF Bot"
    assert not list(tmp_path.glob("page_*.pdf"))


def test_images_to_pdf_embeds_jpeg_stream_losslessly(
    image_factory, settings, tmp_path
) -> None:
    path = image_factory(size=(100, 50), fmt="JPEG")
    output = tmp_path / "lossless.pdf"
    images_to_pdf([path], output, settings)
    assert path.read_bytes() in output.read_bytes()


def test_images_to_pdf_strips_jpeg_metadata(settings, tmp_path) -> None:
    path = tmp_path / "private.jpg"
    image = Image.new("RGB", (80, 40), (10, 20, 30))
    exif = image.getexif()
    exif[0x0112] = 1
    exif[0x010F] = "SecretCamera"
    image.save(path, "JPEG", exif=exif)
    image.close()

    output = tmp_path / "clean.pdf"
    images_to_pdf([path], output, settings)
    assert pdf_page_count(output) == 1
    assert b"SecretCamera" not in output.read_bytes()


def test_images_to_pdf_falls_back_when_embed_is_too_large(
    image_factory, settings, tmp_path, monkeypatch
) -> None:
    path = image_factory(size=(100, 50), fmt="JPEG")
    output = tmp_path / "fallback.pdf"

    def oversized(infos, target, token):
        target.write_bytes(b"not a pdf")
        return settings.max_output_size_bytes + 1

    monkeypatch.setattr(image_ops, "_embed_jpegs", oversized)
    images_to_pdf([path], output, settings)
    assert pdf_page_count(output) == 1
    assert output.stat().st_size <= settings.max_output_size_bytes


def test_images_to_pdf_falls_back_when_embed_fails(
    image_factory, settings, tmp_path, monkeypatch
) -> None:
    path = image_factory(size=(100, 50), fmt="JPEG")
    output = tmp_path / "fallback.pdf"

    def broken(infos, target, token):
        raise RuntimeError("unexpected encoder failure")

    monkeypatch.setattr(image_ops, "_embed_jpegs", broken)
    images_to_pdf([path], output, settings)
    assert pdf_page_count(output) == 1


def test_images_to_pdf_applies_exif_orientation(image_factory, settings, tmp_path) -> None:
    path = image_factory(
        size=(40, 20), fmt="JPEG", exif_orientation=6
    )
    output = tmp_path / "oriented.pdf"
    images_to_pdf([path], output, settings)
    width, height = pdf_page_sizes(output)[0]
    assert round(width, 2) == 9.6
    assert round(height, 2) == 19.2


def test_images_to_pdf_downscales_only_above_limit(
    image_factory, settings, tmp_path
) -> None:
    small = image_factory(size=(10, 20), fmt="PNG")
    large = image_factory(size=(100, 50), fmt="PNG")
    small_out = tmp_path / "small.pdf"
    large_out = tmp_path / "large.pdf"
    constrained = replace(settings, max_image_dimension=50)
    images_to_pdf([small], small_out, constrained)
    images_to_pdf([large], large_out, constrained)
    assert pdf_page_sizes(small_out)[0] == (4.8, 9.6)
    assert pdf_page_sizes(large_out)[0] == (24.0, 12.0)


def test_images_to_pdf_flattens_transparency(image_factory, settings, tmp_path) -> None:
    path = image_factory(
        size=(40, 30), mode="RGBA", color=(255, 0, 0, 128), fmt="PNG"
    )
    output = tmp_path / "transparent.pdf"
    images_to_pdf([path], output, settings)
    assert pdf_page_count(output) == 1


def test_images_to_pdf_rejects_too_many_images(image_factory, settings, tmp_path) -> None:
    path = image_factory(size=(10, 10), fmt="PNG")
    with pytest.raises(ImageValidationError, match="between 1"):
        images_to_pdf([path] * 11, tmp_path / "out.pdf", settings)


def test_images_to_pdf_cleans_up_after_bad_image(
    image_factory, settings, tmp_path
) -> None:
    good = image_factory(size=(10, 10), fmt="PNG")
    bad = tmp_path / "bad.png"
    bad.write_text("not an image", encoding="utf-8")
    output = tmp_path / "out.pdf"
    with pytest.raises(ImageValidationError):
        images_to_pdf([good, bad], output, settings)
    assert not output.exists()
    assert not list(tmp_path.glob("page_*.pdf"))


def test_images_to_pdf_cancellation(image_factory, settings, tmp_path) -> None:
    path = image_factory(size=(10, 10), fmt="PNG")
    token = CancelToken()
    token.cancel()
    output = tmp_path / "out.pdf"
    with pytest.raises(JobCancelled):
        images_to_pdf([path], output, settings, token)
    assert not output.exists()


def test_images_to_pdf_aggregate_input_limit(image_factory, settings, tmp_path) -> None:
    path = image_factory(size=(10, 10), fmt="JPEG")
    with path.open("ab") as stream:
        stream.write(b"0" * (1024 * 1024 + 100))
    with pytest.raises(ImageValidationError, match="add up"):
        images_to_pdf(
            [path],
            tmp_path / "out.pdf",
            replace(settings, max_total_input_mb=1),
        )
