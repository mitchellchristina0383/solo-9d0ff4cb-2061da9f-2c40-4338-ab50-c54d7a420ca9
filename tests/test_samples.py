from __future__ import annotations

import time

import pytest

from samples.app import app, SESSION_STORE
from samples.sessions import SessionStore, parse_cookie_sids, sid_values


@pytest.fixture()
def client(monkeypatch):
    # Every test starts with an empty session store.  use_cookies=False turns
    # the test client into a raw HTTP client: explicit Cookie headers are sent
    # verbatim instead of being rewritten by the client's own cookie jar.
    with app.test_client(use_cookies=False) as client:
        SESSION_STORE._sessions.clear()
        yield client


def set_prefix(client, prefix: str):
    client.environ_base["HTTP_X_FORWARDED_PREFIX"] = prefix
    return client


def test_index_shows_anonymous_state(client):
    set_prefix(client, "/sample-a")
    page = client.get("/")
    assert page.status_code == 200
    assert "当前未登录" in page.get_data(as_text=True)
    assert "/sample-a/login" in page.get_data(as_text=True)


def test_login_sets_path_scoped_sid_without_domain(client):
    set_prefix(client, "/sample-a")
    response = client.post("/login", data={"username": "alice", "remember": "1"})
    assert response.status_code == 200
    cookies = response.headers.getlist("Set-Cookie")
    sid_headers = [value for value in cookies if value.startswith("sid=")]
    assert len(sid_headers) == 1
    header = sid_headers[0]
    assert "Path=/sample-a" in header
    assert "Max-Age=604800" in header
    assert "Domain" not in header
    assert "HttpOnly" in header
    assert "alice" in response.get_data(as_text=True)


def test_session_cookie_has_no_expiry_when_not_remembered(client):
    set_prefix(client, "/sample-b")
    response = client.post("/login", data={"username": "bob"})
    header = response.headers.get("Set-Cookie")
    assert "Path=/sample-b" in header
    assert "Max-Age" not in header
    assert "Expires" not in header


def test_session_page_lists_all_sids_in_order(client):
    set_prefix(client, "/sample-a")
    page = client.get(
        "/session",
        headers={"Cookie": "sid=unknown-one; sid=unknown-two"},
    )
    text = page.get_data(as_text=True)
    assert "未登录或会话已过期" in text
    assert "unknown-one" in text
    assert "unknown-two" in text
    assert text.index("unknown-one") < text.index("unknown-two")


def test_login_then_session_then_logout_roundtrip(client):
    set_prefix(client, "/sample-a")
    login = client.post("/login", data={"username": "carol", "remember": "1"})
    sid = login.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    assert SESSION_STORE.get(sid) is not None

    session_page = client.get("/session", headers={"Cookie": f"sid={sid}"})
    text = session_page.get_data(as_text=True)
    assert "carol" in text
    assert sid in text

    logout = client.get("/logout", headers={"Cookie": f"sid={sid}"})
    expiry = logout.headers["Set-Cookie"]
    assert expiry.startswith("sid=")
    assert "Path=/sample-a" in expiry
    assert "Max-Age=0" in expiry
    assert "Domain" not in expiry
    assert SESSION_STORE.get(sid) is None
    assert "已退出本服务" in logout.get_data(as_text=True)


def test_cookie_path_follows_forwarded_prefix_when_route_is_repointed(client):
    set_prefix(client, "/legacy-a")
    response = client.post("/login", data={"username": "dora"})
    assert "Path=/legacy-a" in response.headers["Set-Cookie"]


def test_cookie_path_falls_back_to_environment_when_direct(monkeypatch):
    monkeypatch.setenv("COOKIE_PATH", "/direct-path")
    with app.test_client() as direct:
        response = direct.post("/login", data={"username": "erin"})
    assert "Path=/direct-path" in response.headers["Set-Cookie"]


def test_delayed_login_response(monkeypatch):
    monkeypatch.setenv("COOKIE_PATH", "/sample-a")
    with app.test_client() as direct:
        started = time.monotonic()
        response = direct.post(
            "/login?delay_ms=400", data={"username": "frank"}
        )
        elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert elapsed >= 0.35
    assert "Path=/sample-a" in response.headers["Set-Cookie"]


def test_echo_reports_sid_order_and_session(client):
    set_prefix(client, "/sample-a")
    login = client.post("/login", data={"username": "grace"})
    sid = login.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    echo = client.get(
        "/hello?delay_ms=0",
        headers={"Cookie": f"sid=stale; sid={sid}"},
    )
    payload = echo.get_json()
    assert payload["sid_order"] == ["stale", sid]
    assert payload["session"]["username"] == "grace"
    assert payload["cookie_path"] == "/sample-a"
    # The valid, known sid wins even when an older sid is listed first.
    assert payload["session"]["sid"] == sid


def test_logout_revokes_every_sid_sent(client):
    set_prefix(client, "/sample-a")
    first = client.post("/login", data={"username": "h1"})
    second = client.post("/login", data={"username": "h2"})
    sid1 = first.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    sid2 = second.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    client.get("/logout", headers={"Cookie": f"sid={sid1}; sid={sid2}"})
    assert SESSION_STORE.get(sid1) is None
    assert SESSION_STORE.get(sid2) is None


def test_session_store_expiry_and_revocation():
    store = SessionStore()
    session = store.create("temp", ttl_seconds=-1)
    assert store.get(session.sid) is None
    alive = store.create("keep", ttl_seconds=60)
    assert store.get(alive.sid) is not None
    store.revoke(alive.sid)
    assert store.get(alive.sid) is None


def test_parse_cookie_sids_preserves_order_and_duplicates():
    header = "sid=a; x=1; sid=b; sid=a"
    assert sid_values(header) == ["a", "b", "a"]
    assert parse_cookie_sids(header) == [
        ("sid", "a"),
        ("x", "1"),
        ("sid", "b"),
        ("sid", "a"),
    ]
    assert sid_values(None) == []
    assert parse_cookie_sids("garbage") == []
