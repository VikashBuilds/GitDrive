# 06 — Security

## 🔑 Authentication

| Surface | Auth |
|---|---|
| `POST /v1/upload`, `DELETE /v1/file/{id}` | `X-API-Key` header, checked against `GITDRIVE_API_KEYS` secret |
| `GET /v1/file/{id}`, `/v1/stats`, `/v1/health` | Public (links are meant to be shareable) |

- Keys are stored in GitHub Secrets only — never in code or repos
- Rotate by editing the secret; multiple comma-separated keys allowed (one per app/bot)
- `GITDRIVE_DB_URL` never leaves the runner env

## 🚦 Abuse Protection

| Threat | Mitigation |
|---|---|
| Anonymous uploads | API key required on all writes |
| Upload flooding | 60 uploads/hour per key → 429; configurable in `server.py` |
| Disk filling (git) | 25 MB git threshold, 2 GB hard cap; monthly storage report in prune log |
| Malicious file types | Mime allowlist — `.exe/.sh/.bat/.html/.svg/.js` blocked by default (XSS + malware vectors), configurable |
| Secret files uploaded by mistake | Public repos only for non-sensitive content; document it in README |
| Key in URL/logs | Headers only; server never logs the key or file bytes |

## 🏷️ Public vs Private Files

- **Public:** anything you want a shareable link for (assets, posters, clips, deliverables for clients)
- **Never upload:** passwords, tokens, private documents, client personal data
- Sensitive files → keep them in a **private** storage repo variant later (private repos still get 2,000 min/mo — enough for a low-volume personal drive; you lose the unlimited-minutes perk, so use sparingly)

## 🧹 Data Hygiene

- `expires_at` on upload → auto-pruned daily
- Releases can be deleted any time (space freed immediately)
- Git deletions are permanent-in-history — the prune script commits deletes but you should occasionally archive closed repos instead of rewriting history

## 📜 GitHub TOS Notes (the gray area)

- Running a personal 24/7 API on Actions is the same gray area as your n8n — keep it **personal scale**, public repo, no abuse
- Storage repos must stay under ~5 GB git size (soft limit)
- Don't serve copyrighted content (movies/songs) or run a public mass-upload service — that's how accounts get flagged
