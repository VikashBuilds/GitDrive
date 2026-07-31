# 09 — Cache Tier: 10 GB × Repo Backup Storage

> The **safe** workaround for runner storage: GitHub's Actions Cache is GitHub-managed, survives every restart, and gives you **10 GB per repo** — 5 repos = 50 GB. This tier backs up GitDrive's release assets (the ones that can be deleted) into cache snapshots.

## How It Works

```
daily (05:17) → cache-backup.yml
  1. prepare job: scripts/cache_backup.py
     - pulls all files from the DB
     - splits them into ≤900 MB tarball chunks
     - emits dynamic matrix of chunk paths
  2. store jobs (matrix): each runs actions/cache@v4
     - key: gitdrive-backup-YYYYMMDD-<chunk-N>
  3. prune: deletes cache entries older than 5 days via the Cache API
     (GITHUB_TOKEN with actions: write can list + delete caches)
```

## Retention Rules (GitHub's, exact)

| Rule | Value |
|---|---|
| Entry removed if not accessed for | 7 days |
| Max lifetime if accessed | 90 days |
| Total cache per repo | 10 GB |

- Our 5-day rolling window keeps us safely under both rules — daily *restores* would also re-arm the 90-day clock
- Chunk size 900 MB keeps entries under the safe per-entry upload limit

## Capacity Math

| Repos | Cache capacity |
|---|---|
| 1 | 10 GB |
| 3 | 30 GB |
| 5 | 50 GB |
| 10 (storage repos + app repo) | ~100 GB |

## Why This Tier Exists

Release assets are deletable (space frees instantly) — which makes them the tier most exposed to accidental deletion. The cache tier is a second copy of everything, kept for 5 days, with zero runner involvement.

## TOS Note

Caching is a designed, documented Actions feature — storing arbitrary files in it is common practice and low-risk. Keep repos public, stay personal-scale.
