# 10 — GRIDCAROUSEL: The Jugaad Storage Protocol

> **The idea:** don't fight the 6-hour runner limit — ride it. Make the fleet a
> **rotating hot pool**. Data continuously moves between ephemeral runner disks
> (280 GB, ultra-fast) and durable anchors (git / Releases / cache). Every runner
> checks its data in **before** it dies; the next runner restores it. The volume
> never decreases — it just *changes places*.

## 🔄 The Lifecycle (one shift = one 6h runner)

```
SHIFT START
  Runner N claims idle sets (≤ 13 GB) from Postgres
  Restores each set from its last anchor (git / Releases)
  Serves + processes from local SSD (fast)

DURING SHIFT (every 20 min)
  Check-in: changed files → git commit / Release upload (durable)
  → worst-case loss if the VM is killed mid-shift = ≤ 20 min of deltas

SHIFT END (5h40m mark — 20 min BEFORE the 6h hard limit)
  Final check-in (graceful, full set anchored)
  Release sets → status idle
  Chain next run → same node id, new VM

SHIFT START (next VM)
  Restores the set → picks up exactly where the last VM stopped
```

Because the check-in interval (20 min) is *much* shorter than the runner lifetime (6h), **the 6h limit stops mattering**. Data is never unanchored for more than 20 minutes, ever.

## 📐 Staggered Rotation (the trick that makes it safe)

All nodes can't die at once. Launch each node's chain 20 minutes apart:

| Node | Starts at (min) | Dies around |
|---|---|---|
| carousel-01 | 00:00 | 05:40 |
| carousel-02 | 00:20 | 06:00 |
| carousel-03 | 00:40 | 06:20 |
| ... | +20 min each | +6h each |

With 20 nodes, one handoff every 18-20 min — the pool always has ≥ 19 healthy copies of its own sets while one set is being rotated.

## 🧮 Capacity & Loss-Bound Math

| Setup | Pool (raw) | Anchor (durable) | Worst-case loss on ANY event |
|---|---|---|---|
| 10 nodes | 140 GB | git + Releases + cache (unlimited-ish) | ≤ 20 min of deltas on 1 set |
| 20 nodes | 280 GB | same | ≤ 20 min of deltas on 1 set |
| All VMs killed simultaneously | — | — | ≤ 20 min of deltas across all sets — **full data survives** |
| Account banned / repo deleted | — | — | anchored data safe in git; pool rebuildable |

**Why graceful cycles are lossless:** final check-in anchors 100% of the set before the VM dies. Only a hard mid-shift kill (VM recycling, GitHub outage) loses anything, and even then it's bounded by CHECKIN_MIN.

## ⚖️ Carousel vs Raw Cluster (GridStore 08)

| | Raw EC cluster (MinIO) | **Carousel** |
|---|---|---|
| Data survives VM churn | self-heals shards (rebuild storms) | check-in before death (no storms) |
| Single node death | rebuild 14 GB from peers | next VM restores from anchor |
| All-nodes death | EC reconstructs if ≤ tolerance | lossless (all anchored) |
| Complexity | MinIO + EC + heal config | git + Releases (already proven) |
| Serving | S3 API, parallel reads | one holder per set (fine at this scale) |
| TOS flag risk | 20 nodes busy 24/7 | same — **keep pool small (5-10 nodes)** |

**Verdict: the carousel is the smarter Jugaad** — simpler, and it converts the ephemeral 280 GB into *semi-durable* storage with a bounded loss window.

## ⚠️ Honest Caveats

- Check-in goes through git/Releases — that's the durable anchor; the cache tier (09) adds an extra snapshot layer
- Serving reads = the holder's tunnel endpoint; a set is read-only-elsewhere during handoff (~1-2 min)
- Pool capacity is *working* storage, not archive — archive always lives in git/Releases
- **Never store the only copy of anything on the pool.** The carousel bounds loss; it doesn't eliminate risk
- Keep the pool on the spare account, public repos, 5-10 nodes max (70-140 GB) to stay under the radar
- 20 concurrent jobs is the account cap — 20 pool nodes = the whole account is the pool

## 🗃️ Implementation (in this repo)

- `scripts/carousel.py` — node daemon: register → claim → restore → serve → check-in → graceful release
- `.github/workflows/carousel-node.yml` — one chain per `node_id` input (staggered starts, chaining passes the same `node_id`)
- `scripts/queue.sql` — `nodes`, `sets`, `shifts` tables

**Start small:** 2 nodes, 20-min stagger → watch one handoff → then scale to 10.
