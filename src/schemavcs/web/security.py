"""Response headers, and a floor under what an error is allowed to say.

Two concerns, both about the deployed instance rather than the local one.

**Headers.** The app serves one stylesheet and one script from its own origin and
nothing else -- no CDN, no webfont, no analytics -- so the policy can be the strict
version rather than the usual `'unsafe-inline'` compromise. Templates carry no inline
`style=` attributes and no inline handlers precisely so this stays true; the two
behaviours that need JavaScript live in `static/app.js` and hang off data attributes.

**Errors.** This app deliberately shows exception text: `DDLError` renders line numbers
and a hint, and `SchemaError` explains that a column name is taken. Those are curated
product messages and the UX depends on them. What must never reach a response is an
*unexpected* exception -- a driver error carrying a path, a `KeyError` naming an internal
id -- so the handler here catches everything the routes did not anticipate, logs it with
a traceback server-side, and returns a fixed string.
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("schemavcs.web")

CSP = "; ".join([
    "default-src 'none'",
    "style-src 'self'",
    "script-src 'self'",
    "img-src 'self' data:",          # the select-arrow SVG is a data: URI in the CSS
    "form-action 'self'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
])

HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    # HSTS is only meaningful over TLS, and asserting it on plain http://localhost
    # would teach a developer's browser to refuse the dev server.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for name, value in HEADERS.items():
            if name == "Strict-Transport-Security" and request.url.scheme != "https":
                continue
            response.headers[name] = value
        return response


GENERIC = (
    "Something went wrong handling that request. The details were logged; nothing "
    "was written to your workspace."
)


async def unhandled(request: Request, exc: Exception) -> HTMLResponse:
    """Last resort. Anything a route did not anticipate ends here.

    `exc_info=exc` keeps the traceback in the server log, which is where it is useful;
    the response says nothing about it, which is where it is dangerous.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path,
                  exc_info=exc)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<link rel=stylesheet href=/static/app.css>"
        f"<main><div class='flash error'>{GENERIC}</div>"
        "<p class='lede'><a href='/'>Start again</a></p></main>",
        status_code=500,
    )
