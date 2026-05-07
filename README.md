# Takeout Migration

A stateless Flask web app that converts a [Google Takeout](https://takeout.google.com) ZIP or TGZ export into
human-readable archives you can keep forever — without any accounts, databases,
or cloud services involved.

I know there are a load of these tools.
Since Google changes the formats over time, there are varying degrees of success with any of them.
This one was written and tested for the formats around May 2026.
No guarantees.

![alt text](image.png)

## What it produces

The app processes whichever services are present in your export and skips the rest,
so a partial Takeout is fine. Everything lands in a single downloadable ZIP with an
`index.html` for navigation.

### Rendered archives (data → searchable HTML)

| Source | Output file(s) | Notes |
|---|---|---|
| Google Chat | `google-chat-archive.html` | Single-file SPA; sidebar navigation + full-text search. Works in Google Drive preview. |
| Google Tasks | `google-tasks.docx` | Task lists with checkboxes, due dates, and notes. Active tasks first, then completed. Opens as a Google Doc. |
| Google Calendar | `google-calendar-archive.html` | Events with attendees, Meet links, recurring-event labels. Filterable by calendar. Sidebar links to raw ICS files for re-import. |
| Google Keep | `google-keep-archive.html` | Notes with colours, pin/archive/trash states, checklists, and label filtering. |
| Google Contacts | `google-contacts-archive.html` | Card grid with email/phone/org; group sidebar; search. Raw VCF files linked for import into any contacts app. |
| Gmail | `gmail-archive.html` | Header index (date, sender, subject, labels) for up to 10,000 most-recent messages. Label filter dropdown. MBOX file linked for import into Thunderbird or Apple Mail (if under 100 MB). |
| Google Meet | `google-meet-archive.html` | Conference history table — date, duration, meeting code, participation status. |
| Google Play Store | `google-play-store-archive.html` | All installed apps with direct Play Store search links; device list in sidebar. |
| Chrome Extensions | `chrome-extensions.html` | Lists every installed extension with a direct link to its Chrome Web Store install page. |

### Passed through unchanged

| Source | Output path | Notes |
|---|---|---|
| Chrome Bookmarks | `chrome/Bookmarks.html` | Importable directly into any browser. |
| Chrome Reading List | `chrome/Reading List.html` | Importable directly into any browser. |
| Google Calendar ICS | `calendar/*.ics` | One file per calendar; importable into any calendar app. |
| Google Contacts VCF | `contacts/*.vcf` | One file per contact group; importable into any contacts app. |
| Gmail MBOX | `mail/*.mbox` | Full message archive; importable into Thunderbird, Apple Mail, etc. (included if under 100 MB). |
| Google Drive | `drive/**` | All files passed through as-is. |
| Google Wallet | `google-wallet/*.pdf` | Pass/ticket PDFs passed through. |

### HTML reports (passed through from Takeout)

These services export HTML reports directly — the app includes them in the output ZIP as-is:

- **My Activity** — search and browsing history
- **Google Account** — account settings and data summary
- **Gemini** — conversation history
- **Android Device Configuration** — device settings backup

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
flask run          # http://127.0.0.1:5000
# or
python app.py
```

Open the URL, drop in your Takeout ZIP or TGZ, and download the result archive.

## Docker

```bash
docker compose up --build
# http://localhost:8000
```

The container runs as a non-root user with a read-only filesystem; all
processing happens in a tmpfs-backed `/tmp`. Worker count scales
automatically with available CPUs (`(2 × CPU) + 1`); override with
`WEB_CONCURRENCY=N` in `docker-compose.yml`.

## Getting a Takeout export

1. Go to [https://takeout.google.com](https://takeout.google.com)
2. Deselect all, then select the products you want
3. Export as ZIP or TGZ and download

You can export multiple products in a single archive or run them through
separately — the app handles both. ZIP and TGZ formats are both accepted.

## Command-line (Chat only)

The chat processor also works as a standalone script that produces a
multi-page static HTML site:

```bash
python google_chat_to_html.py ~/Downloads/Takeout -o ./chat-archive
```

Output layout:

```
chat-archive/
  index.html                    # sortable conversation list
  assets/style.css
  conversations/<slug>.html     # one page per conversation
  files/<slug>/<name>           # copied attachments
```

## Architecture

| Module | Responsibility |
|---|---|
| `app.py` | Flask routes; extracts the upload ZIP/TGZ in a per-request tempdir, calls each processor, streams back a result ZIP. No state persists between requests. |
| `google_chat_to_html.py` | Parses `group_info.json` / `messages.json`; `render_single_page_html()` builds the SPA with embedded search index; `generate_static_site()` is the CLI path. |
| `tasks.py` | Parses `Tasks.json` (single-file `tasks#taskLists` format); renders DOCX via `python-docx`. |
| `calendar_archive.py` | Parses ICS files with custom line-unfolding; handles RRULE, DATE vs DATETIME, UTC vs floating time; renders a filterable SPA. |
| `keep_archive.py` | Parses per-note JSON files; renders colour-coded masonry card layout with view/label/search filtering. |
| `chrome_archive.py` | Passes `Bookmarks.html` / `Reading List.html` through unchanged; renders `Extensions.json` as an HTML page with Web Store links. |
| `contacts_archive.py` | Unfolds and parses VCF 3.0 files; deduplicates across multiple VCF exports; renders a card grid with group sidebar. |
| `mail_archive.py` | Line-by-line MBOX header scan (skips bodies for efficiency); decodes MIME headers; renders a searchable table with label filter. |
| `meet_archive.py` | Parses `conference_history_records.csv`; renders a sortable meeting history table. |
| `play_store_archive.py` | Groups `Installs.json` records by app title across devices; renders app list with Play Store search links and device sidebar. |
| `gunicorn.conf.py` | Gunicorn configuration: dynamic worker count, request recycling, stdout logging. |

## Security notes

- Uploaded files are extracted into a `tempfile.TemporaryDirectory` that is
  deleted when the request completes.
- ZIP and TGZ extraction both guard against path-traversal attacks.
- Flask's `MAX_CONTENT_LENGTH` is set to 500 MB.
- The app carries no session state and writes nothing to disk permanently.
- The Docker image runs as a non-root user on a read-only filesystem.
