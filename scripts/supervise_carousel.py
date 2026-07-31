"""supervise_carousel.py — keeps the relay carousel alive automatically.

Every hour (or on demand) checks each expected chain in the nodes table:
  - no node row yet            -> chain never started           -> dispatch it
  - last_seen older than STALE (default 90 min) -> chain died    -> dispatch again
  - node reported END          -> chain finished its shift       -> dispatch next

Dispatches via `gh workflow run carousel-node.yml -f node_id=carousel-XX`.

Usage:
  python scripts/supervise_carousel.py
  CHAINS="carousel-01 carousel-02 carousel-03" python scripts/supervise_carousel.py
"""

import os
import subprocess
import sys
from datetime import datetime, timezone

import psycopg2

DB_URL = os.environ["DB_URL"]
CHAINS = os.environ.get("CHAINS", "carousel-01 carousel-02 carousel-03").split()
STALE_MIN = int(os.environ.get("STALE_MIN", "90"))
WORKFLOW = os.environ.get("CAROUSEL_WORKFLOW", "carousel-node.yml")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"


def dispatch(node_id: str):
    cmd = ["gh", "workflow", "run", WORKFLOW, "-f", f"node_id={node_id}"]
    if DRY_RUN:
        print(f"[supervise] would dispatch: {cmd}")
        return
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"[supervise] dispatched {node_id}")
    else:
        print(f"[supervise] dispatch FAILED {node_id}: {r.stderr[:200]}")


def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    for chain in CHAINS:
        cur.execute(
            "SELECT last_seen, status, end_at FROM nodes WHERE node_id = %s ORDER BY last_seen DESC LIMIT 1",
            (chain,),
        )
        row = cur.fetchone()
        if not row:
            print(f"[supervise] {chain}: never seen -> dispatch")
            dispatch(chain)
            continue
        last_seen, status, end_at = row
        age_min = (now - last_seen.replace(tzinfo=timezone.utc)).total_seconds() / 60
        if status == "END":
            print(f"[supervise] {chain}: finished (idle {age_min:.0f}m) -> restart")
            dispatch(chain)
        elif age_min > STALE_MIN:
            print(f"[supervise] {chain}: stale {age_min:.0f}m -> dispatch")
            dispatch(chain)
        else:
            print(f"[supervise] {chain}: healthy (seen {age_min:.0f}m ago, {status})")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
