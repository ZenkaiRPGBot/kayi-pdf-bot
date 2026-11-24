"""Page selection and split-range parsing."""

from __future__ import annotations

import pytest

from app.services.errors import PageRangeError
from app.services.page_ranges import (
    format_page_selection,
    parse_page_selection,
    parse_split_ranges,
)


def test_parse_simple_selection() -> None:
    assert parse_page_selection("1,3,5-8", 10) == [1, 3, 5, 6, 7, 8]


def test_parse_preserves_requested_order() -> None:
    assert parse_page_selection("8,1,3-4", 10) == [8, 1, 3, 4]


@pytest.mark.parametrize("dash", ["\u2013", "\u2014", "\u2212"])
def test_parse_unicode_dashes(dash: str) -> None:
    assert parse_page_selection(f"2{dash}4", 5) == [2, 3, 4]


def test_parse_arabic_comma() -> None:
    assert parse_page_selection("1\u060c3", 5) == [1, 3]


def test_parse_whitespace_and_single_page() -> None:
    assert parse_page_selection(" 1 - 3 , 5 ", 5) == [1, 2, 3, 5]
    assert parse_page_selection("2", 2) == [2]


def test_duplicate_pages_rejected_by_default() -> None:
    with pytest.raises(PageRangeError, match="repeated"):
        parse_page_selection("1,2,1", 5)


def test_duplicates_allowed_when_requested() -> None:
    assert parse_page_selection("1,2,1", 5, allow_duplicates=True) == [1, 2, 1]


def test_out_of_range_page() -> None:
    with pytest.raises(PageRangeError, match="out of range"):
        parse_page_selection("6", 5)
    with pytest.raises(PageRangeError, match="out of range"):
        parse_page_selection("4-9", 5)


def test_page_zero_rejected() -> None:
    with pytest.raises(PageRangeError, match="start at 1"):
        parse_page_selection("0-2", 5)


def test_backwards_range_rejected() -> None:
    with pytest.raises(PageRangeError, match="backwards"):
        parse_page_selection("5-2", 10)


@pytest.mark.parametrize("text", ["", "   ", ",", "1,,2", "a", "1-", "-3", "1.5"])
def test_malformed_selections(text: str) -> None:
    with pytest.raises(PageRangeError):
        parse_page_selection(text, 5)


def test_max_pages_enforced() -> None:
    with pytest.raises(PageRangeError, match="no more than"):
        parse_page_selection("1-5", 10, max_pages=4)
    assert parse_page_selection("1-4", 10, max_pages=4) == [1, 2, 3, 4]


def test_selection_too_long() -> None:
    with pytest.raises(PageRangeError, match="too long"):
        parse_page_selection("1," * 1500, 5000)


def test_page_count_must_be_positive() -> None:
    with pytest.raises(PageRangeError):
        parse_page_selection("1", 0)


def test_split_ranges_create_one_output_per_item() -> None:
    outputs = parse_split_ranges("1-2,4,6-7", 10, max_outputs=3, max_total_pages=10)
    assert outputs == [[1, 2], [4], [6, 7]]


def test_split_ranges_allow_overlap() -> None:
    outputs = parse_split_ranges("1-3,2-4", 5, max_outputs=2, max_total_pages=10)
    assert outputs == [[1, 2, 3], [2, 3, 4]]


def test_split_range_limits() -> None:
    with pytest.raises(PageRangeError, match="no more than"):
        parse_split_ranges("1,2,3", 5, max_outputs=2, max_total_pages=10)
    with pytest.raises(PageRangeError, match="more than"):
        parse_split_ranges("1-3,4-6", 6, max_outputs=2, max_total_pages=5)
    with pytest.raises(PageRangeError, match="out of range"):
        parse_split_ranges("1-6", 5, max_outputs=2, max_total_pages=10)


def test_format_page_selection() -> None:
    assert format_page_selection([]) == "none"
    assert format_page_selection([1]) == "1"
    assert format_page_selection([1, 2, 3, 5, 8, 9]) == "1-3, 5, 8-9"
    assert format_page_selection((4, 5, 6)) == "4-6"
