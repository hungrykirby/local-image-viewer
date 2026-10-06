# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

LAN-only image viewer: a Flask server (`server.py`) on the PC serves images from user-selected local folders to a single-page browser UI (`index.html`) used on iPad/Android/PC. It also receives images pushed from the Android app "Nightdrop" (`receiver.py`). No build step, no test suite, no linter config. Dependencies: Flask, optionally waitress (`requirements.txt`). User-facing text, comments, and README are in Japanese.

## Commands

Primary target is Windows/PowerShell (per README):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py   # http://127.0.0.1:5000 (binds 0.0.0.0:5000)
```

On macOS/Linux: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python server.py`. `server.py` uses waitress if importable, else Flask's dev server.

Config comes from `.env` next to `server.py` (see `.env.example`; real env vars take precedence): `VIEWER_UPLOAD_TOKEN`, `VIEWER_LIBRARY_DIR`, `VIEWER_DATA_DIR`, `VIEWER_MAX_UPLOAD_MB`, `VIEWER_HOST`, `VIEWER_PORT`. The upload endpoints return 503 if the token is unset.

For ad-hoc verification, copy `server.py`/`receiver.py`/`index.html` to a temp dir and use `server.app.test_client()` there, since the server writes `folders.json`, `library/`, `data/` next to itself.

## Architecture

- **Index-based image addressing.** The server holds a global `IMAGE_LIST` (absolute paths, guarded by `IMAGE_LOCK`). The client never sees image paths: `/api/images`, `/api/rescan`, `POST /api/folders` return `{count, indices}`, the client shuffles `indices` (`resetIndices` in `index.html`) and fetches `/api/image/<index>`. Indices must stay stable between rescans, so: received images are *appended*, and deleted images are set to `None` (excluded from `indices`, 404 on fetch). A rescan rebuilds the list and invalidates all indices.
- **Folder config** persists in `folders.json` (gitignored; machine-specific absolute paths). `scan_images` skips dot-directories, which is what hides `library/.incoming` and `library/.trash`.
- **Folder picker** (`/api/browse`, `/api/special-folders`) browses the server's filesystem. Special folders always include the receive library; Pictures/Downloads only on Windows.
- **Nightdrop receiving** (`receiver.py`, routes `/api/health`, `/api/upload` in `server.py`):
  - Auth: `Authorization: Bearer <VIEWER_UPLOAD_TOKEN>`, compared with `hmac.compare_digest`.
  - Response contract is the `status` field, mapped to HTTP codes by `STATUS_HTTP`. `saved` / `duplicate` / `deleted` mean the app marks the image as sent; anything else means the app retries. Don't change these names without coordinating with the app.
  - Write path: stream to `library/.incoming/*.part` while hashing → verify size, SHA-256, magic bytes → under a lock, reserve a unique name with `O_EXCL` (`name (2).ext` on collision) → `os.replace` → `os.utime` from `modified_at` (epoch ms) → insert into SQLite. Leftover `.part` files are removed on startup.
  - Dedup/tombstones: `data/nightdrop.db` table `images(sha256 PK, state stored|deleted, ...)`. Dedup is by hash only, regardless of batch. Images already in the library that were not received through the API are not in the DB.
  - Viewer delete (`POST /api/image/<index>/delete`) moves the file to `library/.trash/YYYY-MM-DD/` and upserts `state=deleted`, so a re-send returns `deleted`. This works for any viewed image, not only received ones.
  - Log: `data/upload.log`, tab-separated (time, status, batch, filename, sha256, remote, detail).
- **Frontend** is a single self-contained `index.html` (inline CSS + vanilla JS, no CDN/framework). Two stacked slots (`slot-current` / `slot-next`) crossfade; the next image is preloaded into `img-next`.

## Security constraints to preserve

- Viewer endpoints (browse, folders, delete) have no auth by design; intended for a trusted LAN only. Only the upload/health endpoints require the token.
- `Flask(__name__, static_folder=None)` is deliberate: do not enable static serving of the project directory (would expose `.git`, `folders.json`, `.env`, `data/`). Serve files only via explicit routes.
- Batch names are validated with a whitelist (`BATCH_PATTERN`) plus an `is_within` check against the library; filenames are reduced to a basename and sanitized for Windows. Keep both layers.
