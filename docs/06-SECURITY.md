# 06 — Security

## 🔑 Authentication

| Surface | Auth |
|---|---|
| `POST /v1/upload` (+ chunked & archive endpoints) | `X-API-Key` header; **any** valid key |
| `DELETE /v1/file/{id}`, `POST /v1/dashboard/dispatch` | `X-API-Key` header; **admin** keys only |
| `GET /v1/download/{id}` (private file) | `X-API-Key` header; otherwise 401 |
| `GET /v1/file/{id}`, `/v1/files`, `/v1/stats`, `/v1/health`, public downloads | Public (links are meant to be shareable) |

**Key scopes** — keys in the `GITDRIVE_API_KEYS` secret may carry a scope suffix
(`key:scope`). Clients send just the key name (no suffix):

| Secret entry | Client sends | Powers |
|---|---|---|
| `key-vikash` | `key-vikash` | **admin**: upload, delete, dispatch |
| `key-bot:upload` | `key-bot` | **upload-only**: cannot delete files or dispatch workers (403) |

- Keys are stored in GitHub Secrets only — never in code or repos
- Rotate by editing the secret; multiple comma-separated keys allowed (one per app/bot)
- `GITDRIVE_DB_URL`, `GH_PAT`, `RELAY_TOKEN` never leave the runner env

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
