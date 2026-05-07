"""
meet_archive.py
---------------
Parse Google Meet conference history CSV from Takeout and render a
searchable HTML table.
"""

from __future__ import annotations

import csv
import html
import io
import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Discovery & loading
# ---------------------------------------------------------------------------

def find_meet_dir(takeout_dir: Path) -> Path | None:
    children = list(takeout_dir.iterdir()) if takeout_dir.is_dir() else []
    for candidate in [takeout_dir, *children]:
        if candidate.is_dir() and (candidate / 'Google Meet').is_dir():
            return candidate / 'Google Meet'
    for root, _dirs, _files in os.walk(takeout_dir):
        if Path(root).name == 'Google Meet':
            return Path(root)
    return None


def load_meetings(meet_dir: Path) -> list[dict]:
    csv_path = meet_dir / 'ConferenceHistory' / 'conference_history_records.csv'
    if not csv_path.exists():
        # Try direct child
        for p in meet_dir.rglob('conference_history_records.csv'):
            csv_path = p
            break
        else:
            return []

    meetings: list[dict] = []
    try:
        text = csv_path.read_text(encoding='utf-8', errors='replace')
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            start = row.get('Start Time', '').strip()
            end   = row.get('End Time', '').strip()
            dur   = row.get('Duration', '').strip()
            code  = row.get('Meeting Code', '').strip()
            state = row.get('Participation State', '').strip()
            direction = row.get('Call Direction', '').strip()
            media = row.get('Meeting Media Type', '').strip()
            if not start:
                continue
            meetings.append({
                'start':     start,
                'end':       end,
                'duration':  dur,
                'code':      code,
                'state':     state,
                'direction': direction,
                'media':     media,
            })
    except (OSError, csv.Error):
        return []

    # Sort newest first (Start Time is "YYYY-MM-DD HH:MM:SS UTC")
    meetings.sort(key=lambda m: m['start'], reverse=True)
    return meetings


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
#toolbar-title { font-weight: 700; font-size: 15px; }
#toolbar-stats { font-size: 12px; color: var(--muted); }
#search {
  flex: 1; max-width: 360px; padding: 7px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text);
}
#table-wrap { flex: 1; overflow-y: auto; }
table { width: 100%; border-collapse: collapse; }
thead th {
  position: sticky; top: 0; background: var(--panel);
  text-align: left; padding: 8px 14px; font-size: 11px;
  font-weight: 700; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap;
}
tbody tr { border-bottom: 1px solid var(--border); }
tbody tr:hover { background: #fafafa; }
td { padding: 8px 14px; font-size: 13px; }
.col-date { white-space: nowrap; color: var(--muted); font-size: 12px; }
.col-dur  { white-space: nowrap; font-family: monospace; font-size: 12px; }
.col-code { font-family: monospace; font-size: 12px; color: var(--muted); }
.badge {
  font-size: 10px; font-weight: 600; padding: 2px 7px; border-radius: 999px;
  display: inline-block;
}
.badge-participated { background: #d1fae5; color: #065f46; }
.badge-missed       { background: #fee2e2; color: #991b1b; }
.badge-other        { background: var(--bg); color: var(--muted); }
#empty-msg { text-align: center; color: var(--muted); padding: 60px 20px; }
"""

_JS = r"""
(function () {
  var q = '';
  function applyFilters() {
    var any = false;
    document.querySelectorAll('tbody tr').forEach(function (row) {
      var ok = !q || row.dataset.search.indexOf(q) !== -1;
      row.style.display = ok ? '' : 'none';
      if (ok) any = true;
    });
    document.getElementById('empty-msg').style.display = any ? 'none' : '';
  }
  var el = document.getElementById('search');
  if (el) el.addEventListener('input', function () {
    q = this.value.trim().toLowerCase(); applyFilters();
  });
})();
"""


def _state_badge(state: str) -> str:
    s = state.upper()
    if 'PARTICIPATED' in s:
        return f'<span class="badge badge-participated">Joined</span>'
    if 'MISSED' in s or 'DECLINED' in s:
        return f'<span class="badge badge-missed">{html.escape(state)}</span>'
    return f'<span class="badge badge-other">{html.escape(state)}</span>'


def _fmt_date(s: str) -> str:
    """'2026-05-06 14:01:55 UTC' → 'May 6, 2026 · 14:01 UTC'"""
    if not s:
        return ''
    try:
        date_part, _, rest = s.partition(' ')
        time_part = rest.split()[0] if rest else ''
        y, mo, d = date_part.split('-')
        import datetime as dt
        month = dt.date(int(y), int(mo), int(d)).strftime('%b')
        hm = ':'.join(time_part.split(':')[:2])
        return f'{month} {int(d)}, {y} · {hm} UTC'
    except Exception:
        return s


def render_meet_html(meetings: list[dict]) -> str:
    rows: list[str] = []
    for m in meetings:
        date_str = _fmt_date(m['start'])
        search_val = ' '.join([
            m['start'].lower(),
            m['code'].lower(),
            m['state'].lower(),
            m['duration'].lower(),
        ])
        rows.append(
            f'<tr data-search="{html.escape(search_val)}">'
            f'<td class="col-date">{html.escape(date_str)}</td>'
            f'<td class="col-dur">{html.escape(m["duration"])}</td>'
            f'<td class="col-code">{html.escape(m["code"])}</td>'
            f'<td>{_state_badge(m["state"])}</td>'
            f'</tr>'
        )

    total_participated = sum(
        1 for m in meetings if 'PARTICIPATED' in m['state'].upper()
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Meet History</title>
<style>{_CSS}</style>
</head>
<body>
<div id="app">
  <div id="toolbar">
    <span id="toolbar-title">Meet History</span>
    <span id="toolbar-stats">
      {len(meetings)} meetings · {total_participated} joined
    </span>
    <input id="search" type="search" placeholder="Search date, code, status…" autocomplete="off">
  </div>
  <div id="table-wrap">
    <table>
      <thead><tr>
        <th>Date</th><th>Duration</th><th>Meeting Code</th><th>Status</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <div id="empty-msg" style="display:none">No meetings match.</div>
  </div>
</div>
<script>{_JS}</script>
</body>
</html>"""
