"""Page-range parsing with strict bounds checking.

Rules:
* ``1,3,5-8`` selects individual pages/ranges and preserves the requested
  order for extraction.
* For splitting, every comma-separated item is its own output document.
* Duplicate pages are rejected for extraction and rotation.  Splitting may
  use the same page in two different output documents, which is explicit and
  predictable.
"""

from __future__ import annotations

import re

from app.services.errors import PageRangeError

_TOKEN_RE = re.compile(r"^(\d{1,9})(?:\s*-\s*(\d{1,9}))?$")
_MAX_INPUT_CHARS = 2000


def _normalise(text: str) -> str:
    return (
        (text or "")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
        .replace("\u060c", ",")
        .strip()
    )


def _split_tokens(text: str) -> list[str]:
    normalised = _normalise(text)
    if not normalised:
        raise PageRangeError(
            "Send page numbers first, for example <code>1-3,5</code>."
        )
    if len(normalised) > _MAX_INPUT_CHARS:
        raise PageRangeError("That page selection is too long. Please shorten it.")
    parts = [part.strip() for part in normalised.split(",")]
    if any(not part for part in parts):
        raise PageRangeError(
            "That page selection has an empty part. Use a format like "
            "<code>1-3,5</code>."
        )
    return parts


def _parse_token(
    token: str, page_count: int, *, example: str
) -> tuple[int, int]:
    match = _TOKEN_RE.fullmatch(token)
    if not match:
        raise PageRangeError(
            "Those page numbers are not valid. Use numbers and ranges like "
            f"<code>{example}</code>."
        )
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) is not None else start
    if start < 1 or end < 1:
        raise PageRangeError("Page numbers start at 1.")
    if start > end:
        raise PageRangeError(
            f"The range <code>{start}-{end}</code> is backwards. "
            "Put the smaller page number first."
        )
    if end > page_count:
        raise PageRangeError(
            f"This PDF has <b>{page_count}</b> page"
            f"{'s' if page_count != 1 else ''}, so page {end} is out of range."
        )
    return start, end


def parse_page_selection(
    text: str,
    page_count: int,
    *,
    max_pages: int | None = None,
    allow_duplicates: bool = False,
    example: str = "1-3,5",
) -> list[int]:
    """Expand a page selection while preserving the user's order."""

    if page_count < 1:
        raise PageRangeError("This PDF does not contain any pages.")
    parts = _split_tokens(text)
    pages: list[int] = []
    seen: set[int] = set()
    for token in parts:
        start, end = _parse_token(token, page_count, example=example)
        count = end - start + 1
        if max_pages is not None and len(pages) + count > max_pages:
            raise PageRangeError(
                f"Please select no more than <b>{max_pages}</b> pages at a time."
            )
        for page in range(start, end + 1):
            if not allow_duplicates and page in seen:
                raise PageRangeError(
                    f"Page {page} is repeated. Remove duplicates and try again."
                )
            seen.add(page)
            pages.append(page)
    if not pages:
        raise PageRangeError(
            f"No pages were selected. Try something like <code>{example}</code>."
        )
    return pages


def parse_split_ranges(
    text: str,
    page_count: int,
    *,
    max_outputs: int,
    max_total_pages: int,
    example: str = "1-3,4-6,7-10",
) -> list[list[int]]:
    """Parse comma-separated ranges, one output document per item."""

    if page_count < 1:
        raise PageRangeError("This PDF does not contain any pages.")
    parts = _split_tokens(text)
    if len(parts) > max_outputs:
        raise PageRangeError(
            f"Please create no more than <b>{max_outputs}</b> documents at a time."
        )
    outputs: list[list[int]] = []
    total = 0
    for token in parts:
        start, end = _parse_token(token, page_count, example=example)
        count = end - start + 1
        total += count
        if total > max_total_pages:
            raise PageRangeError(
                f"That would produce more than <b>{max_total_pages}</b> pages "
                "in total. Use fewer or shorter ranges."
            )
        outputs.append(list(range(start, end + 1)))
    if not outputs:
        raise PageRangeError(
            f"No ranges were found. Try something like <code>{example}</code>."
        )
    return outputs


def format_page_selection(pages: list[int] | tuple[int, ...]) -> str:
    """Human-friendly compact form used in confirmations and help text."""

    if not pages:
        return "none"
    ordered = list(pages)
    parts: list[str] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        parts.append(f"{start}" if start == previous else f"{start}-{previous}")
        start = previous = page
    parts.append(f"{start}" if start == previous else f"{start}-{previous}")
    return ", ".join(parts)
