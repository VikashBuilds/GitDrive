# 02 — Storage Model & Capacity

## 📐 The Storage Stack (3 tiers)

| Tier | What | Per-File Limit | Capacity | Cost |
|---|---|---|---|---|
| **git commits** | Every file ≤ 25 MB (routed here) | 100 MB hard (50 MB warned) | **~5 GB per repo** (soft limit, repo stays fast) | ₹0 |
| **Git LFS** | Optional tracked large binaries | 2 GB | **1 GB storage + 1 GB bandwidth/mo** per account | ₹0 |
| **Releases** | Every file > 25 MB | 2 GB per asset | Extra headroom; assets deletable (space frees instantly) | ₹0 |

> [!IMPORTANT]
> The **5 GB** figure is GitHub's soft limit — repos stay warning-free and fast under it. It counts **all git history**, which is why deleted files still occupy space (see gotcha below).

## 🧮 Your Real Capacity

| Setup | Raw Storage | Effective (after compression) |
|---|---|---|
| 1 storage repo | ~6 GB | ~12-30 GB (media) |
| 3 storage repos | ~18 GB | ~36-90 GB |
| 5 storage repos | ~30 GB | ~60-150 GB |
| 10 storage repos | ~60 GB | ~120-300 GB |

**Why compression multiplies capacity:**
- Images: WebP/AVIF typically **5-10×** smaller than PNG/JPEG originals
- Video: FFmpeg h264 crf 28 typically **2-5×** smaller
- Dedupe: identical uploads cost **0 extra bytes** (SHA-256)

> [!TIP]
> The worker only replaces a file when it's ≥ 10% smaller, so you never lose quality for nothing.

## 📁 File Routing Rules

| File size | Route | Serving URL | Durability |
|---|---|---|---|
| ≤ 25 MB | git commit | `https://raw.githubusercontent.com/<owner>/<repo>/main/files/...` | Permanent |
| 25 MB - 2 GB | Release asset (batched `assets-YYYYMMDDHH`) | `https://github.com/<owner>/<repo>/releases/download/<tag>/<name>` | Permanent (until deleted) |
| **any size** | **Archive tier** (chunked ≤1.9 GB parts → Releases) | `/v1/download/<id>` (server streams parts as ONE file) | **Permanent, unlimited** |
| 2 - 12 GB (live) | Relay pool only (buffered → drained into a set) | `https://<pool-node-tunnel>/v1/set/<set>/<name>` | Semi-durable — hot big files |
| > 12 GB (live) | Rejected (413) — use Archive tier instead | — | — |

**Why the pool for 2-12 GB live files:** the 14 GB runner SSD is the only free tier that can *actively serve* files that big with zero-assembly speed. But for files that just need to exist forever, the **Archive tier** is strictly better: chunk into ≤1.9 GB parts → Release assets (no storage limit, no expiry, CDN download) → `/v1/download/{id}` reassembles them on the fly into one continuous stream.

> [!TIP]
> Rule of thumb: **hot & big → pool; cold & big → archive tier.** Archive costs a split-upload once and then costs nothing forever.

> [!CAUTION]
> Files in the pool are served from the current holder's tunnel — the DB always
> refreshes the URL on handoff. While `status: buffered`, the file lives only on
> the API runner's disk (ephemeral) — if the API runner restarts before a
> carousel drains it, re-upload. This is the one true loss window; keep it
> minutes by running a carousel chain.

## ⚡ Runner Disk vs Git Release — The Real Difference

| Dimension | Workflow/job disk (14 GB SSD) | Git Release assets |
|---|---|---|
| **Speed (local I/O)** | 🚀 1-2 GB/s — the VM's own SSD | N/A — not local |
| **Speed (data in/out)** | reads instant; egress multi-Gbps | upload ~20-150 MB/s per stream; **CDN download ultra-fast + Range/resume** |
| **Latency** | ~0 ms | 100-300 ms CDN first byte |
| **Durability** | ❌ ZERO — VM dies at 6h or anytime | ✅ Permanent — survives everything |
| **Capacity** | 14 GB/job, 280 GB fleet max | **Unlimited** (2 GB per part, unlimited parts/repos) |
| **Lifetime** | 6 hours max | Forever (until you delete) |
| **Modification** | ✅ read/write/delete in place | ❌ immutable — replace by re-upload |
| **Access** | only while the job runs | any time, anywhere, via CDN |
| **Cost** | ₹0 | ₹0, unlimited bandwidth (public) |
| **Limits** | 20 concurrent jobs/account | API 1,000 req/hr (GITHUB_TOKEN) / 5,000/hr (PAT) — a 100 GB file ≈ 56 parts ≈ trivial |
| **Failure blast radius** | VM killed = data gone | GitHub S3 backend — effectively bulletproof |
| **Best for** | hot processing, working sets, active big files | durable archive — files that must never die |

**The summary insight:** the runner disk is *fast but temporary*; Releases are *slower to write but permanent and unlimited*. Use the disk as the bridge and Releases as the vault.

## ⚠️ The Deletion Gotcha (read this!)

| Storage | Delete a file? | Space freed? |
|---|---|---|
| Release asset | Yes (API delete) | ✅ **Immediately** |
| git commit | Yes (commit the delete) | ❌ **No** — stays in history forever |
| git commit + history rewrite | `filter-repo` / force-push | ✅ Yes, but rewrites all clones |

**Implications:**
- **Ephemeral files → use Releases** (or just re-upload to a new repo)
- **Long-term files → git is fine** (5 GB is a lot of real docs)
- When a git repo approaches ~5 GB: stop writing to it, spin up `gitdrive-2`, keep the old one as cold archive
- Never store secrets/tokens/private data in public storage repos — auth protects access, not content

## 📊 Repo Layout

```
gitdrive-1/
├── files/
│   └── 2026/
│       └── 08/
│           └── a3/                  ← first 2 chars of SHA-256
│               ├── 9f2k1a_hero-banner.png
│               └── 9f2k1a_clip-final.mp4
└── README.md
```

- `files/YYYY/MM/` — month buckets (easy to archive/delete a month)
- `<sha[:2]>/` — hash prefix buckets (faster git operations, balanced trees)
- `<id>-<name>` — id is the DB primary key; name is URL-safe (spaces/special chars sanitized)

## 🔭 Sharding Strategy

| When | Action |
|---|---|
| Repo > 60% of 5 GB | Create next repo, update `STORAGE_REPOS` secret |
| Frequent large-file uploads | Give the busy repo more Release budget, git repos stay light |
| Old month needed? | Copy the folder to another repo; delete from active one |

Each storage repo is **just a normal public repo** — you can also read/backup/download everything with any git client. Your cloud storage is never locked in.
