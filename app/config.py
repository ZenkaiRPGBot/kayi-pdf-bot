"""Environment-driven configuration for Kayi PDF Bot.

No secret is ever given a meaningful repr, and no value is read from a file
that is not explicitly configured.  The module keeps configuration parsing
separate from the Telegram runtime so that services and tests can build a
``Settings`` instance without creating a bot.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "kayi-pdf-bot"
APP_VERSION = "1.0.0"
BRAND_NAME = "Kayi PDF"

_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class ConfigError(ValueError):
    """Raised when an environment value cannot be used safely."""


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _env(name)
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _env(name)
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if raw == "":
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _normalise_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        raise ConfigError("PUBLIC_BASE_URL must start with http:// or https://")
    if any(ch.isspace() for ch in value):
        raise ConfigError("PUBLIC_BASE_URL must not contain whitespace")
    return value


def _normalise_channel(value: str) -> str:
    value = value.strip()
    if not value:
        raise ConfigError("FSUB_CHANNEL must not be empty")
    if value.lstrip("-").isdigit():
        return value
    return value if value.startswith("@") else f"@{value}"


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings."""

    bot_token: str = field(default="", repr=False)
    owner_id: int = 0
    log_channel_id: str = ""
    public_base_url: str = ""
    webhook_secret: str = field(default="", repr=False)
    webhook_path: str = "/telegram/webhook"
    webhook_auto_set: bool = True
    drop_pending_updates: bool = False
    webhook_queue_size: int = 100
    max_concurrent_updates: int = 16
    update_dedup_ttl: float = 600.0
    update_dedup_max: int = 2000

    fsub_channel: str = "@KayiBots"
    fsub_invite_url: str = "https://t.me/KayiBots"
    fsub_cache_ttl: float = 45.0
    fsub_cache_max: int = 10_000
    pending_action_ttl: float = 900.0
    pending_action_max: int = 10_000

    welcome_video_file_id: str = field(default="", repr=False)

    max_file_size_mb: int = 20
    max_files_merge: int = 10
    max_images: int = 10
    max_pages: int = 200
    max_total_pages_per_job: int = 500
    max_active_jobs: int = 1
    max_queued_jobs: int = 2
    max_total_input_mb: int = 60
    processing_timeout: float = 60.0
    max_output_size_mb: int = 45
    max_total_output_mb: int = 60
    max_split_outputs: int = 10
    max_split_total_pages: int = 500
    split_individual_max: int = 3
    max_zip_entries: int = 10
    max_image_pixels: int = 25_000_000
    max_image_dimension: int = 6000

    max_workspaces: int = 20
    stale_workspace_ttl: float = 7200.0
    workspace_idle_ttl: float = 900.0
    workspace_cleanup_interval: float = 600.0

    telegram_request_timeout: float = 60.0
    download_timeout: float = 60.0

    enable_self_ping: bool = False
    ping_url: str = ""
    self_ping_initial_delay: float = 15.0
    self_ping_interval: float = 600.0
    log_level: str = "INFO"

    temp_root: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / APP_NAME
    )

    def __post_init__(self) -> None:
        if self.webhook_path and not self.webhook_path.startswith("/"):
            raise ConfigError("WEBHOOK_PATH must start with /")
        if self.webhook_secret and not _SECRET_RE.match(self.webhook_secret):
            raise ConfigError(
                "WEBHOOK_SECRET may contain only letters, digits, _ and - (max 256)"
            )
        if self.public_base_url:
            object.__setattr__(
                self, "public_base_url", _normalise_base_url(self.public_base_url)
            )
        if self.fsub_invite_url:
            if not re.match(r"^https?://", self.fsub_invite_url):
                raise ConfigError("FSUB_INVITE_URL must start with http:// or https://")

        self._require_at_least("owner_id", self.owner_id, 0)
        self._require_at_least("max_file_size_mb", self.max_file_size_mb, 1)
        self._require_range("max_files_merge", self.max_files_merge, 2, 10)
        self._require_range("max_images", self.max_images, 1, 10)
        self._require_at_least("max_pages", self.max_pages, 1)
        self._require_at_least(
            "max_total_pages_per_job", self.max_total_pages_per_job, 1
        )
        self._require_at_least("max_active_jobs", self.max_active_jobs, 1)
        self._require_at_least("max_queued_jobs", self.max_queued_jobs, 0)
        self._require_at_least("max_total_input_mb", self.max_total_input_mb, 1)
        self._require_at_least("processing_timeout", self.processing_timeout, 1)
        self._require_at_least("max_output_size_mb", self.max_output_size_mb, 1)
        self._require_at_least("max_total_output_mb", self.max_total_output_mb, 1)
        self._require_range("max_split_outputs", self.max_split_outputs, 1, 50)
        self._require_at_least("max_split_total_pages", self.max_split_total_pages, 1)
        self._require_range(
            "split_individual_max", self.split_individual_max, 1, self.max_split_outputs
        )
        self._require_range("max_zip_entries", self.max_zip_entries, 1, 50)
        self._require_at_least("max_image_pixels", self.max_image_pixels, 1)
        self._require_at_least("max_image_dimension", self.max_image_dimension, 1)
        self._require_at_least("max_workspaces", self.max_workspaces, 1)
        self._require_at_least("stale_workspace_ttl", self.stale_workspace_ttl, 1)
        self._require_at_least("workspace_idle_ttl", self.workspace_idle_ttl, 1)
        self._require_at_least(
            "workspace_cleanup_interval", self.workspace_cleanup_interval, 1
        )
        self._require_at_least(
            "telegram_request_timeout", self.telegram_request_timeout, 1
        )
        self._require_at_least("download_timeout", self.download_timeout, 1)
        self._require_at_least("webhook_queue_size", self.webhook_queue_size, 1)
        self._require_at_least(
            "max_concurrent_updates", self.max_concurrent_updates, 1
        )
        self._require_at_least("update_dedup_ttl", self.update_dedup_ttl, 1)
        self._require_at_least("update_dedup_max", self.update_dedup_max, 1)
        self._require_at_least("fsub_cache_ttl", self.fsub_cache_ttl, 1)
        self._require_at_least("fsub_cache_max", self.fsub_cache_max, 1)
        self._require_at_least("pending_action_ttl", self.pending_action_ttl, 1)
        self._require_at_least("pending_action_max", self.pending_action_max, 1)
        self._require_at_least(
            "self_ping_initial_delay", self.self_ping_initial_delay, 0
        )
        self._require_at_least("self_ping_interval", self.self_ping_interval, 1)

    @staticmethod
    def _require_at_least(name: str, value: int | float, minimum: int | float) -> None:
        if value < minimum:
            raise ConfigError(f"{name.upper()} must be at least {minimum}")

    @staticmethod
    def _require_range(
        name: str, value: int, minimum: int, maximum: int
    ) -> None:
        if not minimum <= value <= maximum:
            raise ConfigError(
                f"{name.upper()} must be between {minimum} and {maximum}"
            )

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def max_total_input_bytes(self) -> int:
        return self.max_total_input_mb * 1024 * 1024

    @property
    def max_output_size_bytes(self) -> int:
        return self.max_output_size_mb * 1024 * 1024

    @property
    def max_total_output_bytes(self) -> int:
        return self.max_total_output_mb * 1024 * 1024

    @property
    def webhook_url(self) -> str:
        if not self.public_base_url:
            return ""
        return f"{self.public_base_url}{self.webhook_path}"

    @property
    def webhook_configured(self) -> bool:
        return bool(self.public_base_url and self.webhook_secret)

    @property
    def fsub_channel_id(self) -> str:
        return self.fsub_channel


def load_settings(*, require_token: bool = True) -> Settings:
    """Load settings from the process environment.

    A webhook secret is generated when one is not supplied.  The generated
    value stays in memory and is used for the webhook registration, so the
    endpoint is never left unauthenticated.
    """

    token = _env("BOT_TOKEN")
    if require_token and not token:
        raise ConfigError("BOT_TOKEN is required")

    provided_secret = _env("WEBHOOK_SECRET")
    secret = provided_secret or secrets.token_urlsafe(32)
    if provided_secret and not _SECRET_RE.match(provided_secret):
        raise ConfigError(
            "WEBHOOK_SECRET may contain only letters, digits, _ and - (max 256)"
        )

    settings = Settings(
        bot_token=token,
        owner_id=_env_int("OWNER_ID", 0, minimum=0),
        log_channel_id=_env("LOG_CHANNEL_ID"),
        # Render injects RENDER_EXTERNAL_URL automatically.  Using it keeps
        # webhook registration working without a second manual URL.
        public_base_url=_normalise_base_url(
            _env("PUBLIC_BASE_URL") or _env("RENDER_EXTERNAL_URL")
        ),
        webhook_secret=secret,
        webhook_path=_env("WEBHOOK_PATH", "/telegram/webhook") or "/telegram/webhook",
        webhook_auto_set=_env_bool("WEBHOOK_AUTO_SET", True),
        drop_pending_updates=_env_bool("DROP_PENDING_UPDATES", False),
        webhook_queue_size=_env_int("WEBHOOK_QUEUE_SIZE", 100, minimum=1),
        max_concurrent_updates=_env_int("MAX_CONCURRENT_UPDATES", 16, minimum=1),
        update_dedup_ttl=_env_float("UPDATE_DEDUP_TTL", 600.0, minimum=1),
        update_dedup_max=_env_int("UPDATE_DEDUP_MAX", 2000, minimum=1),
        fsub_channel=_normalise_channel(_env("FSUB_CHANNEL", "@KayiBots")),
        fsub_invite_url=_env("FSUB_INVITE_URL") or "https://t.me/KayiBots",
        fsub_cache_ttl=_env_float("FSUB_CACHE_TTL", 45.0, minimum=1),
        fsub_cache_max=_env_int("FSUB_CACHE_MAX", 10_000, minimum=1),
        pending_action_ttl=_env_float("PENDING_ACTION_TTL", 900.0, minimum=1),
        pending_action_max=_env_int("PENDING_ACTION_MAX", 10_000, minimum=1),
        welcome_video_file_id=_env("WELCOME_VIDEO_FILE_ID"),
        max_file_size_mb=_env_int("MAX_FILE_SIZE_MB", 20, minimum=1),
        max_files_merge=_env_int("MAX_FILES_MERGE", 10, minimum=2),
        max_images=_env_int("MAX_IMAGES", 10, minimum=1),
        max_pages=_env_int("MAX_PAGES", 200, minimum=1),
        max_total_pages_per_job=_env_int(
            "MAX_TOTAL_PAGES_PER_JOB", 500, minimum=1
        ),
        max_active_jobs=_env_int("MAX_ACTIVE_JOBS", 1, minimum=1),
        max_queued_jobs=_env_int("MAX_QUEUED_JOBS", 2, minimum=0),
        max_total_input_mb=_env_int("MAX_TOTAL_INPUT_MB", 60, minimum=1),
        processing_timeout=_env_float("PROCESSING_TIMEOUT", 60.0, minimum=1),
        max_output_size_mb=_env_int("MAX_OUTPUT_SIZE_MB", 45, minimum=1),
        max_total_output_mb=_env_int("MAX_TOTAL_OUTPUT_MB", 60, minimum=1),
        max_split_outputs=_env_int("MAX_SPLIT_OUTPUTS", 10, minimum=1),
        max_split_total_pages=_env_int(
            "MAX_SPLIT_TOTAL_PAGES", 500, minimum=1
        ),
        split_individual_max=_env_int("SPLIT_INDIVIDUAL_MAX", 3, minimum=1),
        max_zip_entries=_env_int("MAX_ZIP_ENTRIES", 10, minimum=1),
        max_image_pixels=_env_int("MAX_IMAGE_PIXELS", 25_000_000, minimum=1),
        max_image_dimension=_env_int("MAX_IMAGE_DIMENSION", 6000, minimum=1),
        max_workspaces=_env_int("MAX_WORKSPACES", 20, minimum=1),
        stale_workspace_ttl=_env_float(
            "STALE_WORKSPACE_TTL", 7200.0, minimum=1
        ),
        workspace_idle_ttl=_env_float("WORKSPACE_IDLE_TTL", 900.0, minimum=1),
        workspace_cleanup_interval=_env_float(
            "WORKSPACE_CLEANUP_INTERVAL", 600.0, minimum=1
        ),
        telegram_request_timeout=_env_float(
            "TELEGRAM_REQUEST_TIMEOUT", 60.0, minimum=1
        ),
        download_timeout=_env_float("DOWNLOAD_TIMEOUT", 60.0, minimum=1),
        enable_self_ping=_env_bool("ENABLE_SELF_PING", False),
        ping_url=_env("PING_URL"),
        self_ping_initial_delay=_env_float(
            "SELF_PING_INITIAL_DELAY", 15.0, minimum=0
        ),
        self_ping_interval=_env_float("SELF_PING_INTERVAL", 600.0, minimum=1),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        temp_root=Path(_env("TEMP_ROOT") or (Path(tempfile.gettempdir()) / APP_NAME)),
    )
    return settings
