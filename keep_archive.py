"""
keep_archive.py
---------------
Parse Google Keep JSON exports from Takeout and render a single-page
HTML archive that preserves note colours, pin/archive state, and
checkbox list items.

Takeout layout
--------------
    Keep/
        <title or timestamp>.json   one per note
        <title or timestamp>.html   duplicate content, not used

JSON schema (observed 2026)
---------------------------
    color                  : str  DEFAULT | RED | ORANGE | YELLOW | GREEN |
                                   TEAL | BLUE | CERULEAN | PURPLE | PINK |
                                   GRAY | WHITE | BROWN
    isTrashed              : bool
    isPinned               : bool
    isArchived             : bool
    title                  : str  (may be empty)
    textContent            : str  (text notes only)
    textContentHtml        : str  (text notes — inline-styled HTML, not used)
    listContent            : list[{text, textHtml, isChecked}]  (list notes)
    userEditedTimestampUsec: int  microseconds since Unix epoch
    createdTimestampUsec   : int  microseconds since Unix epoch
    labels                 : list[{name}]  (optional)
    attachments            : list[{filePath, mimetype}]  (optional)
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
import json
import os
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Colour map  (Keep name → CSS hex)
# ---------------------------------------------------------------------------

_COLORS: dict[str, str] = {
    'DEFAULT':  '#ffffff',
    'WHITE':    '#ffffff',
    'RED':      '#f28b82',
    'ORANGE':   '#fbbc04',
    'YELLOW':   '#fff475',
    'GREEN':    '#ccff90',
    'TEAL':     '#a7ffeb',
    'BLUE':     '#cbf0f8',
    'CERULEAN': '#aecbfa',
    'PURPLE':   '#d7aefb',
    'PINK':     '#fdcfe8',
    'GRAY':     '#e6e6e6',
    'BROWN':    '#e6c9a8',
}

# Colours dark enough to need a darker border (not just --border)
_DARK_BG = {'RED', 'ORANGE', 'YELLOW', 'GREEN', 'TEAL', 'BLUE',
            'CERULEAN', 'PURPLE', 'PINK', 'BROWN'}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class KeepNote:
    title: str
    text_content: str
    list_content: list[dict]          # [{text, isChecked}]
    color: str                        # normalised Keep colour name
    is_pinned: bool
    is_archived: bool
    is_trashed: bool
    labels: list[str]
    created_usec: int
    edited_usec: int
    attachments: list[dict]

    @property
    def is_list(self) -> bool:
        return bool(self.list_content)

    @property
    def display_title(self) -> str:
        return self.title.strip() or '(untitled)'

    @property
    def edited_dt(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(
            self.edited_usec / 1_000_000, tz=dt.timezone.utc)

    @property
    def bg_color(self) -> str:
        return _COLORS.get(self.color.upper(), '#ffffff')

    @property
    def border_color(self) -> str:
        if self.color.upper() in _DARK_BG:
            return 'rgba(0,0,0,.12)'
        return '#e3e3e7'

    @property
    def sort_key(self) -> tuple:
        # Pinned first, then newest-edited first
        return (not self.is_pinned, -self.edited_usec)


# ---------------------------------------------------------------------------
# Discovery & loading
# ---------------------------------------------------------------------------

def find_keep_dir(takeout_dir: Path) -> Path | None:
    children = list(takeout_dir.iterdir()) if takeout_dir.is_dir() else []
    for candidate in [takeout_dir, *children]:
        if candidate.is_dir() and (candidate / 'Keep').is_dir():
            return candidate / 'Keep'
    for root, _dirs, files in os.walk(takeout_dir):
        if Path(root).name == 'Keep':
            if any(f.lower().endswith('.json') for f in files):
                return Path(root)
    return None


def load_notes(keep_dir: Path) -> list[KeepNote]:
    notes: list[KeepNote] = []
    for path in sorted(keep_dir.iterdir()):
        if path.suffix.lower() != '.json':
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue

        raw_labels = data.get('labels') or []
        labels = [lb.get('name', '') for lb in raw_labels if lb.get('name')]

        notes.append(KeepNote(
            title=data.get('title', ''),
            text_content=data.get('textContent', ''),
            list_content=data.get('listContent') or [],
            color=data.get('color', 'DEFAULT'),
            is_pinned=bool(data.get('isPinned')),
            is_archived=bool(data.get('isArchived')),
            is_trashed=bool(data.get('isTrashed')),
            labels=labels,
            created_usec=data.get('createdTimestampUsec', 0),
            edited_usec=data.get('userEditedTimestampUsec', 0),
            attachments=data.get('attachments') or [],
        ))

    notes.sort(key=lambda n: n.sort_key)
    return notes


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #f7f7f8; --panel: #ffffff; --border: #e3e3e7;
  --text: #1f2328; --muted: #6b7280; --accent: #1a73e8;
}
* { box-sizing: border-box; }
html, body {
  height: 100%; overflow: hidden; margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  font-size: 14px; line-height: 1.5; color: var(--text); background: var(--bg);
}
a { color: var(--accent); text-decoration: none; }

/* Layout */
#app { display: flex; height: 100vh; overflow: hidden; }
#sidebar {
  width: 220px; flex-shrink: 0;
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  background: var(--panel); overflow: hidden;
}
#sidebar-header { padding: 14px 16px; border-bottom: 1px solid var(--border); }
#sidebar-title { font-weight: 700; font-size: 15px; margin-bottom: 2px; }
#sidebar-stats { font-size: 11px; color: var(--muted); line-height: 1.5; margin-bottom: 8px; }
#search {
  display: block; width: 100%;
  padding: 7px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text);
}
#view-tabs { display: flex; flex-direction: column; gap: 2px; padding: 10px 10px; border-bottom: 1px solid var(--border); }
.view-tab {
  padding: 7px 10px; border-radius: 6px; cursor: pointer;
  font-size: 13px; color: var(--muted); border: none; background: none;
  text-align: left;
}
.view-tab:hover { background: var(--bg); color: var(--text); }
.view-tab.active { background: #e8f0fe; color: var(--accent); font-weight: 600; }
#label-section { padding: 10px 10px; overflow-y: auto; flex: 1; }
#label-section-title { font-size: 10px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; padding: 0 6px 6px; }
.label-item {
  padding: 6px 10px; border-radius: 6px; cursor: pointer;
  font-size: 13px; display: flex; align-items: center; justify-content: space-between;
}
.label-item:hover { background: var(--bg); }
.label-item.active { background: #e8f0fe; color: var(--accent); font-weight: 600; }
.label-count { font-size: 11px; color: var(--muted); }

/* Notes grid */
#main { flex: 1; overflow-y: auto; padding: 20px; background: var(--bg); }
#notes-grid { columns: 3 260px; column-gap: 12px; }
#empty-msg { text-align: center; color: var(--muted); margin-top: 60px; font-size: 14px; }

/* Note cards */
.note-card {
  break-inside: avoid; margin-bottom: 12px;
  border-radius: 8px; border: 1px solid var(--border);
  padding: 14px 16px; position: relative;
  transition: box-shadow .15s;
}
.note-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.1); }
.note-pin { position: absolute; top: 10px; right: 12px; font-size: 14px; opacity: .6; }
.note-title { font-weight: 600; font-size: 14px; margin-bottom: 8px; padding-right: 20px; }
.note-body { font-size: 13px; white-space: pre-wrap; word-wrap: break-word; }
.note-list { list-style: none; margin: 0; padding: 0; font-size: 13px; }
.note-list-item { display: flex; gap: 8px; align-items: baseline; padding: 2px 0; }
.note-list-item.checked { color: var(--muted); text-decoration: line-through; }
.note-footer { margin-top: 10px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 4px; }
.note-labels { display: flex; gap: 4px; flex-wrap: wrap; }
.note-label { font-size: 11px; padding: 1px 7px; border-radius: 999px; background: rgba(0,0,0,.08); }
.note-date { font-size: 11px; color: var(--muted); }
.note-status-badge {
  font-size: 10px; font-weight: 600; padding: 2px 7px; border-radius: 999px;
  background: rgba(0,0,0,.07); color: rgba(0,0,0,.5);
  position: absolute; top: 10px; left: 12px;
}
.note-attachment { font-size: 11px; color: var(--muted); margin-top: 6px; }
"""

_JS = r"""
(function () {
  var activeView = 'active';
  var activeLabel = null;
  var activeQuery = '';

  function applyFilters() {
    var q = activeQuery;
    var anyVisible = false;
    document.querySelectorAll('.note-card').forEach(function (card) {
      var archived = card.dataset.archived === '1';
      var trashed  = card.dataset.trashed  === '1';

      var viewOk = (
        activeView === 'active'   && !archived && !trashed ||
        activeView === 'archived' &&  archived && !trashed ||
        activeView === 'trashed'  &&  trashed               ||
        activeView === 'all'      && !trashed
      );
      var labelOk = !activeLabel ||
        (card.dataset.labels || '').split(',').indexOf(activeLabel) !== -1;
      var searchOk = !q || card.dataset.search.indexOf(q) !== -1;

      var show = viewOk && labelOk && searchOk;
      card.style.display = show ? '' : 'none';
      if (show) anyVisible = true;
    });
    document.getElementById('empty-msg').style.display = anyVisible ? 'none' : '';
  }

  document.querySelectorAll('.view-tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      document.querySelector('.view-tab.active').classList.remove('active');
      this.classList.add('active');
      activeView = this.dataset.view;
      applyFilters();
    });
  });

  document.querySelectorAll('.label-item').forEach(function (item) {
    item.addEventListener('click', function () {
      var prev = document.querySelector('.label-item.active');
      if (prev) prev.classList.remove('active');
      if (activeLabel === this.dataset.label) {
        activeLabel = null; // toggle off
      } else {
        this.classList.add('active');
        activeLabel = this.dataset.label;
      }
      applyFilters();
    });
  });

  var searchEl = document.getElementById('search');
  if (searchEl) {
    searchEl.addEventListener('input', function () {
      activeQuery = this.value.trim().toLowerCase();
      applyFilters();
    });
  }

  applyFilters(); // apply initial "active" view filter
})();
"""


def _render_card(note: KeepNote) -> str:
    bg = note.bg_color
    border = note.border_color

    # Search index text
    search_parts = [note.display_title.lower()]
    if note.text_content:
        search_parts.append(note.text_content.lower())
    for item in note.list_content:
        t = item.get('text', '')
        if t:
            search_parts.append(t.lower())
    for lb in note.labels:
        search_parts.append(lb.lower())

    labels_data = ','.join(note.labels)

    out = [
        f'<div class="note-card"'
        f' style="background:{bg};border-color:{border}"'
        f' data-archived="{"1" if note.is_archived else "0"}"'
        f' data-trashed="{"1" if note.is_trashed else "0"}"'
        f' data-pinned="{"1" if note.is_pinned else "0"}"'
        f' data-labels="{html.escape(labels_data)}"'
        f' data-search="{html.escape(" ".join(search_parts))}">'
    ]

    # Status badge (archived / trashed) — top-left
    if note.is_trashed:
        out.append('<div class="note-status-badge">Trash</div>')
    elif note.is_archived:
        out.append('<div class="note-status-badge">Archived</div>')

    # Pin indicator — top-right
    if note.is_pinned:
        out.append('<div class="note-pin">📌</div>')

    # Title
    if note.title.strip():
        out.append(f'<div class="note-title">{html.escape(note.title.strip())}</div>')

    # Body: text or checklist
    if note.is_list:
        # Split into unchecked and checked groups
        unchecked = [i for i in note.list_content if not i.get('isChecked')]
        checked   = [i for i in note.list_content if i.get('isChecked')]
        out.append('<ul class="note-list">')
        for item in unchecked:
            text = html.escape(item.get('text', ''))
            out.append(f'<li class="note-list-item unchecked">☐ {text}</li>')
        for item in checked:
            text = html.escape(item.get('text', ''))
            out.append(f'<li class="note-list-item checked">☑ {text}</li>')
        out.append('</ul>')
    elif note.text_content:
        out.append(f'<div class="note-body">{html.escape(note.text_content)}</div>')

    # Attachment notice (images/audio not embedded — just named)
    if note.attachments:
        names = ', '.join(
            a.get('filePath', '?').split('/')[-1] for a in note.attachments
        )
        out.append(f'<div class="note-attachment">📎 {html.escape(names)}</div>')

    # Footer: labels + date
    date_str = note.edited_dt.strftime(f'%b {note.edited_dt.day}, %Y')
    footer_parts = []
    if note.labels:
        label_chips = ''.join(
            f'<span class="note-label">{html.escape(lb)}</span>'
            for lb in note.labels
        )
        footer_parts.append(f'<div class="note-labels">{label_chips}</div>')
    footer_parts.append(f'<div class="note-date">{date_str}</div>')
    out.append(f'<div class="note-footer">{"".join(footer_parts)}</div>')

    out.append('</div>')
    return ''.join(out)


def render_keep_html(notes: list[KeepNote]) -> str:
    """Render all Keep notes into a single self-contained HTML file."""
    active   = [n for n in notes if not n.is_archived and not n.is_trashed]
    archived = [n for n in notes if n.is_archived and not n.is_trashed]
    trashed  = [n for n in notes if n.is_trashed]

    all_labels: list[str] = sorted({lb for n in notes for lb in n.labels})
    label_counts = {
        lb: sum(1 for n in notes if lb in n.labels and not n.is_trashed)
        for lb in all_labels
    }

    # Sidebar label section (omit if no labels at all)
    if all_labels:
        label_items = ''.join(
            f'<div class="label-item" data-label="{html.escape(lb)}">'
            f'{html.escape(lb)} <span class="label-count">{label_counts[lb]}</span></div>'
            for lb in all_labels
        )
        label_section = (
            f'<div id="label-section">'
            f'<div id="label-section-title">Labels</div>'
            f'{label_items}</div>'
        )
    else:
        label_section = ''

    # View tab counts
    tabs = [
        ('active',   f'Notes ({len(active)})'),
        ('archived', f'Archived ({len(archived)})'),
        ('trashed',  f'Trash ({len(trashed)})'),
        ('all',      f'All ({len(notes)})'),
    ]
    tab_html = ''.join(
        f'<button class="view-tab{"  active" if v == "active" else ""}" data-view="{v}">{label}</button>'
        for v, label in tabs
    )

    cards_html = ''.join(_render_card(n) for n in notes)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Keep Archive</title>
<style>{_CSS}</style>
</head>
<body>
<div id="app">
  <nav id="sidebar">
    <div id="sidebar-header">
      <div id="sidebar-title">Keep Archive</div>
      <div id="sidebar-stats">{len(active)} active · {len(archived)} archived · {len(trashed)} trash</div>
      <input id="search" type="search" placeholder="Search notes…" autocomplete="off">
    </div>
    <div id="view-tabs">{tab_html}</div>
    {label_section}
  </nav>
  <main id="main">
    <div id="notes-grid">{cards_html}</div>
    <div id="empty-msg" style="display:none">No notes match.</div>
  </main>
</div>
<script>{_JS}</script>
</body>
</html>
"""
