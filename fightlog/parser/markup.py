"""EVE's pseudo-HTML in gamelog lines."""

import html
import re

TAG_RE = re.compile(r"<[^>]*>")
COLOR_RE = re.compile(r"<color=(0x[0-9a-fA-F]{8})>")
SHOWINFO_RE = re.compile(r'<a href="showinfo:(\d+)//(\d+)">([^<]*)</a>')

_OPEN = {"[": "]", "(": ")", "<": ">", "“": "”"}


def leading_color(msg: str) -> str | None:
    match = COLOR_RE.search(msg)
    return match.group(1).lower() if match else None


def showinfo_links(msg: str) -> list[tuple[int, int, str]]:
    """(typeID, itemID, text) for every showinfo link. Fleet lines carry character IDs this way."""
    return [(int(a), int(b), html.unescape(c)) for a, b, c in SHOWINFO_RE.findall(msg)]


def clean(msg: str) -> str:
    """Strip tags, *then* decode entities (labels write '<' as '&lt;'), then tidy whitespace."""
    return normalize_ws(html.unescape(TAG_RE.sub("", msg)))


def normalize_ws(text: str) -> str:
    """Collapse whitespace runs to one space, except inside brackets and quotes.

    Tickers can contain spaces ("[J  H]"), so whitespace inside labels must survive.
    """
    out = []
    stack: list[str] = []
    pending_space = False
    for ch in text:
        if not stack and ch.isspace():
            pending_space = True
            continue
        if pending_space:
            if out:
                out.append(" ")
            pending_space = False
        out.append(ch)
        if stack and ch == stack[-1]:
            stack.pop()
        elif ch in _OPEN and len(stack) < 4:
            stack.append(_OPEN[ch])
    return "".join(out).strip()
