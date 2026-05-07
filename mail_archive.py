"""
mail_archive.py
---------------
Parse Gmail MBOX exports from Takeout and render a searchable header index.

Body content is not rendered (too large for a self-contained page); the
raw .mbox file is passed through for import into Thunderbird, Apple Mail,
or any MBOX-compatible client.

Parsing is done line-by-line for efficiency — only header sections are
read, not message bodies.
"""

from __future__ import annotations

import datetime as dt
import email.header
import email.utils
import html
import os
import re
from pathlib import Path

_MAX_MESSAGES = 10_000


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_mail_dir(takeout_dir: Path) -> Path | None:
    children = list(takeout_dir.iterdir()) if takeout_dir.is_dir() else []
    for candidate in [takeout_dir, *children]:
        if candidate.is_dir() and (candidate / 'Mail').is_dir():
            return candidate / 'Mail'
    for root, _dirs, files in os.walk(takeout_dir):
        if Path(root).name == 'Mail':
            if any(f.lower().endswith('.mbox') for f in files):
                return Path(root)
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _decode_hdr(value: str) -> str:
    if not value:
        return ''
    try:
        parts = email.header.decode_header(value)
        out = []
        for data, charset in parts:
            if isinstance(data, bytes):
                out.append(data.decode(charset or 'utf-8', errors='replace'))
            else:
                out.append(str(data))
        return ' '.join(out).strip()
    except Exception:
        return value


def _parse_date(s: str) -> tuple[dt.datetime, str]:
    if not s:
        return dt.datetime.min, ''
    try:
        d = email.utils.parsedate_to_datetime(s)
        return d, d.strftime(f'%b {d.day}, %Y')
    except Exception:
        return dt.datetime.min, s[:30]


def _parse_from(s: str) -> str:
    """Extract display name or email from a From header."""
    if not s:
        return ''
    try:
        name, addr = email.utils.parseaddr(_decode_hdr(s))
        return name or addr
    except Exception:
        return _decode_hdr(s)[:80]


def _parse_labels(s: str) -> list[str]:
    """Parse X-Gmail-Labels header: comma-separated, possibly quoted."""
    if not s:
        return []
    raw = _decode_hdr(s)
    return [lb.strip().strip('"') for lb in raw.split(',') if lb.strip()]


def load_messages(mail_dir: Path, limit: int = _MAX_MESSAGES) -> tuple[list[dict], int]:
    """
    Return (messages, total_scanned).  messages are sorted newest-first,
    capped at *limit*.  Each dict has: date_dt, date_str, from_, subject, labels.
    """
    mbox_files = [p for p in mail_dir.iterdir() if p.suffix.lower() == '.mbox']
    if not mbox_files:
        return [], 0

    mbox_path = mbox_files[0]  # typically one file
    messages: list[dict] = []
    total = 0

    current: dict = {}
    in_header = True
    current_key: str | None = None

    with open(mbox_path, 'r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if line.startswith('From '):
                if current:
                    messages.append(current)
                    total += 1
                current = {}
                in_header = True
                current_key = None
                continue

            if not in_header:
                continue
            if line in ('\n', '\r\n'):
                in_header = False
                continue

            # Header continuation (folded)
            if line[0] in (' ', '\t') and current_key:
                current[current_key] = current.get(current_key, '') + ' ' + line.strip()
                continue

            colon = line.find(':')
            if colon == -1:
                continue
            current_key = line[:colon].lower().strip()
            current[current_key] = line[colon + 1:].strip()

    if current:
        messages.append(current)
        total += 1

    # Parse and sort
    parsed: list[dict] = []
    for m in messages:
        date_dt, date_str = _parse_date(m.get('date', ''))
        parsed.append({
            'date_dt':   date_dt,
            'date_str':  date_str,
            'from_':     _parse_from(m.get('from', '')),
            'subject':   _decode_hdr(m.get('subject', '(no subject)')),
            'labels':    _parse_labels(m.get('x-gmail-labels', '')),
        })

    parsed.sort(key=lambda m: m['date_dt'], reverse=True)
    return parsed[:limit], total


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #f7f7f8; --panel: #fff; --border: #e3e3e7;
  --text: #1f2328; --muted: #6b7280; --accent: #1a73e8;
}
* { box-sizing: border-box; }
html, body {
  height: 100%; overflow: hidden; margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  font-size: 14px; line-height: 1.45; color: var(--text); background: var(--bg);
}
#app { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
#toolbar {
  padding: 12px 20px; background: var(--panel);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 16px; flex-shrink: 0;
}
#toolbar-title { font-weight: 700; font-size: 15px; white-space: nowrap; }
#toolbar-stats { font-size: 12px; color: var(--muted); white-space: nowrap; }
#search {
  flex: 1; max-width: 400px; padding: 7px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text);
}
#label-filter {
  padding: 6px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text);
}
#table-wrap { flex: 1; overflow-y: auto; }
table { width: 100%; border-collapse: collapse; }
thead th {
  position: sticky; top: 0; background: var(--panel);
  text-align: left; padding: 8px 14px; font-size: 11px;
  font-weight: 700; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); border-bottom: 1px solid var(--border);
}
tbody tr { border-bottom: 1px solid var(--border); }
tbody tr:hover { background: #fafafa; }
td { padding: 8px 14px; }
.col-date { white-space: nowrap; color: var(--muted); font-size: 12px; width: 110px; }
.col-from { width: 200px; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 200px; }
.col-subject { font-size: 13px; }
.col-labels { width: 1%; white-space: nowrap; }
.label-chip { font-size: 10px; padding: 1px 6px; border-radius: 999px; background: #e8f0fe; color: #1a56db; margin-left: 4px; }
#empty-msg { text-align: center; color: var(--muted); padding: 60px 20px; }
.mbox-link { font-size: 12px; color: var(--accent); }
.mbox-link:hover { text-decoration: underline; }
"""

_JS = r"""
(function () {
  var q = '';
  var lbl = '';

  function applyFilters() {
    var anyVisible = false;
    document.querySelectorAll('tbody tr').forEach(function (row) {
      var searchOk = !q || row.dataset.search.indexOf(q) !== -1;
      var lblOk    = !lbl || (row.dataset.labels || '').indexOf(lbl) !== -1;
      var show = searchOk && lblOk;
      row.style.display = show ? '' : 'none';
      if (show) anyVisible = true;
    });
    var em = document.getElementById('empty-msg');
    if (em) em.style.display = anyVisible ? 'none' : '';
  }

  var searchEl = document.getElementById('search');
  if (searchEl) {
    searchEl.addEventListener('input', function () {
      q = this.value.trim().toLowerCase();
      applyFilters();
    });
  }

  var lblEl = document.getElementById('label-filter');
  if (lblEl) {
    lblEl.addEventListener('change', function () {
      lbl = this.value;
      applyFilters();
    });
  }
})();
"""


def render_mail_html(messages: list[dict], total_scanned: int,
                     mbox_filenames: list[str] = ()) -> str:
    all_labels: list[str] = sorted({lb for m in messages for lb in m['labels']})

    label_opts = '<option value="">All labels</option>' + ''.join(
        f'<option value="{html.escape(lb)}">{html.escape(lb)}</option>'
        for lb in all_labels
    )

    rows: list[str] = []
    for m in messages:
        subj  = html.escape(m['subject'][:120] or '(no subject)')
        from_ = html.escape(m['from_'][:60])
        date  = html.escape(m['date_str'])
        chips = ''.join(
            f'<span class="label-chip">{html.escape(lb)}</span>'
            for lb in m['labels']
        )
        search_val = ' '.join([
            m['subject'].lower(),
            m['from_'].lower(),
            ' '.join(m['labels']).lower(),
            m['date_str'].lower(),
        ])
        rows.append(
            f'<tr data-search="{html.escape(search_val)}"'
            f' data-labels="{html.escape(",".join(m["labels"]))}">'
            f'<td class="col-date">{date}</td>'
            f'<td class="col-from">{from_}</td>'
            f'<td class="col-subject">{subj}</td>'
            f'<td class="col-labels">{chips}</td>'
            f'</tr>'
        )

    truncated = ''
    if total_scanned > len(messages):
        truncated = (
            f' · showing {len(messages):,} most recent of {total_scanned:,} total'
        )

    mbox_links = ''
    if mbox_filenames:
        links = ' '.join(
            f'<a class="mbox-link" href="mail/{html.escape(n)}" download>⬇ {html.escape(n)}</a>'
            for n in mbox_filenames
        )
        mbox_links = f'<span>Import: {links}</span>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gmail Archive</title>
<style>{_CSS}</style>
</head>
<body>
<div id="app">
  <div id="toolbar">
    <span id="toolbar-title">Gmail</span>
    <span id="toolbar-stats">{len(messages):,} messages{html.escape(truncated)}</span>
    <input id="search" type="search" placeholder="Search subject, sender, label…" autocomplete="off">
    <select id="label-filter">{label_opts}</select>
    {mbox_links}
  </div>
  <div id="table-wrap">
    <table>
      <thead><tr>
        <th>Date</th><th>From</th><th>Subject</th><th>Labels</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <div id="empty-msg" style="display:none">No messages match.</div>
  </div>
</div>
<script>{_JS}</script>
</body>
</html>"""
