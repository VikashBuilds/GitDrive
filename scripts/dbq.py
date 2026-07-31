"""dbq.py — quick DB query tool for GitDrive ops.

Usage:
  python scripts/dbq.py "SELECT v FROM meta WHERE k = 'tunnel_url'"
  python scripts/dbq.py "SELECT node_id, status, last_seen FROM nodes ORDER BY node_id"
  python scripts/dbq.py "SELECT count(*), sum(size) FROM files"
"""

import os
import sys

import psycopg2

DB_URL = os.environ.get("DB_URL", "")
if not DB_URL:
    print("FATAL: set DB_URL env")
    sys.exit(1)

sql = sys.argv[1] if len(sys.argv) > 1 else "SELECT 1"

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()
cur.execute(sql)
for row in cur.fetchall():
    print(row)
cur.close()
conn.close()
