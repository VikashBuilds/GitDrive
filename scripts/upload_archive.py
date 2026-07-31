"""Upload any file (even 100 GB) to GitDrive's Archive tier.

Splits the file into ≤1.8 GB parts locally and pushes each part as a Release
asset. Release assets: no storage limit, no expiry, CDN-served. The server
tracks the parts and streams them back as ONE file via /v1/download/{id}.

Usage:
  python scripts/upload_archive.py file.iso --api https://drive.vikashbuilds.in \
      --key key-vikash [--part-mb 1800] [--delete-after] [--resume file.json]

Resume: pass --resume with a state file written on a previous interrupted run;
it skips parts already uploaded. The state file is written as <file>.gdstate.
"""

import argparse
import hashlib
import json
import os
import sys
import time

import httpx

PART_DEFAULT_MB = 1800


def load_state(state_path):
    if state_path and os.path.exists(state_path):
        with open(state_path) as f:
            return json.load(f)
    return None


def save_state(state_path, state):
    if state_path:
        with open(state_path, "w") as f:
            json.dump(state, f)


def main():
    ap = argparse.ArgumentParser(description="Upload a big file to GitDrive Archive tier")
    ap.add_argument("path", help="file to upload (any size)")
    ap.add_argument("--api", required=True, help="GitDrive API base URL")
    ap.add_argument("--key", required=True, help="GitDrive API key")
    ap.add_argument("--part-mb", type=int, default=PART_DEFAULT_MB, help="part size in MB (max 1900)")
    ap.add_argument("--resume", action="store_true", help="resume from <path>.gdstate")
    ap.add_argument("--delete-after", action="store_true", help="delete local file after upload")
    args = ap.parse_args()

    path = args.path
    name = os.path.basename(path)
    total = os.path.getsize(path)
    part_size = min(args.part_mb, 1900) * 1024 * 1024
    total_parts = max(1, -(-total // part_size))
    state_path = f"{path}.gdstate"
    state = load_state(state_path) if args.resume else None
    headers = {"X-API-Key": args.key}

    with httpx.Client(timeout=httpx.Timeout(3600.0, connect=60.0)) as c:
        if not state:
            print(f"[archive] starting: {name} = {total / (1024**3):.1f} GB in {total_parts} parts")
            r = c.post(f"{args.api}/v1/archive/start",
                       params={"name": name, "total_parts": total_parts},
                       headers=headers)
            r.raise_for_status()
            state = r.json()
            state["parts_done"] = []
            save_state(state_path, state)
        else:
            print(f"[archive] resuming {name}: {len(state['parts_done'])}/{total_parts} parts done")
        fid = state["id"]
        done = set(state["parts_done"])

        with open(path, "rb") as fh:
            for idx in range(total_parts):
                if idx in done:
                    fh.seek(part_size * idx)
                    continue
                chunk = fh.read(part_size)
                r = c.post(f"{args.api}/v1/archive/{fid}/part",
                           params={"index": idx},
                           files={"file": (f"{name}.part{idx:03d}", chunk, "application/octet-stream")},
                           headers=headers)
                if r.status_code == 409:
                    done.add(idx)
                    continue
                r.raise_for_status()
                done.add(idx)
                state["parts_done"] = sorted(done)
                save_state(state_path, state)
                print(f"[archive] part {idx + 1}/{total_parts} done")

        r = c.post(f"{args.api}/v1/archive/{fid}/complete", headers=headers)
        r.raise_for_status()
        result = r.json()
        print(f"[archive] COMPLETE: {result['size'] / (1024**3):.1f} GB in {result['parts']} parts")
        print(f"[archive] download: {args.api}{result['url']}")

        os.remove(state_path) if os.path.exists(state_path) else None
        if args.delete_after:
            os.remove(path)
            print(f"[archive] deleted local file")


if __name__ == "__main__":
    main()
