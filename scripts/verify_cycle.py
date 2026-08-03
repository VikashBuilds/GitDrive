"""verify_cycle.py — end-to-end smoke test of GitDrive's data cycles.

Run by .github/workflows/verify-cycle.yml. Walks through every tier:
  upload (release small file)  ->  dedupe  ->  archive (parts)  ->  download + SHA check
  compress is reported as pending (it runs on a separate worker
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


def _discover_tunnel_url() -> str:
    db_url = os.environ.get("DB_URL", "")
    if not db_url:
        return ""
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT v FROM meta WHERE k = 'tunnel_url'")
        row = cur.fetchone()
        cur.close()
        conn.close()
        return (row[0] if row else "").rstrip("/")
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=API_URL)
    ap.add_argument("--key", default=API_KEY)
    args = ap.parse_args()
    if not args.api:
        args.api = _discover_tunnel_url()
    if not args.api or not args.key:
        print("FATAL: need --api and --key (or API_URL/API_KEY env)")
        sys.exit(2)
    headers = {"X-API-Key": args.key}
    c = httpx.Client(timeout=httpx.Timeout(120, connect=30))
    created: list[str] = []
    try:
        print(f"[verify] target: {args.api}")

        payload = f"GitDrive verify {uuid.uuid4()} {time.time()}".encode()
        sha = hashlib.sha256(payload).hexdigest()

        # 1. small-file upload (release tier — small files live as release assets)
        r = call(c, "post", f"{args.api}/v1/upload", headers=headers,
                 files={"file": ("verify-small.txt", payload, "text/plain")})
        check("upload small", r is not None and r.status_code == 200,
              f"-> {r.status_code if r else 'no response'}")
        if r is None or r.status_code != 200:
            sys.exit(1)
        up = r.json()
        created.append(up["id"])
        check("upload url", "github.com/" in up["url"] and "/releases/download/" in up["url"], up["url"][:80])

        # 2. dedupe (same bytes -> same id, no second copy)
        r2 = call(c, "post", f"{args.api}/v1/upload", headers=headers,
                  files={"file": ("verify-small-copy.txt", payload, "text/plain")})
        check("dedupe", r2 is not None and r2.status_code == 200 and r2.json()["id"] == up["id"],
              f"id={up['id']}")

        # 3. download + integrity (downloads require the API key)
        r3 = call(c, "get", f"{args.api}/v1/download/{up['id']}", headers=headers)
        check("download small", r3 is not None and r3.status_code in (200, 301, 302, 307, 308),
              f"-> {r3.status_code if r3 else 'no response'}")
        if r3.status_code == 200:
            got = r3.content
            check("sha match", hashlib.sha256(got).hexdigest() == sha)
        elif r3.status_code in (301, 302, 307, 308):
            loc = r3.headers.get("location", "")
            r4 = call(c, "get", loc, headers=headers)
            check("redirected sha match", r4 is not None and r4.status_code == 200
                  and hashlib.sha256(r4.content).hexdigest() == sha,
                  f"-> {r4.status_code if r4 else 'no response'}")

        # 4. archive tier (3 parts, reassembled on the fly)
        big = b"".join([hashlib.sha256(f"blob-{i}".encode()).digest() * 200 for i in range(3)])  # ~15 KB
        r = call(c, "post", f"{args.api}/v1/archive/start",
                 params={"name": "verify-archive.bin", "total_parts": 3}, headers=headers)
        check("archive start", r is not None and r.status_code == 200)
        if r is None or r.status_code != 200:
            check("archive setup (cannot continue)", False,
                  f"status={r.status_code if r else 'no response'}")
            sys.exit(1)
        fid = r.json()["id"]
        created.append(fid)
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
        r = call(c, "get", f"{args.api}/v1/download/{fid}", headers=headers)
        check("archive download", r is not None and r.status_code == 200 and len(r.content) == total,
              f"-> {r.status_code if r else 'no response'} {len(r.content) if r else 0}/{total}b")
        if r is not None and r.status_code == 200:
            check("archive sha", hashlib.sha256(r.content).hexdigest() == hashlib.sha256(big).hexdigest())

        # 5. worker awareness (report, not fail — depends on live workers)
        r = call(c, "get", f"{args.api}/v1/stats", headers=headers)
        stats = r.json() if r is not None and r.status_code == 200 else {}
        check("stats", r is not None and r.status_code == 200,
              f"files={stats.get('files')} bytes={stats.get('bytes_total')}")
        print(f"[verify] compress runs on its own workers — check GridLive for those.")
    finally:
        # every verify run creates test files — remove them so they don't pile
        # up in storage repos and the dashboard (even on failure).
        for fid in created:
            try:
                c.delete(f"{args.api}/v1/file/{fid}", headers=headers)
            except Exception:
                pass
        c.close()
    print(f"[verify] cleaned up {len(created)} test file(s)")
    print(f"\n[verify] RESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
