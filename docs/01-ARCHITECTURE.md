# 01 — Architecture

## 🧠 The Big Picture

GitDrive = **a 24/7 API runner** (always on, chain pattern) + **burst matrix workers** (on-demand compression) + **Postgres** (metadata + queue) + **git/Releases** (the actual bytes) + **GitHub CDN** (delivery).

```
                     ┌────────────────────────────────────────────┐
                     │        gitdrive  (24/7 chain runner)       │
   Telegram bot ────►│                                            │
   curl / API ──────►│   FastAPI server :8080                     │
   Webhook ─────────►│   cloudflared tunnel → drive.vikashbuilds.in│
                     │                                            │
                     │   1. hash (dedupe)         4. record in DB │
                     │   2. small → git commit    ──► Postgres    │
                     │   3. large → Release upload                 │
                     └───────────┬────────────────────────────────┘
                                 │ file stored in
                     ┌───────────▼───────────┐    ┌──────────────────┐
                     │ storage repos         │    │ compress-workers │
                     │ gitdrive-1..N         │◄───│ matrix (10 jobs) │
                     │ (git + Releases)      │    └──────────────────┘
                     └───────────┬───────────┘            ▲
                                 │ served via            │ jobs table
                     ┌───────────▼───────────┐            │ (compress queue)
                     │ GitHub CDN:           │   ┌────────┴────────┐
                     │ raw.githubusercontent │   │  Aiven Postgres │
                     │ + release assets     │   │  files + jobs   │
                     └──────────────────────┘   └─────────────────┘
```

## 🔀 Upload Data Flow (small file)

1. Client `POST /v1/upload` with `X-API-Key` header + multipart file
2. Server SHA-256-hashes bytes → checks `files` table → **deduped** if exists
3. Server writes file into its cloned storage repo working copy → `git add/commit/push`
4. Server inserts row into `files` table (id, sha, path, url, mime, size)
5. If mime is image/video and size > 200 KB → inserts a `compress` job into `jobs`
6. Returns `{ id, url }` — URL is `https://raw.githubusercontent.com/<repo>/main/files/...`

## 🔀 Upload Data Flow (large file > 25 MB)

1-2. Same hashing + dedupe
3. Server posts file as an **asset to a batched release** (`assets-YYYYMMDDHH` created on demand) via the Releases API — assets can be up to 2 GB and don't bloat git history
4-6. Same DB insert + compress job + response (URL is the release asset download URL)

## 🗜️ Compression Pipeline

1. `compress-workers.yml` runs on schedule + dispatch
2. **Prepare job** → `pick_jobs.py` selects up to 10 `queued` compress jobs from Postgres → emits dynamic matrix
3. **Matrix workers** (up to 10 parallel) → each `compress_worker.py`:
   - downloads original bytes from its URL
   - image → Pillow re-encode (WebP/AVIF/JPEG quality 80, resize if > 2048px)
   - video → FFmpeg (h264 crf 28, scale ≤ 1080p)
   - if result is ≥ 10% smaller → commits the optimized version, updates the `files` row (new url/size), marks job done; else marks job done (kept original)
4. Original is superseded — the URL in the DB always points at the best version

## 🧹 Prune Pipeline

- `prune.yml` daily → `prune.py`:
  - deletes files where `expires_at < now()` (git commit delete / release asset delete)
  - prints storage report (files, bytes, top downloads) to the job log
  - sends a summary to Telegram via a webhook secret (optional)

## 📦 Storage Repos

- `vikashbuilds/gitdrive-1` (first), later `gitdrive-2`... — each adds ~6 GB and its own concurrency/cache budget
- Layout: `files/YYYY/MM/<sha[:2]>/<id>-<name>`
- Releases: one batched release per day/hour for large files, tagged `assets-YYYYMMDDHH`

## ⚙️ Key Decisions

| Decision | Why |
|---|---|
| Small files → git, big files → Releases | Git blocks files > 100 MB; releases handle up to 2 GB |
| Never artifacts for storage | 90-day retention would delete your files |
| Public storage repos | Unlimited minutes + free CDN; sensitive files stay private via auth instead |
| Postgres for metadata | Free (Aiven), searchable, the queue for workers |
| Server does commits directly | No extra hop — the 24/7 runner has git + token; fastest path |
| Dynamic matrix for compression | Only pay for workers when there's work |
