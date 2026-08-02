"""Chunked uploader for GitDrive — bypasses the 100 MB Cloudflare edge cap.

Works for any file size up to 14 GB. Files over 2 GB are stored as release
parts and re-assembled on download. Sends the file in small chunks (default
64 MB) through a resumable session:

  /v1/upload/start   -> {id, total_size}
  /v1/upload/chunk/{id}?offset=N   (raw body)
  /v1/upload/complete/{id}

Usage:
  python scripts/upload_chunked.py file.iso --api https://drive.vikashbuilds.in \
      --key key-vikash [--chunk-mb 64] [--resume]

Resume: pass --resume; progress is saved in <file>.gdupload and a stale
offset triggers a 409, after which the client re-syncs from the server.
"""

import argparse
import json
import os
import time

import httpx


def main():
    ap = argparse.ArgumentParser(description="Chunked upload to GitDrive (any size up to pool limit)")
    ap.add_argument("path", help="file to upload")
    ap.add_argument("--api", required=True, help="GitDrive API base URL")
    ap.add_argument("--key", required=True, help="GitDrive API key")
    ap.add_argument("--chunk-mb", type=int, default=64, help="chunk size in MB (edge cap is 100 MB)")
    ap.add_argument("--resume", action="store_true", help="resume from <path>.gdupload")
    args = ap.parse_args()

    path = args.path
    name = os.path.basename(path)
    total = os.path.getsize(path)
    chunk = min(args.chunk_mb, 90) * 1024 * 1024
    state_path = f"{path}.gdupload"
    state = {}
    if args.resume and os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
    headers = {"X-API-Key": args.key}

    with httpx.Client(timeout=httpx.Timeout(1800.0, connect=60.0)) as c:
        if not state.get("id"):
            print(f"[chunk] starting: {name} = {total / (1024**3):.2f} GB in {(total + chunk - 1) // chunk} chunks")
            r = c.post(f"{args.api}/v1/upload/start",
                       params={"name": name, "total_size": total, "mime": "application/octet-stream"},
                       headers=headers)
            r.raise_for_status()
            state = r.json()
            state["done"] = 0
            with open(state_path, "w") as f:
                json.dump(state, f)
        fid = state["id"]
        off = int(state.get("done", 0))
        print(f"[chunk] resuming {fid} at {off}/{total}")

        with open(path, "rb") as fh:
            fh.seek(off)
            while off < total:
                data = fh.read(chunk)
                r = c.post(f"{args.api}/v1/upload/chunk/{fid}", params={"offset": off},
                           content=data, headers=headers)
                if r.status_code == 409:
                    off = int(r.json()["detail"].split()[-1])
                    fh.seek(off)
                    continue
                r.raise_for_status()
                off = r.json()["offset"]
                state["done"] = off
                with open(state_path, "w") as f:
                    json.dump(state, f)
                print(f"[chunk] {off}/{total} ({(off * 100) // total}%)")

        r = c.post(f"{args.api}/v1/upload/complete/{fid}", headers=headers)
        r.raise_for_status()
        print(f"[chunk] complete accepted: {r.json()}")
        for _ in range(120):
            r = c.get(f"{args.api}/v1/file/{fid}", headers=headers)
            if r.status_code == 200:
                result = r.json()
                print(f"[chunk] FINAL: id={result['id']} size={result['size'] / (1024**3):.2f} GB "
                      f"status={result['status']} url={result.get('url') or '(none — parts, ready on download)'}")
                break
            time.sleep(5)
        else:
            raise SystemExit("timed out waiting for finalize")
        if os.path.exists(state_path):
            os.remove(state_path)


if __name__ == "__main__":
    main()
