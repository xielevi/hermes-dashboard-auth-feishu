"""Feishu/Lark OAuth provider and the local session store behind it.

Upstream (Feishu/Lark) is only used once per login to prove who the user is. After that the
dashboard runs on locally minted, HMAC-signed session tokens; no Feishu token is persisted.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    InvalidCodeError,
    LoginStart,
    ProviderError,
    RefreshExpiredError,
    Session,
)

logger = logging.getLogger(__name__)

ENDPOINTS = {
    "feishu": dict(authorize="https://accounts.feishu.cn/open-apis/authen/v1/authorize",
                   token="https://accounts.feishu.cn/oauth/v3/token",
                   userinfo="https://open.feishu.cn/open-apis/authen/v1/user_info"),
    "lark": dict(authorize="https://accounts.larksuite.com/open-apis/authen/v1/authorize",
                 token="https://accounts.larksuite.com/oauth/v3/token",
                 userinfo="https://open.larksuite.com/open-apis/authen/v1/user_info"),
}

ABS_TTL = 30 * 86400          # absolute session lifetime
IDLE_TTL = 14 * 86400         # session expires after this long without use
AT_TTL = 12 * 3600            # access-token lifetime before a refresh is needed
GRACE = 60                    # window in which the previous token pair is still honoured
LOGIN_TTL = 300               # pending OAuth state lifetime
MAX_PENDING_LOGINS = 512
HTTP_TIMEOUT = 10
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


def parse_key(raw: str) -> bytes:
    """Decode a hex or base64 signing key of at least 32 bytes. Empty -> random per-process key."""
    raw = (raw or "").strip()
    if not raw:
        logger.warning("dashboard-auth-feishu: no session key configured; sessions will not survive a restart")
        return secrets.token_bytes(32)
    for decode in (bytes.fromhex, lambda s: base64.b64decode(s, validate=True)):
        try:
            key = decode(raw)
        except (ValueError, binascii.Error):
            continue
        if len(key) >= 32:
            return key
    raise ValueError("session key must be hex or base64 and at least 32 bytes")


def _json(response) -> dict:
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("unexpected response shape")
    return body


class Store:
    """Rotating session families: one SQLite row per login, tokens are HMAC-signed row references.

    A token is ``f1.<family>.<version>.<kind>.<sig>``. Refreshing bumps ``version``; presenting an
    older refresh token after the grace window revokes the whole family (replay detection).
    """

    def __init__(self, path: Path, key: bytes, tenant: str, owners: frozenset,
                 clock: Callable[[], float] = time.time):
        if len(key) < 32 or not tenant or not owners:
            raise ValueError("session key (>=32 bytes), tenant and owners are required")
        self.path, self.key, self.tenant, self.owners, self.clock = Path(path), key, tenant, owners, clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():  # create private from the start instead of chmod-after-create
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS families (id TEXT PRIMARY KEY, tenant TEXT, owner TEXT,"
                       " name TEXT, created INTEGER, seen INTEGER, revoked INTEGER, version INTEGER,"
                       " rotated INTEGER)")
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def db(self):
        """Short-lived connection per operation: commit on success, roll back on error, always close."""
        conn = sqlite3.connect(self.path, timeout=0.5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def token(self, family: str, version: int, kind: str) -> str:
        prefix = f"f1.{family}.{version}.{kind}"
        digest = hmac.new(self.key, prefix.encode(), hashlib.sha256).digest()
        return prefix + "." + base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def parse(self, token: str, kind: str):
        try:
            if len(token) > 200:
                return None
            tag, family, version, k, _sig = token.split(".")
            n = int(version)
            if tag != "f1" or k != kind or len(family) != 32 or n < 0:
                return None
            return (family, n) if hmac.compare_digest(token, self.token(family, n, kind)) else None
        except (ValueError, TypeError, AttributeError):
            return None

    def _valid(self, row, now: int) -> bool:
        # Tenant and allow-list are re-checked on every request, so removing an owner takes effect
        # on the next dashboard restart without touching the database.
        return bool(row and not row["revoked"] and row["tenant"] == self.tenant and row["owner"] in self.owners
                    and now < row["created"] + ABS_TTL and now < row["seen"] + IDLE_TTL)

    def _session(self, row) -> Session:
        return Session(user_id=row["owner"], org_id=row["tenant"], email="",
                       display_name=row["name"] or "Feishu user", provider="feishu",
                       expires_at=min(row["rotated"] + AT_TTL, row["created"] + ABS_TTL),
                       access_token=self.token(row["id"], row["version"], "a"),
                       refresh_token=self.token(row["id"], row["version"], "r"))

    def mint(self, owner: str, name: str = "") -> Session:
        now = int(self.clock())
        family = secrets.token_hex(16)
        try:
            with self.db() as db:
                # Housekeeping: drop rows that can no longer validate (expired, or revoked over a day ago).
                db.execute("DELETE FROM families WHERE created < ? OR seen < ? OR (revoked > 0 AND revoked < ?)",
                           (now - ABS_TTL - 86400, now - IDLE_TTL - 86400, now - 86400))
                db.execute("INSERT INTO families VALUES (?,?,?,?,?,?,?,?,?)",
                           (family, self.tenant, owner, name, now, now, 0, 0, now))
                row = db.execute("SELECT * FROM families WHERE id=?", (family,)).fetchone()
        except sqlite3.Error:
            raise ProviderError("Local session store unavailable") from None
        return self._session(row)

    def verify(self, token: str) -> Session | None:
        parsed = self.parse(token, "a")
        if not parsed:
            return None
        family, version = parsed
        now = int(self.clock())
        try:
            with self.db() as db:
                row = db.execute("SELECT * FROM families WHERE id=?", (family,)).fetchone()
                if not self._valid(row, now):
                    return None
                current = version == row["version"] and now < row["rotated"] + AT_TTL
                previous = version == row["version"] - 1 and now < row["rotated"] + GRACE
                if not (current or previous):
                    return None
                if now - row["seen"] >= 600:  # throttle idle-tracking writes
                    db.execute("UPDATE families SET seen=? WHERE id=?", (now, family))
            return self._session(row)
        except sqlite3.Error:
            raise ProviderError("Local session store unavailable") from None

    def refresh(self, token: str) -> Session:
        parsed = self.parse(token, "r")
        if not parsed:
            raise RefreshExpiredError("Invalid session")
        family, version = parsed
        now = int(self.clock())
        try:
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM families WHERE id=?", (family,)).fetchone()
                if not self._valid(row, now):
                    raise RefreshExpiredError("Expired session")
                if version == row["version"]:
                    db.execute("UPDATE families SET version=version+1, rotated=?, seen=? WHERE id=? AND version=?",
                               (now, now, family, version))
                elif version == row["version"] - 1 and now < row["rotated"] + GRACE:
                    pass  # concurrent refresh from another tab: hand back the same current pair
                else:
                    db.execute("UPDATE families SET revoked=? WHERE id=?", (now, family))
                    db.commit()
                    raise RefreshExpiredError("Refresh replay refused")
                row = db.execute("SELECT * FROM families WHERE id=?", (family,)).fetchone()
            return self._session(row)
        except sqlite3.Error:
            raise ProviderError("Local session store unavailable") from None

    def revoke(self, token: str) -> None:
        parsed = self.parse(token, "r")
        if parsed:
            with self.db() as db:
                db.execute("UPDATE families SET revoked=? WHERE id=?", (max(1, int(self.clock())), parsed[0]))


class FeishuProvider(DashboardAuthProvider):
    """Server-side (confidential client) Feishu/Lark OAuth with a tenant + open_id allow-list."""

    name = "feishu"
    display_name = "Feishu"  # "Lark" when built for the Lark endpoints

    def __init__(self, app_id: str, app_secret: str, store: Store, public_url: str,
                 endpoints: dict | None = None, http=httpx, clock: Callable[[], float] = time.time):
        if not app_id or not app_secret:
            raise ValueError("Feishu app credentials required")
        u = urlsplit(public_url)
        if u.scheme not in ("https", "http") or not u.netloc or u.query or u.fragment or "//" in u.path:
            raise ValueError("public_url must look like https://hermes.example.com or https://example.com/hermes")
        if u.scheme == "http" and u.hostname not in _LOOPBACK:
            raise ValueError("public_url must be https unless it is loopback")
        self.app_id, self.app_secret, self.store = app_id, app_secret, store
        # A path prefix is kept, matching how Hermes builds the redirect URI from dashboard.public_url.
        self.public_url = f"{u.scheme}://{u.netloc}{u.path}".rstrip("/")
        self.ep = endpoints or ENDPOINTS["feishu"]
        if self.ep is ENDPOINTS["lark"]:
            self.display_name = "Lark"
        self.http, self.clock = http, clock
        self._lock = threading.Lock()
        self._logins: dict[str, tuple[str, float]] = {}

    @classmethod
    def from_settings(cls, *, app_id, app_secret, tenant_key, owner_open_ids, public_url,
                      domain="feishu", session_key=""):
        if domain not in ENDPOINTS:
            raise ValueError("domain must be 'feishu' or 'lark'")
        from plugins.plugin_storage import plugin_data_dir  # per-plugin data dir under the active Hermes home

        store = Store(plugin_data_dir("dashboard-auth-feishu") / "session.sqlite3", parse_key(session_key),
                      tenant_key, frozenset(owner_open_ids))
        return cls(app_id, app_secret, store, public_url, ENDPOINTS[domain])

    @property
    def callback(self) -> str:
        return self.public_url + "/auth/callback"

    # -- login -------------------------------------------------------------------------------

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        if redirect_uri != self.callback:
            raise ProviderError("Unexpected callback origin")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        with self._lock:
            now = self.clock()
            self._logins = {k: v for k, v in self._logins.items() if v[1] > now}
            while len(self._logins) >= MAX_PENDING_LOGINS:
                # Bounded memory without a lock-out: evict the oldest pending login.
                self._logins.pop(min(self._logins, key=lambda k: self._logins[k][1]))
            self._logins[state] = (verifier, now + LOGIN_TTL)
        # The state/verifier pair lives server-side and in the host's HttpOnly login cookie, so a callback
        # only succeeds in the browser that started it. PKCE is not sent upstream: Feishu's v3 token
        # endpoint rejected otherwise-valid S256 challenges in testing (error 20049).
        params = dict(client_id=self.app_id, response_type="code", redirect_uri=redirect_uri, state=state)
        return LoginStart(self.ep["authorize"] + "?" + urlencode(params),
                          {"hermes_session_pkce": f"state={state};verifier={verifier}"})

    def complete_login(self, *, code: str, state: str, code_verifier: str, redirect_uri: str) -> Session:
        if redirect_uri != self.callback or not state or len(code_verifier or "") < 43:
            raise InvalidCodeError("Invalid login context")
        with self._lock:
            pending = self._logins.pop(state, None)
        if not pending or pending[1] <= self.clock():
            raise InvalidCodeError("Login expired; start a new login")
        if not hmac.compare_digest(pending[0], code_verifier):
            raise InvalidCodeError("Login verifier mismatch; start a new login")
        if not isinstance(code, str) or not code or len(code) > 4096:
            raise InvalidCodeError("Invalid code")
        data = dict(grant_type="authorization_code", client_id=self.app_id, client_secret=self.app_secret,
                    code=code, redirect_uri=redirect_uri)
        try:
            r = self.http.post(self.ep["token"], data=data, timeout=HTTP_TIMEOUT, follow_redirects=False)
            body = _json(r)
        except (httpx.RequestError, ValueError):
            raise ProviderError("Feishu token endpoint unavailable") from None
        if r.status_code >= 500:
            raise ProviderError("Feishu token endpoint unavailable")
        if r.status_code != 200 or body.get("code", 0) != 0 or not isinstance(body.get("access_token"), str):
            # Log the upstream error code only: never the authorization code or the secret.
            logger.warning("dashboard-auth-feishu: token exchange rejected status=%s error_code=%s error=%s",
                           r.status_code, body.get("code"), str(body.get("error", ""))[:80])
            raise InvalidCodeError("Authorization code rejected")
        owner, name = self._identity(body["access_token"])
        return self.store.mint(owner, name)

    def _identity(self, access_token: str) -> tuple[str, str]:
        try:
            r = self.http.get(self.ep["userinfo"], headers={"Authorization": "Bearer " + access_token},
                              timeout=HTTP_TIMEOUT, follow_redirects=False)
            body = _json(r)
        except (httpx.RequestError, ValueError):
            raise ProviderError("Feishu identity unavailable") from None
        if r.status_code >= 500:
            raise ProviderError("Feishu identity unavailable")
        if r.status_code != 200 or body.get("code") != 0:
            raise InvalidCodeError("Identity rejected")
        user = body.get("data")
        if not isinstance(user, dict) or not isinstance(user.get("open_id"), str) \
                or not isinstance(user.get("tenant_key"), str):
            raise ProviderError("Feishu identity response malformed")
        if user["tenant_key"] != self.store.tenant or user["open_id"] not in self.store.owners:
            # These IDs are not secrets, and logging them is the supported way to find the values to
            # allow-list: sign in once, read this line from the dashboard log, then configure them.
            logger.warning("dashboard-auth-feishu: sign-in refused for tenant_key=%s open_id=%s",
                           user["tenant_key"][:64], user["open_id"][:64])
            raise InvalidCodeError("This identity is not allowed")
        return user["open_id"], str(user.get("name") or "")[:80]

    # -- sessions ----------------------------------------------------------------------------

    def verify_session(self, *, access_token: str) -> Session | None:
        return self.store.verify(access_token)

    def refresh_session(self, *, refresh_token: str) -> Session:
        return self.store.refresh(refresh_token)

    def revoke_session(self, *, refresh_token: str) -> None:
        try:
            self.store.revoke(refresh_token)
        except sqlite3.Error:
            pass  # logout is best-effort per the provider protocol
