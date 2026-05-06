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
  - google-chat-archive.html  (single-page SPA, suitable for Google Drive)
  - google-tasks.docx         (if the export contains Tasks data)
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


def _error_page(message: str) -> tuple:
    error_block = f'<div class="error-box">{message}</div>'
    return _UPLOAD_PAGE.replace("{error_block}", error_block), 400


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
