"""Seed the carousel sets from the files table (run once, idempotent).

Creates set-00..set-N, assigns every file to a set by stable hash,
and rebuilds each set's manifest (what the pool should hold).

Usage:
  python scripts/seed_sets.py            # create sets + assign + manifests
  python scripts/seed_sets.py --info     # print per-set summary only
"""

import json
import os
import sys

import psycopg2

DB_URL = os.environ["DB_URL"]
SET_COUNT = int(os.environ.get("SET_COUNT", "20"))


def db():
    return psycopg2.connect(DB_URL)


def set_of(sha: str) -> str:
    return f"set-{int(sha[:2], 16) % SET_COUNT:02d}"


def main():
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()

    if "--info" not in sys.argv:
        cur.execute("SELECT size_bytes, manifest_json FROM sets")
        existing = {r[0]: r[1] for r in cur.fetchall()}
        for i in range(SET_COUNT):
            sid = f"set-{i:02d}"
            if sid not in existing:
                cur.execute(
                    "INSERT INTO sets (set_id, size_bytes, manifest_json) VALUES (%s, 0, '[]')",
                    (sid,),
                )

        cur.execute("SELECT id, sha, name, size, url FROM files")
        manifests = {f"set-{i:02d}": [] for i in range(SET_COUNT)}
        sizes = {f"set-{i:02d}": 0 for i in range(SET_COUNT)}
        for fid, sha, name, size, url in cur.fetchall():
            sid = set_of(sha)
            cur.execute("UPDATE files SET set_id = %s WHERE id = %s", (sid, fid))
            manifests[sid].append({"name": f"{fid}_{name}", "size": size, "url": url})
            sizes[sid] += size

        for sid in manifests:
            cur.execute(
                "UPDATE sets SET size_bytes = %s, manifest_json = %s WHERE set_id = %s",
                (sizes[sid], json.dumps(manifests[sid]), sid),
            )
        print(f"[seed] {SET_COUNT} sets ensured, all files assigned, manifests rebuilt")

    cur.execute("SELECT set_id, size_bytes, status, holder, last_anchor_at FROM sets ORDER BY set_id")
    rows = cur.fetchall()
    total = sum(r[1] for r in rows)
    held = sum(1 for r in rows if r[2] == "held")
    print(f"\n{'set':<8}{'size':>10}  {'status':<8} holder")
    print("-" * 45)
    for sid, size, status, holder, anchor in rows:
        print(f"{sid:<8}{size / (1024 * 1024):>8.0f} MB  {status:<8} {holder or '-'}")
    print("-" * 45)
    print(f"total: {total / (1024 * 1024 * 1024):.1f} GB across {len(rows)} sets, {held} held")


if __name__ == "__main__":
    main()
