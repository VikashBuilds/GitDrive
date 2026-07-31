# 04 — GitHub Actions Workflows

All workflows live in `.github/workflows/` inside the **`gitdrive`** repo (they trigger and manage the storage repos).

---

## 1. `upload-service.yml` — 24/7 API Server (chain pattern)

| | |
|---|---|
| Trigger | `workflow_dispatch` + `schedule: cron '17 */6 * * *'` (backup restart every 6 h) |
| Runner | 1 × ubuntu-latest, `timeout-minutes: 360` |
| Permission | `actions: write` (needed to self-trigger) |

**Lifecycle of one run:**
1. Checkout + install Python deps (`fastapi`, `uvicorn`, `httpx`, `psycopg2-binary`)
2. Clone the storage repo (`gitdrive-1`) into the runner working copy
3. Start `cloudflared tunnel --url http://localhost:8080` → public URL
4. Write the tunnel URL to the DB (so GridLive/Telegram can find it) — then start FastAPI
5. Serve uploads for ~5 h 45 m, then graceful shutdown
6. `if: always()` → re-trigger itself via `gh workflow run` (3 attempts, 10 s apart)

> [!IMPORTANT]
> The cron minute `17` is offset so this chain never collides with your other 24/7 chains (n8n, Minecraft). `GH_PAT` secret is the fallback if `actions: write` alone gets a 403.

**Secrets needed:** `GITDRIVE_DB_URL`, `GITDRIVE_API_KEYS`, `GH_PAT`, `GITDRIVE_STORAGE_REPO`.

---

## 2. `compress-workers.yml` — Compression Farm (dynamic matrix)

| | |
|---|---|
| Trigger | `workflow_dispatch` + `schedule: cron '37 */2 * * *'` + `repository_dispatch: [compress]` |
| Runner | 1 prepare + up to 10 matrix workers |

**How it works:**
1. **Prepare job** runs `scripts/pick_jobs.py --limit 10 --type compress`
   - selects `queued` compress jobs from Postgres, flips them to `processing`
   - emits `{"include": [{"job_id": 3}, {"job_id": 7}, ...]}` (or `null` if empty)
2. **Worker job** `if: needs.prepare.outputs.matrix != 'null'` → `strategy.matrix: ${{ fromJson(...) }}`
   - each worker runs `scripts/compress_worker.py --job-id N`
   - image → Pillow re-encode; video → FFmpeg; re-upload if ≥ 10% smaller
3. On failure, job attempts reset to `queued` (retry next cycle) via `attempts < 3`

**Why dynamic matrix:** workers only spawn when there's actual work. Zero idle runner cost.

---

## 3. `prune.yml` — Daily Cleanup & Stats

| | |
|---|---|
| Trigger | `schedule: cron '47 3 * * *'` + `workflow_dispatch` |
| Runner | 1 × ubuntu-latest |

1. `scripts/prune.py` connects to Postgres
2. Deletes rows where `expires_at < now()` — git deletes + release asset deletes via API
3. Prints a storage report (files, bytes by tier, top downloads)
4. Optional: POST summary to a Telegram bot webhook (`TELEGRAM_WEBHOOK` secret)

---

## Checklist: consistent patterns used

| Pattern | Where | Why |
|---|---|---|
| Chain (self-trigger + `concurrency.cancel-in-progress: false`) | upload-service | 24/7 uptime |
| Dynamic matrix from DB | compress-workers | pay-per-work |
| `schedule` offset minutes (17/37/47) | all three | never collide with other chains |
| GitHub API token in env, never printed | all | secret hygiene |
| `if: always()` chaining step | upload-service | restart even if service crashed |
