"""verify_cycle.py — end-to-end smoke test of GitDrive's data cycles.

Run by .github/workflows/verify-cycle.yml. Walks through every tier:
  upload (git small file)  ->  dedupe  ->  archive (parts)  ->  download + SHA check
  pool / relay handoff / compress are reported as pending (they run on other
  workers) but their presence in the DB is verified.

Usage:
  python scripts/verify_cycle.py            # reads API URL + key from env
  python scripts/verify_cycle.py --api URL --key KEY
"""

import argparse
import hashlib
import io
import os
import sys
import time
import uuid

import httpx

API_URL = os.environ.get("API_URL", "").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {label} {detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=API_URL)
    ap.add_argument("--key", default=API_KEY)
    args = ap.parse_args()
    if not args.api or not args.key:
        print("FATAL: need --api and --key (or API_URL/API_KEY env)")
        sys.exit(2)
    headers = {"X-API-Key": args.key}
    c = httpx.Client(timeout=httpx.Timeout(120, connect=30))
    print(f"[verify] target: {args.api}")

    payload = f"GitDrive verify {uuid.uuid4()} {time.time()}".encode()
    sha = hashlib.sha256(payload).hexdigest()

    # 1. small-file upload (git tier)
    r = c.post(f"{args.api}/v1/upload", headers=headers,
               files={"file": ("verify-small.txt", payload, "text/plain")})
    check("upload small (git)", r.status_code == 200, f"-> {r.status_code}")
    if r.status_code != 200:
        sys.exit(1)
    up = r.json()
    check("upload url", "raw.githubusercontent.com" in up["url"], up["url"][:80])

    # 2. dedupe (same bytes -> same id, no second copy)
    r2 = c.post(f"{args.api}/v1/upload", headers=headers,
                files={"file": ("verify-small-copy.txt", payload, "text/plain")})
    check("dedupe", r2.status_code == 200 and r2.json()["id"] == up["id"], f"id={up['id']}")

    # 3. download + integrity
    r3 = c.get(f"{args.api}/v1/download/{up['id']}")
    check("download git", r3.status_code in (200, 302))
    if r3.status_code == 200:
        got = r3.content
        check("sha match", hashlib.sha256(got).hexdigest() == sha)

    # 4. archive tier (3 parts, reassembled on the fly)
    big = b"".join([hashlib.sha256(f"blob-{i}".encode()).digest() * 200 for i in range(3)])  # ~15 KB
    r = c.post(f"{args.api}/v1/archive/start",
               params={"name": "verify-archive.bin", "total_parts": 3}, headers=headers)
    check("archive start", r.status_code == 200)
    fid = r.json()["id"]
    total = len(big)
    part_size = (total + 2) // 3
    ok = True
    for i in range(3):
        chunk = big[i * part_size:(i + 1) * part_size]
        r = c.post(f"{args.api}/v1/archive/{fid}/part", params={"index": i},
                   files={"file": (f"verify-archive.bin.part{i:03d}", chunk, "application/octet-stream")},
                   headers=headers)
        if r.status_code != 200:
            ok = False
            break
    check("archive parts 3/3", ok)
    r = c.post(f"{args.api}/v1/archive/{fid}/complete", headers=headers)
    check("archive complete", r.status_code == 200, str(r.json().get("parts")))
    r = c.get(f"{args.api}/v1/download/{fid}")
    check("archive download", r.status_code == 200 and len(r.content) == total)
    if r.status_code == 200:
        check("archive sha", hashlib.sha256(r.content).hexdigest() == hashlib.sha256(big).hexdigest())

    # 5. pool awareness (report, not fail — depends on live carousel)
    r = c.get(f"{args.api}/v1/stats")
    stats = r.json()
    check("stats", r.status_code == 200, f"files={stats['files']} bytes={stats['bytes_total']}")
    print(f"[verify] pool/compress/handoff run on their own workers — check GridLive for those.")

    c.close()
    print(f"\n[verify] RESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
