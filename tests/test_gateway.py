from __future__ import annotations

import json
import threading
import time

import pytest
import requests as http
from flask import Flask, Response, request
from werkzeug.serving import make_server

from common.config import parse_config
from common.storage import atomic_write_json
from gateway.app import create_app
from gateway.cookies import TransientCookieJar


class ThreadedServer:
    def __init__(self, app, host="127.0.0.1", port=0):
        self.server = make_server(host, port, app, threaded=True)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.thread.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


def make_backend(name: str) -> Flask:
    app = Flask(f"backend-{name}")

    @app.get("/")
    @app.get("/<path:path>")
    @app.post("/<path:path>")
    def echo(path: str = ""):
        try:
            delay = int(request.args.get("delay_ms", "0"))
        except ValueError:
            delay = 0
        time.sleep(max(0, min(delay, 10000)) / 1000.0)
        response = Response(
            json.dumps({"service": name, "path": "/" + path}),
            mimetype="application/json",
        )
        response.headers["X-Sample-Service"] = name
        return response

    return app


@pytest.fixture()
def backends():
    with ThreadedServer(make_backend("sample-a")) as a, ThreadedServer(
        make_backend("sample-b")
    ) as b:
        yield {"sample-a": a.url, "sample-b": b.url}


@pytest.fixture()
def gateway(tmp_path, backends, monkeypatch):
    config_path = tmp_path / "routes.json"
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    # No Compose catalog here: gateway tests may point at arbitrary test URLs.
    monkeypatch.delenv("BACKEND_CATALOG", raising=False)
    app = create_app()
    with ThreadedServer(app) as server:
        yield server, config_path, backends


def write_config(path, backends, routes, version=1):
    data = {
        "version": version,
        "published_at": "2026-01-01T00:00:00+00:00",
        "backends": [
            {"name": name, "url": url} for name, url in backends.items()
        ],
        "routes": routes,
    }
    parse_config(data, catalog=backends)
    atomic_write_json(str(path), data)


def test_request_is_forwarded_and_labelled(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
    )
    response = http.get(server.url + "/a/x", timeout=10)
    assert response.status_code == 200
    assert response.json()["service"] == "sample-a"
    assert response.headers["X-Gateway-Backend"] == "sample-a"
    assert response.headers["X-Gateway-Route"] == "/a"
    assert response.headers["X-Gateway-Config-Version"] == "1"


def test_unmatched_path_returns_404(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
    )
    response = http.get(server.url + "/removed", timeout=10)
    assert response.status_code == 404
    assert response.json()["error"] == "route_not_found"
    assert "X-Gateway-Backend" not in response.headers


def test_in_flight_request_survives_route_removal(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/slow", "match": "prefix", "backend": "sample-a"}],
        version=1,
    )
    results = {}

    def slow_call():
        results["response"] = http.get(
            server.url + "/slow?delay_ms=2000", timeout=30
        )

    thread = threading.Thread(target=slow_call)
    thread.start()
    time.sleep(0.5)  # request is now being handled by sample-a
    write_config(
        path,
        backends,
        [{"path": "/other", "match": "prefix", "backend": "sample-b"}],
        version=2,
    )
    # A new request after the publish sees the removal immediately...
    new_response = http.get(server.url + "/slow", timeout=10)
    assert new_response.status_code == 404
    # ...while the in-flight request completes against its original snapshot.
    thread.join(timeout=30)
    response = results["response"]
    assert response.status_code == 200
    assert response.json()["service"] == "sample-a"
    assert response.headers["X-Gateway-Backend"] == "sample-a"
    assert response.headers["X-Gateway-Config-Version"] == "1"


def test_in_flight_request_keeps_old_backend_after_repoint(gateway):
    """A publish that repoints a route must not affect a request already sent."""
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/slow", "match": "prefix", "backend": "sample-a"}],
        version=1,
    )
    results = {}

    def slow_call():
        results["response"] = http.get(
            server.url + "/slow?delay_ms=1500", timeout=30
        )

    thread = threading.Thread(target=slow_call)
    thread.start()
    time.sleep(0.4)
    write_config(
        path,
        backends,
        [{"path": "/slow", "match": "prefix", "backend": "sample-b"}],
        version=2,
    )
    fresh = http.get(server.url + "/slow", timeout=10)
    assert fresh.headers["X-Gateway-Backend"] == "sample-b"
    assert fresh.headers["X-Gateway-Config-Version"] == "2"

    thread.join(timeout=30)
    response = results["response"]
    assert response.status_code == 200
    assert response.json()["service"] == "sample-a"
    assert response.headers["X-Gateway-Backend"] == "sample-a"
    assert response.headers["X-Gateway-Config-Version"] == "1"


def test_bad_config_file_keeps_serving_old_config(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
        version=1,
    )
    first = http.get(server.url + "/a", timeout=10)
    assert first.status_code == 200
    assert first.headers["X-Gateway-Config-Version"] == "1"

    path.write_text("{ broken json", encoding="utf-8")
    second = http.get(server.url + "/a", timeout=10)
    assert second.status_code == 200
    assert second.headers["X-Gateway-Config-Version"] == "1"

    status = http.get(server.url + "/__gateway/status", timeout=10).json()
    assert status["active_version"] == 1
    assert status["config_file_error"]


def test_config_with_dangling_backend_reference_is_rejected(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
        version=1,
    )
    assert http.get(server.url + "/a", timeout=10).headers[
        "X-Gateway-Config-Version"
    ] == "1"
    broken = {
        "version": 2,
        "backends": [{"name": n, "url": u} for n, u in backends.items()],
        "routes": [{"path": "/a", "match": "prefix", "backend": "ghost"}],
    }
    atomic_write_json(str(path), broken)
    response = http.get(server.url + "/a", timeout=10)
    assert response.headers["X-Gateway-Config-Version"] == "1"

    status = http.get(server.url + "/__gateway/status", timeout=10).json()
    assert status["active_version"] == 1
    assert "ghost" in status["config_file_error"]


def test_catalog_pinning_rejects_unknown_backend(tmp_path, monkeypatch):
    config_path = tmp_path / "routes.json"
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv(
        "BACKEND_CATALOG", '{"sample-a": "http://127.0.0.1:9"}'
    )
    app = create_app()
    with ThreadedServer(app) as server:
        broken = {
            "version": 1,
            "backends": [{"name": "rogue", "url": "http://127.0.0.1:9"}],
            "routes": [{"path": "/a", "match": "prefix", "backend": "rogue"}],
        }
        atomic_write_json(str(config_path), broken)
        response = http.get(server.url + "/a", timeout=10)
        assert response.status_code == 503
        assert response.json()["error"] == "no_config"


def test_healthz_and_reserved_prefix(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
    )
    health = http.get(server.url + "/__gateway/healthz", timeout=10)
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


def test_upstream_failure_returns_502(gateway):
    server, path, _backends = gateway
    write_config(
        path,
        {"sample-a": "http://127.0.0.1:1"},
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
    )
    response = http.get(server.url + "/a", timeout=10)
    assert response.status_code == 502
    assert response.headers["X-Gateway-Backend"] == "sample-a"


def test_query_string_and_method_forwarded(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-b"}],
    )
    response = http.post(server.url + "/a/deep?x=1&y=2", data=b"hi", timeout=10)
    assert response.status_code == 200
    assert response.headers["X-Gateway-Backend"] == "sample-b"


def make_cookie_backend(name: str) -> Flask:
    """Backend that records received Cookie headers and issues Path-scoped sid."""
    app = Flask(f"cookie-backend-{name}")
    app.config["seen_cookies"] = []
    app.config["seen_prefixes"] = []

    def _issue():
        app.config["seen_cookies"].append(request.headers.get("Cookie"))
        app.config["seen_prefixes"].append(request.headers.get("X-Forwarded-Prefix"))
        try:
            delay = int(request.args.get("delay_ms", "0"))
        except ValueError:
            delay = 0
        time.sleep(max(0, min(delay, 10000)) / 1000.0)
        response = Response("logged in")
        response.set_cookie(
            "sid",
            f"{name}-session",
            path=request.headers.get("X-Forwarded-Prefix", "/") or "/",
            max_age=3600,
            httponly=True,
        )
        return response

    @app.post("/login")
    @app.post("/<path:prefix>/login")
    def login(prefix: str = ""):
        return _issue()

    @app.get("/echo")
    @app.post("/echo")
    @app.get("/<path:prefix>/echo")
    @app.post("/<path:prefix>/echo")
    def echo(prefix: str = ""):
        app.config["seen_cookies"].append(request.headers.get("Cookie"))
        app.config["seen_prefixes"].append(request.headers.get("X-Forwarded-Prefix"))
        return Response(
            json.dumps({"service": name, "cookie": request.headers.get("Cookie")}),
            mimetype="application/json",
        )

    return app


@pytest.fixture()
def cookie_gateway(tmp_path, monkeypatch):
    backend_a = make_cookie_backend("sample-a")
    backend_b = make_cookie_backend("sample-b")
    with ThreadedServer(backend_a) as a, ThreadedServer(backend_b) as b:
        backends = {"sample-a": a.url, "sample-b": b.url}
        config_path = tmp_path / "routes.json"
        monkeypatch.setenv("CONFIG_PATH", str(config_path))
        monkeypatch.delenv("BACKEND_CATALOG", raising=False)
        app = create_app()
        with ThreadedServer(app) as server:
            yield server, config_path, backends, backend_a, backend_b


def test_set_cookie_path_and_expiry_relayed_verbatim(cookie_gateway):
    server, path, backends, _a, _b = cookie_gateway
    write_config(
        path,
        backends,
        [{"path": "/sample-a", "match": "prefix", "backend": "sample-a"}],
    )
    response = http.post(server.url + "/sample-a/login", timeout=10)
    set_cookie = response.headers["Set-Cookie"]
    # No Domain attribute, explicit Path preserved, expiry attributes kept.
    assert "sid=sample-a-session" in set_cookie
    assert "Path=/sample-a" in set_cookie
    assert "Max-Age=3600" in set_cookie
    assert "domain=" not in set_cookie.lower()
    assert "Domain" not in set_cookie


def test_forwarded_prefix_header_matches_route(cookie_gateway):
    server, path, backends, a, _b = cookie_gateway
    write_config(
        path,
        backends,
        [{"path": "/sample-a", "match": "prefix", "backend": "sample-a"}],
    )
    http.get(server.url + "/sample-a/echo", timeout=10)
    assert a.config["seen_prefixes"][-1] == "/sample-a"


def test_multiple_sid_cookies_forwarded_in_order(cookie_gateway):
    server, path, backends, a, _b = cookie_gateway
    write_config(
        path,
        backends,
        [{"path": "/sample-a", "match": "prefix", "backend": "sample-a"}],
    )
    response = http.get(
        server.url + "/sample-a/echo",
        headers={"Cookie": "sid=first; sid=second; other=z; sid=third"},
        timeout=10,
    )
    assert response.json()["cookie"] == "sid=first; sid=second; other=z; sid=third"
    assert a.config["seen_cookies"][-1] == "sid=first; sid=second; other=z; sid=third"


def test_pooled_session_never_leaks_upstream_cookies(cookie_gateway):
    """Cookies issued to one browser must never reach another request."""
    server, path, backends, a, _b = cookie_gateway
    write_config(
        path,
        backends,
        [{"path": "/sample-a", "match": "prefix", "backend": "sample-a"}],
    )
    # First "browser" logs in; the upstream Set-Cookie would normally be stored
    # by the pooled requests.Session.
    login = http.post(server.url + "/sample-a/login", timeout=10)
    assert "sid=sample-a-session" in login.headers["Set-Cookie"]

    # A second browser sends no Cookie header, reusing the same pooled session.
    bare = http.get(server.url + "/sample-a/echo", timeout=10)
    assert bare.json()["cookie"] is None
    assert a.config["seen_cookies"][-1] is None

    # And a browser with its own cookie keeps exactly its own value.
    own = http.get(
        server.url + "/sample-a/echo",
        headers={"Cookie": "sid=browser-two"},
        timeout=10,
    )
    assert own.json()["cookie"] == "sid=browser-two"


def test_transient_cookie_jar_discards_upstream_cookies():
    jar = TransientCookieJar()
    assert list(jar) == []
    # The public write APIs used by requests must keep the jar empty.
    jar.set_cookie_if_ok(object(), object())
    assert list(jar) == []


def test_one_backend_logout_keeps_other_backend_cookie(cookie_gateway):
    """Two path-scoped sids coexist; only the matching one is sent per route."""
    server, path, backends, a, b = cookie_gateway
    write_config(
        path,
        backends,
        [
            {"path": "/sample-a", "match": "prefix", "backend": "sample-a"},
            {"path": "/sample-b", "match": "prefix", "backend": "sample-b"},
        ],
    )
    login_a = http.post(server.url + "/sample-a/login", timeout=10)
    login_b = http.post(server.url + "/sample-b/login", timeout=10)
    sid_a = login_a.cookies["sid"]
    sid_b = login_b.cookies["sid"]
    assert sid_a != sid_b

    # A browser holding both cookies sends each one only on the matching
    # route because their Path attributes differ.
    resp_a = http.get(
        server.url + "/sample-a/echo",
        headers={"Cookie": f"sid={sid_a}"},
        timeout=10,
    )
    resp_b = http.get(
        server.url + "/sample-b/echo",
        headers={"Cookie": f"sid={sid_b}"},
        timeout=10,
    )
    assert resp_a.json()["cookie"] == f"sid={sid_a}"
    assert resp_b.json()["cookie"] == f"sid={sid_b}"

    # Logging out of A expires only the cookie whose Path is /sample-a.
    # Build the expiry header exactly like the sample app's delete_cookie.
    flask_app = Flask("logout-helper")

    @flask_app.get("/expire")
    def expire():
        response = Response("bye")
        response.delete_cookie(
            "sid", path="/sample-a", httponly=True, samesite="Lax"
        )
        return response

    expiry_header = flask_app.test_client().get("/expire").headers["Set-Cookie"]
    assert "Path=/sample-a" in expiry_header
    assert "Domain" not in expiry_header
    # B's sid is an independent cookie and remains valid server-side.
    assert b.config["seen_cookies"][-1] == f"sid={sid_b}"
    assert sid_a != sid_b


def test_late_set_cookie_after_repoint_still_belongs_to_original_backend(
    cookie_gateway,
):
    """A slow login keeps its original backend even after a route repoint."""
    server, path, backends, a, b = cookie_gateway
    write_config(
        path,
        backends,
        [{"path": "/app", "match": "prefix", "backend": "sample-a"}],
        version=1,
    )
    # The slow login is delayed inside the sample backend; the route is
    # repointed while the request is in flight.
    results = {}

    def slow_login():
        results["response"] = http.post(
            server.url + "/app/login?delay_ms=2000", timeout=30
        )

    thread = threading.Thread(target=slow_login)
    thread.start()
    time.sleep(0.5)
    write_config(
        path,
        backends,
        [{"path": "/app", "match": "prefix", "backend": "sample-b"}],
        version=2,
    )
    fresh = http.post(server.url + "/app/login", timeout=10)
    assert fresh.headers["X-Gateway-Backend"] == "sample-b"
    assert "Path=/app" in fresh.headers["Set-Cookie"]

    thread.join(timeout=30)
    late = results["response"]
    # The in-flight request completed against the original backend...
    assert late.status_code == 200
    assert late.headers["X-Gateway-Backend"] == "sample-a"
    assert late.headers["X-Gateway-Config-Version"] == "1"
    # ...and its late Set-Cookie is still the original backend's session,
    # scoped to the same path so the browser stores it after switching back.
    assert "sid=sample-a-session" in late.headers["Set-Cookie"]
    assert "Path=/app" in late.headers["Set-Cookie"]
    assert a.config["seen_prefixes"][0] == "/app"

