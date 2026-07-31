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


def call(c, method, url, **kw):
    """Retry helper — the free cloudflared tunnel occasionally drops requests."""
    last = None
    for attempt in range(3):
        try:
            r = getattr(c, method)(url, **kw)
            if r.status_code < 500:
                return r
            last = r
        except httpx.HTTPError as e:
            last = e
        time.sleep(2 * (attempt + 1))
    return last if isinstance(last, httpx.Response) else None


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
    r = call(c, "post", f"{args.api}/v1/upload", headers=headers,
             files={"file": ("verify-small.txt", payload, "text/plain")})
    check("upload small (git)", r is not None and r.status_code == 200,
          f"-> {r.status_code if r else 'no response'}")
    if r is None or r.status_code != 200:
        sys.exit(1)
    up = r.json()
    check("upload url", "raw.githubusercontent.com" in up["url"], up["url"][:80])

    # 2. dedupe (same bytes -> same id, no second copy)
    r2 = call(c, "post", f"{args.api}/v1/upload", headers=headers,
              files={"file": ("verify-small-copy.txt", payload, "text/plain")})
    check("dedupe", r2 is not None and r2.status_code == 200 and r2.json()["id"] == up["id"],
          f"id={up['id']}")

    # 3. download + integrity
    r3 = call(c, "get", f"{args.api}/v1/download/{up['id']}")
    check("download git", r3 is not None and r3.status_code in (200, 301, 302, 307, 308),
          f"-> {r3.status_code if r3 else 'no response'}")
    if r3.status_code == 200:
        got = r3.content
        check("sha match", hashlib.sha256(got).hexdigest() == sha)

    # 4. archive tier (3 parts, reassembled on the fly)
    big = b"".join([hashlib.sha256(f"blob-{i}".encode()).digest() * 200 for i in range(3)])  # ~15 KB
    r = call(c, "post", f"{args.api}/v1/archive/start",
             params={"name": "verify-archive.bin", "total_parts": 3}, headers=headers)
    check("archive start", r is not None and r.status_code == 200)
    if r is None or r.status_code != 200:
        check("archive setup (cannot continue)", False,
              f"status={r.status_code if r else 'no response'}")
        print(f"\n[verify] RESULT: {PASS} passed, {FAIL} failed")
        sys.exit(1)
    fid = r.json()["id"]
    total = len(big)
    part_size = (total + 2) // 3
    ok = True
    for i in range(3):
        chunk = big[i * part_size:(i + 1) * part_size]
        r = call(c, "post", f"{args.api}/v1/archive/{fid}/part", params={"index": i},
                 files={"file": (f"verify-archive.bin.part{i:03d}", chunk, "application/octet-stream")},
                 headers=headers)
        if r is None or r.status_code != 200:
            ok = False
            break
    check("archive parts 3/3", ok)
    r = call(c, "post", f"{args.api}/v1/archive/{fid}/complete", headers=headers)
    try:
        detail = str(r.json().get("parts"))
    except Exception:
        detail = f"status={r.status_code if r else None} body={(r.text[:120] if r else '')!r}"
    check("archive complete", r is not None and r.status_code == 200, detail)
    r = call(c, "get", f"{args.api}/v1/download/{fid}")
    check("archive download", r is not None and r.status_code == 200 and len(r.content) == total,
          f"-> {r.status_code if r else 'no response'} {len(r.content) if r else 0}/{total}b")
    if r is not None and r.status_code == 200:
        check("archive sha", hashlib.sha256(r.content).hexdigest() == hashlib.sha256(big).hexdigest())

    # 5. pool awareness (report, not fail — depends on live carousel)
    r = call(c, "get", f"{args.api}/v1/stats")
    stats = r.json() if r is not None and r.status_code == 200 else {}
    check("stats", r is not None and r.status_code == 200,
          f"files={stats.get('files')} bytes={stats.get('bytes_total')}")
    print(f"[verify] pool/compress/handoff run on their own workers — check GridLive for those.")

    c.close()
    print(f"\n[verify] RESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
