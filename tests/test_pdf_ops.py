"""PDF validation, merge, split, extract, and rotate operations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.services.cancellation import CancelToken
from app.services.errors import (
    JobCancelled,
    OutputLimitError,
    PageRangeError,
    PdfEncryptedError,
    PdfValidationError,
)
from app.services.pdf_ops import (
    _check_output,
    extract_pages,
    inspect_pdf,
    merge_pdfs,
    rotate_pdf,
    split_pdf,
)
from tests.helpers import (
    pdf_metadata,
    pdf_page_count,
    pdf_page_rotations,
    pdf_page_sizes,
)


def test_inspect_pdf_reports_pages_and_size(pdf_factory, settings) -> None:
    path = pdf_factory(page_count=4, size=(100.0, 200.0))
    info = inspect_pdf(path, settings)
    assert info.page_count == 4
    assert info.size == path.stat().st_size
    assert info.path == path


def test_inspect_pdf_rejects_non_pdf(tmp_path: Path, settings) -> None:
    path = tmp_path / "not.pdf"
    path.write_bytes(b"this is definitely not a PDF")
    with pytest.raises(PdfValidationError):
        inspect_pdf(path, settings)


def test_inspect_pdf_rejects_empty_file(tmp_path: Path, settings) -> None:
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    with pytest.raises(PdfValidationError):
        inspect_pdf(path, settings)


def test_inspect_pdf_accepts_header_after_leading_bytes(
    tmp_path: Path, pdf_factory, settings
) -> None:
    source = pdf_factory(page_count=1)
    path = tmp_path / "offset.pdf"
    path.write_bytes(b"JUNK" * 20 + source.read_bytes())
    assert inspect_pdf(path, settings).page_count == 1


def test_inspect_pdf_rejects_encrypted(pdf_factory, settings) -> None:
    path = pdf_factory(page_count=1)
    writer = PdfWriter(clone_from=str(path))
    writer.encrypt("secret")
    encrypted = path.with_name("encrypted.pdf")
    with encrypted.open("wb") as stream:
        writer.write(stream)
    writer.close()
    with pytest.raises(PdfEncryptedError):
        inspect_pdf(encrypted, settings)


def test_inspect_pdf_rejects_too_many_pages(pdf_factory, settings) -> None:
    path = pdf_factory(page_count=3)
    with pytest.raises(PdfValidationError, match="limit"):
        inspect_pdf(path, replace(settings, max_pages=2))


def test_inspect_pdf_rejects_oversized_file(pdf_factory, settings) -> None:
    path = pdf_factory(page_count=1)
    with path.open("ab") as stream:
        stream.write(b"0" * (1024 * 1024 + 100))
    with pytest.raises(PdfValidationError, match="too large"):
        inspect_pdf(path, replace(settings, max_file_size_mb=1))


def test_merge_preserves_order_and_metadata(pdf_factory, settings, tmp_path) -> None:
    first = pdf_factory(page_count=2, size=(100.0, 200.0))
    second = pdf_factory(page_count=3, size=(300.0, 400.0))
    output = tmp_path / "merged.pdf"
    result = merge_pdfs([first, second], output, settings)
    assert result == output
    assert pdf_page_count(output) == 5
    assert pdf_page_sizes(output) == [
        (100.0, 200.0),
        (101.0, 201.0),
        (300.0, 400.0),
        (301.0, 401.0),
        (302.0, 402.0),
    ]
    metadata = pdf_metadata(output)
    assert metadata.get("/Producer") == "Kayi PDF Bot"
    assert metadata.get("/Creator") == "Kayi PDF Bot"


def test_merge_requires_two_files(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=1)
    with pytest.raises(PdfValidationError, match="between 2"):
        merge_pdfs([source], tmp_path / "out.pdf", settings)


def test_merge_rejects_duplicate_input(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=2)
    with pytest.raises(PdfValidationError, match="more than once"):
        merge_pdfs([source, source], tmp_path / "out.pdf", settings)


def test_merge_enforces_total_page_limit(pdf_factory, settings, tmp_path) -> None:
    first = pdf_factory(page_count=3)
    second = pdf_factory(page_count=3)
    with pytest.raises(PdfValidationError, match="total"):
        merge_pdfs(
            [first, second],
            tmp_path / "out.pdf",
            replace(settings, max_total_pages_per_job=5),
        )


def test_merge_enforces_total_input_size(pdf_factory, settings, tmp_path) -> None:
    first = pdf_factory(page_count=1)
    second = pdf_factory(page_count=1)
    for path in (first, second):
        with path.open("ab") as stream:
            stream.write(b"0" * 600_000)
    with pytest.raises(PdfValidationError, match="add up"):
        merge_pdfs(
            [first, second],
            tmp_path / "out.pdf",
            replace(settings, max_total_input_mb=1),
        )


def test_merge_cancellation_leaves_no_output(pdf_factory, settings, tmp_path) -> None:
    first = pdf_factory(page_count=2)
    second = pdf_factory(page_count=2)
    token = CancelToken()
    token.cancel()
    output = tmp_path / "out.pdf"
    with pytest.raises(JobCancelled):
        merge_pdfs([first, second], output, settings, token)
    assert not output.exists()


def test_split_creates_separate_documents(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=5)
    output_dir = tmp_path / "split"
    outputs = split_pdf(source, [[1], [2, 3], [4, 5]], output_dir, settings)
    assert [path.name for path in outputs] == [
        "part_01.pdf",
        "part_02.pdf",
        "part_03.pdf",
    ]
    assert [pdf_page_count(path) for path in outputs] == [1, 2, 2]
    assert pdf_page_sizes(outputs[1]) == [(201.0, 301.0), (202.0, 302.0)]
    assert pdf_metadata(outputs[0]).get("/Producer") == "Kayi PDF Bot"


def test_split_allows_overlapping_ranges(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=4)
    outputs = split_pdf(source, [[1, 2], [2, 3]], tmp_path / "split", settings)
    assert [pdf_page_count(path) for path in outputs] == [2, 2]


def test_split_enforces_limits(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=6)
    with pytest.raises(PageRangeError, match="no more than"):
        split_pdf(
            source,
            [[1], [2], [3]],
            tmp_path / "split",
            replace(settings, max_split_outputs=2, split_individual_max=2),
        )
    with pytest.raises(PageRangeError, match="in total"):
        split_pdf(
            source,
            [[1, 2, 3], [4, 5, 6]],
            tmp_path / "split",
            replace(settings, max_split_total_pages=5),
        )
    with pytest.raises(PageRangeError, match="out of bounds"):
        split_pdf(source, [[7]], tmp_path / "split", settings)
    with pytest.raises(PageRangeError, match="at least one"):
        split_pdf(source, [], tmp_path / "split", settings)


def test_extract_preserves_requested_order(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=4, size=(100.0, 200.0))
    output = tmp_path / "extracted.pdf"
    extract_pages(source, [3, 1, 2], output, settings)
    assert pdf_page_count(output) == 3
    assert pdf_page_sizes(output) == [
        (102.0, 202.0),
        (100.0, 200.0),
        (101.0, 201.0),
    ]


def test_extract_validation(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=3)
    with pytest.raises(PageRangeError, match="at least one"):
        extract_pages(source, [], tmp_path / "out.pdf", settings)
    with pytest.raises(PageRangeError, match="repeated"):
        extract_pages(source, [1, 1], tmp_path / "out.pdf", settings)
    with pytest.raises(PageRangeError, match="out of range"):
        extract_pages(source, [4], tmp_path / "out.pdf", settings)


def test_extract_cancellation(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=3)
    token = CancelToken()
    token.cancel()
    output = tmp_path / "out.pdf"
    with pytest.raises(JobCancelled):
        extract_pages(source, [1, 2], output, settings, token)
    assert not output.exists()


def test_rotate_all_pages(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=3)
    output = tmp_path / "rotated.pdf"
    rotate_pdf(source, None, 90, output, settings)
    assert pdf_page_rotations(output) == [90, 90, 90]
    assert pdf_metadata(output).get("/Producer") == "Kayi PDF Bot"


def test_rotate_selected_pages(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=4)
    output = tmp_path / "rotated.pdf"
    rotate_pdf(source, [2, 4], 180, output, settings)
    assert pdf_page_rotations(output) == [0, 180, 0, 180]


def test_rotate_validation(pdf_factory, settings, tmp_path) -> None:
    source = pdf_factory(page_count=3)
    with pytest.raises(PdfValidationError, match="90, 180"):
        rotate_pdf(source, None, 45, tmp_path / "out.pdf", settings)
    with pytest.raises(PageRangeError, match="at least one"):
        rotate_pdf(source, [], 90, tmp_path / "out.pdf", settings)
    with pytest.raises(PageRangeError, match="repeated"):
        rotate_pdf(source, [1, 1], 90, tmp_path / "out.pdf", settings)
    with pytest.raises(PageRangeError, match="out of range"):
        rotate_pdf(source, [5], 90, tmp_path / "out.pdf", settings)


def test_output_limit_removes_oversized_result(tmp_path: Path, settings) -> None:
    path = tmp_path / "large.pdf"
    path.write_bytes(b"x" * (1024 * 1024 + 10))
    with pytest.raises(OutputLimitError):
        _check_output(path, replace(settings, max_output_size_mb=1))
    assert not path.exists()


def test_aggregate_output_limit(tmp_path: Path, settings) -> None:
    path = tmp_path / "small.pdf"
    path.write_bytes(b"x" * 100)
    with pytest.raises(OutputLimitError, match="combined"):
        _check_output(path, settings, total=settings.max_total_output_bytes + 1)
    assert not path.exists()
