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
  border: 2px dashed var(--border); border-radius: 8px;
  padding: 40px 24px; text-align: center; cursor: pointer;
  transition: border-color 0.15s, background 0.15s; margin-bottom: 20px;
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
#progress { display: none; color: var(--muted); font-size: 13px; margin-top: 12px; text-align: center; }
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
        <input type="file" name="takeout_zip" id="file-input" accept=".zip,.tgz,.gz">
        <div class="icon">📦</div>
        <div class="label">Drop your Takeout ZIP or TGZ here</div>
        <div class="hint">or click to choose a file &nbsp;·&nbsp; .zip and .tgz both accepted</div>
      </div>
      <div id="file-name"></div>
      <button type="submit" id="submit-btn" disabled>Convert &amp; Download</button>
      <p id="progress">Processing… large exports (especially with Mail) may take up to a minute.</p>
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
  var dropZone = document.getElementById('drop-zone');
  var fileInput = document.getElementById('file-input');
  var fileName  = document.getElementById('file-name');
  var submitBtn = document.getElementById('submit-btn');
  var form      = document.getElementById('upload-form');
  var progress  = document.getElementById('progress');

  dropZone.addEventListener('click', function () { fileInput.click(); });
  dropZone.addEventListener('dragover', function (e) { e.preventDefault(); dropZone.classList.add('over'); });
  dropZone.addEventListener('dragleave', function () { dropZone.classList.remove('over'); });
  dropZone.addEventListener('drop', function (e) {
    e.preventDefault(); dropZone.classList.remove('over');
    if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; onFile(e.dataTransfer.files[0]); }
  });
  fileInput.addEventListener('change', function () { if (this.files.length) onFile(this.files[0]); });

  function onFile(f) {
    fileName.textContent = f.name + '  (' + (f.size / 1024 / 1024).toFixed(1) + ' MB)';
    submitBtn.disabled = false;
  }
  form.addEventListener('submit', function () { submitBtn.disabled = true; progress.style.display = 'block'; });
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
    f = request.files.get("takeout_zip")
    if not f or not f.filename:
        return _error_page("No file was uploaded.")

    fname_lower = f.filename.lower()
    is_zip = fname_lower.endswith(".zip")
    is_tgz = fname_lower.endswith(".tgz") or fname_lower.endswith(".tar.gz")
    if not (is_zip or is_tgz):
        return _error_page("Please upload a .zip or .tgz file exported from Google Takeout.")

    # TemporaryDirectory managed manually so send_file can stream after we return.
    tmpdir_obj = tempfile.TemporaryDirectory()
    try:
        return _process_upload(f, fname_lower, is_zip, tmpdir_obj)
    except MemoryError:
        tmpdir_obj.cleanup()
        logger.error("OOM processing %s", f.filename)
        return _error_page(
            "The server ran out of memory processing this archive. "
            "Try a smaller export or contact the administrator."
        )
    except Exception:
        tmpdir_obj.cleanup()
        logger.exception("Unhandled error processing %s", f.filename)
        return _error_page("An unexpected error occurred. Please try again.")


def _process_upload(f, fname_lower: str, is_zip: bool, tmpdir_obj: tempfile.TemporaryDirectory):
    tmp = Path(tmpdir_obj.name)
    archive_path = tmp / "upload"
    f.save(str(archive_path))
    upload_mb = archive_path.stat().st_size / 1024 / 1024
    logger.info("processing upload: name=%r size=%.1f MB", f.filename, upload_mb)

    extract_dir = tmp / "extracted"
    extract_dir.mkdir()

    if is_zip:
        try:
            with zipfile.ZipFile(archive_path) as zf:
                _safe_extract_zip(zf, extract_dir)
        except zipfile.BadZipFile:
            tmpdir_obj.cleanup()
            return _error_page("The uploaded file is not a valid ZIP archive.")
    else:
        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                _safe_extract_tar(tf, extract_dir)
        except tarfile.TarError as e:
            tmpdir_obj.cleanup()
            return _error_page(f"The uploaded file is not a valid TGZ archive: {e}")

    # Delete the raw upload now — we only need the extracted tree from here on.
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
            chat_html = render_single_page_html(conversations, owner)
            out_zip.writestr("google-chat-archive.html", chat_html.encode("utf-8"))
            results.append(
                f"Google Chat: {len(conversations)} conversations, "
                f"{sum(len(c.messages) for c in conversations)} messages"
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
                    out_zip.write(pdf, f"google-wallet/{pdf.relative_to(wallet_dir)}")
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
                    out_zip.write(df, f"drive/{df.relative_to(drive_dir)}")
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
