"""Cookie handling helpers for the gateway.

The gateway is a pure relay for the ``Cookie`` / ``Set-Cookie`` headers and
must never become a cookie jar itself:

* The browser's ``Cookie`` header is forwarded verbatim (the proxy layer in
  ``app`` merges repeated header lines while preserving order).
* Upstream ``Set-Cookie`` headers are returned to the browser untouched.
* A pooled, connection-reusing :class:`requests.Session` must not remember an
  upstream-issued cookie and later attach it to another browser's request.

``TransientCookieJar`` plugs into ``requests`` while storing nothing, so the
session keeps reusing upstream TCP connections without ever accumulating
cookies across requests.

Note on repeated Cookie header lines: real browsers always fold every cookie
into a single ``Cookie`` line separated by ``"; "`` and never emit more than
one ``Cookie`` line.  The WSGI server (gunicorn) joins any hypothetical
repeated lines with ``", "`` before the application runs, per PEP 3333, so the
gateway's own ``"; "`` merge applies to the single value it receives and the
browser's cookie order is preserved end to end.
"""

from __future__ import annotations

from http.cookiejar import CookieJar


class TransientCookieJar(CookieJar):
    """A cookie jar that accepts no cookies.

    ``requests`` calls :meth:`extract_cookies` after every response and
    :meth:`add_cookie_header` while preparing every request.  Overriding the
    two write entry points means upstream ``Set-Cookie`` responses are never
    stored, while the explicit ``Cookie`` header built by the gateway is left
    in place.  The jar is therefore safe to share for the lifetime of a pooled
    session.
    """

    def set_cookie(self, cookie) -> None:  # type: ignore[override]
        return None

    def set_cookie_if_ok(self, cookie, request) -> None:  # type: ignore[override]
        return None

    def make_cookies(self, response, request):  # type: ignore[override]
        return []
