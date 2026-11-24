"""Sample Compose backend: identifies itself, delays responses and logs in.

Two deployments of this app (``sample-a`` / ``sample-b``) stand in for two
legacy applications reached through the same gateway.  Both remember a login
with a cookie named ``sid``; the cookie carries an explicit ``Path`` scoped
to the gateway route prefix (taken from ``X-Forwarded-Prefix``) and never a
``Domain`` attribute, so the browser keeps one independent ``sid`` per app
and logging out of one leaves the other's session untouched.
"""

from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import quote

from flask import Flask, Response, make_response, render_template_string, request

from samples.sessions import SessionStore, parse_cookie_sids, sid_values

app = Flask(__name__)
SERVICE_NAME = os.environ.get("SERVICE_NAME", "sample-a")
SERVICE_COLOR = os.environ.get("SERVICE_COLOR", "blue")
MAX_DELAY_MS = 120_000
DEFAULT_REMEMBER_SECONDS = 7 * 24 * 3600
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]

# Single gunicorn worker in Compose keeps this store shared by every request.
SESSION_STORE = SessionStore()


def _delay_ms() -> int:
    try:
        delay_ms = int(request.args.get("delay_ms", "0"))
    except ValueError:
        delay_ms = 0
    return max(0, min(delay_ms, MAX_DELAY_MS))


def _cookie_path() -> str:
    """Path attribute for the ``sid`` cookie.

    The gateway sends the matched route prefix in ``X-Forwarded-Prefix``.  When
    the sample is reached directly (no gateway) the environment variable
    ``COOKIE_PATH`` or ``/`` is used instead.  The value always starts with
    ``/`` and has no trailing slash (except for ``/`` itself).
    """
    prefix = request.headers.get("X-Forwarded-Prefix", "").strip()
    if not prefix:
        prefix = os.environ.get("COOKIE_PATH", "/").strip() or "/"
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    return prefix.rstrip("/") or "/"


def _forwarded_prefix_name() -> str:
    """The route prefix without its leading slash ('' for bare root)."""
    return _cookie_path().lstrip("/")


def _all_sids() -> list[str]:
    """Every sid value the browser sent, in the original header order."""
    return sid_values(request.headers.get("Cookie"))


def _current_sid() -> str | None:
    """The sid the app considers active: the first one still known server-side.

    When several ``sid`` cookies arrive (overlapping paths, old and new login)
    the first valid, server-side-known value wins.  The full ordered list is
    still shown on the session page so forwarding order stays observable.
    """
    for sid in _all_sids():
        if SESSION_STORE.get(sid) is not None:
            return sid
    return None


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{{ service }} · 样例登录</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; color: #1f2933; }
    .card { border: 1px solid #cbd2d9; border-radius: 8px; padding: 1rem 1.25rem;
            max-width: 40rem; margin-bottom: 1rem; }
    .badge { display: inline-block; padding: .15rem .55rem; border-radius: 999px;
             background: {{ color }}; color: white; font-size: .85rem; }
    table { border-collapse: collapse; margin-top: .5rem; }
    td, th { border: 1px solid #cbd2d9; padding: .25rem .6rem; font-size: .92rem; }
    a { margin-right: .8rem; }
    label { display: block; margin: .5rem 0 .2rem; }
    input[name="username"] { width: 14rem; }
    .muted { color: #616e7c; font-size: .85rem; }
    code { background: #f0f4f8; padding: .05rem .3rem; border-radius: 4px; }
  </style>
</head>
<body>
  <h1><span class="badge">{{ service }}</span> 样例页面</h1>
  <div class="card">
    %(body)s
  </div>
  <p class="muted">
    网关入口：<code>{{ gateway_prefix }}{{ request.path }}</code> ｜
    Cookie Path：<code>{{ cookie_path }}</code>
  </p>
</body>
</html>"""

INDEX_BODY = """
{% if session %}
  <p>当前已登录为 <strong>{{ session.username }}</strong>。</p>
  <p>
    <a href="{{ gateway_prefix }}/session">查看会话</a>
    <a href="{{ gateway_prefix }}/logout">退出登录</a>
  </p>
{% else %}
  <p>当前未登录。</p>
  <p><a href="{{ gateway_prefix }}/login">前往登录</a></p>
{% endif %}
<p class="muted">回声接口（原有样例能力）：<a href="{{ gateway_prefix }}/hello">{{ gateway_prefix }}/hello</a></p>
"""
INDEX_PAGE = PAGE % {"body": INDEX_BODY}

LOGIN_BODY = """
{% if session %}
  <p>已经登录为 <strong>{{ session.username }}</strong>，无需重复登录。</p>
  <p><a href="{{ gateway_prefix }}/session">查看会话</a></p>
{% else %}
  <form method="post" action="{{ gateway_prefix }}/login">
    <label>用户名 <input name="username" value="demo" required maxlength="64"></label>
    <label><input type="checkbox" name="remember" value="1" checked>
      记住我（持久 Cookie，7 天有效）；不勾选则为会话级 Cookie</label>
    <label>登录响应延时（毫秒，便于测试迟到响应）
      <input name="delay_ms" value="0" inputmode="numeric"></label>
    <p><button type="submit">登录</button></p>
  </form>
{% endif %}
"""
LOGIN_PAGE = PAGE % {"body": LOGIN_BODY}

SESSION_BODY = """
{% if session %}
  <h2>会话信息</h2>
  <table>
    <tr><th>服务</th><td>{{ service }}</td></tr>
    <tr><th>用户名</th><td>{{ session.username }}</td></tr>
    <tr><th>sid</th><td><code>{{ session.sid }}</code></td></tr>
    <tr><th>创建时间</th><td>{{ created_iso }}</td></tr>
    <tr><th>过期时间</th><td>{{ expires_iso or '浏览器会话结束即失效' }}</td></tr>
    <tr><th>Cookie Path</th><td><code>{{ cookie_path }}</code></td></tr>
  </table>
  <p>
    <a href="{{ gateway_prefix }}/logout">退出登录</a>
    <a href="{{ gateway_prefix }}/">返回首页</a>
  </p>
{% else %}
  <p>未登录或会话已过期。</p>
  <p><a href="{{ gateway_prefix }}/login">前往登录</a></p>
{% endif %}
  <h3>本次请求浏览器送来的 sid（按原始顺序）</h3>
  {% if sids %}
  <ol>{% for sid in sids %}<li><code>{{ sid }}</code>
    {%- if session and sid == session.sid %} ← 当前会话{% endif %}</li>{% endfor %}</ol>
  {% else %}<p class="muted">无</p>{% endif %}
"""
SESSION_PAGE = PAGE % {"body": SESSION_BODY}

LOGOUT_BODY = """
  <p>已退出本服务（<strong>{{ service }}</strong>）的登录，sid 已清除。</p>
  <p><a href="{{ gateway_prefix }}/login">重新登录</a>
     <a href="{{ gateway_prefix }}/">返回首页</a></p>
  <p class="muted">另一个样例服务的登录不受影响——它的 <code>sid</code> 带有不同的 Path。</p>
"""
LOGOUT_PAGE = PAGE % {"body": LOGOUT_BODY}


def _render(template: str, **context) -> str:
    gateway_prefix = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
    base = {
        "service": SERVICE_NAME,
        "color": SERVICE_COLOR,
        "gateway_prefix": gateway_prefix,
        "cookie_path": _cookie_path(),
        "request": request,
    }
    base.update(context)
    return render_template_string(template, **base)


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _set_sid_cookie(response: Response, sid: str, *, persistent: bool) -> None:
    """Issue ``sid`` with an explicit Path, without any Domain attribute."""
    response.set_cookie(
        "sid",
        sid,
        path=_cookie_path(),
        max_age=DEFAULT_REMEMBER_SECONDS if persistent else None,
        expires=None,
        secure=False,
        httponly=True,
        samesite="Lax",
    )


def _clear_sid_cookie(response: Response) -> None:
    """Expire every sid the browser might hold for this cookie path."""
    response.delete_cookie("sid", path=_cookie_path(), httponly=True, samesite="Lax")


@app.get("/")
def index() -> Response:
    sid = _current_sid()
    html = _render(
        INDEX_PAGE,
        session=SESSION_STORE.get(sid) if sid else None,
    )
    return Response(html, mimetype="text/html")


@app.route("/login", methods=["GET", "POST"])
@app.route("/<path:prefix>/login", methods=["GET", "POST"])
def login(prefix: str = "") -> Response:
    existing = _current_sid()
    if request.method == "GET":
        return Response(
            _render(LOGIN_PAGE, session=SESSION_STORE.get(existing) if existing else None),
            mimetype="text/html",
        )

    # The login response may be delayed so tests can repoint the route while
    # the request is in flight; the late Set-Cookie still belongs to this app.
    time.sleep(_delay_ms() / 1000.0)
    username = (request.form.get("username") or "").strip()[:64] or "anonymous"
    persistent = request.form.get("remember") == "1"
    ttl = DEFAULT_REMEMBER_SECONDS if persistent else None
    session_obj = SESSION_STORE.create(username, ttl)
    response = make_response(
        _render(
            SESSION_PAGE,
            session=session_obj,
            sids=_all_sids(),
            created_iso=_iso(session_obj.created_at),
            expires_iso=_iso(session_obj.expires_at),
        )
    )
    _set_sid_cookie(response, session_obj.sid, persistent=persistent)
    response.headers["X-Sample-Service"] = SERVICE_NAME
    response.headers["X-Sample-Login"] = quote(username)
    return response


@app.get("/session")
@app.get("/<path:prefix>/session")
def session_view(prefix: str = "") -> Response:
    sid = _current_sid()
    session_obj = SESSION_STORE.get(sid) if sid else None
    return Response(
        _render(
            SESSION_PAGE,
            session=session_obj,
            sids=_all_sids(),
            created_iso=_iso(session_obj.created_at) if session_obj else None,
            expires_iso=_iso(session_obj.expires_at) if session_obj else None,
        ),
        mimetype="text/html",
    )


@app.route("/logout", methods=["GET", "POST"])
@app.route("/<path:prefix>/logout", methods=["GET", "POST"])
def logout(prefix: str = "") -> Response:
    for sid in _all_sids():
        SESSION_STORE.revoke(sid)
    response = make_response(_render(LOGOUT_PAGE))
    _clear_sid_cookie(response)
    response.headers["X-Sample-Service"] = SERVICE_NAME
    return response


@app.get("/healthz")
def healthz() -> Response:
    return Response(
        json.dumps(
            {
                "status": "ok",
                "service": SERVICE_NAME,
                "sessions": SESSION_STORE.count(),
            }
        ),
        mimetype="application/json",
    )


# The session endpoints above own "/", "/login", "/session" and "/logout"
# (both bare and gateway-prefixed, e.g. "/sample-a/login"); every other path
# keeps the original echo behaviour of the sample service.
@app.route("/<path:path>", methods=ALL_METHODS)
def echo(path: str = "") -> Response:
    # A request to the prefixed root (e.g. "/sample-a/") renders the same
    # landing page as the bare "/" route instead of an echo payload.
    if request.method == "GET" and path.rstrip("/") == _forwarded_prefix_name():
        sid = _current_sid()
        return Response(
            _render(
                INDEX_PAGE,
                session=SESSION_STORE.get(sid) if sid else None,
            ),
            mimetype="text/html",
        )
    delay_ms = _delay_ms()
    started = time.monotonic()
    if delay_ms:
        time.sleep(delay_ms / 1000.0)
    waited_ms = int((time.monotonic() - started) * 1000)
    sid = _current_sid()
    session_obj = SESSION_STORE.get(sid) if sid else None
    raw_cookie = request.headers.get("Cookie")
    payload = {
        "service": SERVICE_NAME,
        "color": SERVICE_COLOR,
        "method": request.method,
        "path": "/" + path,
        "query": request.args.to_dict(flat=False),
        "requested_delay_ms": delay_ms,
        "waited_ms": waited_ms,
        "host": request.host,
        "cookie_path": _cookie_path(),
        "sid_order": sid_values(raw_cookie),
        "cookies": [
            {"name": name, "value": value}
            for name, value in parse_cookie_sids(raw_cookie)
        ],
        "session": (
            {
                "username": session_obj.username,
                "sid": session_obj.sid,
                "expires_at": _iso(session_obj.expires_at),
            }
            if session_obj
            else None
        ),
        "headers": {
            key: value
            for key, value in request.headers.items()
            if key.lower().startswith("x-")
        },
        "body": request.get_data(as_text=True)[:2000],
    }
    response = Response(
        json.dumps(payload, ensure_ascii=False) + "\n",
        status=200,
        mimetype="application/json",
    )
    response.headers["X-Sample-Service"] = SERVICE_NAME
    response.headers["X-Sample-Color"] = SERVICE_COLOR
    return response


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "9000")))
