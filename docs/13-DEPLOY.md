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
- Pool files on the *current* holder are 6h-ephemeral; only the anchors (git/release parts) are permanent — by design.
- `release_delete_parts` needs the release tag intact; if a release was deleted manually, cleanup is skipped.
- Verify-cycle covers upload/dedupe/archive/download; relay handoff needs a live chain to test end-to-end.
