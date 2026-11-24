"""FastAPI application, webhook endpoint, and health checks."""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import APP_NAME, APP_VERSION, Settings, load_settings
from app.runtime import TelegramRuntime
from app.services.webhook import IngressStatus
from app.utils.logging import configure_logging

log = logging.getLogger(__name__)

_MAX_WEBHOOK_BODY = 1_000_000
_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def create_app(
    settings: Settings | None = None,
    runtime: Any | None = None,
) -> FastAPI:
    settings = settings or load_settings(require_token=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.log_level)
        instance = runtime or TelegramRuntime(settings)
        app.state.runtime = instance
        await instance.start()
        try:
            yield
        finally:
            await instance.stop()

    app = FastAPI(
        title="Kayi PDF Bot",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": APP_NAME, "status": "ok"}

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    async def health(request: Request):
        instance = getattr(request.app.state, "runtime", None)
        if instance is None or not getattr(instance, "ready", False):
            return JSONResponse(
                {"status": "unavailable", "service": APP_NAME},
                status_code=503,
            )
        return JSONResponse(instance.health(), status_code=200)

    @app.post(settings.webhook_path)
    async def telegram_webhook(request: Request):
        secret = settings.webhook_secret
        provided = request.headers.get(_SECRET_HEADER, "")
        if not secret:
            return JSONResponse({"ok": False}, status_code=503)
        # Compare bytes: a malformed non-ASCII header must produce 401, not a
        # TypeError from hmac.compare_digest on str values.
        if not hmac.compare_digest(
            provided.encode("utf-8", "ignore"), secret.encode("utf-8")
        ):
            return JSONResponse({"ok": False}, status_code=401)

        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > _MAX_WEBHOOK_BODY:
                    return JSONResponse({"ok": False}, status_code=413)
            except ValueError:
                return JSONResponse({"ok": False}, status_code=400)
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > _MAX_WEBHOOK_BODY:
                return JSONResponse({"ok": False}, status_code=413)
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"ok": False}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False}, status_code=400)

        instance = getattr(request.app.state, "runtime", None)
        if instance is None:
            return JSONResponse({"ok": False}, status_code=503)
        status = await instance.ingest(payload)
        if status in {IngressStatus.ACCEPTED, IngressStatus.DUPLICATE}:
            return JSONResponse({"ok": True}, status_code=200)
        if status is IngressStatus.FULL:
            return JSONResponse(
                {"ok": False},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        return JSONResponse({"ok": False}, status_code=400)

    return app


app = create_app()
