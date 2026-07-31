# 07 — Roadmap

## ✅ v0.1 — Foundation (this scaffold)

- [x] 24/7 upload API (FastAPI + tunnel)
- [x] Small files → git, large files → Releases
- [x] SHA-256 dedupe
- [x] Postgres metadata + queue
- [x] Matrix compression farm (images + video)
- [x] Daily prune + stats

## 🎯 v0.2 — Personal Daily Driver

- [ ] Telegram bot (`/upload`, `/drive`, `/delete`, `/stats`) — upload straight from chat
- [ ] Named Cloudflare tunnel config (stable `drive.vikashbuilds.in`)
- [ ] Share-page HTML (`/share/{id}`) — pretty download page with filename + size + copy button
- [ ] Upload CLI (`gitdrive push file.jpg`) — 50-line Python script
- [ ] Compressed-version keeps original option (store both, serve optimized)

## 🚀 v1.0 — Product-Ready

- [ ] Folders/albums (tag files, list by folder)
- [ ] Webhook on upload (notify your n8n/GitDrive automations)
- [ ] Per-key rate plans (free 50/day + paid keys via your future SaaS)
- [ ] Temporary links with TTL + download-count limits
- [ ] Multi-repo sharding automation (auto-roll to next storage repo at 60% fill)
- [ ] GridLive integration (live stats card)

## 🌌 v2.0 — The Cloud Service

- [ ] Simple web dashboard (upload via drag-drop)
- [ ] Public API for your clients (paid tiers — Razorpay/Cashfree flow per your playbook)
- [ ] Image resizing API on the fly (`/v1/resize/{id}?w=640&format=webp`) — powered by compress workers
- [ ] Video preview thumbnails (ffmpeg sprite sheets)
- [ ] OAuth via GitHub (client access without raw API keys)

---

## 🧭 North Star

> A free, self-owned file service that a solo creator can actually live on — upload, share, resize, deliver — with zero monthly cost and no lock-in. Everything is plain git that you can clone, backup, or migrate at any moment.
