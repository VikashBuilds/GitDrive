# 13. DEPLOY — Day-One Checklist

Everything below is manual once; the system runs itself after. Do it in order.

## 0. Prereqs (one-time)

| Step | Where | Result |
|---|---|---|
| Create `vikashbuilds/storage-01` … `storage-06` | GitHub web | 6 empty repos |
| Create secrets in `vikashbuilds/gitdrive` | repo → Settings → Secrets | see list below |
| Create PAT (`repo`, `workflow`, `actions:write`) | GitHub → Developer settings | `GH_PAT` value |
| Start a free Postgres (Neon/Turso/Supabase) | provider | `GITDRIVE_DB_URL` |

Secrets needed: `GITDRIVE_DB_URL`, `GITDRIVE_API_KEYS`, `STORAGE_REPOS`, `GH_PAT`, `RELAY_TOKEN`.

## 1. Schema

```bash
psql "$GITDRIVE_DB_URL" -f scripts/queue.sql
```

## 2. Seed

```bash
python scripts/seed_sets.py --count 20     # creates the 20 sets
# then activate the carousel chains in DB (or let supervisor do it):
# UPDATE nodes SET status='END' ... or just run carousel manually once
```

## 3. Start the API

1. Trigger `upload-service.yml` manually → get the **cloudflared tunnel URL** from job logs.
2. Paste it into the DB:
   ```sql
   INSERT INTO meta (key, value) VALUES ('tunnel_url', 'https://drive-xxx.trycloudflare.com')
   ON CONFLICT (key) DO UPDATE SET value = excluded.value;
   ```
3. Health check:
   ```bash
   curl https://drive-xxx.trycloudflare.com/v1/stats -H "X-API-Key: key-xxx"
   ```

## 4. Day-1 smoke test — the proof

```bash
# small file → git CDN
curl -F "file=@test.txt" https://…/v1/upload -H "X-API-Key: key-xxx"

# big file → archive tier (unlimited)
python scripts/upload_archive.py my-backup.iso \
  --api https://… --key key-xxx

# automated full-cycle verification (runs daily from now on)
# → Actions tab → verify-cycle → Run workflow → expect "0 failed"
```

## 5. Start the factory (workers)

| Workflow | Trigger | Purpose |
|---|---|---|
| `carousel-node.yml` | dispatch `node_id=carousel-01` | relay pool + handoff (start 3 chains, ~45 min apart) |
| `carousel-supervisor.yml` | auto (hourly) | restarts dead chains automatically |
| `compress-workers.yml` | auto (continuous) | image/video compression farm |
| `prune.yml` | auto (daily) | expiry + stats to Telegram |
| `cache-backup.yml` | auto (daily) | 10 GB cache mirror layer |

## 6. Watch it work

1. Open `GridLive` (stats endpoint) — watch bytes grow.
2. Upload a 3 GB file → wait 1 handoff → confirm it survives on the *next* node.
3. Delete a file → confirm it disappears from disk on the next check-in (`deleted pool file` in logs).
4. Kill a runner mid-shift → supervisor re-dispatches within the hour.

## 7. Known honest limits (don't be surprised)

- First cloudflared URL changes each deploy → re-update `tunnel_url` in DB.
- **The tunnel URL rotates every ~6 min** (chain pattern: each run starts a new tunnel). Always read the URL from `meta` — the verify harness and carousel do this automatically.
- Pool files on the *current* holder are 6h-ephemeral; only the anchors (git/release parts) are permanent — by design.
- `release_delete_parts` needs the release tag intact; if a release was deleted manually, cleanup is skipped.
- Verify-cycle covers upload/dedupe/archive/download; relay handoff needs a live chain to test end-to-end.

## 8. Real deployment lessons (learned the hard way — live-run fixes)

These were found while deploying for real; each is fixed in code but worth knowing:

| Landmine | Symptom | Fix (now in code) |
|---|---|---|
| Release asset uploads to `api.github.com/.../assets` | **404 Not Found** | use `uploads.github.com/.../assets` |
| Multipart `files=` on asset upload | asset stored WITH its multipart envelope (+200 B) — SHA mismatch | send **raw body** (`content=data` + Content-Type) |
| Creating a Release on an **empty repo** | HTTP 422 "Repository is empty" | startup auto-seeds an init commit; `ensure_release` retries after seeding |
| `/mnt/data` on hosted runners | **Permission denied** | use `/tmp/carousel-data` (env-overridable) |
| `nodes` table missing `status`/`end_at` | supervisor crashes on `SELECT status` | added columns + heartbeat sets `status='active'` |
| Archive sha `archive-<size>-<name>` | UNIQUE violation on repeated uploads → 500 | sha now includes the file id |
| Free cloudflared tunnel drops rapid requests | random 520/530/HTML pages | verify harness retries 5xx/network errors |
| `gh` token lacks `workflow` scope | pushes touching `.github/workflows/*` rejected | `gh auth refresh -s workflow` once |
| Cloudflare **100 MB body cap** on free quick tunnels | 413 Payload Too Large on any big upload | chunked upload protocol `/v1/upload/start \| chunk \| complete` (client: `scripts/upload_chunked.py`) |
| Pre-truncated spool in `/v1/upload/start` | every chunk 409 "offset mismatch" | start with an EMPTY spool file |
| 500 on `/v1/upload/complete` | FK violation `jobs.target_id` → `files(id)` | insert the `pool-store` job AFTER the files row |
| Sync finalize in `/v1/upload/complete` | Cloudflare 524 (>100 s work) | finalize runs as a **BackgroundTask**; client polls `GET /v1/file/{id}` |
| Chunked session id ≠ file id | client polls forever | finalize reuses the session id as the file id |
| Buffered spool lives only on the API runner | drain (hours later) finds 404 → orphaned job | pool buffers are **release-asset parts** (`parts_json`), durable until a node drains them |
| `uploads.github.com` rejects streamed/chunked bodies | 400 on generator `content=` | stage each part to disk, upload with `curl -T` (real Content-Length) |
| GitHub asset URLs **302→CDN** | drain fails on redirect | carousel client uses `follow_redirects=True` |
| Empty `TUNNEL_URL` clobbers DB on slow cloudflared | `tunnel_url` = "" in meta | publish step skips empty values |
| upload-service window too short (345 s) | multi-GB upload 502 mid-flight | window raised to 1800 s (30 min) |

> [!TIP]
> The `verify-cycle` workflow runs the full battery daily and fails loudly if any tier breaks — the tunnel rotation and flakiness are already handled inside it.
