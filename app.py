"""
app.py
------
Stateless Flask app for converting Google Takeout exports into
human-readable archives.  Accepts both .zip and .tgz uploads.

POST /process  accepts a Takeout archive and returns a ZIP containing
               HTML archives, importable files, and an index.html.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import tarfile
import zipfile
from pathlib import Path
import tempfile

from flask import Flask, request, send_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

from google_chat_to_html import (
    find_chat_root, load_conversations, load_user_info, render_single_page_html,
)
from tasks import find_tasks_dir, load_task_lists, render_tasks_docx
from calendar_archive import find_calendar_dir, load_calendars, render_calendar_html
from keep_archive import find_keep_dir, load_notes, render_keep_html
from chrome_archive import (
    find_chrome_dir, load_extensions, render_extensions_html, PASSTHROUGH_FILES,
)
from contacts_archive import find_contacts_dir, load_contacts, render_contacts_html
from mail_archive import find_mail_dir, load_messages, render_mail_html
from meet_archive import find_meet_dir, load_meetings, render_meet_html
from play_store_archive import (
    find_play_store_dir, load_apps, load_devices, render_play_store_html,
)

app = Flask(__name__)

_MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = _MAX_UPLOAD_BYTES

# Per-file cap for binary pass-throughs (Drive, Wallet).  Individual files
# larger than this are skipped to avoid exhausting tmpfs and RAM.
_MAX_PASSTHROUGH_FILE = int(os.environ.get("MAX_PASSTHROUGH_MB", "500")) * 1024 * 1024

# Total budget for all Chat attachment files in the output ZIP.
# Chat exports can contain thousands of images; without a cap the output
# ZIP balloons to several GB.  Default 500 MB; override with MAX_CHAT_ATTACH_MB.
# Attachments are added newest-conversation-first so the budget drops the oldest.
_MAX_CHAT_ATTACHMENTS = int(os.environ.get("MAX_CHAT_ATTACH_MB", "500")) * 1024 * 1024

# File extensions that are already compressed — use ZIP_STORED so we don't
# waste CPU re-expanding and re-deflating data that won't compress further.
_PRECOMPRESSED_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".mp3", ".m4a", ".aac", ".ogg",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z",
    ".pdf",
}

# Services whose HTML files are passed through as-is (Google-generated reports).
# (takeout_dir_name, output_prefix, display_name)
_PASSTHROUGH_HTML_SERVICES = [
    ("My Activity",                          "my-activity",    "My Activity"),
    ("Google Account",                       "google-account", "Google Account"),
    ("Gemini",                               "gemini",         "Gemini"),
    ("Android Device Configuration Service", "device-config",  "Device Config"),
]

# Max size for including raw MBOX in output ZIP (large files are indexed-only).
_MAX_MBOX_SIZE = 100 * 1024 * 1024  # 100 MB


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
.page { max-width: 580px; margin: 60px auto; padding: 0 24px; }
h1 { font-size: 24px; margin-bottom: 6px; }
.sub { color: var(--muted); font-size: 14px; margin-bottom: 32px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 28px 32px; }
.drop-zone {
  position: relative;
  border: 2px dashed var(--border); border-radius: 8px;
  padding: 40px 24px; text-align: center;
  transition: border-color 0.15s, background 0.15s; margin-bottom: 20px;
}
.drop-zone.over, .drop-zone:hover { border-color: var(--accent); background: #f0f6ff; }
.drop-zone input[type=file] {
  position: absolute; inset: 0; width: 100%; height: 100%;
  opacity: 0; cursor: pointer; z-index: 1;
}
.drop-zone-inner { position: relative; pointer-events: none; }
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
.outputs { margin-top: 24px; }
.outputs h3 { font-size: 12px; font-weight: 700; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.06em; margin: 0 0 12px; }
.output-cols { display: flex; gap: 24px; }
.output-col { flex: 1; }
.output-item { display: flex; gap: 8px; align-items: flex-start; margin-bottom: 10px; }
.output-icon { font-size: 18px; flex-shrink: 0; margin-top: 1px; }
.output-info strong { display: block; font-size: 13px; }
.output-info span { font-size: 12px; color: var(--muted); }
.error-box {
  background: #fef2f2; border: 1px solid #fca5a5; color: #991b1b;
  border-radius: 8px; padding: 12px 16px; margin-bottom: 16px; font-size: 14px;
}
#progress-section { display: none; margin-top: 16px; }
#progress-track {
  height: 8px; background: var(--border); border-radius: 999px;
  overflow: hidden; margin-bottom: 10px;
}
#progress-bar {
  height: 100%; width: 0%; background: var(--accent);
  border-radius: 999px; transition: width 0.4s ease;
}
#progress-bar.done { background: #16a34a; }
#progress-row {
  display: flex; justify-content: space-between; align-items: baseline;
  font-size: 13px; margin-bottom: 4px;
}
#progress-status { font-weight: 600; color: var(--text); }
#progress-pct { color: var(--muted); font-variant-numeric: tabular-nums; }
#progress-detail { font-size: 12px; color: var(--muted); }
</style>
</head>
<body>
<div class="page">
  <h1>Takeout Converter</h1>
  <p class="sub">Convert a Google Takeout ZIP or TGZ into browsable, importable archives.</p>
  {error_block}
  <div class="card">
    <form id="upload-form" method="post" action="/process" enctype="multipart/form-data">
      <div class="drop-zone" id="drop-zone">
        <input type="file" name="takeout_zip" id="file-input" accept=".zip,.tgz,.gz" multiple>
        <div class="drop-zone-inner">
          <div class="icon">📦</div>
          <div class="label">Drop your Takeout archives here</div>
          <div class="hint">or click to choose files &nbsp;·&nbsp; .zip and .tgz · multiple files OK</div>
        </div>
      </div>
      <div id="file-name"></div>
      <button type="submit" id="submit-btn" disabled>Convert &amp; Download</button>
      <div id="progress-section" style="display:none">
        <div id="progress-track"><div id="progress-bar"></div></div>
        <div id="progress-row">
          <span id="progress-status">Uploading…</span>
          <span id="progress-pct">0%</span>
        </div>
        <div id="progress-detail"></div>
        <button type="button" id="again-btn" style="display:none;margin-top:14px;width:100%;padding:10px;font-size:14px;font-weight:600;background:var(--panel);color:var(--accent);border:1px solid var(--accent);border-radius:8px;cursor:pointer;">Convert another file</button>
      </div>
    </form>
    <div class="outputs">
      <h3>What you'll get (whichever services are in your export)</h3>
      <div class="output-cols">
        <div class="output-col">
          <div class="output-item"><div class="output-icon">💬</div><div class="output-info"><strong>Chat archive</strong><span>Searchable SPA, works in Drive preview</span></div></div>
          <div class="output-item"><div class="output-icon">✅</div><div class="output-info"><strong>Tasks DOCX</strong><span>Checkboxes, due dates, notes</span></div></div>
          <div class="output-item"><div class="output-icon">📅</div><div class="output-info"><strong>Calendar archive + ICS</strong><span>Events, Meet links, attendees</span></div></div>
          <div class="output-item"><div class="output-icon">🗒️</div><div class="output-info"><strong>Keep archive</strong><span>Colour-coded notes with label filter</span></div></div>
          <div class="output-item"><div class="output-icon">👥</div><div class="output-info"><strong>Contacts archive + VCF</strong><span>Searchable directory, importable VCF</span></div></div>
        </div>
        <div class="output-col">
          <div class="output-item"><div class="output-icon">📧</div><div class="output-info"><strong>Gmail index</strong><span>Header index with label filter</span></div></div>
          <div class="output-item"><div class="output-icon">📹</div><div class="output-info"><strong>Meet history</strong><span>Meeting log with duration</span></div></div>
          <div class="output-item"><div class="output-icon">📱</div><div class="output-info"><strong>Play Store apps</strong><span>App list with Play Store links</span></div></div>
          <div class="output-item"><div class="output-icon">🧩</div><div class="output-info"><strong>Chrome extensions + bookmarks</strong><span>Web Store links, importable bookmarks</span></div></div>
          <div class="output-item"><div class="output-icon">📂</div><div class="output-info"><strong>Drive files + activity reports</strong><span>Your files passed through unchanged</span></div></div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
(function () {
  var dropZone      = document.getElementById('drop-zone');
  var fileInput     = document.getElementById('file-input');
  var fileName      = document.getElementById('file-name');
  var submitBtn     = document.getElementById('submit-btn');
  var form          = document.getElementById('upload-form');
  var progressSec   = document.getElementById('progress-section');
  var progressBar   = document.getElementById('progress-bar');
  var progressStatus= document.getElementById('progress-status');
  var progressPct   = document.getElementById('progress-pct');
  var progressDetail= document.getElementById('progress-detail');
  var againBtn      = document.getElementById('again-btn');

  if (againBtn) againBtn.addEventListener('click', function () { window.location.reload(); });

  // The input is a full-size transparent overlay, so drag events land on it directly.
  fileInput.addEventListener('dragover',  function (e) { e.preventDefault(); dropZone.classList.add('over'); });
  fileInput.addEventListener('dragleave', function ()  { dropZone.classList.remove('over'); });
  fileInput.addEventListener('drop', function (e) {
    e.preventDefault(); dropZone.classList.remove('over');
    var files = e.dataTransfer.files;
    if (files.length) {
      // Populate the input so FormData picks them up when the form is submitted.
      var dt = new DataTransfer();
      for (var i = 0; i < files.length; i++) dt.items.add(files[i]);
      fileInput.files = dt.files;
      onFiles(fileInput.files);
    }
  });
  fileInput.addEventListener('change', function () { if (this.files.length) onFiles(this.files); });

  function onFiles(files) {
    if (files.length === 1) {
      fileName.textContent = files[0].name + '  (' + (files[0].size / 1024 / 1024).toFixed(1) + ' MB)';
    } else {
      var total = 0;
      for (var i = 0; i < files.length; i++) total += files[i].size;
      fileName.textContent = files.length + ' files  (' + (total / 1024 / 1024).toFixed(1) + ' MB total)';
    }
    submitBtn.disabled = false;
  }

  function fmtMB(bytes) { return (bytes / 1024 / 1024).toFixed(1) + ' MB'; }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    submitBtn.style.display = 'none';
    progressSec.style.display = 'block';

    var xhr = new XMLHttpRequest();
    var speedBytes = 0, speedTime = Date.now(), speedLast = 0;

    xhr.upload.addEventListener('progress', function (e) {
      if (!e.lengthComputable) return;
      var pct = e.loaded / e.total;
      progressBar.style.width = (pct * 100).toFixed(1) + '%';
      progressPct.textContent  = Math.round(pct * 100) + '%';

      var now = Date.now(), elapsed = (now - speedTime) / 1000;
      if (elapsed >= 0.8) {
        speedBytes = (e.loaded - speedLast) / elapsed;
        speedLast = e.loaded; speedTime = now;
      }
      var speedStr = speedBytes > 0 ? ' · ' + fmtMB(speedBytes) + '/s' : '';
      progressDetail.textContent = fmtMB(e.loaded) + ' of ' + fmtMB(e.total) + speedStr;

      if (pct >= 1) {
        progressStatus.textContent  = 'Processing…';
        progressDetail.textContent  = 'Waiting for server — large exports may take a minute.';
        progressPct.textContent     = '';
      }
    });

    xhr.responseType = 'blob';

    xhr.addEventListener('load', function () {
      if (xhr.status === 200) {
        var cd = xhr.getResponseHeader('Content-Disposition') || '';
        var m  = cd.match(/filename[^;=\n]*=(["']?)([^"'\n;]+)\1/);
        var fn = m ? m[2] : 'takeout-archive.zip';
        var url = URL.createObjectURL(xhr.response);
        var a = document.createElement('a');
        a.href = url; a.download = fn;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(url); }, 30000);
        progressBar.classList.add('done');
        progressBar.style.width = '100%';
        progressStatus.textContent = 'Done — your download has started.';
        progressDetail.textContent = '';
        progressPct.textContent    = '';
        againBtn.style.display = 'block';
      } else {
        var reader = new FileReader();
        reader.onload = function () { document.open(); document.write(reader.result); document.close(); };
        reader.readAsText(xhr.response);
      }
    });

    function resetForm(msg) {
      progressStatus.textContent = msg;
      progressBar.style.background = '#dc2626';
      progressDetail.textContent = '';
      progressPct.textContent = '';
      againBtn.style.display = 'block';
    }

    xhr.addEventListener('error',   function () { resetForm('Upload failed — check your connection and try again.'); });
    xhr.addEventListener('abort',   function () { resetForm('Upload cancelled.'); });
    xhr.addEventListener('timeout', function () { resetForm('Timed out — the server took too long to respond.'); });

    xhr.open('POST', form.action);
    xhr.send(new FormData(form));
  });
})();
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return _UPLOAD_PAGE.replace("{error_block}", "")


@app.errorhandler(413)
def too_large(_e):
    limit_mb = _MAX_UPLOAD_BYTES // (1024 * 1024)
    return _error_page(
        f"File too large. Maximum upload size is {limit_mb} MB.<br>"
        f"For very large exports, self-host and set the "
        f"<code>MAX_UPLOAD_MB</code> environment variable."
    )


# ---------------------------------------------------------------------------
# Processing endpoint
# ---------------------------------------------------------------------------

@app.route("/process", methods=["POST"])
def process():
    files = [f for f in request.files.getlist("takeout_zip") if f and f.filename]
    if not files:
        return _error_page("No file was uploaded.")

    for f in files:
        fn = f.filename.lower()
        if not (fn.endswith(".zip") or fn.endswith(".tgz") or fn.endswith(".tar.gz")):
            return _error_page(
                f"Unsupported file type: {f.filename!r}. "
                "Please upload .zip or .tgz files exported from Google Takeout."
            )

    # TemporaryDirectory managed manually so send_file can stream after we return.
    tmpdir_obj = tempfile.TemporaryDirectory()
    filenames = ", ".join(f.filename for f in files)
    try:
        return _process_upload(files, tmpdir_obj)
    except MemoryError:
        tmpdir_obj.cleanup()
        logger.error("OOM processing %s", filenames)
        return _error_page(
            "The server ran out of memory processing this archive. "
            "Try a smaller export or contact the administrator."
        )
    except Exception:
        tmpdir_obj.cleanup()
        logger.exception("Unhandled error processing %s", filenames)
        return _error_page("An unexpected error occurred. Please try again.")


def _process_upload(files: list, tmpdir_obj: tempfile.TemporaryDirectory):
    tmp = Path(tmpdir_obj.name)
    extract_dir = tmp / "extracted"
    extract_dir.mkdir()

    for i, f in enumerate(files):
        fname_lower = f.filename.lower()
        archive_path = tmp / f"upload_{i}"
        f.save(str(archive_path))
        upload_mb = archive_path.stat().st_size / 1024 / 1024
        logger.info("extracting %d/%d: name=%r size=%.1f MB", i + 1, len(files), f.filename, upload_mb)

        if fname_lower.endswith(".zip"):
            try:
                with zipfile.ZipFile(archive_path) as zf:
                    _safe_extract_zip(zf, extract_dir)
            except zipfile.BadZipFile:
                tmpdir_obj.cleanup()
                return _error_page(f"{f.filename!r} is not a valid ZIP archive.")
        else:
            try:
                with tarfile.open(archive_path, "r:gz") as tf:
                    _safe_extract_tar(tf, extract_dir)
            except tarfile.TarError as e:
                tmpdir_obj.cleanup()
                return _error_page(f"{f.filename!r} is not a valid TGZ archive: {e}")

        archive_path.unlink()

    output_path = tmp / "output.zip"
    results: list[str] = []
    errors: list[str] = []

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as out_zip:

        # ── Google Chat ──────────────────────────────────────────────
        try:
            chat_root = find_chat_root(extract_dir)
            owner = load_user_info(chat_root)
            conversations = load_conversations(chat_root, owner)
            chat_html = render_single_page_html(
                conversations, owner, attachment_url_prefix="chat-files"
            )
            out_zip.writestr("google-chat-archive.html", chat_html.encode("utf-8"))
            attach_count = 0
            attach_bytes = 0
            attach_skipped = 0
            # Newest conversations first so budget keeps the most recent attachments.
            import datetime as _dt
            sorted_convos = sorted(
                conversations,
                key=lambda c: c.last_activity or _dt.datetime.min,
                reverse=True,
            )
            for c in sorted_convos:
                # Newest files first within each conversation (by mtime).
                files = sorted(
                    (f for f in c.source_dir.iterdir()
                     if f.is_file() and f.suffix.lower() != ".json"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
                for f in files:
                    size = f.stat().st_size
                    if size > _MAX_PASSTHROUGH_FILE:
                        attach_skipped += 1
                        logger.info("skipping large chat attachment (%.1f MB): %s",
                                    size / 1024 / 1024, f.name)
                        continue
                    if attach_bytes + size > _MAX_CHAT_ATTACHMENTS:
                        attach_skipped += 1
                        continue
                    ext = f.suffix.lower()
                    compress = zipfile.ZIP_STORED if ext in _PRECOMPRESSED_EXTS else zipfile.ZIP_DEFLATED
                    out_zip.write(f, f"chat-files/{c.slug}/{f.name}", compress_type=compress)
                    attach_count += 1
                    attach_bytes += size
            if attach_skipped:
                logger.info("chat attachments: included %d (%.1f MB), skipped %d (budget/size limit)",
                            attach_count, attach_bytes / 1024 / 1024, attach_skipped)
            results.append(
                f"Google Chat: {len(conversations)} conversations, "
                f"{sum(len(c.messages) for c in conversations)} messages"
                + (f", {attach_count} attachment(s)" if attach_count else "")
                + (f" ({attach_skipped} skipped — over size limit)" if attach_skipped else "")
            )
        except SystemExit as e:
            errors.append(f"Google Chat not found: {e}")
        except Exception as e:
            logger.exception("Google Chat processing failed")
            errors.append(f"Google Chat error: {e}")

        # ── Google Tasks ─────────────────────────────────────────────
        try:
            tasks_dir = find_tasks_dir(extract_dir)
            if tasks_dir:
                task_lists = load_task_lists(tasks_dir)
                if task_lists:
                    docx_buf = render_tasks_docx(task_lists)
                    out_zip.writestr("google-tasks.docx", docx_buf.read())
                    total_tasks = sum(len(tl.get("items") or []) for tl in task_lists)
                    results.append(f"Google Tasks: {len(task_lists)} lists, {total_tasks} tasks")
                else:
                    errors.append("Google Tasks: directory found but no JSON files")
            else:
                errors.append("Google Tasks: not present in this export")
        except Exception as e:
            logger.exception("Google Tasks processing failed")
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
                    out_zip.writestr("google-calendar-archive.html", cal_html.encode("utf-8"))
                    for name in ics_names:
                        out_zip.write(cal_dir / name, f"calendar/{name}")
                    total_events = sum(len(c["events"]) for c in calendars)
                    results.append(
                        f"Google Calendar: {len(calendars)} calendars, {total_events} events"
                    )
                else:
                    errors.append("Google Calendar: directory found but no .ics files")
            else:
                errors.append("Google Calendar: not present in this export")
        except Exception as e:
            logger.exception("Google Calendar processing failed")
            errors.append(f"Google Calendar error: {e}")

        # ── Google Keep ───────────────────────────────────────────────
        try:
            keep_dir = find_keep_dir(extract_dir)
            if keep_dir:
                keep_notes = load_notes(keep_dir)
                if keep_notes:
                    keep_html = render_keep_html(keep_notes)
                    out_zip.writestr("google-keep-archive.html", keep_html.encode("utf-8"))
                    active_count = sum(
                        1 for n in keep_notes if not n.is_archived and not n.is_trashed
                    )
                    results.append(f"Google Keep: {len(keep_notes)} notes ({active_count} active)")
                else:
                    errors.append("Google Keep: directory found but no JSON files")
            else:
                errors.append("Google Keep: not present in this export")
        except Exception as e:
            logger.exception("Google Keep processing failed")
            errors.append(f"Google Keep error: {e}")

        # ── Chrome ────────────────────────────────────────────────────
        try:
            chrome_dir = find_chrome_dir(extract_dir)
            if chrome_dir:
                chrome_parts: list[str] = []
                passed: list[str] = []
                for name in PASSTHROUGH_FILES:
                    src = chrome_dir / name
                    if src.exists():
                        out_zip.write(src, f"chrome/{name}")
                        passed.append(name)
                extensions = load_extensions(chrome_dir)
                if extensions:
                    out_zip.writestr("chrome-extensions.html",
                                     render_extensions_html(extensions).encode("utf-8"))
                    chrome_parts.append(f"{len(extensions)} extension(s)")
                if passed:
                    chrome_parts.append(f"bookmarks ({', '.join(passed)})")
                if chrome_parts:
                    results.append("Chrome: " + ", ".join(chrome_parts))
                else:
                    errors.append("Chrome: directory found but no usable files")
            else:
                errors.append("Chrome: not present in this export")
        except Exception as e:
            logger.exception("Chrome processing failed")
            errors.append(f"Chrome error: {e}")

        # ── Contacts ──────────────────────────────────────────────────
        try:
            contacts_dir = find_contacts_dir(extract_dir)
            if contacts_dir:
                contacts = load_contacts(contacts_dir)
                if contacts:
                    vcf_paths = sorted(contacts_dir.rglob("*.vcf"))
                    vcf_names = [vp.name for vp in vcf_paths]
                    for vp in vcf_paths:
                        out_zip.write(vp, f"contacts/{vp.name}")
                    out_zip.writestr(
                        "contacts-archive.html",
                        render_contacts_html(contacts, vcf_files=vcf_names).encode("utf-8"),
                    )
                    results.append(f"Contacts: {len(contacts)} contacts")
                else:
                    errors.append("Contacts: directory found but no contacts parsed")
            else:
                errors.append("Contacts: not present in this export")
        except Exception as e:
            logger.exception("Contacts processing failed")
            errors.append(f"Contacts error: {e}")

        # ── Gmail / Mail ──────────────────────────────────────────────
        try:
            mail_dir = find_mail_dir(extract_dir)
            if mail_dir:
                messages, total_scanned = load_messages(mail_dir)
                if messages:
                    mbox_files = sorted(
                        p for p in mail_dir.iterdir() if p.suffix.lower() == ".mbox"
                    )
                    mbox_names: list[str] = []
                    for mf in mbox_files:
                        if mf.stat().st_size <= _MAX_MBOX_SIZE:
                            out_zip.write(mf, f"mail/{mf.name}")
                            mbox_names.append(mf.name)
                        else:
                            logger.info(
                                "skipping MBOX (%.0f MB > limit): %s",
                                mf.stat().st_size / 1024 / 1024, mf.name,
                            )
                    out_zip.writestr(
                        "mail-archive.html",
                        render_mail_html(messages, total_scanned,
                                         mbox_filenames=mbox_names).encode("utf-8"),
                    )
                    truncation = (
                        f", {len(messages):,} of {total_scanned:,} indexed"
                        if total_scanned > len(messages)
                        else f", {len(messages):,} messages"
                    )
                    results.append(f"Gmail{truncation}")
                else:
                    errors.append("Gmail: directory found but no messages parsed")
            else:
                errors.append("Gmail: not present in this export")
        except Exception as e:
            logger.exception("Gmail processing failed")
            errors.append(f"Gmail error: {e}")

        # ── Google Meet ───────────────────────────────────────────────
        try:
            meet_dir = find_meet_dir(extract_dir)
            if meet_dir:
                meetings = load_meetings(meet_dir)
                if meetings:
                    out_zip.writestr(
                        "google-meet-archive.html",
                        render_meet_html(meetings).encode("utf-8"),
                    )
                    results.append(f"Google Meet: {len(meetings)} meetings")
                else:
                    errors.append("Google Meet: directory found but no meeting records")
            else:
                errors.append("Google Meet: not present in this export")
        except Exception as e:
            logger.exception("Google Meet processing failed")
            errors.append(f"Google Meet error: {e}")

        # ── Play Store ────────────────────────────────────────────────
        try:
            play_dir = find_play_store_dir(extract_dir)
            if play_dir:
                apps = load_apps(play_dir)
                devices = load_devices(play_dir)
                if apps:
                    out_zip.writestr(
                        "play-store-archive.html",
                        render_play_store_html(apps, devices).encode("utf-8"),
                    )
                    results.append(
                        f"Play Store: {len(apps)} apps"
                        + (f" across {len(devices)} device(s)" if devices else "")
                    )
                else:
                    errors.append("Play Store: directory found but no install data")
            else:
                errors.append("Play Store: not present in this export")
        except Exception as e:
            logger.exception("Play Store processing failed")
            errors.append(f"Play Store error: {e}")

        # ── Google Wallet (PDF pass-through) ──────────────────────────
        try:
            wallet_dir = _find_service_dir(extract_dir, "Google Wallet")
            if wallet_dir:
                pdfs = sorted(wallet_dir.rglob("*.pdf"))
                included = 0
                for pdf in pdfs:
                    size = pdf.stat().st_size
                    if size > _MAX_PASSTHROUGH_FILE:
                        logger.info(
                            "skipping Wallet PDF (%.0f MB > limit): %s",
                            size / 1024 / 1024, pdf.name,
                        )
                        continue
                    out_zip.write(pdf, f"google-wallet/{pdf.relative_to(wallet_dir)}",
                                  compress_type=zipfile.ZIP_STORED)
                    included += 1
                if included:
                    results.append(f"Google Wallet: {included} file(s)")
        except Exception as e:
            logger.exception("Google Wallet processing failed")
            errors.append(f"Google Wallet error: {e}")

        # ── Drive (pass-through all files) ────────────────────────────
        try:
            drive_dir = _find_service_dir(extract_dir, "Drive")
            if drive_dir:
                drive_files = sorted(p for p in drive_dir.rglob("*") if p.is_file())
                included = 0
                skipped = 0
                for df in drive_files:
                    size = df.stat().st_size
                    if size > _MAX_PASSTHROUGH_FILE:
                        logger.info(
                            "skipping Drive file (%.0f MB > limit): %s",
                            size / 1024 / 1024, df.name,
                        )
                        skipped += 1
                        continue
                    _compress = (zipfile.ZIP_STORED
                                if df.suffix.lower() in _PRECOMPRESSED_EXTS
                                else zipfile.ZIP_DEFLATED)
                    out_zip.write(df, f"drive/{df.relative_to(drive_dir)}",
                                  compress_type=_compress)
                    included += 1
                if included:
                    note = f", {skipped} skipped (too large)" if skipped else ""
                    results.append(f"Drive: {included} file(s){note}")
        except Exception as e:
            logger.exception("Drive processing failed")
            errors.append(f"Drive error: {e}")

        # ── Pre-formatted HTML reports (My Activity, Account, Gemini, etc.)
        for dir_name, out_prefix, display_name in _PASSTHROUGH_HTML_SERVICES:
            try:
                svc_dir = _find_service_dir(extract_dir, dir_name)
                if svc_dir:
                    html_files = sorted(svc_dir.rglob("*.html"))
                    if html_files:
                        for hf in html_files:
                            out_zip.write(hf, f"{out_prefix}/{hf.relative_to(svc_dir)}")
                        results.append(f"{display_name}: {len(html_files)} report(s)")
            except Exception as e:
                logger.exception("%s processing failed", display_name)
                errors.append(f"{display_name} error: {e}")

        # ── index.html ────────────────────────────────────────────────
        if results:
            out_zip.writestr(
                "index.html",
                _render_index_html(out_zip.namelist(), results).encode("utf-8"),
            )

    logger.info(
        "done: results=%s errors=%s output=%.1f MB",
        results, errors, output_path.stat().st_size / 1024 / 1024,
    )

    if not results:
        tmpdir_obj.cleanup()
        return _error_page(
            "No recognisable Google data found in this archive.<br>"
            + "<br>".join(errors)
        )

    response = send_file(
        output_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="takeout-archive.zip",
    )
    response.call_on_close(tmpdir_obj.cleanup)
    return response


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    dest_resolved = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            continue
        zf.extract(member, dest)


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    dest_resolved = dest.resolve()
    for member in tf.getmembers():
        # Reject absolute paths and traversal components upfront.
        if member.name.startswith('/') or '..' in member.name.split('/'):
            continue
        target = (dest / member.name).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            continue
        if sys.version_info >= (3, 12):
            tf.extract(member, dest, filter="data")
        else:
            tf.extract(member, dest, set_attrs=False)


def _find_service_dir(takeout_dir: Path, service_name: str) -> Path | None:
    """Locate a named Takeout service directory anywhere in the tree."""
    children = list(takeout_dir.iterdir()) if takeout_dir.is_dir() else []
    for candidate in [takeout_dir, *children]:
        if candidate.is_dir() and (candidate / service_name).is_dir():
            return candidate / service_name
    for root, _dirs, _files in os.walk(takeout_dir):
        if Path(root).name == service_name:
            return Path(root)
    return None


# ---------------------------------------------------------------------------
# Index page rendering
# ---------------------------------------------------------------------------

_HTML_ARCHIVE_INFO: dict[str, tuple[str, str, str]] = {
    "google-chat-archive.html":     ("💬", "Google Chat",       "Searchable conversation browser"),
    "google-calendar-archive.html": ("📅", "Google Calendar",   "Events with attendees, Meet links, and calendar filter"),
    "google-keep-archive.html":     ("🗒️", "Google Keep",       "Colour-coded notes with label and view filters"),
    "chrome-extensions.html":       ("🧩", "Chrome Extensions", "Links to reinstall each extension from the Web Store"),
    "contacts-archive.html":        ("👥", "Contacts",          "Searchable directory with VCF download links"),
    "mail-archive.html":            ("📧", "Gmail",             "Email header index with label and search filters"),
    "google-meet-archive.html":     ("📹", "Google Meet",       "Meeting history with duration and join status"),
    "play-store-archive.html":      ("📱", "Play Store",        "Installed apps with Play Store search links"),
}

_DOWNLOAD_INFO: dict[str, tuple[str, str, str]] = {
    "google-tasks.docx":        ("✅", "Google Tasks",        "Open as a Google Doc or in Word"),
    "chrome/Bookmarks.html":    ("🔖", "Chrome Bookmarks",   "Import into Chrome, Firefox, or Safari"),
    "chrome/Reading List.html": ("📖", "Chrome Reading List","Import into Chrome"),
}

# Subdir prefixes whose HTML files are activity reports (not our generated archives).
_REPORT_PREFIXES: dict[str, tuple[str, str]] = {
    "my-activity":    ("📊", "My Activity"),
    "google-account": ("🔑", "Google Account"),
    "gemini":         ("✨", "Gemini"),
    "device-config":  ("📱", "Device Config"),
}


def _render_index_html(names: list[str], results: list[str]) -> str:
    import datetime as dt
    import html as html_mod

    date_str = dt.date.today().strftime("%B %d, %Y")
    name_set = set(names)

    # ── Browse cards (our generated HTML archives) ──
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

    # ── Downloads ──
    download_items = []
    for fname, (icon, title, desc) in _DOWNLOAD_INFO.items():
        if fname in name_set:
            download_items.append(
                f'<li><a href="{fname}" download>{icon} <strong>{title}</strong></a>'
                f' <span class="dl-desc">— {desc}</span></li>'
            )
    # ICS
    for ics in sorted(n for n in names if n.startswith("calendar/") and n.endswith(".ics")):
        fn = ics.split("/")[-1]
        download_items.append(
            f'<li><a href="{ics}" download>📆 <strong>{html_mod.escape(fn)}</strong></a>'
            f' <span class="dl-desc">— Import into Google Calendar, Apple Calendar, etc.</span></li>'
        )
    # VCF
    for vcf in sorted(n for n in names if n.startswith("contacts/") and n.endswith(".vcf")):
        fn = vcf.split("/")[-1]
        download_items.append(
            f'<li><a href="{vcf}" download>👥 <strong>{html_mod.escape(fn)}</strong></a>'
            f' <span class="dl-desc">— Import into Gmail Contacts, Apple Contacts, etc.</span></li>'
        )
    # MBOX
    for mbox in sorted(n for n in names if n.startswith("mail/") and n.endswith(".mbox")):
        fn = mbox.split("/")[-1]
        download_items.append(
            f'<li><a href="{mbox}" download>📧 <strong>{html_mod.escape(fn)}</strong></a>'
            f' <span class="dl-desc">— Import into Thunderbird, Apple Mail, or any MBOX client</span></li>'
        )
    # Google Wallet PDFs
    for pdf in sorted(n for n in names if n.startswith("google-wallet/") and n.endswith(".pdf")):
        fn = pdf.split("/")[-1]
        download_items.append(
            f'<li><a href="{pdf}" download>💳 <strong>{html_mod.escape(fn)}</strong></a>'
            f' <span class="dl-desc">— Google Wallet pass</span></li>'
        )

    # ── Activity reports (pass-through HTML) ──
    report_sections: list[str] = []
    for prefix, (icon, label) in _REPORT_PREFIXES.items():
        files = sorted(n for n in names if n.startswith(f"{prefix}/") and n.endswith(".html"))
        if files:
            links = ' '.join(
                f'<a href="{n}">{n.split("/")[-1]}</a>' for n in files
            )
            report_sections.append(
                f'<li>{icon} <strong>{label}</strong>: {links}</li>'
            )

    # ── Drive files ──
    drive_files = sorted(n for n in names if n.startswith("drive/"))
    drive_section = ''
    if drive_files:
        drive_rows = ''.join(
            f'<li><a href="{n}" download>{html_mod.escape(n[6:])}</a></li>'
            for n in drive_files
        )
        drive_section = (
            f'<h2>Drive Files ({len(drive_files)})</h2>'
            f'<ul class="dl-list">{drive_rows}</ul>'
        )

    # ── Assemble ──
    archives_html = ''
    if archive_cards:
        archives_html = f'<h2>Browse</h2><div class="card-grid">{"".join(archive_cards)}</div>'

    downloads_html = ''
    if download_items:
        downloads_html = (
            f'<h2>Import &amp; Download</h2>'
            f'<ul class="dl-list">{"".join(download_items)}</ul>'
        )

    reports_html = ''
    if report_sections:
        reports_html = (
            f'<h2>Activity Reports</h2>'
            f'<ul class="dl-list">{"".join(report_sections)}</ul>'
        )

    summary = html_mod.escape(" · ".join(results))

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
  border-radius: 10px; padding: 16px 20px; text-decoration: none; color: var(--text);
  width: 260px; transition: box-shadow .15s, border-color .15s;
}}
.card:hover {{ box-shadow: 0 2px 10px rgba(0,0,0,.08); border-color: #b0c4e8; }}
.card-icon {{ font-size: 28px; line-height: 1; flex-shrink: 0; margin-top: 1px; }}
.card-title {{ font-weight: 600; font-size: 14px; margin-bottom: 3px; color: var(--accent); }}
.card-desc {{ font-size: 12px; color: var(--muted); }}
.dl-list {{ list-style: none; padding: 0; margin: 0 0 36px; display: flex; flex-direction: column; gap: 8px; }}
.dl-list a {{ color: var(--accent); text-decoration: none; }}
.dl-list a:hover {{ text-decoration: underline; }}
.dl-desc {{ color: var(--muted); font-size: 13px; }}
</style>
</head>
<body>
<h1>Takeout Archive</h1>
<p class="sub">Generated {date_str} &nbsp;·&nbsp; {summary}</p>
{archives_html}
{downloads_html}
{reports_html}
{drive_section}
</body>
</html>"""


def _error_page(message: str) -> tuple:
    error_block = f'<div class="error-box">{message}</div>'
    return _UPLOAD_PAGE.replace("{error_block}", error_block), 400


if __name__ == "__main__":
    app.run(debug=True)
