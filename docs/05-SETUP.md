# 05 — Setup Guide (30 minutes)

## Prerequisites

- GitHub accounts: `vikashbuilds` (API runner) — storage repos can live here too
- Aiven Postgres (already running for your n8n) — just add a database: `CREATE DATABASE gitdrive;`
- Cloudflare account (already have — n8n tunnel works the same way)
- A PAT with `repo` + `workflow` scopes → secret `GH_PAT`

## Step 1 — Create the repos (5 min)

```
vikashbuilds/gitdrive        ← the app repo (workflows + code + docs)
vikashbuilds/gitdrive-1      ← storage repo #1 (empty, just README)
```

Both **public** (unlimited minutes + free CDN). Push this folder's contents to `gitdrive`.

## Step 2 — Add secrets (5 min)

In `vikashbuilds/gitdrive` → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `GITDRIVE_DB_URL` | `postgres://user:pass@host:port/gitdrive` |
| `GITDRIVE_API_KEYS` | comma-separated upload keys, e.g. `key-vikash,key-bot` |
| `GITDRIVE_STORAGE_REPO` | `vikashbuilds/gitdrive-1` |
| `STORAGE_REPOS` | comma-separated list for sharding: `vikashbuilds/gitdrive-1,vikashbuilds/gitdrive-2,vikashbuilds/gitdrive-3` — files spread by set hash, each repo keeps its own ~5 GB budget |
| `RELAY_TOKEN` | shared secret for pool peer transfer (`carousel-node.yml`) |
| `GH_PAT` | PAT with `repo` + `workflow` scopes |

> [!TIP]
> Start with 1 storage repo. When it passes ~60% of 5 GB, create `gitdrive-2`, add it to `STORAGE_REPOS`, and re-run `scripts/seed_sets.py` — new uploads spread automatically.

## Step 3 — Init the database (2 min)

Run `scripts/queue.sql` against the `gitdrive` database:

```sql
psql "$GITDRIVE_DB_URL" -f scripts/queue.sql
```

## Step 4 — Launch the service (5 min)

1. In the repo → Actions → **GitDrive Upload Service** → **Run workflow** (branch `main`)
2. First run: the chain starts, cloudflared prints a tunnel URL
3. Read the tunnel URL from the workflow log, then point a DNS record at it:
   - Cloudflare → DNS → add CNAME `drive` → `<random>.trycloudflare.com` (or use a **named tunnel** for a stable URL — recommended)
4. Verify: `curl https://drive.vikashbuilds.in/v1/health`

> [!TIP]
> A **named Cloudflare tunnel** gives you a stable URL (`drive.vikashbuilds.in`) across restarts. Quick tunnels (`trycloudflare.com`) change every 6 h chain run. For GitDrive, stability matters — use a named tunnel with a config that forwards `drive.vikashbuilds.in` → `localhost:8080`.

## Step 5 — First upload (3 min)

```bash
curl -X POST https://drive.vikashbuilds.in/v1/upload \
  -H "X-API-Key: key-vikash" \
  -F "file=@test.png"
```

Open the returned `url` in a browser. Then:
- upload a big file (> 25 MB) → confirm it lands in a `assets-*` Release
- upload an image twice → confirm `"deduped": true`

## Step 6 — Enable compression + prune (2 min)

- `compress-workers.yml` starts automatically on schedule; test with **Run workflow**
- `prune.yml` runs daily at 03:47; test once with **Run workflow**
- `cache-backup.yml` runs daily at 05:17; test once

## Step 7 — Start the relay pool (5 min)

1. Run `python scripts/seed_sets.py` (needs `DB_URL`) → creates 20 sets + assigns files
2. Actions → **GridCarousel Node** → Run workflow with `node_id: carousel-01`
3. After 5 min, start `carousel-02` (stagger ~20 min apart for clean rotation)
4. Watch a handoff: the first shift ends at ~320 min — the log shows `handoff done: set-XX`
5. Verify zero downtime: read any pool file URL during the handoff window (CDN serves)

> [!TIP]
> The relay overlaps old and new VMs for ~20 min — **no concurrency group** on
> `carousel-node.yml` on purpose. Double-start protection is atomic in the DB.
> Keep total chains ≤ 9-10 per account (≈ 11 concurrent jobs, under the 20 cap).

## Step 8 — Watch it (optional)

- Point GridLive at `https://drive.vikashbuilds.in/v1/stats` for live numbers
- Add `https://drive.vikashbuilds.in/v1/health` to Uptime Kuma / status page

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Chain dies, no restart | Check `GH_PAT` + `actions: write` permission |
| Upload returns 502 | Storage repo clone failed — check `GITDRIVE_STORAGE_REPO` secret |
| Compression never runs | `pick_jobs.py` needs `DB_URL` env on the prepare job |
| Tunnel URL changes every run | Use a named Cloudflare tunnel (Step 4) |
| Upload very slow (> 25 MB) | Expected — release upload path; speed improves with 1 GB+ files |
