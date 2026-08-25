"""Reading HTML in tests, without a browser and without a parser dependency.

These are deliberately narrow: they pull out the few things the templates promise to
render (flash messages, conflict form fields, the merge token) so a test can assert on
behaviour rather than on markup. If a template stops rendering one of them, the helper
returns None and the test fails with a clear reason -- which is the point.
"""
import html
import re


def flash(page: str) -> tuple[str, str] | None:
    """(kind, text) of the notice/error banner, or None."""
    m = re.search(r'class="flash (\w+)"[^>]*>\s*(?:<pre>)?(.*?)(?:</pre>)?\s*</div>',
                  page, re.S)
    # Unescaped, so tests assert on the message a person reads rather than on entities.
    return (m.group(1), html.unescape(m.group(2)).strip()) if m else None


def error(page: str) -> str:
    f = flash(page)
    assert f and f[0] == "error", f"expected an error banner, got {f!r}"
    return f[1]


def notice(page: str) -> str:
    f = flash(page)
    assert f and f[0] == "notice", f"expected a notice banner, got {f!r}"
    return f[1]


def conflict_keys(html: str) -> list[str]:
    return sorted(set(re.findall(r'name="resolve:([^"]+)"', html)))


def token(html: str) -> str | None:
    m = re.search(r'name="token" value="([^"]+)"', html)
    return m.group(1) if m else None


def status(html: str) -> str | None:
    m = re.search(r'class="status \w+">([^<]+)<', html)
    return m.group(1).strip() if m else None


def sql(page: str) -> str | None:
    m = re.search(r'<pre class="sql">(.*?)</pre>', page, re.S)
    return html.unescape(m.group(1)).strip() if m else None


def change_lines(html: str) -> list[str]:
    return [f"{w} {c}" for w, c in
            re.findall(r'<span class="tag \w+">([^<]+)</span>\s*<code>([^<]+)</code>', html)]
