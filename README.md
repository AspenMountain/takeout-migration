# Takeout Migration

A stateless Flask web app that converts a Google Takeout ZIP export into
human-readable archives you can keep forever — without any accounts, databases,
or cloud services involved.

## What it produces

| Source | Output | Works in Google Drive? |
|---|---|---|
| Google Chat | `google-chat-archive.html` — single-file SPA with sidebar navigation | ✅ Drive preview |
| Google Tasks | `google-tasks.docx` — task lists with checkboxes, due dates, notes | ✅ Opens as Google Doc |

The output is a single ZIP containing whichever of the above were found in your
export.

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

Then open the URL, drop in your Takeout ZIP, and download the result.

## Getting a Takeout ZIP

1. Go to [https://takeout.google.com](https://takeout.google.com)
2. Deselect all, then select **Google Chat** and/or **Tasks**
3. Export as ZIP and download

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

- **`app.py`** — Flask routes; extracts the ZIP in a temp directory, calls
  the processors, streams back a result ZIP. No state is written to disk
  between requests.
- **`google_chat_to_html.py`** — Parses `group_info.json` / `messages.json`
  from the Chat export and renders HTML. `render_single_page_html()` produces
  the self-contained SPA; `generate_static_site()` is the CLI path.
- **`tasks.py`** — Parses per-list JSON files from the Tasks export and
  renders a DOCX via `python-docx`.

## Security notes

- Uploaded files are extracted into a `tempfile.TemporaryDirectory` that is
  deleted when the request completes.
- ZIP extraction guards against path-traversal (zip-slip) attacks.
- Flask's `MAX_CONTENT_LENGTH` is set to 500 MB.
- The app carries no session state and writes nothing to disk permanently.
