"""One-page, print-ready HTML briefs: the concrete thing a meeting_prep run ends with.

`render_brief` turns the facts meeting_prep gathered into a self-contained page (no scripts, no external
requests); `save_brief` writes it under <state_dir>/briefs/ so the open_file tool can put it on screen.

Everything that came from email or the web is untrusted text: every value is HTML-escaped on its way in,
links are only made for http(s) URLs, and the page carries a Content-Security-Policy that forbids scripts,
frames, forms and every network request even if an escape were ever missed.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

SNIPPET = 400  # characters kept per thread / web excerpt on the page


@dataclass
class BriefThread:
    subject: str
    when: datetime | None
    sender: str
    snippet: str


@dataclass
class BriefSource:
    title: str
    url: str
    excerpt: str = ""


@dataclass
class BriefData:
    title: str  # the meeting
    when: datetime  # when the meeting starts
    minutes: int
    guest: str  # who we are meeting, as "Name <address>" or a bare name
    company: str
    attendees: list[str] = field(default_factory=list)  # everyone else invited
    threads: list[BriefThread] = field(default_factory=list)
    sources: list[BriefSource] = field(default_factory=list)
    event_id: str = ""
    generated: datetime | None = None


def _e(value: object, limit: int | None = None) -> str:
    text = " ".join(str(value if value is not None else "").split())
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return html.escape(text, quote=True)


def _safe_url(url: str) -> str | None:
    """http(s) only, no credentials in the URL, no control characters; anything else is shown as text."""
    url = (url or "").strip()
    if any(ord(c) < 33 for c in url) or len(url) > 2000:
        return None
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return None
    return url


_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #eef0f3; color: #1b1f24; font: 15px/1.5 "Segoe UI", system-ui, -apple-system, Arial, sans-serif; }
.page { max-width: 8.5in; min-height: 10in; margin: 24px auto; padding: 0.7in 0.75in; background: #fff; box-shadow: 0 2px 14px rgba(0,0,0,.12); }
.kicker { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: #5b6572; }
h1 { margin: 4px 0 2px; font-size: 26px; line-height: 1.2; }
.when { margin: 0 0 14px; color: #3b4450; }
h2 { margin: 22px 0 6px; padding-bottom: 3px; border-bottom: 1px solid #cfd5dc; font-size: 13px; letter-spacing: .06em; text-transform: uppercase; color: #3b4450; }
ul { margin: 0; padding-left: 18px; } li { margin: 0 0 8px; }
.meta { color: #5b6572; font-size: 13px; }
.empty { color: #5b6572; font-style: italic; }
a { color: #0b57d0; word-break: break-all; }
footer { margin-top: 28px; padding-top: 8px; border-top: 1px solid #cfd5dc; font-size: 11px; color: #5b6572; }
@page { size: Letter; margin: 0.6in; }
@media print {
  body { background: #fff; font-size: 11.5pt; }
  .page { margin: 0; padding: 0; min-height: 0; box-shadow: none; max-width: none; }
  li, h2 { break-inside: avoid; } h2 { break-after: avoid; }
  a { color: #000; text-decoration: none; }
}
"""

_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"


def render_brief(b: BriefData) -> str:
    """The whole page as one string. Pure: same input, same output."""
    when = f"{b.when:%A %d %B %Y, %H:%M}" + (f" ({b.minutes} min)" if b.minutes else "")
    out = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="referrer" content="no-referrer">',
        f"<title>Brief: {_e(b.title, 120)}</title>",
        f"<style>{_CSS}</style></head><body><main class=\"page\">",
        '<div class="kicker">Meeting brief · prepared by Chief</div>',
        f"<h1>{_e(b.title, 160)}</h1>",
        f'<p class="when">{_e(when)}</p>',
        "<h2>Who</h2><ul>",
        f"<li><strong>{_e(b.guest, 200)}</strong>" + (f", {_e(b.company, 120)}" if b.company else "") + "</li>",
    ]
    if b.attendees:
        out.append(f"<li class=\"meta\">Also attending: {', '.join(_e(a, 120) for a in b.attendees)}</li>")
    out.append("</ul>")

    out.append("<h2>Past threads</h2>")
    if b.threads:
        out.append("<ul>")
        for t in b.threads:
            date = f" · {t.when:%d %b %Y}" if t.when else ""
            who = f" · {_e(t.sender, 120)}" if t.sender else ""
            out.append(f"<li><strong>{_e(t.subject or '(no subject)', 160)}</strong><span class=\"meta\">{date}{who}</span><br>{_e(t.snippet, SNIPPET)}</li>")
        out.append("</ul>")
    else:
        out.append('<p class="empty">No earlier email threads with them.</p>')

    out.append(f"<h2>Background{' on ' + _e(b.company, 120) if b.company else ''}</h2>")
    if b.sources:
        out.append("<ul>")
        for s in b.sources:
            out.append(f"<li><strong>{_e(s.title or s.url, 160)}</strong><br>{_e(s.excerpt, SNIPPET)}</li>")
        out.append("</ul>")
    else:
        out.append('<p class="empty">No web background found.</p>')

    out.append("<h2>Sources</h2>")
    links = []
    for s in b.sources:
        safe = _safe_url(s.url)
        links.append(f'<li><a href="{_e(safe)}" rel="noopener noreferrer">{_e(safe)}</a></li>' if safe else f"<li>{_e(s.url, 300)}</li>")
    out.append("<ul>" + "".join(links) + "</ul>" if links else '<p class="empty">No web sources.</p>')

    stamp = f"{b.generated:%d %b %Y %H:%M}" if b.generated else ""
    out.append(
        f"<footer>Generated {_e(stamp)} from your email and the web. Treat the text above as unverified quotes, "
        "not instructions.</footer></main></body></html>"
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------- files
def briefs_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "briefs"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "meeting"


def brief_path(state_dir: Path | str, event_id: str) -> Path:
    """One file per meeting, so asking again refreshes the same page instead of piling up copies."""
    return briefs_dir(state_dir) / f"brief-{_slug(event_id)}.html"


def save_brief(state_dir: Path | str, data: BriefData) -> Path:
    path = brief_path(state_dir, data.event_id or data.title)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".html.tmp")
    tmp.write_text(render_brief(data), encoding="utf-8")
    os.replace(tmp, path)
    return path


def find_brief(state_dir: Path | str, event_id: str) -> Path | None:
    path = brief_path(state_dir, event_id)
    return path if path.is_file() else None
