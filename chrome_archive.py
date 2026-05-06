"""
chrome_archive.py
-----------------
Handle Chrome data from a Google Takeout export.

Two things happen:
  1. Bookmarks.html and Reading List.html are passed through unchanged —
     they are already in the Netscape bookmark format that every browser
     can import directly (Chrome: chrome://bookmarks → Import).
  2. Extensions.json is rendered into a simple HTML page that links each
     extension directly to its Chrome Web Store install page.

Takeout layout
--------------
    Chrome/
        Bookmarks.html
        Reading List.html
        Extensions.json
        History.json         (not processed — too large / low value)
        Addresses and more.json
        ...
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path


_WEBSTORE_BASE = "https://chromewebstore.google.com/detail"
_CWS_UPDATE_URL = "https://clients2.google.com/service/update2/crx"

# Files that are useful as-is; copied into chrome/ in the output ZIP.
PASSTHROUGH_FILES = ["Bookmarks.html", "Reading List.html"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_chrome_dir(takeout_dir: Path) -> Path | None:
    children = list(takeout_dir.iterdir()) if takeout_dir.is_dir() else []
    for candidate in [takeout_dir, *children]:
        if candidate.is_dir() and (candidate / "Chrome").is_dir():
            return candidate / "Chrome"
    for root, _dirs, _files in os.walk(takeout_dir):
        if Path(root).name == "Chrome":
            return Path(root)
    return None


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------

def load_extensions(chrome_dir: Path) -> list[dict]:
    ext_file = chrome_dir / "Extensions.json"
    if not ext_file.exists():
        return []
    try:
        data = json.loads(ext_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    result = []
    for e in (data.get("Extensions") or []):
        ext_id = (e.get("id") or "").strip()
        if not ext_id:
            continue
        is_webstore = e.get("update_url", "") == _CWS_UPDATE_URL
        result.append({
            "id": ext_id,
            "version": e.get("version", ""),
            "enabled": bool(e.get("enabled", True)),
            "incognito": bool(e.get("incognito_enabled", False)),
            "webstore_url": f"{_WEBSTORE_BASE}/{ext_id}" if is_webstore else None,
        })
    return result


def render_extensions_html(extensions: list[dict]) -> str:
    rows: list[str] = []
    for e in sorted(extensions, key=lambda x: (not x["enabled"], x["id"])):
        ext_id  = html.escape(e["id"])
        version = html.escape(e["version"])
        status  = "Enabled" if e["enabled"] else "Disabled"
        sc      = "enabled" if e["enabled"] else "disabled"
        extra   = " · Incognito" if e["incognito"] else ""
        if e["webstore_url"]:
            link_cell = (
                f'<a href="{html.escape(e["webstore_url"])}" '
                f'target="_blank" rel="noopener">Install from Web Store ↗</a>'
            )
        else:
            link_cell = '<span class="no-link">Not on Web Store</span>'
        rows.append(
            f'<tr>'
            f'<td class="ext-id"><code>{ext_id}</code></td>'
            f'<td>{version}</td>'
            f'<td><span class="status {sc}">{status}{html.escape(extra)}</span></td>'
            f'<td>{link_cell}</td>'
            f'</tr>'
        )

    n = len(extensions)
    plural = "s" if n != 1 else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chrome Extensions</title>
<style>
:root {{
  --bg: #f7f7f8; --panel: #fff; --border: #e3e3e7;
  --text: #1f2328; --muted: #6b7280; --accent: #1a73e8;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 32px 40px; background: var(--bg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  font-size: 14px; color: var(--text); line-height: 1.5;
}}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
.sub {{ color: var(--muted); font-size: 13px; margin: 0 0 24px; }}
.sub a {{ color: var(--accent); text-decoration: none; }}
.sub a:hover {{ text-decoration: underline; }}
table {{
  width: 100%; border-collapse: collapse;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; overflow: hidden;
}}
th {{
  text-align: left; padding: 10px 14px;
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .05em; color: var(--muted);
  border-bottom: 1px solid var(--border);
}}
td {{ padding: 10px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: #fafafa; }}
.ext-id code {{ font-size: 12px; color: var(--muted); }}
.status {{
  font-size: 12px; padding: 2px 8px; border-radius: 999px;
  display: inline-block; white-space: nowrap;
}}
.status.enabled  {{ background: #d1fae5; color: #065f46; }}
.status.disabled {{ background: #fee2e2; color: #991b1b; }}
.no-link {{ font-size: 12px; color: var(--muted); }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>Chrome Extensions</h1>
<p class="sub">
  {n} extension{plural} exported &nbsp;·&nbsp;
  Click <em>Install from Web Store</em> to open the extension's install page in Chrome.
  &nbsp;·&nbsp;
  <a href="chrome://extensions" target="_blank">Manage installed extensions ↗</a>
</p>
<table>
  <thead>
    <tr>
      <th>Extension ID</th>
      <th>Version</th>
      <th>Status</th>
      <th>Web Store</th>
    </tr>
  </thead>
  <tbody>
{"".join(rows)}
  </tbody>
</table>
</body>
</html>"""
