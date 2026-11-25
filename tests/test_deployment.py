"""Render blueprint parity and safe owner-identifier defaults."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_render_blueprint_uses_the_shared_service_defaults() -> None:
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "region: frankfurt" in text
    assert "autoDeploy: true" in text
    assert "maxShutdownDelaySeconds: 30" in text
    assert "healthCheckPath: /health" in text
    assert "uvicorn app.main:app --host 0.0.0.0 --port $PORT" in text
    assert "- key: PYTHONUNBUFFERED" in text


def test_owner_identifiers_stay_dashboard_managed() -> None:
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "- key: OWNER_ID\n        sync: false" in text
    assert "- key: LOG_CHANNEL_ID\n        sync: false" in text
    assert "- key: RENDER_DEPLOY_HOOK_URL\n        sync: false" in text
    assert "986380678" not in text
    assert "@gmail.com" not in text
    assert "@paruluniversity" not in text
    assert "?key=" not in text
    assert "srv-" not in text


def test_example_environment_has_no_secrets_or_personal_values() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "BOT_TOKEN=\n" in text
    assert "OWNER_ID=\n" in text
    assert "LOG_CHANNEL_ID=\n" in text
    assert "RENDER_DEPLOY_HOOK_URL=\n" in text
    assert "RENDER_EMAIL=\n" in text
    assert "@gmail.com" not in text
    assert "@paruluniversity" not in text
