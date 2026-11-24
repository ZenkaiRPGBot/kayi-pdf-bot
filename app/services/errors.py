"""Application-level exceptions with user-safe messages."""

from __future__ import annotations


class KayiPdfError(Exception):
    """Base class for expected failures."""

    user_message = "Something went wrong. Please try again."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.user_message)
        if message:
            self.user_message = message


class FileValidationError(KayiPdfError):
    user_message = "This file could not be used. Please try another one."


class PdfValidationError(FileValidationError):
    user_message = "I couldn't read this PDF. It may be damaged or unsupported."


class PdfEncryptedError(PdfValidationError):
    user_message = (
        "This PDF is password-protected. Remove the password and send it again."
    )


class ImageValidationError(FileValidationError):
    user_message = "This image could not be used. Send a JPG or PNG image."


class PageRangeError(KayiPdfError):
    user_message = "Those page numbers are not valid for this PDF."


class OutputLimitError(KayiPdfError):
    user_message = "The result is too large to send. Try a smaller file or fewer pages."


class JobCapacityError(KayiPdfError):
    user_message = "The bot is at capacity right now. Please try again shortly."


class JobAlreadyActiveError(KayiPdfError):
    user_message = "You already have a task in progress. Wait for it to finish or use /cancel."


class JobCancelled(KayiPdfError):
    user_message = "Task cancelled."


class ProcessingTimeout(KayiPdfError):
    user_message = "This took too long to process. Try a smaller file or fewer pages."

    def __init__(self, message: str | None = None, *, cleanup_deferred: bool = False) -> None:
        super().__init__(message)
        self.cleanup_deferred = cleanup_deferred


class DownloadError(KayiPdfError):
    user_message = (
        "I couldn't download that file from Telegram. Please send it again."
    )


class UploadError(KayiPdfError):
    user_message = "I couldn't send the result back. Please try again."


class TelegramUnavailableError(KayiPdfError):
    user_message = "Telegram is not responding right now. Please try again."


class WorkspaceCapacityError(JobCapacityError):
    user_message = (
        "The bot is handling as many files as it can right now. "
        "Please try again in a few minutes."
    )
