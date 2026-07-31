# 11 — THE DATA CYCLE: How All the Tiers Actually Work Together

> The master explanation. You have four storage tiers with different lifetimes.
> This doc shows exactly how data moves between them — and why the 20-min
> carousel shift is invisible to you.

## 📊 The Four Tiers (and what each is FOR)

| Tier | Size | Lifetime | Role in the cycle |
|---|---|---|---|
| **Runner disk** | 14 GB × 20 = 280 GB | Ephemeral (6h max) | **Shadow pool** — fast working copy of hot sets. NEVER the only copy |
| **Actions Cache** | 10 GB × repo | 7-90 days | **Snapshot backup** — daily mirror of stored files. Not serving |
| **git repo** | ~5 GB × repo | Permanent | **Durable archive** — every file ≤ 25 MB lives here forever |
| **Releases** | 2 GB/file | Permanent (until deleted) | **Durable archive** — every file > 25 MB lives here |

## 🧭 THE PRINCIPLE (the whole trick in one sentence)

> **Every byte always has a durable home (git/Releases). The runner disk is a
> shadow. The database stores the durable CDN URL. So no matter what happens to
> any runner, the data and its address survive.**

The carousel doesn't *store* your data — it *pre-warms and accelerates* it. That's why shifts can never break anything.

## 🔄 The Full Cycle (three paths)

### Path 1 — WRITE (what happens on every upload)

```
client ──> 24/7 API runner (disk = buffer, not storage)
   │
   ├─ ≤ 25 MB  ──> git commit → storage repo ──> CDN URL (raw.githubusercontent)
   │                 └─ THIS IS PERMANENT. Runner disk not needed anymore.
   │
   ├─ 25 MB - 2 GB ──> Release asset (tag assets-YYYYMMDDHH) ──> CDN download URL
   │                 └─ ALSO PERMANENT (until you delete it)
   │
   ├─ 2 - 12 GB  ──> buffered on API runner ──> carousel node drains it into its
   │                 set (pool URL) ──> parachute chunks it into ≤1.9 GB
   │                 release parts (recoverable from any collapse)
   │
   └─ every file ──> row in Postgres: {id, sha, url, size, set_id}
                      url = the address to serve from  ← source of truth
```

**Result:** the write is complete the moment it hits git/Releases (or the pool set for >2 GB). The runner that accepted it can die 1 second later — nothing is lost.

### Path 2 — CHECK-IN (the carousel's 20-min heartbeat)

```
Runner N holds set-07 on its 14 GB disk (files pre-warmed from CDN)
   │
   ├─ every 20 min: scan files it holds
   ├─ new/changed ≤ 100 MB → git commit (durable)
   ├─ new/changed > 100 MB → Release asset (durable)
   └─ update manifest_json + last_anchor_at in Postgres
```

Why 20 min: it's 18× shorter than the 6h shift, so even a worst-case mid-shift kill loses ≤ 20 min of *shadow* work — and the durable files (Path 1) are never at risk at all.

### Path 3 — READ (why shifts are invisible)

```
client asks for file id ──> Postgres ──> url = https://raw.githubusercontent.com/... (CDN)
                                        └─ served by GitHub's CDN — NO RUNNER IN THE PATH
```

**The 24/7 API runner isn't even in the read path.** Files are served by GitHub's CDN from the durable archive. A runner handoff, a killed VM, or all 20 runners dead — the CDN doesn't care. Reads never break.

Optional accelerator: if the file's set is held by a healthy runner, you *may* read from the pool (faster). If the pool misses — fall back to the CDN URL. 100% transparent.

## ⏱️ Scenario Table — "does the 20-min shift affect anything?"

| What happens | What you experience |
|---|---|
| Runner N finishes its shift, dies | Nothing. All its files were check-in'ed at 5h40m (100% anchored). CDN serves |
| VM killed mid-shift (no warning) | Stored files: zero impact (they're in git/Releases). Pool shadows: ≤ 20 min of re-runnable work |
| Read during a handoff | Served from CDN (DB URL). No interruption |
| Write during a handoff | Written straight to git/Releases by the API runner. No interruption |
| ALL 20 VMs die at once | CDN keeps serving everything. Pool rebuilds lazily on next shifts |
| GitHub expires a cache entry | Only the snapshot backup disappears. Stored files unaffected |
| Storage repo hits 5 GB | Roll to gitdrive-2, update secret. CDN URLs just work |
| Account gets flagged/banned | Anchored data is safe in git. You can re-clone it anywhere, anytime |

## 🗃️ The Set System (how files map to runners)

```
set_id = "set-" + (first 2 hex of sha256 mod N)      # N = number of sets (default 20)

files ──> assigned to a set by hash (stable, no rebalancing)
sets ──> claimed by carousel nodes (each ≤ 13 GB budget)
set manifest ──> stored in Postgres (name, size, url per file)
```

- Any file's set is deterministic — the system always knows which runner *should* hold it
- If that runner is busy/dead, the claim falls to another idle set or the CDN serves — no dependency

## 🧮 What "Consistent Storage" Actually Means Now

| Perspective | Number |
|---|---|
| **Durable, permanent** (git + Releases) | ~5 GB/repo + 2 GB/file — grows with repos (5 repos = 25 GB) |
| **Snapshot backup** (cache) | +10 GB/repo (5 repos = 50 GB) |
| **Working pool** (accelerated, semi-durable) | 140 GB (10 nodes) to 280 GB (20 nodes) |
| **Effective consistent** | Everything on CDN + pool rebuilt ≤ 20 min after any event |

## ✅ Verification Checklist (prove it yourself)

1. Upload a file → `curl` its URL → kill nothing, it just works
2. Read the URL while a carousel node is mid-handoff → still works (CDN)
3. Let a node die without graceful shutdown → file URL still works
4. Compare the file SHA before/after 3 full shifts → identical (zero loss)
5. `df -h` on a node → confirm real free space (often > 14 GB guaranteed)

## 🎯 Bottom Line

> You don't have "storage that survives shifts" — you have **storage that never
> lived on a runner in the first place**. The 280 GB pool is a bonus speed layer
> on top of durable git/Releases. That's why the 20-min shift is a non-event.
