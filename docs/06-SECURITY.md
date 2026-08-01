# 06 — Security

## 🔑 Authentication

| Surface | Auth |
|---|---|
| `POST /v1/upload` (+ chunked & archive endpoints) | `X-API-Key` header; **any** valid key |
| `DELETE /v1/file/{id}`, `POST /v1/dashboard/dispatch` | `X-API-Key` header; **admin** keys only |
| `GET /v1/download/{id}` (any file) | `X-API-Key` header; **any valid key** |
| `GET /v1/file/{id}`, `/v1/files`, `/v1/stats`, `/v1/dashboard` | `X-API-Key` header; **admin** keys only |
| `GET /v1/health` | Public |

The dashboard (GitHub Pages) is **locked behind a gate**: you must enter your
API key (and optionally your master password) before any file list, metadata,
upload or delete is possible. The key lives only in your browser's
localStorage — anyone hitting `gitdrive.vikashbuilds.in` sees nothing but the
gate.

**Key scopes** — keys in the `GITDRIVE_API_KEYS` secret may carry a scope suffix
(`key:scope`). Clients send just the key name (no suffix):

| Secret entry | Client sends | Powers |
|---|---|---|
| `key-vikash` | `key-vikash` | **admin**: upload, delete, dispatch |
| `key-bot:upload` | `key-bot` | **upload-only**: cannot delete files or dispatch workers (403) |

- Keys are stored in GitHub Secrets only — never in code or repos
- Rotate by editing the secret; multiple comma-separated keys allowed (one per app/bot)
- `GITDRIVE_DB_URL`, `GH_PAT`, `RELAY_TOKEN` never leave the runner env

## 🔐 End-to-End Encryption

Uploads with `enc=true` are encrypted **in the browser** before a single byte
reaches the server. The server and GitHub only ever see ciphertext.

- **Master key** — PBKDF2-SHA256 (200k iterations) from your master password;
  the salt is stored in localStorage (`gd_enc_salt`) so the same password
  always derives the same key. The key itself never leaves the tab.
- **Per-file key** — HKDF-SHA256(`gitdrive:e2e:<fileId>`) from the master key,
  so each file gets a unique AES-256-GCM key and the id can live in the URL.
- **Format** — each file is a `GDENC1` header (magic + 1 MiB chunk size)
  followed by independent 1 MiB AES-256-GCM chunks with a 12-byte IV
  (counter at bytes 8-11). Chunks are independent → chunked uploads and
  streamed downloads decrypt correctly.
- **Round-trip verified end-to-end** against the live API (encrypt → upload →
  download → decrypt → byte-identical; wrong password → rejected).
- **What it protects:** GitHub admins, the runner operator, anyone with a
  public/copied URL — nobody can read the content without your password.
- **What it does NOT protect:** the metadata (name, size, date) is plaintext;
  the file id must be kept (the key derives from it); passwords must be strong
  (PBKDF2 makes brute force ~200k× slower, but a weak password stays weak).
- On the dashboard: unlock with password → new uploads are encrypted and show
  a 🔒; downloading an encrypted file requires unlocking first; the lock pill
  wipes the key from the tab.

## 🔒 Private Files

Uploads with `private=true` are stored in `PRIVATE_STORAGE_REPOS`
(`VikashBuilds/private-p1`, `private-p2` — private GitHub repos):

- No public URL is ever created — the stored URL is `/v1/download/<id>`
- `/v1/download` proxies private files through **authenticated** GitHub API calls
  (raw contents / release assets), so the private repo URL is never exposed
- Private and public uploads **never dedupe against each other** (a private file
  can't inherit a public URL)
- Big private files (2-12 GB) stay as durable parts in the private repo; pool
  nodes never hold or serve them, and `checkin_set` never anchors them into
  public repos
- Compression and cache-backup workers skip private files (they would re-host
  the content in public places)
- Download from the dashboard (needs your API key) or any `X-API-Key` request
- Telegram bot: `/private` toggles private uploads; private files show no link

## 🤖 Telegram Bot Lockdown

- `BOT_ALLOW_CHAT_ID` secret (comma-separated) = owner allowlist; any other
  chat is ignored entirely. Unset = open to everyone (not recommended).

## 🚦 Abuse Protection

| Threat | Mitigation |
|---|---|
| Anonymous uploads | API key required on all writes |
| Upload flooding | 60 uploads/hour per key → 429; configurable in `server.py` |
| Disk filling (git) | 25 MB git threshold, 2 GB hard cap; monthly storage report in prune log |
| Malicious file types | Mime allowlist — `.exe/.sh/.bat/.html/.svg/.js` blocked by default (XSS + malware vectors), configurable |
| Secret files uploaded by mistake | Use the private upload option; dashboard default remains public |
| Full visibility of the dashboard URL | Gate + API key required for every metadata call (see above) |
| Anyone with a share link reading content | Public links are the point; use `enc=true` + password or `private=true` for real secrecy |
| Key in URL/logs | Headers only; server never logs the key or file bytes |

## 🗄️ Database Backup

- `.github/workflows/db-backup.yml` dumps the Postgres DB nightly (`pg_dump | gzip`)
  into a private repo as a `db-YYYYMMDD` release, keeping 14 days.

## 🏷️ Public vs Private Files

- **Public:** anything you want a shareable link for (assets, posters, clips, deliverables for clients)
- **Private:** documents, credentials, anything you don't want guessable/leakable
- Private repos still get 2,000 min/mo — enough for a low-volume personal drive; use sparingly

## 🧹 Data Hygiene

- `expires_at` on upload → auto-pruned daily
- Releases can be deleted any time (space freed immediately)
- Git deletions are permanent-in-history — the prune script commits deletes but you should occasionally archive closed repos instead of rewriting history

## 📜 GitHub TOS Notes (the gray area)

- Running a personal 24/7 API on Actions is the same gray area as your n8n — keep it **personal scale**, public repo, no abuse
- Storage repos must stay under ~5 GB git size (soft limit)
- Don't serve copyrighted content (movies/songs) or run a public mass-upload service — that's how accounts get flagged
