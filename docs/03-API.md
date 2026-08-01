# 03 — API Reference

Base URL: `https://drive.vikashbuilds.in` (Cloudflare tunnel → the 24/7 runner)

Auth: `X-API-Key: <key>` header on all write endpoints (see `06-SECURITY.md`).

Keys can be scoped in the secret config (`key:scope`); the client always sends
just the key name — `key:upload` → send `key` (upload-only), plain keys are
admin (upload, delete, dispatch). Send `private=true` (form field or query
param) to store the file privately.

---

## `POST /v1/upload`

Upload a file (multipart/form-data). Max 2 GB per file.

```
curl -X POST https://drive.vikashbuilds.in/v1/upload \
  -H "X-API-Key: $KEY" \
  -F "file=@hero-banner.png" \
  -F "expire_days=30"          # optional: auto-delete after 30 days
  -F "private=true"            # optional: private storage (default false)
```

**Response 200:**
```json
{
  "id": "9f2k1a",
  "name": "hero-banner.png",
  "size": 2457600,
  "mime": "image/png",
  "url": "https://raw.githubusercontent.com/vikashbuilds/gitdrive-1/main/files/2026/08/a3/9f2k1a_hero-banner.png",
  "deduped": false,
  "private": false,
  "expires_at": null
}
```

- `deduped: true` → file already existed (same SHA-256 + same visibility); returns the existing record, **stored 0 extra bytes**
- **Private uploads** (`private=true`) land in `VikashBuilds/private-p*` repos, `url` becomes `/v1/download/<id>`, and the file is ONLY reachable via that authenticated proxy — never a public GitHub URL. Private files never dedupe against public ones (or vice versa).

---

## `GET /v1/file/{id}`

Fetch metadata for one file.

**Response 200:** same object as upload, plus `download_count`, `status` (`ready` | `compressing` | `compressed`), `created_at`, `private`.

**404:** unknown id.

---

## `GET /v1/files?limit=50&offset=0&mime=image/png`

List files (newest first). Optional `mime` filter. Returns `{ items: [...], total }`. Each item includes `private`.

---

## `DELETE /v1/file/{id}`

Delete a file.

- git-stored → commits the deletion (URL stops working; space NOT freed — see `02-STORAGE.md`)
- release-stored → deletes the release asset (space freed immediately)
- Response: `{ "deleted": true }`

---

## `GET /v1/stats`

Usage summary (no auth needed):

```json
{
  "files": 128,
  "bytes_total": 1081344000,
  "bytes_git": 734003200,
  "bytes_release": 347340800,
  "top_files": [{ "id": "9f2k1a", "name": "clip-final.mp4", "downloads": 342 }],
  "repo": "gitdrive-1"
}
```

---

## `GET /v1/health`

Liveness probe: `{ "ok": true, "uptime_s": 12345 }` — used by GridLive/uptime monitors.

---

## Error Format

```json
{ "error": "message" }
```

| Code | When |
|---|---|
| 401 | missing/invalid API key |
| 403 | upload-scoped key used for an admin action (delete/dispatch) |
| 413 | file > 2 GB |
| 415 | blocked mime type (see security doc) |
| 429 | rate limited (default 60 uploads/hour/key) |
| 502 | storage repo write failed |

---

## `GET /v1/download/{id}`

Stream any file as one continuous download:

- public git/release files → 302 redirect to the CDN URL
- **archive files → server concatenates all release parts on the fly** (correct `Content-Length`, attachment filename)
- **private files → proxied through authenticated GitHub API** (send `X-API-Key`); the private GitHub URL is never exposed

```
curl -O https://drive.vikashbuilds.in/v1/download/abc123
```

| Code | When |
|---|---|
| 401 | private file without a valid `X-API-Key` |
| 404 | private release/parts missing |

---

## Archive Tier (unlimited durable storage)

Split any file into ≤1.9 GB parts → Release assets (no limit, no expiry) → served back as ONE file.

### `POST /v1/archive/start?name=X&total_parts=N`

Creates the archive + its release. Returns `{ id, tag, total_parts, repo }`.

### `POST /v1/archive/{id}/part?index=0`

Upload one ≤2 GB part (multipart). Parts must come in order (0,1,2...). Returns `{ index, parts, url }`.

### `POST /v1/archive/{id}/complete`

Finalizes; returns `{ id, parts, size, url: "/v1/download/<id>" }`.

### Easiest usage — the CLI

```bash
python scripts/upload_archive.py big-backup.iso \
  --api https://drive.vikashbuilds.in --key key-vikash
# 100 GB file → ~56 parts → one permanent download URL
# --resume  → continues an interrupted upload (state file: big-backup.iso.gdstate)
```

---

## Telegram Bot Surface (planned in v0.2)

- `/upload <file>` — bot downloads the media from Telegram and uploads it for you
- `/drive <id>` — returns the share link
- `/delete <id>`
- `/stats`
