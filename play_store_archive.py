"""
play_store_archive.py
---------------------
Parse Google Play Store Installs.json and render a searchable app list
with links to the Play Store so users can reinstall apps.

Also renders device information from Devices.json.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from urllib.parse import quote_plus


_PLAY_STORE_SEARCH = "https://play.google.com/store/search?q={q}&c=apps"


# ---------------------------------------------------------------------------
# Discovery & loading
# ---------------------------------------------------------------------------

def find_play_store_dir(takeout_dir: Path) -> Path | None:
    children = list(takeout_dir.iterdir()) if takeout_dir.is_dir() else []
    for candidate in [takeout_dir, *children]:
        if candidate.is_dir() and (candidate / 'Google Play Store').is_dir():
            return candidate / 'Google Play Store'
    for root, _dirs, _files in os.walk(takeout_dir):
        if Path(root).name == 'Google Play Store':
            return Path(root)
    return None


def load_apps(play_store_dir: Path) -> list[dict]:
    """
    Return a deduplicated list of apps, each with a list of device installs.
    Structure: {title, search_url, installs: [{device, first_installed, last_updated}]}
    """
    path = play_store_dir / 'Installs.json'
    if not path.exists():
        return []
    try:
        records = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return []

    apps: dict[str, dict] = {}
    for record in records:
        inst = record.get('install', {})
        doc  = inst.get('doc', {})
        title = doc.get('title', '').strip()
        if not title:
            continue
        device = inst.get('deviceAttribute', {}).get('deviceDisplayName', '')
        first  = inst.get('firstInstallationTime', '')[:10]
        last   = inst.get('lastUpdateTime', '')[:10]

        if title not in apps:
            apps[title] = {
                'title':      title,
                'search_url': _PLAY_STORE_SEARCH.format(q=quote_plus(title)),
                'installs':   [],
            }
        apps[title]['installs'].append({
            'device': device,
            'first':  first,
            'last':   last,
        })

    result = sorted(apps.values(), key=lambda a: a['title'].lower())
    return result


def load_devices(play_store_dir: Path) -> list[dict]:
    path = play_store_dir / 'Devices.json'
    if not path.exists():
        return []
    try:
        records = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return []
    devices = []
    for record in records:
        d = record.get('device', {})
        data = d.get('mostRecentData', {})
        last_active = d.get('lastTimeDeviceActive', '')[:10]
        devices.append({
            'name':        data.get('deviceDisplayName', data.get('modelName', '')),
            'model':       data.get('modelName', ''),
            'manufacturer':data.get('manufacturer', ''),
            'carrier':     data.get('carrierName', ''),
            'android':     data.get('androidSdkVersion', ''),
            'last_active': last_active,
        })
    return devices


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
  display: flex; flex-direction: column; overflow: hidden; padding: 14px 16px;
}
#sidebar-title { font-weight: 700; font-size: 15px; margin-bottom: 2px; }
#sidebar-stats { font-size: 11px; color: var(--muted); margin-bottom: 12px; }
#search {
  display: block; width: 100%; padding: 7px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text); margin-bottom: 16px;
}
#devices-section { flex: 1; overflow-y: auto; }
#devices-title {
  font-size: 10px; font-weight: 700; color: var(--muted);
  text-transform: uppercase; letter-spacing: .06em; margin-bottom: 8px;
}
.device-card {
  background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
  padding: 8px 10px; margin-bottom: 8px; font-size: 12px;
}
.device-name { font-weight: 600; font-size: 13px; }
.device-detail { color: var(--muted); margin-top: 2px; }
#main { flex: 1; overflow-y: auto; padding: 20px; }
#apps-list { display: flex; flex-direction: column; gap: 8px; }
.app-row {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 14px;
  display: flex; align-items: flex-start; gap: 12px;
}
.app-row:hover { border-color: #b0c4e8; }
.app-title { font-weight: 600; font-size: 14px; flex: 1; }
.app-devices { font-size: 12px; color: var(--muted); margin-top: 3px; }
.app-link { font-size: 12px; white-space: nowrap; flex-shrink: 0; }
#empty-msg { text-align: center; color: var(--muted); margin-top: 60px; }
"""

_JS = r"""
(function () {
  var q = '';
  function applyFilters() {
    var any = false;
    document.querySelectorAll('.app-row').forEach(function (row) {
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


def render_play_store_html(apps: list[dict], devices: list[dict]) -> str:
    device_cards = ''
    if devices:
        cards = []
        for d in devices:
            name = html.escape(d['name'] or d['model'])
            carrier = html.escape(d['carrier'])
            android = html.escape(str(d['android']))
            last = html.escape(d['last_active'])
            detail_parts = []
            if carrier:
                detail_parts.append(carrier)
            if android:
                detail_parts.append(f'Android SDK {android}')
            if last:
                detail_parts.append(f'active {last}')
            detail = ' · '.join(detail_parts)
            cards.append(
                f'<div class="device-card">'
                f'<div class="device-name">{name}</div>'
                f'<div class="device-detail">{detail}</div>'
                f'</div>'
            )
        device_cards = (
            f'<div id="devices-section">'
            f'<div id="devices-title">Devices ({len(devices)})</div>'
            + ''.join(cards)
            + '</div>'
        )

    rows: list[str] = []
    for app in apps:
        title = html.escape(app['title'])
        url   = html.escape(app['search_url'])
        device_names = sorted({i['device'] for i in app['installs'] if i['device']})
        devices_str  = ', '.join(device_names) if device_names else ''
        install_count = len(app['installs'])
        search_val = app['title'].lower() + ' ' + devices_str.lower()
        rows.append(
            f'<div class="app-row" data-search="{html.escape(search_val)}">'
            f'<div style="flex:1">'
            f'<div class="app-title">{title}</div>'
            f'<div class="app-devices">'
            + (html.escape(devices_str) if devices_str else f'{install_count} install(s)')
            + f'</div></div>'
            f'<a class="app-link" href="{url}" target="_blank" rel="noopener">'
            f'Search Play Store ↗</a>'
            f'</div>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Play Store Apps</title>
<style>{_CSS}</style>
</head>
<body>
<div id="app">
  <nav id="sidebar">
    <div id="sidebar-title">Play Store</div>
    <div id="sidebar-stats">{len(apps)} apps</div>
    <input id="search" type="search" placeholder="Search apps…" autocomplete="off">
    {device_cards}
  </nav>
  <main id="main">
    <div id="apps-list">{"".join(rows)}</div>
    <div id="empty-msg" style="display:none">No apps match.</div>
  </main>
</div>
<script>{_JS}</script>
</body>
</html>"""
