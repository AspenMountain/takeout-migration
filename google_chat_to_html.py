#!/usr/bin/env python3
"""
google_chat_to_html.py
======================

Reformat a Google Chat Takeout export into a browsable static HTML site.

Designed as the foundation for a small webapp (Flask/FastAPI/etc.) that lets
team members preserve their chat history. The rendering logic is split into
pure functions so they can be reused server-side without modification.

Usage
-----
    python3 google_chat_to_html.py <takeout_dir> -o <output_dir>

Where <takeout_dir> is the extracted Takeout folder containing
"Google Chat/" (i.e. the parent of the Google Chat directory, OR the
Google Chat directory itself — both are accepted).

Examples
--------
    python3 google_chat_to_html.py ~/Downloads/Takeout -o ~/chat-archive
    python3 google_chat_to_html.py ~/Downloads/Takeout/Google\\ Chat -o ./out

Output layout
-------------
    <output_dir>/
        index.html                       # conversation list, sortable
        assets/style.css                 # shared stylesheet
        conversations/<slug>.html        # one page per conversation
        files/<slug>/<original_name>     # copied message attachments

Schema reference (observed in real Takeout exports as of 2026)
--------------------------------------------------------------
group_info.json:
    name?       : str (Spaces only; DMs omit this)
    members     : list[ {name, email, user_type} ]

messages.json:
    messages: list of {
        creator       : {name, email, user_type}
        created_date  : str (e.g. "Wednesday, April 15, 2026 at 2:39:25 PM UTC")
        text?         : str
        topic_id?     : str (threading)
        message_id    : str
        annotations?  : list[ {start_index, length, format_metadata?, url_metadata?, drive_metadata?} ]
        attached_files? : list[ {original_name, export_name} ]
        reactions?    : list[ {emoji: {unicode}, reactor_emails: [str]} ]
        quoted_message_metadata? : {creator, text}
        secondary_message_key?   : str
    }

user_info.json:
    user            : {name, email, user_type}
    membership_info : list[ {group_id, membership_state} ]
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Member:
    name: str
    email: str
    user_type: str = "Human"


@dataclasses.dataclass
class Conversation:
    group_id: str            # e.g. "DM tniIfiAAAAE" or "Space AAQAmCy9ddg"
    kind: str                # "Space" or "DM"
    display_name: str        # human-facing title
    members: list[Member]
    messages: list[dict]     # raw messages from Takeout (we render lazily)
    source_dir: Path         # original folder under Groups/
    last_activity: dt.datetime | None = None

    @property
    def slug(self) -> str:
        return slugify(self.group_id)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


# Google's date string can include a NARROW NO-BREAK SPACE (U+202F) between
# the time and the AM/PM marker. Normalize it to a regular space.
_DATE_FMT = "%A, %B %d, %Y at %I:%M:%S %p %Z"


def parse_chat_date(s: str) -> dt.datetime | None:
    if not s:
        return None
    cleaned = s.replace(" ", " ")
    # %Z accepts UTC; if the export ever ships another zone, fall back to naive.
    try:
        return dt.datetime.strptime(cleaned, _DATE_FMT)
    except ValueError:
        # Drop the tz suffix and parse without it
        try:
            return dt.datetime.strptime(cleaned.rsplit(" ", 1)[0],
                                        "%A, %B %d, %Y at %I:%M:%S %p")
        except ValueError:
            return None


def slugify(s: str) -> str:
    """Make a filesystem-safe slug from a group id like 'DM tniIfiAAAAE'."""
    s = s.strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def find_chat_root(takeout_dir: Path) -> Path:
    """Accept the Takeout root, a one-level subdirectory, or the Google Chat folder itself."""
    if takeout_dir.name == "Google Chat":
        return takeout_dir
    # Check direct child and one level down (ZIP extracts to extracted/Takeout/...)
    candidates = [takeout_dir] + [p for p in takeout_dir.iterdir() if p.is_dir()]
    for candidate in candidates:
        if (candidate / "Google Chat").is_dir():
            return candidate / "Google Chat"
    raise SystemExit(
        f"Could not find a 'Google Chat' folder under {takeout_dir!s}. "
        "Pass the extracted Takeout dir or the 'Google Chat' folder itself."
    )


def load_user_info(chat_root: Path) -> Member | None:
    users_dir = chat_root / "Users"
    if not users_dir.is_dir():
        return None
    for d in users_dir.iterdir():
        info = d / "user_info.json"
        if info.exists():
            data = json.loads(info.read_text(encoding="utf-8"))
            u = data.get("user", {})
            if u:
                return Member(name=u.get("name", ""),
                              email=u.get("email", ""),
                              user_type=u.get("user_type", "Human"))
    return None


def load_conversations(chat_root: Path, owner: Member | None) -> list[Conversation]:
    groups_dir = chat_root / "Groups"
    if not groups_dir.is_dir():
        raise SystemExit(f"No Groups folder at {groups_dir}")

    conversations: list[Conversation] = []
    for d in sorted(groups_dir.iterdir()):
        if not d.is_dir():
            continue

        info_path = d / "group_info.json"
        msgs_path = d / "messages.json"
        if not msgs_path.exists():
            continue

        info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
        members_raw = info.get("members", []) or []
        members = [Member(name=m.get("name", ""),
                          email=m.get("email", ""),
                          user_type=m.get("user_type", "Human"))
                   for m in members_raw]

        msg_data = json.loads(msgs_path.read_text(encoding="utf-8"))
        messages = msg_data.get("messages", []) or []

        kind = "Space" if d.name.startswith("Space") else "DM" if d.name.startswith("DM") else "Group"
        display_name = info.get("name") or _derive_dm_title(members, owner) or d.name

        last = None
        for m in messages:
            ts = parse_chat_date(m.get("created_date", ""))
            if ts and (last is None or ts > last):
                last = ts

        conversations.append(Conversation(
            group_id=d.name,
            kind=kind,
            display_name=display_name,
            members=members,
            messages=messages,
            source_dir=d,
            last_activity=last,
        ))

    # Most recently active first
    conversations.sort(key=lambda c: c.last_activity or dt.datetime.min, reverse=True)
    return conversations


def _derive_dm_title(members: list[Member], owner: Member | None) -> str:
    """For DMs, the title is 'the other people in the conversation'."""
    if not members:
        return ""
    others = [m for m in members if not (owner and m.email == owner.email)]
    if not others:
        # DM with self / notes
        return ", ".join(m.name for m in members) or "(self)"
    return ", ".join(m.name for m in others)


# ---------------------------------------------------------------------------
# Annotation rendering
# ---------------------------------------------------------------------------


def render_text_with_annotations(text: str, annotations: list[dict] | None) -> str:
    """
    Render text honoring inline format annotations.

    Handles BOLD and FONT_COLOR. URL and Drive metadata are surfaced as
    appended cards in render_message; here we only deal with character-range
    formatting.
    """
    if not text:
        return ""

    spans: list[tuple[int, int, str, str]] = []  # (start, end, open_html, close_html)
    for a in annotations or []:
        fmt = a.get("format_metadata")
        if not fmt:
            continue
        start = a.get("start_index", 0)
        length = a.get("length", 0)
        end = start + length
        ftype = fmt.get("format_type")
        if ftype == "BOLD":
            spans.append((start, end, "<strong>", "</strong>"))
        elif ftype == "FONT_COLOR":
            color = fmt.get("font_color", 0)
            css = _argb_to_css(color)
            spans.append((start, end, f'<span style="color:{css}">', "</span>"))
        elif ftype == "ITALIC":
            spans.append((start, end, "<em>", "</em>"))
        elif ftype in ("STRIKE", "STRIKETHROUGH"):
            spans.append((start, end, "<s>", "</s>"))
        elif ftype == "UNDERLINE":
            spans.append((start, end, "<u>", "</u>"))
        elif ftype == "MONOSPACE":
            spans.append((start, end, "<code>", "</code>"))

    if not spans:
        return _linkify(html.escape(text))

    # Build per-character open/close events. We use a stable sort so closings
    # at the same boundary happen in reverse-open order, keeping nesting valid.
    chars = list(text)
    out: list[str] = []
    # event lists keyed by index
    open_at: dict[int, list[str]] = {}
    close_at: dict[int, list[str]] = {}
    # Sort spans so longer/earlier-opening spans wrap shorter/inner ones
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    for start, end, op, cl in spans:
        open_at.setdefault(start, []).append(op)
        # Closings stack: insert at front so latest-opened closes first
        close_at.setdefault(end, []).insert(0, cl)

    for i, ch in enumerate(chars):
        if i in close_at:
            out.extend(close_at[i])
        if i in open_at:
            out.extend(open_at[i])
        out.append(html.escape(ch))
    # Close anything still open at end-of-string
    if len(chars) in close_at:
        out.extend(close_at[len(chars)])

    rendered = "".join(out)
    # Linkify only outside of existing tags by doing a careful walk.
    return _linkify_html(rendered)


_URL_RE = re.compile(r"(https?://[^\s<]+)")


def _linkify(escaped: str) -> str:
    """Linkify already-escaped plain text (no existing HTML tags)."""
    return _URL_RE.sub(lambda m: f'<a href="{m.group(1)}" rel="noopener noreferrer" target="_blank">{m.group(1)}</a>', escaped)


def _linkify_html(s: str) -> str:
    """Linkify URLs in a string that may already contain HTML tags."""
    parts = re.split(r"(<[^>]+>)", s)
    for i in range(0, len(parts), 2):  # only non-tag segments
        parts[i] = _URL_RE.sub(
            lambda m: f'<a href="{m.group(1)}" rel="noopener noreferrer" target="_blank">{m.group(1)}</a>',
            parts[i],
        )
    return "".join(parts)


def _argb_to_css(argb: int) -> str:
    """Google stores font_color as a 32-bit ARGB int. Convert to #RRGGBB."""
    if not isinstance(argb, int):
        return "inherit"
    r = (argb >> 16) & 0xFF
    g = (argb >> 8) & 0xFF
    b = argb & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


STYLE_CSS = """
:root {
  --bg: #f7f7f8;
  --panel: #ffffff;
  --border: #e3e3e7;
  --text: #1f2328;
  --muted: #6b7280;
  --accent: #1a73e8;
  --self-bubble: #d9eaff;
  --other-bubble: #f1f3f5;
  --thread-rule: #d4d6da;
  --quote-bg: #f8f9fa;
  --quote-border: #c1c8d1;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 14px;
  line-height: 1.45;
  color: var(--text);
  background: var(--bg);
}
header.site {
  padding: 16px 24px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
header.site h1 { font-size: 18px; margin: 0; }
header.site .subtitle { color: var(--muted); font-size: 12px; }
main { max-width: 960px; margin: 0 auto; padding: 24px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* Index page */
.search-row { margin-bottom: 16px; }
.search-row input {
  width: 100%; padding: 10px 12px; font-size: 14px;
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--panel);
}
table.convo-list { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
table.convo-list th, table.convo-list td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
table.convo-list th { background: #fafafa; font-weight: 600; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
table.convo-list tr:last-child td { border-bottom: none; }
.kind-pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
.kind-Space { background: #e8f0fe; color: #174ea6; }
.kind-DM { background: #fce8e6; color: #8c1d18; }
.kind-Group { background: #fef7e0; color: #8a5e00; }

/* Conversation page */
.convo-meta { color: var(--muted); font-size: 12px; margin-bottom: 16px; }
.day-divider {
  text-align: center;
  margin: 24px 0 12px;
  color: var(--muted);
  font-size: 12px;
  position: relative;
}
.day-divider::before, .day-divider::after {
  content: "";
  position: absolute; top: 50%;
  width: 30%; height: 1px; background: var(--border);
}
.day-divider::before { left: 0; }
.day-divider::after { right: 0; }
.thread {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 12px;
}
.thread-multi { border-left: 3px solid var(--accent); }
.message { margin: 8px 0; }
.message + .message { border-top: 1px dashed var(--border); padding-top: 8px; }
.msg-header { display: flex; gap: 8px; align-items: baseline; margin-bottom: 2px; }
.msg-author { font-weight: 600; }
.msg-time { color: var(--muted); font-size: 12px; }
.msg-self .msg-author { color: var(--accent); }
.msg-text { white-space: pre-wrap; word-wrap: break-word; }
.msg-quote {
  border-left: 3px solid var(--quote-border);
  background: var(--quote-bg);
  padding: 6px 10px; margin: 6px 0;
  font-size: 13px; color: #444;
  border-radius: 0 4px 4px 0;
}
.msg-quote .quote-author { font-weight: 600; font-size: 12px; color: var(--muted); display: block; margin-bottom: 2px; }
.attachments { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
.attachments img { max-width: 320px; max-height: 320px; border-radius: 6px; border: 1px solid var(--border); }
.attachment-file {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 10px; background: var(--quote-bg); border: 1px solid var(--border); border-radius: 6px;
  font-size: 13px;
}
.link-card {
  display: block; margin-top: 6px;
  padding: 8px 10px; background: var(--quote-bg); border: 1px solid var(--border); border-radius: 6px;
  font-size: 13px;
}
.link-card .link-title { font-weight: 600; }
.link-card .link-snippet { color: var(--muted); margin-top: 2px; }
.reactions { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px; }
.reaction {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border: 1px solid var(--border); border-radius: 999px;
  background: var(--panel); font-size: 12px;
}
nav.breadcrumbs { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
nav.breadcrumbs a { color: var(--muted); }
"""

# Extra CSS layered on top of STYLE_CSS for the single-page SPA layout.
SINGLE_PAGE_EXTRA_CSS = """
html, body { height: 100%; overflow: hidden; }
#app { display: flex; height: 100vh; overflow: hidden; }
#sidebar {
  width: 280px; flex-shrink: 0;
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  background: var(--panel);
  overflow: hidden;
}
#sidebar-header { padding: 14px 16px; border-bottom: 1px solid var(--border); }
#sidebar-title { font-weight: 700; font-size: 15px; margin-bottom: 2px; }
#sidebar-meta, #sidebar-stats { font-size: 11px; color: var(--muted); line-height: 1.5; }
#search {
  display: block; width: 100%; margin-top: 8px;
  padding: 7px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text);
}
#convo-list { list-style: none; margin: 0; padding: 0; overflow-y: auto; flex: 1; }
.convo-item {
  padding: 10px 16px; cursor: pointer;
  border-bottom: 1px solid var(--border);
}
.convo-item:hover { background: var(--bg); }
.convo-item.active { background: #e8f0fe; }
.convo-item-title { font-weight: 500; font-size: 13px; margin-bottom: 3px; }
.convo-item-meta { display: flex; gap: 8px; align-items: center; font-size: 11px; color: var(--muted); flex-wrap: wrap; }
#main { flex: 1; overflow-y: auto; background: var(--bg); }
#welcome {
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  height: 100%; text-align: center;
  color: var(--muted);
}
#welcome h2 { font-size: 20px; color: var(--text); margin-bottom: 8px; }
#welcome .meta { font-size: 12px; }
.convo-page { padding: 24px; max-width: 800px; display: none; }
.convo-page-header { margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }
.convo-page-header h2 { margin: 0 0 6px 0; font-size: 18px; }
"""

SINGLE_PAGE_JS = r"""
(function () {
  var current = null;
  var main = document.getElementById('main');
  var welcome = document.getElementById('welcome');

  function show(id) {
    if (current) {
      var prev = document.getElementById('page-' + current);
      if (prev) prev.style.display = 'none';
      var prevItem = document.querySelector('.convo-item.active');
      if (prevItem) prevItem.classList.remove('active');
    }
    var page = document.getElementById('page-' + id);
    if (!page) return;
    welcome.style.display = 'none';
    page.style.display = 'block';
    var item = document.querySelector('[data-id="' + id + '"]');
    if (item) item.classList.add('active');
    main.scrollTop = 0;
    current = id;
  }

  document.querySelectorAll('.convo-item').forEach(function (item) {
    item.addEventListener('click', function () { show(this.dataset.id); });
  });

  var search = document.getElementById('search');
  if (search) {
    search.addEventListener('input', function () {
      var q = this.value.trim().toLowerCase();
      document.querySelectorAll('.convo-item').forEach(function (item) {
        item.style.display = item.dataset.search.indexOf(q) !== -1 ? '' : 'none';
      });
    });
  }
})();
"""


INDEX_JS = r"""
(function() {
  var input = document.getElementById('search');
  if (!input) return;
  var rows = document.querySelectorAll('table.convo-list tbody tr');
  input.addEventListener('input', function() {
    var q = this.value.trim().toLowerCase();
    rows.forEach(function(row) {
      row.style.display = row.dataset.search.indexOf(q) !== -1 ? '' : 'none';
    });
  });
})();
"""


def render_index_html(conversations: list[Conversation], owner: Member | None) -> str:
    rows: list[str] = []
    for c in conversations:
        last = c.last_activity.strftime("%Y-%m-%d %H:%M UTC") if c.last_activity else "—"
        members_str = ", ".join(m.name for m in c.members) or "—"
        msg_count = len(c.messages)
        search_blob = " ".join([c.display_name, members_str, c.kind, c.group_id]).lower()
        rows.append(
            f'<tr data-search="{html.escape(search_blob)}">'
            f'<td><span class="kind-pill kind-{c.kind}">{c.kind}</span></td>'
            f'<td><a href="conversations/{html.escape(c.slug)}.html">{html.escape(c.display_name)}</a></td>'
            f'<td>{html.escape(members_str)}</td>'
            f'<td>{msg_count}</td>'
            f'<td>{html.escape(last)}</td>'
            f'</tr>'
        )

    owner_line = f"Export owner: <strong>{html.escape(owner.name)}</strong> &lt;{html.escape(owner.email)}&gt;" if owner else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Google Chat archive</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<header class="site">
  <h1>Google Chat archive</h1>
  <span class="subtitle">{owner_line}</span>
</header>
<main>
  <div class="search-row">
    <input id="search" type="search" placeholder="Filter conversations by name, member, or id…" autofocus>
  </div>
  <table class="convo-list">
    <thead><tr><th>Type</th><th>Conversation</th><th>Members</th><th>Messages</th><th>Last activity</th></tr></thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</main>
<script>{INDEX_JS}</script>
</body>
</html>
"""


def render_conversation_html(c: Conversation, owner: Member | None,
                             attachment_url_prefix: str = "../files") -> str:
    """
    Render one conversation as a complete HTML page.

    Messages are grouped by day, then by topic_id within each day. Threads
    with multiple messages get a vertical accent rail.
    """
    # Sort messages by created_date (None goes last)
    sorted_msgs = sorted(c.messages,
                         key=lambda m: parse_chat_date(m.get("created_date", "")) or dt.datetime.max)

    # Group by day
    by_day: dict[str, list[dict]] = {}
    day_order: list[str] = []
    for m in sorted_msgs:
        ts = parse_chat_date(m.get("created_date", ""))
        day = ts.strftime("%A, %B %d, %Y") if ts else "Unknown date"
        if day not in by_day:
            by_day[day] = []
            day_order.append(day)
        by_day[day].append(m)

    body_parts: list[str] = []
    for day in day_order:
        body_parts.append(f'<div class="day-divider">{html.escape(day)}</div>')

        # Within a day, group by topic_id (preserving order of first appearance)
        threads: dict[str, list[dict]] = {}
        thread_order: list[str] = []
        for m in by_day[day]:
            tid = m.get("topic_id") or m.get("message_id", "")
            if tid not in threads:
                threads[tid] = []
                thread_order.append(tid)
            threads[tid].append(m)

        for tid in thread_order:
            msgs = threads[tid]
            multi = "thread-multi" if len(msgs) > 1 else ""
            body_parts.append(f'<div class="thread {multi}">')
            for m in msgs:
                body_parts.append(render_message(m, owner, c, attachment_url_prefix))
            body_parts.append('</div>')

    members_str = ", ".join(f"{m.name} &lt;{m.email}&gt;" for m in c.members) or "—"
    msg_count = len(c.messages)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(c.display_name)} — Google Chat archive</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<header class="site">
  <h1>{html.escape(c.display_name)}</h1>
  <span class="subtitle"><span class="kind-pill kind-{c.kind}">{c.kind}</span> {html.escape(c.group_id)}</span>
</header>
<main>
  <nav class="breadcrumbs"><a href="../index.html">← All conversations</a></nav>
  <div class="convo-meta">
    {msg_count} messages · Members: {members_str}
  </div>
  {''.join(body_parts)}
</main>
</body>
</html>
"""


def _render_conversation_body(c: Conversation, owner: Member | None) -> str:
    """Render the day/thread/message body of a conversation without page chrome."""
    sorted_msgs = sorted(c.messages,
                         key=lambda m: parse_chat_date(m.get("created_date", "")) or dt.datetime.max)

    by_day: dict[str, list[dict]] = {}
    day_order: list[str] = []
    for m in sorted_msgs:
        ts = parse_chat_date(m.get("created_date", ""))
        day = ts.strftime("%A, %B %d, %Y") if ts else "Unknown date"
        if day not in by_day:
            by_day[day] = []
            day_order.append(day)
        by_day[day].append(m)

    parts: list[str] = []
    for day in day_order:
        parts.append(f'<div class="day-divider">{html.escape(day)}</div>')
        threads: dict[str, list[dict]] = {}
        thread_order: list[str] = []
        for m in by_day[day]:
            tid = m.get("topic_id") or m.get("message_id", "")
            if tid not in threads:
                threads[tid] = []
                thread_order.append(tid)
            threads[tid].append(m)
        for tid in thread_order:
            msgs = threads[tid]
            multi = "thread-multi" if len(msgs) > 1 else ""
            parts.append(f'<div class="thread {multi}">')
            for m in msgs:
                # No attachment files are available in the web-upload context.
                parts.append(render_message(m, owner, c, attachment_url_prefix="#"))
            parts.append('</div>')
    return "".join(parts)


def render_single_page_html(conversations: list[Conversation], owner: Member | None) -> str:
    """
    Render all conversations into a single self-contained HTML file.

    The page is a two-pane SPA: a filterable sidebar listing all conversations
    and a main area that shows the selected one.  All CSS and JS are inlined so
    the file can be dropped into Google Drive (or any file host) and opened
    without a server.
    """
    owner_line = (f"{html.escape(owner.name)} &lt;{html.escape(owner.email)}&gt;"
                  if owner else "unknown")
    total_msgs = sum(len(c.messages) for c in conversations)

    sidebar_items: list[str] = []
    pages: list[str] = []

    for c in conversations:
        last = c.last_activity.strftime("%Y-%m-%d") if c.last_activity else "—"
        search_val = html.escape(" ".join([c.display_name, c.kind,
                                           " ".join(m.name for m in c.members)]).lower())
        sidebar_items.append(
            f'<li class="convo-item" data-id="{html.escape(c.slug)}" data-search="{search_val}">'
            f'<div class="convo-item-title">{html.escape(c.display_name)}</div>'
            f'<div class="convo-item-meta">'
            f'<span class="kind-pill kind-{c.kind}">{c.kind}</span>'
            f'<span>{len(c.messages)} msgs</span>'
            f'<span>{last}</span>'
            f'</div></li>'
        )

        members_str = html.escape(", ".join(m.name for m in c.members) or "—")
        body_html = _render_conversation_body(c, owner)
        pages.append(
            f'<div id="page-{html.escape(c.slug)}" class="convo-page">'
            f'<div class="convo-page-header">'
            f'<h2>{html.escape(c.display_name)}</h2>'
            f'<div class="convo-meta">'
            f'<span class="kind-pill kind-{c.kind}">{c.kind}</span> '
            f'{len(c.messages)} messages · {members_str}'
            f'</div></div>'
            f'{body_html}'
            f'</div>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Chat Archive</title>
<style>
{STYLE_CSS}
{SINGLE_PAGE_EXTRA_CSS}
</style>
</head>
<body>
<div id="app">
  <nav id="sidebar">
    <div id="sidebar-header">
      <div id="sidebar-title">Google Chat Archive</div>
      <div id="sidebar-meta">Owner: {owner_line}</div>
      <div id="sidebar-stats">{len(conversations)} conversations · {total_msgs} messages</div>
      <input id="search" type="search" placeholder="Filter conversations…" autocomplete="off">
    </div>
    <ul id="convo-list">{''.join(sidebar_items)}</ul>
  </nav>
  <main id="main">
    <div id="welcome">
      <h2>Google Chat Archive</h2>
      <p>Select a conversation from the sidebar.</p>
      <p class="meta">{len(conversations)} conversations · {total_msgs} messages</p>
    </div>
    {''.join(pages)}
  </main>
</div>
<script>{SINGLE_PAGE_JS}</script>
</body>
</html>
"""


def render_message(m: dict, owner: Member | None, c: Conversation,
                   attachment_url_prefix: str) -> str:
    creator = m.get("creator", {}) or {}
    author_name = creator.get("name", "(unknown)")
    author_email = creator.get("email", "")
    is_self = bool(owner and author_email == owner.email)
    self_class = "msg-self" if is_self else ""

    ts = parse_chat_date(m.get("created_date", ""))
    time_str = ts.strftime("%-I:%M:%S %p") if ts else m.get("created_date", "")

    body: list[str] = [f'<div class="message {self_class}">']
    body.append('<div class="msg-header">')
    body.append(f'<span class="msg-author">{html.escape(author_name)}</span>')
    body.append(f'<span class="msg-time">{html.escape(time_str)}</span>')
    body.append('</div>')

    quoted = m.get("quoted_message_metadata")
    if quoted:
        qa = (quoted.get("creator") or {}).get("name", "")
        qt = quoted.get("text", "") or ""
        body.append('<div class="msg-quote">')
        if qa:
            body.append(f'<span class="quote-author">↳ {html.escape(qa)}</span>')
        body.append(html.escape(qt))
        body.append('</div>')

    text = m.get("text", "") or ""
    annotations = m.get("annotations") or []
    if text:
        body.append(f'<div class="msg-text">{render_text_with_annotations(text, annotations)}</div>')

    # Link cards from URL and Drive annotations (rendered after text).
    for a in annotations:
        if "url_metadata" in a:
            um = a["url_metadata"] or {}
            url = ""
            url_obj = um.get("url") or {}
            url = (url_obj.get("private_do_not_access_or_else_safe_url_wrapped_value")
                   or um.get("url_wrapped_value") or "")
            title = um.get("title", url) or url
            snippet = um.get("snippet", "") or ""
            href = html.escape(url)
            body.append(
                f'<a class="link-card" href="{href}" rel="noopener noreferrer" target="_blank">'
                f'<span class="link-title">{html.escape(title)}</span>'
                f'<div class="link-snippet">{html.escape(snippet)}</div>'
                f'</a>'
            )
        elif "drive_metadata" in a:
            dm = a["drive_metadata"] or {}
            drive_id = dm.get("id", "")
            title = dm.get("title", "Drive file") or "Drive file"
            url = f"https://drive.google.com/file/d/{drive_id}/view" if drive_id else "#"
            body.append(
                f'<a class="link-card" href="{html.escape(url)}" rel="noopener noreferrer" target="_blank">'
                f'<span class="link-title">📄 {html.escape(title)}</span>'
                f'<div class="link-snippet">Google Drive · {html.escape(drive_id)}</div>'
                f'</a>'
            )

    # Attached files
    files = m.get("attached_files") or []
    if files:
        body.append('<div class="attachments">')
        for f in files:
            export_name = f.get("export_name", "")
            original_name = f.get("original_name", export_name)
            href = f"{attachment_url_prefix}/{c.slug}/{export_name}"
            ext = os.path.splitext(export_name)[1].lower()
            if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                body.append(f'<a href="{html.escape(href)}" target="_blank">'
                            f'<img src="{html.escape(href)}" alt="{html.escape(original_name)}"></a>')
            else:
                body.append(f'<a class="attachment-file" href="{html.escape(href)}" target="_blank">📎 {html.escape(original_name)}</a>')
        body.append('</div>')

    # Reactions
    reactions = m.get("reactions") or []
    if reactions:
        body.append('<div class="reactions">')
        for r in reactions:
            emoji = (r.get("emoji") or {}).get("unicode", "")
            count = len(r.get("reactor_emails") or [])
            tip = ", ".join(r.get("reactor_emails") or [])
            body.append(f'<span class="reaction" title="{html.escape(tip)}">{html.escape(emoji)} {count}</span>')
        body.append('</div>')

    body.append('</div>')
    return "".join(body)


# ---------------------------------------------------------------------------
# Site generation
# ---------------------------------------------------------------------------


def generate_static_site(takeout_dir: Path, output_dir: Path,
                         copy_attachments: bool = True) -> dict:
    chat_root = find_chat_root(takeout_dir)
    owner = load_user_info(chat_root)
    conversations = load_conversations(chat_root, owner)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "assets").mkdir(exist_ok=True)
    (output_dir / "conversations").mkdir(exist_ok=True)
    (output_dir / "files").mkdir(exist_ok=True)

    (output_dir / "assets" / "style.css").write_text(STYLE_CSS, encoding="utf-8")

    # Per-conversation pages
    for c in conversations:
        page = render_conversation_html(c, owner)
        (output_dir / "conversations" / f"{c.slug}.html").write_text(page, encoding="utf-8")

        if copy_attachments:
            # Copy any non-JSON files (images, etc.) into files/<slug>/
            target = output_dir / "files" / c.slug
            copied = 0
            for f in c.source_dir.iterdir():
                if f.is_file() and f.suffix.lower() not in {".json"}:
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target / f.name)
                    copied += 1

    # Index
    (output_dir / "index.html").write_text(render_index_html(conversations, owner),
                                           encoding="utf-8")

    return {
        "owner": dataclasses.asdict(owner) if owner else None,
        "conversation_count": len(conversations),
        "message_count": sum(len(c.messages) for c in conversations),
        "output_dir": str(output_dir),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Convert a Google Chat Takeout export into a browsable HTML site."
    )
    p.add_argument("takeout_dir",
                   help="Path to the extracted Takeout dir, OR the 'Google Chat' folder inside it.")
    p.add_argument("-o", "--output", default="./chat-archive",
                   help="Output directory for the static site (default: ./chat-archive)")
    p.add_argument("--no-attachments", action="store_true",
                   help="Skip copying attachment files into the output directory.")
    args = p.parse_args(argv)

    summary = generate_static_site(
        Path(args.takeout_dir).expanduser(),
        Path(args.output).expanduser(),
        copy_attachments=not args.no_attachments,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
