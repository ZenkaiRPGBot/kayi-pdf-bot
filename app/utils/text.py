"""Small, dependency-free helpers used across the application."""

from __future__ import annotations

import html
import math
import re
from pathlib import Path

_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")


def escape(value: str | None) -> str:
    """Escape text for Telegram HTML parse mode."""

    return html.escape(value or "", quote=False)


def truncate(value: str, limit: int = 60, suffix: str = "…") -> str:
    if limit < 1:
        return ""
    if len(value) <= limit:
        return value
    if limit == 1:
        return suffix[:1]
    return value[: limit - len(suffix)] + suffix


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_pages(pages: list[int] | tuple[int, ...]) -> str:
    """Compact a sorted/ordered page list, e.g. [1, 2, 3, 5] -> '1-3, 5'."""

    if not pages:
        return ""
    ordered = list(pages)
    parts: list[str] = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page == prev + 1:
            prev = page
            continue
        parts.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = page
    parts.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(parts)


def sanitize_filename(name: str | None, default: str = "document.pdf") -> str:
    """Return a safe display filename, never a path.

    Only the basename is retained.  The value is used as a Telegram upload
    name, never as a filesystem path.
    """

    raw = (name or "").replace("\\", "/").split("/")[-1]
    raw = _UNSAFE_FILENAME.sub("", raw)
    raw = _WHITESPACE.sub(" ", raw).strip(" .")
    if not raw:
        raw = default
    stem, dot, suffix = raw.rpartition(".")
    if not dot:
        stem, suffix = raw, ""
    stem = stem[:80].strip(" .") or "document"
    suffix = re.sub(r"[^A-Za-z0-9]", "", suffix)[:10]
    return f"{stem}.{suffix}" if suffix else stem


def filename_stem(name: str | None, default: str = "document") -> str:
    safe = sanitize_filename(name, f"{default}.pdf")
    stem = Path(safe).stem.strip(" .")
    return stem[:60] or default


def output_pdf_name(source_name: str | None, suffix: str, default: str = "document") -> str:
    stem = filename_stem(source_name, default)
    return f"{stem}_{suffix}.pdf"


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def paginate_text(lines: list[str], limit: int = 3800) -> str:
    """Join lines, cutting at the last newline if Telegram's limit is exceeded."""

    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > 0 else limit].rstrip() + "…"


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def ceil_div(a: int, b: int) -> int:
    return math.ceil(a / b) if b else 0
