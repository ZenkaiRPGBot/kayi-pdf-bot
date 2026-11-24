"""Test helpers."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader


def pdf_page_sizes(path: Path) -> list[tuple[float, float]]:
    reader = PdfReader(str(path))
    try:
        return [
            (float(page.mediabox.width), float(page.mediabox.height))
            for page in reader.pages
        ]
    finally:
        reader.stream.close()


def pdf_page_rotations(path: Path) -> list[int]:
    reader = PdfReader(str(path))
    try:
        return [int(page.get("/Rotate", 0) or 0) % 360 for page in reader.pages]
    finally:
        reader.stream.close()


def pdf_metadata(path: Path) -> dict:
    reader = PdfReader(str(path))
    try:
        return dict(reader.metadata or {})
    finally:
        reader.stream.close()


def pdf_page_count(path: Path) -> int:
    reader = PdfReader(str(path))
    try:
        return len(reader.pages)
    finally:
        reader.stream.close()
