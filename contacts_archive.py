"""
contacts_archive.py
-------------------
Parse Google Contacts VCF exports and render a searchable HTML directory.

Takeout layout
--------------
    Contacts/
        All Contacts/All Contacts.vcf
        My Contacts/My Contacts.vcf
        Starred in Android/Starred in Android.vcf
        <any>.vcf

VCF 3.0 fields used
-------------------
    FN, N, EMAIL (+ itemN. prefix variant), TEL, ORG, TITLE,
    CATEGORIES, NOTE
"""

from __future__ import annotations

import dataclasses
import html
import os
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_AVATAR_COLORS = [
    '#4285f4', '#ea4335', '#fbbc04', '#34a853',
    '#ff6d00', '#46bdc6', '#7986cb', '#e67c73',
]


@dataclasses.dataclass
class Contact:
    full_name: str
    last_name: str
    first_name: str
    emails: list[tuple[str, str]]   # (label/type, address)
    phones: list[tuple[str, str]]   # (label/type, number)
    org: str
    title: str
    groups: list[str]               # from CATEGORIES
    note: str

    @property
    def display_name(self) -> str:
        if self.full_name.strip():
            return self.full_name.strip()
        if self.emails:
            return self.emails[0][1]
        return '(no name)'

    @property
    def initials(self) -> str:
        words = self.display_name.split()
        if len(words) >= 2:
            return (words[0][0] + words[-1][0]).upper()
        return self.display_name[:2].upper()

    @property
    def avatar_color(self) -> str:
        idx = sum(ord(c) for c in self.initials) % len(_AVATAR_COLORS)
        return _AVATAR_COLORS[idx]

    @property
    def sort_key(self) -> tuple:
        return (
            self.last_name.lower() or self.full_name.lower(),
            self.first_name.lower(),
        )

    @property
    def dedup_key(self) -> str:
        email = self.emails[0][1].lower() if self.emails else ''
        return f'{self.full_name.lower()}|{email}'


# ---------------------------------------------------------------------------
# Discovery & loading
# ---------------------------------------------------------------------------

def find_contacts_dir(takeout_dir: Path) -> Path | None:
    children = list(takeout_dir.iterdir()) if takeout_dir.is_dir() else []
    for candidate in [takeout_dir, *children]:
        if candidate.is_dir() and (candidate / 'Contacts').is_dir():
            return candidate / 'Contacts'
    for root, _dirs, files in os.walk(takeout_dir):
        if Path(root).name == 'Contacts':
            if any(f.lower().endswith('.vcf') for f in files):
                return Path(root)
    return None


def _unfold(text: str) -> str:
    return re.sub(r'\r?\n[ \t]', '', text)


def _parse_vcf_block(block: str) -> Contact | None:
    """Parse one VCARD block into a Contact."""
    full_name = ''
    last_name = ''
    first_name = ''
    emails: list[tuple[str, str]] = []
    phones: list[tuple[str, str]] = []
    org = ''
    title = ''
    groups: list[str] = []
    note = ''

    for line in block.splitlines():
        if not line or line.upper().startswith('BEGIN:') or line.upper().startswith('END:'):
            continue

        # Strip itemN. prefix (Google's multi-value labeling)
        line = re.sub(r'^item\d+\.', '', line, flags=re.IGNORECASE)

        colon = line.find(':')
        if colon == -1:
            continue
        prop_part = line[:colon]
        value = line[colon + 1:].strip()
        if not value:
            continue

        # Split prop_part into name and params
        parts = prop_part.split(';')
        prop_name = parts[0].strip().upper()
        params: dict[str, str] = {}
        for p in parts[1:]:
            if '=' in p:
                k, _, v = p.partition('=')
                params[k.upper()] = v.strip('"').strip()
            else:
                params['TYPE'] = p.strip()

        # Unescape common sequences
        value = (value.replace('\\n', '\n').replace('\\N', '\n')
                      .replace('\\,', ',').replace('\\;', ';')
                      .replace('\\\\', '\\'))

        if prop_name == 'FN':
            full_name = value
        elif prop_name == 'N':
            name_parts = value.split(';')
            last_name  = name_parts[0] if len(name_parts) > 0 else ''
            first_name = name_parts[1] if len(name_parts) > 1 else ''
        elif prop_name in ('EMAIL', 'EMAIL;TYPE'):
            label = params.get('TYPE', 'Email')
            if value and '@' in value:
                emails.append((label, value))
        elif prop_name in ('TEL', 'TEL;TYPE'):
            label = params.get('TYPE', 'Phone')
            if value:
                phones.append((label, value))
        elif prop_name == 'ORG':
            org = value.split(';')[0]  # org;dept → take org only
        elif prop_name == 'TITLE':
            title = value
        elif prop_name == 'CATEGORIES':
            groups = [g.strip() for g in value.split(',') if g.strip()]
        elif prop_name == 'NOTE':
            note = value

    if not full_name and not emails:
        return None

    return Contact(
        full_name=full_name,
        last_name=last_name,
        first_name=first_name,
        emails=emails,
        phones=phones,
        org=org,
        title=title,
        groups=groups,
        note=note,
    )


def load_contacts(contacts_dir: Path) -> list[Contact]:
    seen: set[str] = set()
    contacts: list[Contact] = []

    vcf_paths: list[Path] = []
    for root, _dirs, files in os.walk(contacts_dir):
        for f in files:
            if f.lower().endswith('.vcf'):
                vcf_paths.append(Path(root) / f)
    vcf_paths.sort()

    for path in vcf_paths:
        try:
            text = _unfold(path.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            continue
        # Split into individual vCard blocks
        blocks = re.split(r'(?i)BEGIN:VCARD', text)
        for block in blocks[1:]:  # skip text before first BEGIN
            end = re.search(r'(?i)END:VCARD', block)
            if end:
                block = block[:end.start()]
            c = _parse_vcf_block(block)
            if c and c.dedup_key not in seen:
                seen.add(c.dedup_key)
                contacts.append(c)

    contacts.sort(key=lambda c: c.sort_key)
    return contacts


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
  font-size: 14px; line-height: 1.5; color: var(--text); background: var(--bg);
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
#app { display: flex; height: 100vh; overflow: hidden; }
#sidebar {
  width: 220px; flex-shrink: 0; background: var(--panel);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden;
}
#sidebar-header { padding: 14px 16px; border-bottom: 1px solid var(--border); }
#sidebar-title { font-weight: 700; font-size: 15px; margin-bottom: 2px; }
#sidebar-stats { font-size: 11px; color: var(--muted); margin-bottom: 8px; }
#search {
  display: block; width: 100%; padding: 7px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text);
}
#group-list { list-style: none; margin: 0; padding: 8px; overflow-y: auto; flex: 1; }
.group-item {
  padding: 6px 10px; border-radius: 6px; cursor: pointer;
  font-size: 13px; display: flex; justify-content: space-between; align-items: center;
}
.group-item:hover { background: var(--bg); }
.group-item.active { background: #e8f0fe; color: var(--accent); font-weight: 600; }
.group-count { font-size: 11px; color: var(--muted); }
#vcf-section { padding: 10px 16px; border-top: 1px solid var(--border); font-size: 12px; }
#vcf-title { font-size: 10px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
.vcf-link { display: block; padding: 3px 0; color: var(--accent); }
.vcf-link:hover { text-decoration: underline; }
#main { flex: 1; overflow-y: auto; padding: 20px; }
#contacts-grid { columns: 3 240px; column-gap: 12px; }
#empty-msg { text-align: center; color: var(--muted); margin-top: 60px; }
.contact-card {
  break-inside: avoid; margin-bottom: 12px;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 16px;
  display: flex; gap: 12px; align-items: flex-start;
}
.contact-card:hover { border-color: #b0c4e8; }
.avatar {
  width: 38px; height: 38px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 14px; font-weight: 700; color: #fff; flex-shrink: 0;
}
.contact-name { font-weight: 600; font-size: 14px; }
.contact-title-org { font-size: 12px; color: var(--muted); margin-top: 1px; }
.contact-detail { font-size: 12px; margin-top: 5px; }
.contact-detail a { color: var(--accent); }
.contact-label { font-size: 10px; color: var(--muted); text-transform: uppercase; margin-right: 4px; }
"""

_JS = r"""
(function () {
  var activeGroup = 'all';
  var activeQuery = '';

  function applyFilters() {
    var q = activeQuery;
    var anyVisible = false;
    document.querySelectorAll('.contact-card').forEach(function (card) {
      var groupOk = activeGroup === 'all' ||
        (card.dataset.groups || '').split(',').indexOf(activeGroup) !== -1;
      var searchOk = !q || card.dataset.search.indexOf(q) !== -1;
      var show = groupOk && searchOk;
      card.style.display = show ? '' : 'none';
      if (show) anyVisible = true;
    });
    document.getElementById('empty-msg').style.display = anyVisible ? 'none' : '';
  }

  document.querySelectorAll('.group-item').forEach(function (item) {
    item.addEventListener('click', function () {
      document.querySelector('.group-item.active').classList.remove('active');
      this.classList.add('active');
      activeGroup = this.dataset.group;
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
})();
"""


def _render_card(c: Contact) -> str:
    search_parts = [c.display_name.lower()]
    for _, addr in c.emails:
        search_parts.append(addr.lower())
    for _, num in c.phones:
        search_parts.append(num.lower())
    if c.org:
        search_parts.append(c.org.lower())
    if c.title:
        search_parts.append(c.title.lower())

    groups_data = ','.join(c.groups)

    parts = [
        f'<div class="contact-card"'
        f' data-groups="{html.escape(groups_data)}"'
        f' data-search="{html.escape(" ".join(search_parts))}">'
    ]
    parts.append(
        f'<div class="avatar" style="background:{c.avatar_color}">'
        f'{html.escape(c.initials)}</div>'
    )
    parts.append('<div class="contact-body" style="flex:1;min-width:0">')
    parts.append(f'<div class="contact-name">{html.escape(c.display_name)}</div>')

    if c.title or c.org:
        subtitle = ' · '.join(filter(None, [c.title, c.org]))
        parts.append(f'<div class="contact-title-org">{html.escape(subtitle)}</div>')

    for label, addr in c.emails[:3]:
        parts.append(
            f'<div class="contact-detail">'
            f'<a href="mailto:{html.escape(addr)}">{html.escape(addr)}</a>'
            f'</div>'
        )
    for label, num in c.phones[:2]:
        parts.append(
            f'<div class="contact-detail">'
            f'<a href="tel:{html.escape(num)}">{html.escape(num)}</a>'
            f'</div>'
        )
    parts.append('</div></div>')
    return ''.join(parts)


def render_contacts_html(contacts: list[Contact], vcf_files: list[str] = ()) -> str:
    # Group sidebar
    all_groups: list[str] = sorted({g for c in contacts for g in c.groups})
    group_counts = {g: sum(1 for c in contacts if g in c.groups) for g in all_groups}

    group_items = [
        f'<li class="group-item active" data-group="all">'
        f'All <span class="group-count">{len(contacts)}</span></li>'
    ]
    for g in all_groups:
        group_items.append(
            f'<li class="group-item" data-group="{html.escape(g)}">'
            f'{html.escape(g)} <span class="group-count">{group_counts[g]}</span></li>'
        )

    cards = ''.join(_render_card(c) for c in contacts)

    vcf_section = ''
    if vcf_files:
        links = ''.join(
            f'<a class="vcf-link" href="contacts/{html.escape(name)}" download>'
            f'⬇ {html.escape(name)}</a>'
            for name in sorted(vcf_files)
        )
        vcf_section = (
            f'<div id="vcf-section">'
            f'<div id="vcf-title">Download VCF</div>'
            f'{links}</div>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Contacts Archive</title>
<style>{_CSS}</style>
</head>
<body>
<div id="app">
  <nav id="sidebar">
    <div id="sidebar-header">
      <div id="sidebar-title">Contacts</div>
      <div id="sidebar-stats">{len(contacts)} contacts</div>
      <input id="search" type="search" placeholder="Search…" autocomplete="off">
    </div>
    <ul id="group-list">{''.join(group_items)}</ul>
    {vcf_section}
  </nav>
  <main id="main">
    <div id="contacts-grid">{cards}</div>
    <div id="empty-msg" style="display:none">No contacts match.</div>
  </main>
</div>
<script>{_JS}</script>
</body>
</html>"""
