# 12 — RELAY HANDOFF + ALL CYCLES TOGETHER (The Full System)

> The relay upgrade makes the pool **zero-downtime** — but it replaces nothing.
> Every existing cycle stays. This doc shows the complete machine: all layers,
> all cycles, and how they reinforce each other for maximum output.

## 🧬 The Layered Storage Stack (top = fastest, bottom = most durable)

```
┌────────────────────────────────────────────────────────────────────┐
│  L1  RELAY POOL       20 chains × ~13 GB = 260 GB live on runners   │
│      hot, direct peer-to-peer, zero-downtime handoff               │
├────────────────────────────────────────────────────────────────────┤
│  L2  GIT + RELEASES   5 GB × repos (git) + 2 GB/file (releases)    │
│      THE durable archive. Every byte's permanent home. CDN-served  │
├────────────────────────────────────────────────────────────────────┤
│  L3  ACTIONS CACHE    10 GB × repo snapshot backup (7-90 days)     │
│      restart-proof mirror, not serving                             │
├────────────────────────────────────────────────────────────────────┤
│  L4  POSTGRES         metadata + URLs + queue + set registry       │
│      the brain: source of truth for everything                     │
└────────────────────────────────────────────────────────────────────┘
```

## 🔄 All Seven Cycles (each one still runs, always)

| # | Cycle | Workflow/Code | What it does | Status |
|---|---|---|---|---|
| 1 | **Upload** | `upload-service.yml` + `server.py` | client → git/Releases → CDN URL | ✅ unchanged |
| 2 | **Serve** | GitHub CDN (raw.githubusercontent / release assets) | reads never touch a runner | ✅ unchanged |
| 3 | **Compress** | `compress-workers.yml` + `compress_worker.py` | matrix farm re-encodes images/video → back to git | ✅ unchanged |
| 4 | **Cache backup** | `cache-backup.yml` + `cache_backup.py` | daily snapshot of all files → Actions Cache | ✅ unchanged |
| 5 | **Prune** | `prune.yml` + `prune.py` | expiry deletes + storage report | ✅ unchanged |
| 6 | **Parachute** | inside `carousel.py` | pool checks in to git/Releases every 20 min | ✅ built into relay |
| 7 | **Relay handoff** | `carousel-node.yml` + `carousel.py` | A streams data directly to B before dying | ✅ NEW — zero downtime |

## ⚡ Relay Mechanics (the new cycle, precise)

```
t=320 min   A announces handoff on its sets (handoff_state='pending')
            A dispatches successor: gh workflow run -f node_id=carousel-01

t=322-335   B boots → claims the pending sets atomically (handoff_to=its run id)
            A reads B's tunnel URL from the nodes table → streams every file
            PUT /v1/relay/recv/{set}/{file} (X-Relay-Token auth)
            B verifies each file's size → A marks handoff_state='done'

t=335       A exits gracefully (no chain step needed — B is already live)
            B flips sets to held → serves for its own 340 min → repeats
```

- **Concurrency cost:** 1 job per chain normally, 2 during the 20-min overlap → 10 chains ≈ 10-11 concurrent jobs, always under the 20 cap
- **Double-start protection:** handoff claims are atomic DB updates (`FOR UPDATE SKIP LOCKED`)
- **Self-heal:** if A dies before handing off, the workflow's `if: failure()` step re-dispatches; B falls back to restoring from the anchor

## 🛡️ What Happens on Every Failure Mode (full matrix)

| Event | Uploads | Serving (CDN) | Pool sets |
|---|---|---|---|
| B normal handoff | unaffected | unaffected | lossless, zero downtime (pool URLs refreshed in DB) |
| A killed mid-transfer | unaffected | unaffected | ≤20 min anchored, B restores from anchor |
| All 20 VMs die | unaffected | unaffected | next shifts restore from git/Releases (big files reassembled from chunks) |
| Cache entry expires | unaffected | unaffected | nothing depends on cache |
| Repo hits 5 GB | rolls to next repo (STORAGE_REPOS) | unaffected | — |
| GitHub deletes a runner VM mid-shift | unaffected | unaffected | ≤20 min re-runnable work |

## 📦 The Size-Routing Story (why the pool matters)

| File size | Durable home | Notes |
|---|---|---|
| ≤ 25 MB | git | permanent, CDN |
| 25 MB - 2 GB | Release asset | permanent, CDN |
| **2 - 12 GB** | **pool only** | the 14 GB runner SSD is the *only* free tier that fits them; parachute chunks into release parts |
| > 12 GB | rejected | pool node budget |

## 📦 The Seven-Workflow Deployment

| Workflow | Cadence | Concurrency |
|---|---|---|
| `upload-service.yml` | 24/7 chain (1 runner) | 1 |
| `carousel-node.yml` | ×10 chains, relay handoff | ~10-11 avg |
| `compress-workers.yml` | every 2h + on-demand | burst ≤10 |
| `cache-backup.yml` | daily 05:17 | 1 |
| `prune.yml` | daily 03:47 | 1 |
| `seed_sets.py` | once + when adding repos | manual |

**Peak concurrent jobs: ~23** → stay under 20 by using 8-9 carousel chains, or split chains across both accounts (e.g., 6 here + 6 on VikashMeena777 = 12 chains ≈ 240 GB pool).

## ✅ Max-Output Principle

> **Everything is additive.** The relay pool (L1) makes the fleet fast and
> self-healing; git/Releases (L2) make every byte permanent; cache (L3) is the
> extra mirror; Postgres (L4) coordinates it all. You lose nothing by adding the
> relay — you only gain zero-downtime rotation on top of an already durable
> system.
