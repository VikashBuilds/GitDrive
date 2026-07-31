# ☁️ GitDrive — Your Free Personal Cloud Storage on GitHub

> Upload → workers compress/thumbnail/dedupe → files live in git + Releases → served by GitHub's CDN with shareable links. **₹0 forever.**

---

## 📊 How Much Storage Do You Get? (TL;DR)

| Source | Free Space | Notes |
|---|---|---|
| **Git commits** (per storage repo) | **~5 GB** | GitHub soft limit; files ≤ 100 MB each |
| **Git LFS** (per account) | **1 GB** + 1 GB/mo bandwidth | For larger tracked files |
| **Actions Cache** (per repo) | **10 GB** | GitHub-managed, survives restarts (tier 09) |
| **Runner SSDs** (fleet) | **280 GB raw** (20 × 14 GB) | Ephemeral — see the GridStore design (08) |
| **Carousel pool** | **140-280 GB usable** | Rotating hot pool, ≤20 min loss bound (tier 10) |
| **GitHub Releases** | Assets up to **2 GB each**, **unlimited total** | Big files + archive parts — NOT in git history |
| **Archive tier** | **Unlimited, permanent** | Any file → ≤1.9 GB release parts → streamed back as ONE file |
| **Your total per repo** | **≈ 6 GB** | 5 GB git + 1 GB LFS |
| **Sharded across 5 storage repos** | **≈ 30 GB raw** | Each repo gets its own limits |
| **+ 10 GB Actions Cache per repo** | **≈ 50 GB** | Restart-proof backup tier |
| **After compression workers** | **≈ 60-150 GB effective** | Images 5-10× smaller, video 2-5× smaller |

> [!TIP]
> GitDrive stores files in **plain git repos** (cheap, permanent) and **Releases** (for anything over 100 MB). Compression + dedupe by SHA-256 mean your real-world capacity is 2-5× the raw number.

---

## 🧩 What Is It?

Your personal S3-style file service, built entirely on GitHub's free tier:

```
Upload (API / Telegram) ──> GitDrive server (24/7 runner) ──> dedupe + route
   ├─ small file (≤25 MB)  ──> git commit to storage repo ──> raw.githubusercontent CDN link
   ├─ big file (>25 MB)    ──> batched GitHub Release ──> release asset download link
   └─ image/video          ──> compress worker (matrix) ──> optimized version replaces it
```

- **Share links:** every file gets a permanent public URL (or private with auth)
- **Dedupe:** identical files (by SHA-256) are stored once, ever
- **Compression farm:** matrix runners compress images (Pillow/WebP/AVIF) and video (FFmpeg) automatically
- **Expiry:** optional auto-delete after N days
- **Stats:** download counts, storage used, per-file metadata — all in Postgres

---

## 📁 Project Structure

```
GitDrive/
├── README.md                      ← you are here
├── docs/
│   ├── 01-ARCHITECTURE.md         ← how it all fits together
│   ├── 02-STORAGE.md              ← capacity math, limits, sharding
│   ├── 03-API.md                  ← REST endpoint spec
│   ├── 04-WORKFLOWS.md            ← every GitHub Actions workflow explained
│   ├── 05-SETUP.md                ← step-by-step deployment
│   ├── 06-SECURITY.md             ← keys, rate limits, abuse protection
│   ├── 07-ROADMAP.md              ← v0.1 → v2.0
│   ├── 08-GRIDSTORAGE.md          ← the 280 GB runner-cluster design
│   ├── 09-CACHE-TIER.md           ← 10 GB × repo backup tier
│   ├── 10-CAROUSEL.md             ← the rotating-pool Jugaad protocol
│   ├── 11-DATA-CYCLE.md           ← how all tiers move data (master doc)
│   ├── 12-RELAY-HANDOFF.md        ← zero-downtime relay + ALL cycles
│   └── 13-DEPLOY.md               ← day-one deploy checklist + smoke tests
├── .github/workflows/
│   ├── upload-service.yml         ← 24/7 API server (chain pattern)
│   ├── compress-workers.yml       ← matrix compression farm
│   ├── prune.yml                  ← daily expiry cleanup + stats
│   ├── cache-backup.yml           ← daily snapshot into Actions Cache
│   ├── carousel-node.yml          ← relay pool node (zero-downtime handoff)
│   ├── carousel-supervisor.yml    ← hourly: auto-restart dead chains
│   └── verify-cycle.yml           ← daily end-to-end integrity proof
├── app/
│   └── server.py                  ← FastAPI upload server (runs 24/7)
└── scripts/
    ├── queue.sql                  ← Postgres schema (files + jobs + sets)
    ├── pick_jobs.py               ← hands jobs to matrix workers
    ├── compress_worker.py         ← compress one file (Pillow/FFmpeg)
    ├── prune.py                   ← expiry + cleanup script
    ├── cache_backup.py            ← daily cache snapshot chunks
    ├── carousel.py                ← rotating hot-pool node daemon
    ├── seed_sets.py               ← create sets, assign files, manifests
    ├── verify_cycle.py            ← full-cycle smoke test (all tiers)
    ├── upload_archive.py          ← upload ANY file (parts → Releases)
    └── supervise_carousel.py      ← keeps relay chains alive forever
```

---

## 🚀 Quick Facts

- **Cost:** ₹0/month (public repos, unlimited minutes)
- **Runners:** 1 for the 24/7 API + burst workers for compression
- **Repo home:** `vikashbuilds/gitdrive` + storage repos `gitdrive-1`, `gitdrive-2`...
- **Domain:** `drive.vikashbuilds.in` (Cloudflare tunnel)
- **Stack:** FastAPI · Postgres (Aiven) · git · GitHub Releases API · GitHub Pages CDN

---

## 🧭 Docs

1. [01-ARCHITECTURE.md](docs/01-ARCHITECTURE.md) — system design & data flow
2. [02-STORAGE.md](docs/02-STORAGE.md) — the storage model & capacity
3. [03-API.md](docs/03-API.md) — upload/download/delete API
4. [04-WORKFLOWS.md](docs/04-WORKFLOWS.md) — workflows deep-dive
5. [05-SETUP.md](docs/05-SETUP.md) — deploy it in 30 minutes
6. [06-SECURITY.md](docs/06-SECURITY.md) — keys, limits, safety
7. [07-ROADMAP.md](docs/07-ROADMAP.md) — where it's going
8. [13-DEPLOY.md](docs/13-DEPLOY.md) — day-one checklist + smoke tests

---

> [!CAUTION]
> Files committed to git are **permanent in history** — deleting a file doesn't shrink the repo until history is rewritten. Shard across repos and use Releases for anything ephemeral or huge. Details in `docs/02-STORAGE.md`.
