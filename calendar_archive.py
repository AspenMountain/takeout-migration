"""
calendar_archive.py
-------------------
Parse Google Calendar ICS exports from a Takeout archive and render a
single-page HTML archive.

Takeout layout
--------------
    Calendar/
        <email>.ics                       primary calendar
        <email>.appointment_schedule.ics  booking-page events (optional)
        meet_settings.json                ignored

ICS fields used
---------------
    SUMMARY, DTSTART, DTEND, DESCRIPTION, LOCATION, STATUS,
    ORGANIZER (CN + mailto), ATTENDEE (CN + mailto),
    RRULE, X-GOOGLE-CONFERENCE, UID
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
import os
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class CalEvent:
    uid: str
    summary: str
    dtstart: dt.datetime | dt.date
    dtend: dt.datetime | dt.date | None
    is_all_day: bool
    description: str
    location: str
    organizer_name: str
    organizer_email: str
    attendees: list[tuple[str, str]]   # (display_name, email)
    is_recurring: bool
    rrule: str
    meet_link: str
    status: str                        # CONFIRMED | CANCELLED | TENTATIVE
    calendar_slug: str

    @property
    def sort_key(self) -> dt.datetime:
        s = self.dtstart
        if isinstance(s, dt.datetime):
            return s.replace(tzinfo=None) if s.tzinfo else s
        return dt.datetime(s.year, s.month, s.day)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_calendar_dir(takeout_dir: Path) -> Path | None:
    children = list(takeout_dir.iterdir()) if takeout_dir.is_dir() else []
    for candidate in [takeout_dir, *children]:
        if candidate.is_dir() and (candidate / "Calendar").is_dir():
            return candidate / "Calendar"
    for root, _dirs, files in os.walk(takeout_dir):
        if Path(root).name == "Calendar":
            if any(f.lower().endswith(".ics") for f in files):
                return Path(root)
    return None


# ---------------------------------------------------------------------------
# ICS parsing
# ---------------------------------------------------------------------------

def _unfold(text: str) -> str:
    """Remove ICS line folding (CRLF + leading space/tab → nothing)."""
    return re.sub(r'\r?\n[ \t]', '', text)


def _unescape(s: str) -> str:
    return (s.replace('\\n', '\n').replace('\\N', '\n')
             .replace('\\,', ',').replace('\\;', ';')
             .replace('\\\\', '\\'))


def _parse_prop(line: str) -> tuple[str, dict[str, str], str]:
    """Return (property_name, params_dict, value) for one ICS property line."""
    name_params, _, value = line.partition(':')
    value = _unescape(value)
    parts = name_params.split(';')
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for p in parts[1:]:
        if '=' in p:
            k, _, v = p.partition('=')
            params[k.strip().upper()] = v.strip().strip('"')
    return name, params, value


def _parse_dt(value: str, params: dict[str, str]) -> tuple[dt.datetime | dt.date, bool]:
    """Parse an ICS date/datetime value. Returns (parsed, is_all_day)."""
    is_date = params.get('VALUE', '').upper() == 'DATE' or ('T' not in value and len(value) == 8)
    if is_date:
        try:
            return dt.date(int(value[:4]), int(value[4:6]), int(value[6:8])), True
        except ValueError:
            return dt.date.today(), True
    try:
        y, mo, d = int(value[0:4]), int(value[4:6]), int(value[6:8])
        h, mi = int(value[9:11]), int(value[11:13])
        s = int(value[13:15]) if len(value) >= 15 else 0
        tz = dt.timezone.utc if value.endswith('Z') else None
        return dt.datetime(y, mo, d, h, mi, s, tzinfo=tz), False
    except (ValueError, IndexError):
        return dt.datetime.now(dt.timezone.utc), False


def _rrule_label(rrule: str) -> str:
    """Convert a raw RRULE string to a short human label like 'Weekly (Mon, Wed)'."""
    parts = {}
    for token in rrule.split(';'):
        if '=' in token:
            k, _, v = token.partition('=')
            parts[k.upper()] = v
    freq = parts.get('FREQ', '').capitalize()
    interval = int(parts.get('INTERVAL', 1))
    day_map = {'MO': 'Mon', 'TU': 'Tue', 'WE': 'Wed', 'TH': 'Thu',
               'FR': 'Fri', 'SA': 'Sat', 'SU': 'Sun'}
    byday = parts.get('BYDAY', '')
    # BYDAY can be "MO,WE" or "2SU" (nth weekday of month) — extract day codes
    day_codes = re.findall(r'[A-Z]{2}', byday)
    day_names = ', '.join(day_map.get(d, d) for d in day_codes if d in day_map)

    label = f'Every {interval} {freq.lower()}s' if interval > 1 else freq
    if day_names:
        label += f' ({day_names})'
    return label or 'Recurring'


def parse_ics(text: str, calendar_slug: str) -> list[CalEvent]:
    text = _unfold(text)
    events: list[CalEvent] = []
    cur: dict | None = None

    for line in text.splitlines():
        if not line:
            continue
        if line == 'BEGIN:VEVENT':
            cur = {'ATTENDEE': []}
        elif line == 'END:VEVENT' and cur is not None:
            events.append(_make_event(cur, calendar_slug))
            cur = None
        elif cur is not None and ':' in line:
            try:
                name, params, value = _parse_prop(line)
            except (ValueError, IndexError):
                continue

            if name == 'ATTENDEE':
                cn = params.get('CN', '')
                email = value.removeprefix('mailto:')
                cur['ATTENDEE'].append((cn, email))
            elif name == 'ORGANIZER':
                cur['ORGANIZER_NAME'] = params.get('CN', '')
                cur['ORGANIZER_EMAIL'] = value.removeprefix('mailto:')
            elif name in ('DTSTART', 'DTEND'):
                parsed, is_date = _parse_dt(value, params)
                cur[name] = parsed
                if name == 'DTSTART':
                    cur['IS_ALL_DAY'] = is_date
            else:
                cur.setdefault(name, value)

    return events


def _make_event(cur: dict, calendar_slug: str) -> CalEvent:
    return CalEvent(
        uid=cur.get('UID', ''),
        summary=cur.get('SUMMARY', '(no title)'),
        dtstart=cur.get('DTSTART', dt.datetime.now(dt.timezone.utc)),
        dtend=cur.get('DTEND'),
        is_all_day=cur.get('IS_ALL_DAY', False),
        description=cur.get('DESCRIPTION', ''),
        location=cur.get('LOCATION', ''),
        organizer_name=cur.get('ORGANIZER_NAME', ''),
        organizer_email=cur.get('ORGANIZER_EMAIL', ''),
        attendees=cur.get('ATTENDEE', []),
        is_recurring='RRULE' in cur,
        rrule=cur.get('RRULE', ''),
        meet_link=cur.get('X-GOOGLE-CONFERENCE', ''),
        status=cur.get('STATUS', 'CONFIRMED'),
        calendar_slug=calendar_slug,
    )


def load_calendars(calendar_dir: Path) -> list[dict]:
    """Load all .ics files; return list of {name, slug, events} dicts."""
    calendars = []
    for path in sorted(calendar_dir.iterdir()):
        if path.suffix.lower() != '.ics':
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        stem = path.stem
        if '.appoint' in stem.lower():
            base = stem[:stem.lower().index('.appoint')]
            name = f'{base} (Appointments)'
        else:
            name = stem
        slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
        events = parse_ics(text, slug)
        calendars.append({'name': name, 'slug': slug, 'events': events})
    return calendars


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
  font-size: 14px; line-height: 1.45; color: var(--text); background: var(--bg);
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
#app { display: flex; height: 100vh; overflow: hidden; }
#sidebar {
  width: 260px; flex-shrink: 0;
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  background: var(--panel); overflow: hidden;
}
#sidebar-header { padding: 14px 16px; border-bottom: 1px solid var(--border); }
#sidebar-title { font-weight: 700; font-size: 15px; margin-bottom: 2px; }
#sidebar-stats { font-size: 11px; color: var(--muted); line-height: 1.5; }
#search {
  display: block; width: 100%; margin-top: 8px;
  padding: 7px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text);
}
#filters {
  padding: 10px 16px; border-bottom: 1px solid var(--border);
  font-size: 13px; color: var(--muted);
}
#filters label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
#filters input[type=checkbox] { accent-color: var(--accent); }
#cal-list { list-style: none; margin: 0; padding: 0; overflow-y: auto; flex: 1; }
.cal-item {
  padding: 9px 16px; cursor: pointer;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
  font-size: 13px;
}
.cal-item:hover { background: var(--bg); }
.cal-item.active { background: #e8f0fe; font-weight: 600; }
.cal-count { font-size: 11px; color: var(--muted); }
.cal-item.active .cal-count { color: var(--accent); }
#main { flex: 1; overflow-y: auto; padding: 24px; background: var(--bg); }
.month-section { margin-bottom: 28px; }
.month-header {
  font-size: 11px; font-weight: 700; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.08em;
  padding-bottom: 8px; margin-bottom: 10px;
  border-bottom: 1px solid var(--border);
}
.event-card {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 16px; margin-bottom: 8px;
}
.event-card:hover { border-color: #b0c4e8; }
.event-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 5px; }
.event-title { font-weight: 600; font-size: 14px; flex: 1; }
.event-cancelled .event-title { text-decoration: line-through; color: var(--muted); }
.event-badges { display: flex; gap: 4px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }
.badge { font-size: 10px; font-weight: 600; padding: 2px 7px; border-radius: 999px; white-space: nowrap; }
.badge-recurring { background: #e8f0fe; color: #1a56db; }
.badge-cancelled { background: #fef2f2; color: #b91c1c; }
.badge-tentative { background: #fef9c3; color: #854d0e; }
.event-when, .event-attendees, .event-location, .event-meet { font-size: 12px; color: var(--muted); margin-top: 3px; }
.event-meet a { color: var(--accent); font-weight: 500; }
.event-description {
  margin-top: 8px; font-size: 12px; color: #4b5563;
  white-space: pre-wrap; word-wrap: break-word;
  border-top: 1px solid var(--border); padding-top: 7px;
}
"""

_JS = r"""
(function () {
  var activeCal = 'all';
  var activeQuery = '';
  var recurringOnly = false;

  function applyFilters() {
    var q = activeQuery;
    document.querySelectorAll('.event-card').forEach(function (card) {
      var calOk = activeCal === 'all' || card.dataset.cal === activeCal;
      var searchOk = !q || card.dataset.search.indexOf(q) !== -1;
      var recOk = !recurringOnly || card.dataset.recurring === '1';
      card.style.display = calOk && searchOk && recOk ? '' : 'none';
    });
    // Hide month sections that have no visible events.
    document.querySelectorAll('.month-section').forEach(function (section) {
      var cards = section.querySelectorAll('.event-card');
      var any = false;
      for (var i = 0; i < cards.length; i++) {
        if (cards[i].style.display !== 'none') { any = true; break; }
      }
      section.style.display = any ? '' : 'none';
    });
  }

  document.querySelectorAll('.cal-item').forEach(function (item) {
    item.addEventListener('click', function () {
      var prev = document.querySelector('.cal-item.active');
      if (prev) prev.classList.remove('active');
      this.classList.add('active');
      activeCal = this.dataset.cal;
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

  var recEl = document.getElementById('recurring-only');
  if (recEl) {
    recEl.addEventListener('change', function () {
      recurringOnly = this.checked;
      applyFilters();
    });
  }
})();
"""


def _fmt_time(d: dt.datetime | dt.date, is_all_day: bool) -> str:
    day = f"{d.strftime('%a, %b')} {d.day}, {d.year}"
    if is_all_day or not isinstance(d, dt.datetime):
        return day
    h = d.hour % 12 or 12
    ampm = 'AM' if d.hour < 12 else 'PM'
    suffix = ' UTC' if d.tzinfo == dt.timezone.utc else ''
    return f'{day} · {h}:{d.minute:02d} {ampm}{suffix}'


def _fmt_when(event: CalEvent) -> str:
    start = _fmt_time(event.dtstart, event.is_all_day)
    if not event.dtend or event.is_all_day:
        return start
    if isinstance(event.dtend, dt.datetime):
        h = event.dtend.hour % 12 or 12
        ampm = 'AM' if event.dtend.hour < 12 else 'PM'
        suffix = ' UTC' if event.dtend.tzinfo == dt.timezone.utc else ''
        return f'{start} – {h}:{event.dtend.minute:02d} {ampm}{suffix}'
    return start


def _clean_desc(desc: str) -> str:
    """Strip Google Meet boilerplate and HTML tags; truncate."""
    cut = desc.find('-::~:')
    if cut != -1:
        desc = desc[:cut]
    desc = re.sub(r'<[^>]+>', ' ', desc)
    desc = re.sub(r'\s+', ' ', desc).strip()
    return (desc[:300] + '…') if len(desc) > 300 else desc


def _render_card(event: CalEvent) -> str:
    search_parts = [event.summary.lower()]
    if event.organizer_name:
        search_parts.append(event.organizer_name.lower())
    for name, email in event.attendees:
        if name:
            search_parts.append(name.lower())
    if event.location:
        search_parts.append(event.location.lower())
    desc = _clean_desc(event.description)
    if desc:
        search_parts.append(desc[:200].lower())

    cancelled_cls = ' event-cancelled' if event.status == 'CANCELLED' else ''
    rec_attr = ' data-recurring="1"' if event.is_recurring else ''

    out = [
        f'<div class="event-card{cancelled_cls}"'
        f' data-cal="{html.escape(event.calendar_slug)}"'
        f' data-search="{html.escape(" ".join(search_parts))}"{rec_attr}>'
    ]

    # Title + badges
    out.append('<div class="event-header">')
    out.append(f'<div class="event-title">{html.escape(event.summary)}</div>')
    badges = []
    if event.is_recurring:
        label = _rrule_label(event.rrule) if event.rrule else 'Recurring'
        badges.append(
            f'<span class="badge badge-recurring" title="{html.escape(event.rrule)}">'
            f'↻ {html.escape(label)}</span>'
        )
    if event.status == 'CANCELLED':
        badges.append('<span class="badge badge-cancelled">Cancelled</span>')
    elif event.status == 'TENTATIVE':
        badges.append('<span class="badge badge-tentative">Tentative</span>')
    if badges:
        out.append(f'<div class="event-badges">{"".join(badges)}</div>')
    out.append('</div>')

    # When
    out.append(f'<div class="event-when">📅 {html.escape(_fmt_when(event))}</div>')

    # Attendees
    if event.attendees:
        names = [n or e.split('@')[0] for n, e in event.attendees if n or e]
        if names:
            shown = ', '.join(names[:3])
            rest = len(names) - 3
            att = shown + (f' +{rest} more' if rest > 0 else '')
            out.append(f'<div class="event-attendees">👥 {html.escape(att)}</div>')

    # Location
    if event.location.strip():
        out.append(f'<div class="event-location">📍 {html.escape(event.location[:120])}</div>')

    # Meet link
    if event.meet_link:
        short = event.meet_link.rstrip('/').split('/')[-1]
        out.append(
            f'<div class="event-meet">'
            f'<a href="{html.escape(event.meet_link)}" rel="noopener noreferrer" target="_blank">'
            f'🎥 meet.google.com/{html.escape(short)}</a></div>'
        )

    # Description
    if desc:
        out.append(f'<div class="event-description">{html.escape(desc)}</div>')

    out.append('</div>')
    return ''.join(out)


def render_calendar_html(calendars: list[dict]) -> str:
    """Render all calendars into a single self-contained HTML file."""
    all_events: list[CalEvent] = []
    for cal in calendars:
        all_events.extend(cal['events'])
    all_events.sort(key=lambda e: e.sort_key, reverse=True)  # newest first

    total = len(all_events)
    recurring_count = sum(1 for e in all_events if e.is_recurring)
    meet_count = sum(1 for e in all_events if e.meet_link)

    # Sidebar calendar list
    sidebar = [
        f'<li class="cal-item active" data-cal="all">'
        f'All Calendars <span class="cal-count">{total}</span></li>'
    ]
    for cal in calendars:
        n = len(cal['events'])
        sidebar.append(
            f'<li class="cal-item" data-cal="{html.escape(cal["slug"])}">'
            f'{html.escape(cal["name"])} <span class="cal-count">{n}</span></li>'
        )

    # Group events by month, render cards
    month_label: dict[str, str] = {}
    month_order: list[str] = []
    month_cards: dict[str, list[str]] = {}

    for event in all_events:
        s = event.dtstart
        key = f'{s.year}-{s.month:02d}'
        if key not in month_label:
            month_label[key] = s.strftime('%B %Y')
            month_order.append(key)
            month_cards[key] = []
        month_cards[key].append(_render_card(event))

    sections = []
    for key in month_order:
        sections.append(
            f'<div class="month-section" data-month="{key}">'
            f'<div class="month-header">{html.escape(month_label[key])}</div>'
            + ''.join(month_cards[key])
            + '</div>'
        )

    owner_names = ', '.join(
        c['name'] for c in calendars if 'appointment' not in c['name'].lower()
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Calendar Archive</title>
<style>
{_CSS}
</style>
</head>
<body>
<div id="app">
  <nav id="sidebar">
    <div id="sidebar-header">
      <div id="sidebar-title">Calendar Archive</div>
      <div id="sidebar-stats">{html.escape(owner_names)}</div>
      <div id="sidebar-stats">{total} events · {recurring_count} recurring · {meet_count} with Meet</div>
      <input id="search" type="search" placeholder="Search events…" autocomplete="off">
    </div>
    <div id="filters">
      <label><input type="checkbox" id="recurring-only"> Recurring events only</label>
    </div>
    <ul id="cal-list">{''.join(sidebar)}</ul>
  </nav>
  <main id="main">
    {''.join(sections)}
  </main>
</div>
<script>{_JS}</script>
</body>
</html>
"""
