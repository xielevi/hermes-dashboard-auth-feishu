"""Provider contract and session-store behaviour, with simulated Feishu HTTP responses."""
import logging
import sqlite3
import threading

import pytest
from feishu_auth_pkg import provider as P
from hermes_cli.dashboard_auth import InvalidCodeError, ProviderError, RefreshExpiredError
from hermes_cli.dashboard_auth.base import assert_protocol_compliance

OWNER, TENANT, KEY, URL = "ou_owner", "tenant1", b"k" * 32, "https://hermes.example.com"


class Clock:
    t = 1_000_000

    def __call__(self):
        return self.t


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Http:
    def __init__(self, token=None, user=None):
        self.token = token or Resp(200, {"code": 0, "access_token": "uat"})
        self.user = user or Resp(200, {"code": 0, "data": {"tenant_key": TENANT, "open_id": OWNER, "name": "Alice"}})
        self.posts = []

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return self.token

    def get(self, url, **kw):
        return self.user


@pytest.fixture
def clock():
    return Clock()


def make(tmp_path, clock, http=None, owners=(OWNER,)):
    store = P.Store(tmp_path / "s.sqlite3", KEY, TENANT, frozenset(owners), clock=clock)
    return P.FeishuProvider("app", "secret", store, URL, http=http or Http(), clock=clock)


def login(p):
    cookie = p.start_login(redirect_uri=p.callback).cookie_payload["hermes_session_pkce"]
    state = cookie.split("state=")[1].split(";")[0]
    verifier = cookie.split("verifier=")[1]
    return state, verifier


def finish(p, state, verifier, code="c"):
    return p.complete_login(code=code, state=state, code_verifier=verifier, redirect_uri=p.callback)


def test_protocol_compliance():
    assert_protocol_compliance(P.FeishuProvider)


def test_authorize_url_has_no_secret(tmp_path, clock):
    p = make(tmp_path, clock)
    url = p.start_login(redirect_uri=p.callback).redirect_url
    assert url.startswith(P.ENDPOINTS["feishu"]["authorize"] + "?")
    assert "client_id=app" in url and "secret" not in url and "code_challenge" not in url


def test_login_flow_and_verify(tmp_path, clock):
    http = Http()
    p = make(tmp_path, clock, http)
    s = finish(p, *login(p))
    assert (s.user_id, s.org_id, s.display_name, s.provider) == (OWNER, TENANT, "Alice", "feishu")
    assert p.verify_session(access_token=s.access_token).user_id == OWNER
    assert http.posts[0][1]["data"]["client_secret"] == "secret"
    assert http.posts[0][1]["follow_redirects"] is False


def test_state_is_single_use(tmp_path, clock):
    p = make(tmp_path, clock)
    state, verifier = login(p)
    finish(p, state, verifier)
    with pytest.raises(InvalidCodeError):
        finish(p, state, verifier)


def test_state_expires(tmp_path, clock):
    p = make(tmp_path, clock)
    state, verifier = login(p)
    clock.t += P.LOGIN_TTL + 1
    with pytest.raises(InvalidCodeError):
        finish(p, state, verifier)


def test_wrong_verifier_and_callback(tmp_path, clock):
    p = make(tmp_path, clock)
    state, _ = login(p)
    with pytest.raises(InvalidCodeError):
        finish(p, state, "x" * 64)
    with pytest.raises(ProviderError):
        p.start_login(redirect_uri="https://evil.example/auth/callback")


def test_foreign_identity_rejected(tmp_path, clock):
    other_user = Resp(200, {"code": 0, "data": {"tenant_key": TENANT, "open_id": "ou_other"}})
    other_tenant = Resp(200, {"code": 0, "data": {"tenant_key": "other", "open_id": OWNER}})
    for user in (other_user, other_tenant):
        p = make(tmp_path, clock, Http(user=user))
        with pytest.raises(InvalidCodeError):
            finish(p, *login(p))


def test_token_endpoint_errors(tmp_path, clock):
    cases = [(Resp(200, {"code": 20049, "error": "bad"}), InvalidCodeError),
             (Resp(400, {"error": "invalid_grant"}), InvalidCodeError),
             (Resp(503, {}), ProviderError),
             (Resp(502, ValueError("html error page")), ProviderError),
             (Resp(200, ["not", "a", "dict"]), ProviderError)]
    for token, exc in cases:
        p = make(tmp_path, clock, Http(token=token))
        with pytest.raises(exc):
            finish(p, *login(p))


def test_refresh_rotation_and_replay(tmp_path, clock):
    p = make(tmp_path, clock)
    s = p.store.mint(OWNER, "A")
    clock.t += 100
    s2 = p.refresh_session(refresh_token=s.refresh_token)
    assert s2.access_token != s.access_token
    assert p.refresh_session(refresh_token=s.refresh_token).access_token == s2.access_token  # parallel tab
    assert p.verify_session(access_token=s.access_token)            # previous pair inside grace
    clock.t += P.GRACE + 1
    assert p.verify_session(access_token=s.access_token) is None    # grace over
    with pytest.raises(RefreshExpiredError):
        p.refresh_session(refresh_token=s.refresh_token)            # replay revokes the family
    assert p.verify_session(access_token=s2.access_token) is None


def test_idle_and_absolute_expiry(tmp_path, clock):
    p = make(tmp_path, clock)
    s = p.store.mint(OWNER, "A")
    clock.t += P.IDLE_TTL + 1
    with pytest.raises(RefreshExpiredError):
        p.refresh_session(refresh_token=s.refresh_token)
    s = p.store.mint(OWNER, "A")
    started = clock.t
    while clock.t + P.AT_TTL < started + P.ABS_TTL:                 # an always-active session...
        clock.t += P.AT_TTL - 1
        s = p.refresh_session(refresh_token=s.refresh_token)
    assert s.expires_at <= started + P.ABS_TTL
    clock.t = started + P.ABS_TTL                                    # ...still ends at the absolute limit
    with pytest.raises(RefreshExpiredError):
        p.refresh_session(refresh_token=s.refresh_token)


def test_access_expiry_and_revoke(tmp_path, clock):
    p = make(tmp_path, clock)
    s = p.store.mint(OWNER, "A")
    clock.t += P.AT_TTL + 1
    assert p.verify_session(access_token=s.access_token) is None
    s = p.store.mint(OWNER, "A")
    p.revoke_session(refresh_token=s.refresh_token)
    assert p.verify_session(access_token=s.access_token) is None


def test_tampered_token_and_removed_owner(tmp_path, clock):
    p = make(tmp_path, clock)
    s = p.store.mint(OWNER, "A")
    assert p.verify_session(access_token=s.access_token[:-2] + "xx") is None
    assert p.verify_session(access_token=s.refresh_token) is None   # wrong token kind
    assert p.verify_session(access_token="f1." + "a" * 300) is None
    p2 = make(tmp_path, clock, owners=("ou_someone_else",))          # owner removed from allow-list
    assert p2.verify_session(access_token=s.access_token) is None


def test_public_url_validation(tmp_path, clock):
    store = P.Store(tmp_path / "x.sqlite3", KEY, TENANT, frozenset([OWNER]))
    for bad in ("http://hermes.example.com", "https://h.example.com/?q", "https://h.example.com/#f",
                "https://h.example.com//x", "ftp://x"):
        with pytest.raises(ValueError):
            P.FeishuProvider("a", "s", store, bad)
    assert P.FeishuProvider("a", "s", store, "http://127.0.0.1:9119/").callback == "http://127.0.0.1:9119/auth/callback"


def test_public_url_path_prefix(tmp_path, clock):
    # Hermes builds redirect_uri as dashboard.public_url + "/auth/callback", prefix included.
    store = P.Store(tmp_path / "x.sqlite3", KEY, TENANT, frozenset([OWNER]))
    p = P.FeishuProvider("a", "s", store, "https://example.com/hermes/", http=Http())
    assert p.callback == "https://example.com/hermes/auth/callback"
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fhermes%2Fauth%2Fcallback" in \
        p.start_login(redirect_uri=p.callback).redirect_url
    with pytest.raises(ProviderError):
        p.start_login(redirect_uri="https://example.com/auth/callback")


def test_display_name_follows_domain(tmp_path, clock):
    store = P.Store(tmp_path / "x.sqlite3", KEY, TENANT, frozenset([OWNER]))
    assert P.FeishuProvider("a", "s", store, URL).display_name == "Feishu"
    lark = P.FeishuProvider("a", "s", store, URL, P.ENDPOINTS["lark"])
    assert lark.display_name == "Lark" and lark.name == "feishu"


def test_store_file_is_private(tmp_path, clock):
    p = make(tmp_path, clock)
    assert (p.store.path.stat().st_mode & 0o777) == 0o600


def test_parse_key():
    assert len(P.parse_key("ab" * 32)) == 32
    assert len(P.parse_key("")) == 32
    with pytest.raises(ValueError):
        P.parse_key("short")


def test_pending_logins_evict_oldest_instead_of_lockout(tmp_path, clock):
    p = make(tmp_path, clock)
    first = login(p)
    for _ in range(P.MAX_PENDING_LOGINS + 5):
        login(p)
    assert len(p._logins) <= P.MAX_PENDING_LOGINS
    assert finish(p, *login(p)).user_id == OWNER                     # new logins still work
    with pytest.raises(InvalidCodeError):                             # the flooded-out oldest one is gone
        finish(p, *first)


def test_dead_families_are_purged_on_mint(tmp_path, clock):
    p = make(tmp_path, clock)
    old = p.store.mint(OWNER, "A")
    clock.t += P.ABS_TTL + 2 * 86400
    p.store.mint(OWNER, "B")
    with p.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM families").fetchone()[0] == 1
    assert p.verify_session(access_token=old.access_token) is None


def test_store_connections_are_closed(tmp_path, clock):
    p = make(tmp_path, clock)
    with p.store.db() as db:
        conn = db
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_malformed_identity_responses(tmp_path, clock):
    for data in (None, "x", ["ou_owner"], {"tenant_key": TENANT}, {"tenant_key": TENANT, "open_id": 7},
                 {"tenant_key": None, "open_id": OWNER}):
        p = make(tmp_path, clock, Http(user=Resp(200, {"code": 0, "data": data})))
        with pytest.raises(ProviderError):
            finish(p, *login(p))
    for user, exc in ((Resp(200, ["x"]), ProviderError), (Resp(502, ValueError("html")), ProviderError),
                      (Resp(401, {"code": 99991663}), InvalidCodeError)):
        p = make(tmp_path, clock, Http(user=user))
        with pytest.raises(exc):
            finish(p, *login(p))


def test_refused_identity_is_logged_for_onboarding(tmp_path, clock, caplog):
    user = Resp(200, {"code": 0, "data": {"tenant_key": "t_new", "open_id": "ou_new", "name": "N"}})
    p = make(tmp_path, clock, Http(user=user))
    with caplog.at_level(logging.WARNING), pytest.raises(InvalidCodeError):
        finish(p, *login(p))
    assert "tenant_key=t_new open_id=ou_new" in caplog.text
    assert "uat" not in caplog.text and "secret" not in caplog.text


def test_mint_store_failure_is_provider_error(tmp_path, clock):
    p = make(tmp_path, clock)
    p.store.path.unlink()
    p.store.path.mkdir()                                             # the database file is now unusable
    with pytest.raises(ProviderError):
        finish(p, *login(p))


def test_locked_store_fails_cleanly_then_recovers(tmp_path, clock):
    p = make(tmp_path, clock)
    s = p.store.mint(OWNER, "A")
    clock.t += 700                                                   # verify() will want to write `seen`
    blocker = sqlite3.connect(p.store.path)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        for call in (lambda: p.verify_session(access_token=s.access_token),
                     lambda: p.refresh_session(refresh_token=s.refresh_token),
                     lambda: p.store.mint(OWNER, "B")):
            with pytest.raises(ProviderError):
                call()
        p.revoke_session(refresh_token=s.refresh_token)              # best effort: must not raise
    finally:
        blocker.rollback()
        blocker.close()
    assert p.verify_session(access_token=s.access_token).user_id == OWNER   # nothing was revoked
    assert p.refresh_session(refresh_token=s.refresh_token).access_token


def test_concurrent_refresh_rotates_once(tmp_path, clock):
    p = make(tmp_path, clock)
    s = p.store.mint(OWNER, "A")
    clock.t += 100
    n, barrier, results, errors = 8, threading.Barrier(8), [], []

    def worker():
        barrier.wait()
        for _ in range(20):                                          # ride out short lock contention
            try:
                results.append(p.refresh_session(refresh_token=s.refresh_token).access_token)
                return
            except ProviderError:
                continue
            except Exception as exc:                                 # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(results) == n
    assert len(set(results)) == 1                                    # every tab got the same new pair
    with p.store.db() as db:
        row = db.execute("SELECT version, revoked FROM families").fetchone()
    assert (row["version"], row["revoked"]) == (1, 0)
