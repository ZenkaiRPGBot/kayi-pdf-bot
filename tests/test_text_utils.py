"""Dependency-free utility helpers."""

from __future__ import annotations

import pytest

from app.utils.text import (
    ceil_div,
    clamp,
    escape,
    filename_stem,
    format_bytes,
    format_pages,
    output_pdf_name,
    paginate_text,
    parse_pdf_filename,
    safe_int,
    sanitize_filename,
    sanitize_pdf_filename,
    truncate,
)


def test_escape_for_html_parse_mode() -> None:
    assert escape("<b>&x</b>") == "&lt;b&gt;&amp;x&lt;/b&gt;"
    assert escape(None) == ""
    assert escape('a "quote"') == 'a "quote"'


def test_truncate() -> None:
    assert truncate("hello", 10) == "hello"
    assert truncate("hello", 1) == "…"
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abcdef", 0) == ""


def test_format_bytes() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(1000) == "1000 B"
    assert format_bytes(2048) == "2 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_format_pages() -> None:
    assert format_pages([]) == ""
    assert format_pages([1]) == "1"
    assert format_pages([1, 2, 3, 5, 7, 8]) == "1-3, 5, 7-8"


@pytest.mark.parametrize(
    "raw",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\cmd.exe",
        "bad<name>|?.pdf",
        "  .hidden.pdf  ",
    ],
)
def test_sanitize_filename_removes_paths_and_unsafe_characters(raw: str) -> None:
    safe = sanitize_filename(raw, "fallback.pdf")
    assert "/" not in safe
    assert "\\" not in safe
    assert "<" not in safe
    assert ">" not in safe
    assert "|" not in safe
    assert "?" not in safe
    assert safe
    assert not safe.startswith(".")


def test_sanitize_filename_limits_stem() -> None:
    safe = sanitize_filename("a" * 200 + ".pdf")
    assert safe.endswith(".pdf")
    assert len(safe) <= 85


def test_sanitize_filename_defaults() -> None:
    assert sanitize_filename(None) == "document.pdf"
    assert sanitize_filename("", "x.pdf") == "x.pdf"
    assert sanitize_filename("no_extension") == "no_extension"


def test_filename_stem_and_output_name() -> None:
    assert filename_stem("Report 2026.pdf") == "Report 2026"
    assert filename_stem(None, "merged") == "merged"
    assert output_pdf_name("Report.pdf", "rotated") == "Report_rotated.pdf"


def test_parse_pdf_filename_normalises_and_keeps_unicode() -> None:
    assert parse_pdf_filename("Report") == "Report.pdf"
    assert parse_pdf_filename("Report.PDF") == "Report.pdf"
    assert parse_pdf_filename("Report.pdf.pdf") == "Report.pdf"
    assert parse_pdf_filename("  Résumé 2026  ") == "Résumé 2026.pdf"
    assert parse_pdf_filename("a<b>c|d?.pdf") == "abcd.pdf"
    assert parse_pdf_filename("../../etc/passwd") == "passwd.pdf"
    assert parse_pdf_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe.pdf"
    assert parse_pdf_filename("a" * 200) == "a" * 80 + ".pdf"


def test_parse_pdf_filename_rejects_empty_or_invalid_names() -> None:
    for raw in (None, "", "   ", ".pdf", "..", "/", "\\", "///", "\x00"):
        assert parse_pdf_filename(raw) is None
    assert sanitize_pdf_filename(None) == "Images.pdf"
    assert sanitize_pdf_filename("", "Fallback") == "Fallback.pdf"
    assert sanitize_pdf_filename("Valid") == "Valid.pdf"


def test_paginate_text_respects_limit() -> None:
    short = paginate_text(["a", "b"], limit=100)
    assert short == "a\nb"
    long = paginate_text([f"line {index} " + "x" * 20 for index in range(100)], limit=200)
    assert len(long) <= 201
    assert long.endswith("…")


def test_small_math_helpers() -> None:
    assert clamp(5, 0, 3) == 3
    assert clamp(-1, 0, 3) == 0
    assert safe_int("42") == 42
    assert safe_int("nope", 7) == 7
    assert safe_int(None, 7) == 7
    assert ceil_div(5, 2) == 3
    assert ceil_div(5, 0) == 0
