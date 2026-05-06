"""
app.py
------
Stateless Flask app for converting Google Takeout exports into
human-readable archives.

Usage
-----
    pip install -r requirements.txt
    flask run            # development
    python app.py        # also fine for development

What it does
------------
POST /process  accepts a Takeout ZIP and returns a ZIP containing:
  - google-chat-archive.html      (single-page SPA, suitable for Google Drive)
  - google-tasks.docx             (if the export contains Tasks data)
  - google-calendar-archive.html  (single-page SPA for Calendar events)
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
import tempfile

from flask import Flask, request, send_file

from google_chat_to_html import (
    find_chat_root,
    load_conversations,
    load_user_info,
    render_single_page_html,
)
from tasks import find_tasks_dir, load_task_lists, render_tasks_docx
from calendar_archive import find_calendar_dir, load_calendars, render_calendar_html
from keep_archive import find_keep_dir, load_notes, render_keep_html
from chrome_archive import (
    find_chrome_dir, load_extensions, render_extensions_html,
    PASSTHROUGH_FILES,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB hard cap


# ---------------------------------------------------------------------------
# Upload form
# ---------------------------------------------------------------------------

_UPLOAD_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Takeout Converter</title>
<style>
:root {
  --bg: #f7f7f8; --panel: #fff; --border: #e3e3e7;
  --text: #1f2328; --muted: #6b7280; --accent: #1a73e8;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  font-size: 15px; color: var(--text); line-height: 1.5;
}
.page {
  max-width: 560px; margin: 80px auto; padding: 0 24px;
}
h1 { font-size: 24px; margin-bottom: 6px; }
.sub { color: var(--muted); font-size: 14px; margin-bottom: 32px; }
.card {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 28px 32px;
}
.drop-zone {
  border: 2px dashed var(--border); border-radius: 8px;
  padding: 40px 24px; text-align: center; cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
  margin-bottom: 20px;
}
.drop-zone.over, .drop-zone:hover { border-color: var(--accent); background: #f0f6ff; }
.drop-zone input[type=file] { display: none; }
.drop-zone .icon { font-size: 36px; margin-bottom: 8px; }
.drop-zone .label { font-weight: 600; }
.drop-zone .hint { color: var(--muted); font-size: 13px; margin-top: 4px; }
#file-name { font-size: 13px; color: var(--muted); margin-bottom: 16px; min-height: 18px; }
button[type=submit] {
  width: 100%; padding: 12px; font-size: 15px; font-weight: 600;
  background: var(--accent); color: #fff; border: none;
  border-radius: 8px; cursor: pointer; transition: opacity 0.15s;
}
button[type=submit]:disabled { opacity: 0.5; cursor: default; }
.outputs { margin-top: 20px; }
.outputs h3 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.05em; margin: 0 0 10px; }
.output-item { display: flex; gap: 10px; align-items: flex-start; margin-bottom: 12px; }
.output-icon { font-size: 22px; flex-shrink: 0; margin-top: 1px; }
.output-info strong { display: block; font-size: 14px; }
.output-info span { font-size: 13px; color: var(--muted); }
.error-box {
  background: #fef2f2; border: 1px solid #fca5a5; color: #991b1b;
  border-radius: 8px; padding: 12px 16px; margin-bottom: 16px;
  font-size: 14px;
}
#progress { display: none; color: var(--muted); font-size: 13px; margin-top: 12px; text-align: center; }
</style>
</head>
<body>
<div class="page">
  <h1>Takeout Converter</h1>
  <p class="sub">Convert your Google Takeout ZIP into browsable archives you can keep forever.</p>

  {error_block}

  <div class="card">
    <form id="upload-form" method="post" action="/process" enctype="multipart/form-data">
      <div class="drop-zone" id="drop-zone">
        <input type="file" name="takeout_zip" id="file-input" accept=".zip">
        <div class="icon">📦</div>
        <div class="label">Drop your Takeout ZIP here</div>
        <div class="hint">or click to choose a file</div>
      </div>
      <div id="file-name"></div>
      <button type="submit" id="submit-btn" disabled>Convert &amp; Download</button>
      <p id="progress">Processing… this may take a moment for large exports.</p>
    </form>

    <div class="outputs">
      <h3>What you'll get</h3>
      <div class="output-item">
        <div class="output-icon">💬</div>
        <div class="output-info">
          <strong>google-chat-archive.html</strong>
          <span>Single-file chat browser — works in Google Drive preview, any browser, no server needed.</span>
        </div>
      </div>
      <div class="output-item">
        <div class="output-icon">✅</div>
        <div class="output-info">
          <strong>google-tasks.docx</strong>
          <span>All your task lists, with checkboxes, due dates, and notes. Opens as a Google Doc.</span>
        </div>
      </div>
      <div class="output-item">
        <div class="output-icon">📅</div>
        <div class="output-info">
          <strong>google-calendar-archive.html</strong>
          <span>All calendar events with attendees, Meet links, and recurring-event flags. Filterable by calendar.</span>
        </div>
      </div>
      <div class="output-item">
        <div class="output-icon">🗒️</div>
        <div class="output-info">
          <strong>google-keep-archive.html</strong>
          <span>All Keep notes with colours, pin/archive state, checklists, and label filtering.</span>
        </div>
      </div>
      <div class="output-item">
        <div class="output-icon">🔖</div>
        <div class="output-info">
          <strong>chrome/Bookmarks.html &amp; Reading List.html</strong>
          <span>Browser-importable bookmark files, passed through unchanged.</span>
        </div>
      </div>
      <div class="output-item">
        <div class="output-icon">🧩</div>
        <div class="output-info">
          <strong>chrome-extensions.html</strong>
          <span>Your installed extensions with direct links to the Chrome Web Store install pages.</span>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
(function () {
  var dropZone = document.getElementById('drop-zone');
  var fileInput = document.getElementById('file-input');
  var fileName  = document.getElementById('file-name');
  var submitBtn = document.getElementById('submit-btn');
  var form      = document.getElementById('upload-form');
  var progress  = document.getElementById('progress');

  dropZone.addEventListener('click', function () { fileInput.click(); });
  dropZone.addEventListener('dragover', function (e) {
    e.preventDefault(); dropZone.classList.add('over');
  });
  dropZone.addEventListener('dragleave', function () { dropZone.classList.remove('over'); });
  dropZone.addEventListener('drop', function (e) {
    e.preventDefault(); dropZone.classList.remove('over');
    if (e.dataTransfer.files.length) {
      fileInput.files = e.dataTransfer.files;
      onFile(e.dataTransfer.files[0]);
    }
  });
  fileInput.addEventListener('change', function () {
    if (this.files.length) onFile(this.files[0]);
  });

  function onFile(f) {
    fileName.textContent = f.name + '  (' + (f.size / 1024 / 1024).toFixed(1) + ' MB)';
    submitBtn.disabled = false;
  }

  form.addEventListener('submit', function () {
    submitBtn.disabled = true;
    progress.style.display = 'block';
  });
})();
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return _UPLOAD_PAGE.replace("{error_block}", "")


# ---------------------------------------------------------------------------
# Processing endpoint
# ---------------------------------------------------------------------------

@app.route("/process", methods=["POST"])
def process():
    f = request.files.get("takeout_zip")
    if not f or not f.filename:
        return _error_page("No file was uploaded.")
    if not f.filename.lower().endswith(".zip"):
        return _error_page("Please upload a .zip file exported from Google Takeout.")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        zip_path = tmp / "takeout.zip"
        f.save(str(zip_path))

        extract_dir = tmp / "extracted"
        extract_dir.mkdir()
        try:
            with zipfile.ZipFile(zip_path) as zf:
                _safe_extract(zf, extract_dir)
        except zipfile.BadZipFile:
            return _error_page("The uploaded file is not a valid ZIP archive.")

        output_buf = io.BytesIO()
        results: list[str] = []
        errors: list[str] = []

        with zipfile.ZipFile(output_buf, "w", zipfile.ZIP_DEFLATED) as out_zip:
            # ── Google Chat ──────────────────────────────────────────────
            try:
                chat_root = find_chat_root(extract_dir)
                owner = load_user_info(chat_root)
                conversations = load_conversations(chat_root, owner)
                chat_html = render_single_page_html(conversations, owner)
                out_zip.writestr("google-chat-archive.html",
                                 chat_html.encode("utf-8"))
                results.append(
                    f"Google Chat: {len(conversations)} conversations, "
                    f"{sum(len(c.messages) for c in conversations)} messages"
                )
            except SystemExit as e:
                errors.append(f"Google Chat not found: {e}")
            except Exception as e:
                errors.append(f"Google Chat error: {e}")

            # ── Google Tasks ─────────────────────────────────────────────
            try:
                tasks_dir = find_tasks_dir(extract_dir)
                if tasks_dir:
                    task_lists = load_task_lists(tasks_dir)
                    if task_lists:
                        docx_buf = render_tasks_docx(task_lists)
                        out_zip.writestr("google-tasks.docx", docx_buf.read())
                        total_tasks = sum(len(tl.get("items") or [])
                                          for tl in task_lists)
                        results.append(
                            f"Google Tasks: {len(task_lists)} lists, "
                            f"{total_tasks} tasks"
                        )
                    else:
                        errors.append("Google Tasks: directory found but no JSON files")
                else:
                    errors.append("Google Tasks: not present in this export")
            except Exception as e:
                errors.append(f"Google Tasks error: {e}")

            # ── Google Calendar ───────────────────────────────────────────
            try:
                cal_dir = find_calendar_dir(extract_dir)
                if cal_dir:
                    calendars = load_calendars(cal_dir)
                    if calendars:
                        ics_names = [
                            p.name for p in sorted(cal_dir.iterdir())
                            if p.suffix.lower() == ".ics"
                        ]
                        cal_html = render_calendar_html(calendars, ics_files=ics_names)
                        out_zip.writestr("google-calendar-archive.html",
                                         cal_html.encode("utf-8"))
                        for name in ics_names:
                            out_zip.write(cal_dir / name, f"calendar/{name}")
                        total_events = sum(len(c["events"]) for c in calendars)
                        results.append(
                            f"Google Calendar: {len(calendars)} calendars, "
                            f"{total_events} events"
                        )
                    else:
                        errors.append("Google Calendar: directory found but no .ics files")
                else:
                    errors.append("Google Calendar: not present in this export")
            except Exception as e:
                errors.append(f"Google Calendar error: {e}")

            # ── Google Keep ───────────────────────────────────────────────────
            try:
                keep_dir = find_keep_dir(extract_dir)
                if keep_dir:
                    keep_notes = load_notes(keep_dir)
                    if keep_notes:
                        keep_html = render_keep_html(keep_notes)
                        out_zip.writestr("google-keep-archive.html",
                                         keep_html.encode("utf-8"))
                        active_count = sum(
                            1 for n in keep_notes
                            if not n.is_archived and not n.is_trashed
                        )
                        results.append(
                            f"Google Keep: {len(keep_notes)} notes "
                            f"({active_count} active)"
                        )
                    else:
                        errors.append("Google Keep: directory found but no JSON files")
                else:
                    errors.append("Google Keep: not present in this export")
            except Exception as e:
                errors.append(f"Google Keep error: {e}")

            # ── Chrome ───────────────────────────────────────────────────────
            try:
                chrome_dir = find_chrome_dir(extract_dir)
                if chrome_dir:
                    chrome_results: list[str] = []

                    # Pass through importable files unchanged.
                    passed: list[str] = []
                    for name in PASSTHROUGH_FILES:
                        src = chrome_dir / name
                        if src.exists():
                            out_zip.write(src, f"chrome/{name}")
                            passed.append(name)

                    # Extensions → HTML with Web Store links.
                    extensions = load_extensions(chrome_dir)
                    if extensions:
                        ext_html = render_extensions_html(extensions)
                        out_zip.writestr("chrome-extensions.html",
                                         ext_html.encode("utf-8"))
                        chrome_results.append(
                            f"{len(extensions)} extension"
                            f"{'s' if len(extensions) != 1 else ''}"
                        )

                    if passed:
                        chrome_results.append(f"bookmarks ({', '.join(passed)})")

                    if chrome_results:
                        results.append("Chrome: " + ", ".join(chrome_results))
                    else:
                        errors.append("Chrome: directory found but no usable files")
                else:
                    errors.append("Chrome: not present in this export")
            except Exception as e:
                errors.append(f"Chrome error: {e}")

            # ── Index ─────────────────────────────────────────────────────────
            if results:
                index_html = _render_index_html(out_zip.namelist(), results)
                out_zip.writestr("index.html", index_html.encode("utf-8"))

        if not results:
            return _error_page(
                "No recognisable Google data found in this ZIP.<br>"
                + "<br>".join(errors)
            )

        output_buf.seek(0)
        return send_file(
            output_buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name="takeout-archive.zip",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a ZIP while guarding against path-traversal (zip-slip)."""
    dest_resolved = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            continue  # skip entries that would escape dest
        zf.extract(member, dest)


_HTML_ARCHIVE_INFO: dict[str, tuple[str, str, str]] = {
    "google-chat-archive.html":     ("💬", "Google Chat",     "Searchable conversation browser"),
    "google-calendar-archive.html": ("📅", "Google Calendar", "Events with attendees, Meet links, and calendar filter"),
    "google-keep-archive.html":     ("🗒️", "Google Keep",     "Colour-coded notes with label and view filters"),
    "chrome-extensions.html":       ("🧩", "Chrome Extensions","Links to reinstall each extension from the Web Store"),
}

_DOWNLOAD_INFO: dict[str, tuple[str, str, str]] = {
    "google-tasks.docx":       ("✅", "Google Tasks",         "Task lists — open as a Google Doc or in Word"),
    "chrome/Bookmarks.html":   ("🔖", "Chrome Bookmarks",    "Import into Chrome, Firefox, or Safari"),
    "chrome/Reading List.html":("📖", "Chrome Reading List", "Import into Chrome"),
}


def _render_index_html(names: list[str], results: list[str]) -> str:
    import datetime as dt
    date_str = dt.date.today().strftime("%B %d, %Y")
    name_set = set(names)

    # HTML archives section
    archive_cards = []
    for fname, (icon, title, desc) in _HTML_ARCHIVE_INFO.items():
        if fname in name_set:
            archive_cards.append(
                f'<a class="card" href="{fname}">'
                f'<div class="card-icon">{icon}</div>'
                f'<div class="card-body">'
                f'<div class="card-title">{title}</div>'
                f'<div class="card-desc">{desc}</div>'
                f'</div></a>'
            )

    # Downloads section (DOCX, bookmarks, ICS files)
    download_items = []
    for fname, (icon, title, desc) in _DOWNLOAD_INFO.items():
        if fname in name_set:
            download_items.append(
                f'<li><a href="{fname}" download>'
                f'{icon} <strong>{title}</strong></a>'
                f' <span class="dl-desc">— {desc}</span></li>'
            )
    ics_files = sorted(n for n in names if n.startswith("calendar/") and n.endswith(".ics"))
    for ics in ics_files:
        fname = ics.split("/")[-1]
        download_items.append(
            f'<li><a href="{ics}" download>'
            f'📆 <strong>{fname}</strong></a>'
            f' <span class="dl-desc">— Calendar data (import into Google Calendar, Apple Calendar, etc.)</span></li>'
        )

    archives_section = ""
    if archive_cards:
        archives_section = (
            f'<h2>Browse</h2>'
            f'<div class="card-grid">{"".join(archive_cards)}</div>'
        )

    downloads_section = ""
    if download_items:
        downloads_section = (
            f'<h2>Import &amp; Download</h2>'
            f'<ul class="dl-list">{"".join(download_items)}</ul>'
        )

    summary = " · ".join(results)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Takeout Archive</title>
<style>
:root {{
  --bg: #f7f7f8; --panel: #fff; --border: #e3e3e7;
  --text: #1f2328; --muted: #6b7280; --accent: #1a73e8;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 40px 48px; background: var(--bg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  font-size: 14px; color: var(--text); line-height: 1.5;
}}
h1 {{ font-size: 26px; margin: 0 0 4px; }}
.sub {{ color: var(--muted); font-size: 13px; margin: 0 0 32px; }}
h2 {{ font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .06em; color: var(--muted); margin: 0 0 14px; }}
.card-grid {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 36px; }}
.card {{
  display: flex; align-items: flex-start; gap: 14px;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 20px;
  text-decoration: none; color: var(--text);
  width: 260px; transition: box-shadow .15s, border-color .15s;
}}
.card:hover {{ box-shadow: 0 2px 10px rgba(0,0,0,.08); border-color: #b0c4e8; }}
.card-icon {{ font-size: 28px; line-height: 1; flex-shrink: 0; margin-top: 1px; }}
.card-title {{ font-weight: 600; font-size: 14px; margin-bottom: 3px; color: var(--accent); }}
.card-desc {{ font-size: 12px; color: var(--muted); }}
.dl-list {{ list-style: none; padding: 0; margin: 0 0 36px; display: flex; flex-direction: column; gap: 10px; }}
.dl-list a {{ color: var(--accent); text-decoration: none; }}
.dl-list a:hover {{ text-decoration: underline; }}
.dl-desc {{ color: var(--muted); font-size: 13px; }}
</style>
</head>
<body>
<h1>Takeout Archive</h1>
<p class="sub">Generated {date_str} &nbsp;·&nbsp; {summary}</p>
{archives_section}
{downloads_section}
</body>
</html>"""


def _error_page(message: str) -> tuple:
    error_block = f'<div class="error-box">{message}</div>'
    return _UPLOAD_PAGE.replace("{error_block}", error_block), 400


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
