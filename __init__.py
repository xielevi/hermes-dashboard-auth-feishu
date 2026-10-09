"""Register the Feishu/Lark dashboard auth provider through Hermes' public plugin context."""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_NAME = "dashboard-auth-feishu"
ENV_PREFIX = "HERMES_DASHBOARD_FEISHU_"
LAST_SKIP_REASON = ""


def _env(name: str) -> str:
    return os.environ.get(ENV_PREFIX + name, "").strip()


def _setting(ctx, key: str) -> Any:
    return ctx.get_config(key, default=None) if ctx is not None else None


def _host_public_url() -> str:
    """The host decides whether the dashboard is gated and which origin it serves."""
    try:
        from hermes_cli.dashboard_auth.prefix import resolve_public_url
    except ImportError:
        return ""
    return resolve_public_url() or ""


def _owner_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    return [str(item).strip() for item in (raw or []) if str(item).strip()]


def resolve_settings(ctx=None) -> dict | None:
    """Merge plugin settings with environment overrides. Returns None (and records why) if incomplete.

    Precedence: ``HERMES_DASHBOARD_FEISHU_*`` environment > plugin settings > host public URL.
    Secrets come from the environment only.
    """
    global LAST_SKIP_REASON
    app_id = _env("APP_ID") or str(_setting(ctx, "app_id") or "").strip()
    tenant = _env("TENANT_KEY") or str(_setting(ctx, "tenant_key") or "").strip()
    owners = _owner_list(_env("OWNER_OPEN_IDS") or _setting(ctx, "owner_open_ids"))
    public_url = _env("PUBLIC_URL") or str(_setting(ctx, "public_url") or "").strip() or _host_public_url()
    domain = (_env("DOMAIN") or str(_setting(ctx, "domain") or "feishu")).strip().lower()
    # A dedicated secret is preferred; FEISHU_APP_SECRET lets you reuse the gateway's Feishu app.
    secret = _env("APP_SECRET") or os.environ.get("FEISHU_APP_SECRET", "").strip()

    missing = [name for name, value in (
        ("app_id", app_id), ("tenant_key", tenant), ("owner_open_ids", owners),
        ("public_url", public_url), ("app secret", secret)) if not value]
    if missing:
        LAST_SKIP_REASON = "not configured (missing: " + ", ".join(missing) + ")"
        return None
    return dict(app_id=app_id, app_secret=secret, tenant_key=tenant, owner_open_ids=owners,
                public_url=public_url, domain=domain, session_key=_env("SESSION_KEY"))


def register(ctx) -> None:
    """Local-only registration: no network calls, subprocesses or config writes."""
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""
    settings = resolve_settings(ctx)
    if settings is None:
        logger.debug("%s: %s", PLUGIN_NAME, LAST_SKIP_REASON)
        return
    try:
        from .provider import FeishuProvider
        provider = FeishuProvider.from_settings(**settings)
    except (ValueError, ImportError, OSError, sqlite3.Error) as exc:
        LAST_SKIP_REASON = f"construction failed: {exc}"
        logger.warning("%s: %s", PLUGIN_NAME, LAST_SKIP_REASON)
        return
    ctx.register_dashboard_auth_provider(provider)
    logger.info("%s: registered (%d allowed open_id, domain=%s)", PLUGIN_NAME,
                len(settings["owner_open_ids"]), settings["domain"])
