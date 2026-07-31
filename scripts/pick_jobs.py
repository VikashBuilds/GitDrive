import argparse
import json
import os

import psycopg2

DB_URL = os.environ["DB_URL"]


def pick(limit: int, job_type: str) -> str:
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, target_id FROM jobs
        WHERE type = %s AND status = 'queued'
        ORDER BY id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (job_type, limit),
    )
    rows = cur.fetchall()
    if not rows:
        cur.close()
        conn.close()
        return "null"
    cur.executemany(
        "UPDATE jobs SET status = 'processing', attempts = attempts + 1, updated_at = now() WHERE id = %s",
        [(r[0],) for r in rows],
    )
    conn.commit()
    cur.close()
    conn.close()
    return json.dumps({"include": [{"job_id": r[0]} for r in rows]})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--type", default="compress")
    args = ap.parse_args()
    print(pick(args.limit, args.type))
