# 08 — GRIDSTORAGE: The 280 GB Runner Cluster (Design & Verdict)

> The dream: 20 runners × 14 GB SSD = **280 GB of free, ultra-fast storage**.
> The reality: runner VMs are **ephemeral** — they die every 6 h (or anytime GitHub recycles them), and the disk dies with them.

This doc is the honest engineering answer: what's buildable, how, what it costs in reliability, and the safe way to do it.

---

## ⚡ The Hard Truths (design constraints)

| Constraint | Reality |
|---|---|
| VM lifetime | Max 6 h per job (chain pattern keeps a node "alive" via handoff, but the *disk* never survives a restart) |
| VM recycling | GitHub can kill/recycle a runner at any moment, no warning |
| No inbound ports | Runners are outbound-only — peer-to-peer traffic must go through tunnels (cloudflared/bore/playit) |
| No private network | All inter-node traffic crosses the public internet via tunnels (~100-500 Mbps realistic) |
| Concurrency cap | 20 jobs/account → **a 20-node cluster occupies the entire account** |
| Disk guarantee | 14 GB guaranteed on ubuntu-latest; real free space is often larger — always verify: `df -h` |

## 📐 Architecture: MinIO Erasure-Coded Cluster

```
Node 1..20 (each = 1 chained runner, 6h cycle)
├─ minio server (single binary, S3 API)
├─ disk dir: /mnt/data   (14 GB guaranteed)
├─ cloudflared tunnel → https://gridstore-n1.vikashbuilds.in (peer endpoint)
└─ chain workflow: start minio → run 5h45m → graceful stop → chain next run

Postgres (Aiven)
├─ node registry: node id, tunnel URL, status, last_seen
└─ bucket map: bucket → erasure set

Client: s3://... → any node → data striped across the cluster (EC 10+10)
```

**Erasure coding (the magic part):** MinIO splits every object into N shards across N nodes. You pick the ratio:

| EC ratio | Nodes | Raw | Usable | Node failures tolerated |
|---|---|---|---|---|
| 10+10 | 20 | 280 GB | **140 GB** | 10 (bulletproof) |
| 14+6 | 20 | 280 GB | **196 GB** | 6 |
| 8+4 | 12 | 168 GB | 112 GB | 4 |

## 🔄 The 6-Hour Rebuild Cycle (the hard part)

1. Node's 6h job ends → VM + disk destroyed
2. New VM boots → tunnel up → MinIO joins the cluster → **sees its shards missing**
3. MinIO heals: re-downloads its ~14 GB of shards from 19 peers (1-3 min at runner speeds)
4. Repeat for every node, every 6h — that's ~56 GB/day/node of rebuild traffic (free)

**Why this works:** with EC 10+10 you can lose up to 10 nodes at once and still serve + heal. GitHub maintenance usually recycles a few nodes per window — survivable.

**Why data can still die:** if GitHub recycles a batch larger than your tolerance *during* a rebuild, stripes are irrecoverable. The cluster is **self-healing, never durable**.

## 🛡️ TOS & Account Risk (read before building)

- A 20-node 24/7 cluster is the definition of "abusing Actions as free hosting" — **high flag risk**, up to account ban
- Must run on **public repos** (unlimited minutes) and **personal scale**
- **Never run it on the account hosting n8n/Minecraft/GitDrive** — a ban kills your real services
- Accept the account might be banned; store **nothing irreplaceable** on GridStore

## ✅ Safe Usage Pattern

```
GridStore = hot/scratch tier (fast, self-healing, ephemeral)
git + Releases + Actions Cache = durable tier (GitDrive's real storage)
Pipeline: GridStore for live processing (RenderPool, compress farm) → results moved to git/Releases
```

## 🏗️ Build Plan (if greenlit)

| Phase | Scope | Verify |
|---|---|---|
| 1 | Single node on a throwaway repo | `df -h`, minio S3 GET/PUT via tunnel |
| 2 | 4-node cluster | EC(2+2), kill a node, heal test |
| 3 | Benchmarks: `s3cmd` put 5 GB, throughput, heal time | numbers to your notes |
| 4 | 20-node on the spare account (VikashMeena777) | 6h churn soak test 48h |

## Verdict

- **Buildable:** yes. Magical: absolutely — a distributed S3 on free runners.
- **Usable as your primary storage:** no — ephemeral by nature.
- **Recommended:** build it as the experiment on the spare account, pair it with GitDrive's durable tiers, and never store the only copy of anything on it.
