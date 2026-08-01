import argparse
import json
import os
import tarfile
from datetime import date, datetime

import httpx
import psycopg2

DB_URL = os.environ["DB_URL"]
GH_TOKEN = os.environ["GH_TOKEN"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
GH = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
CHUNK_MAX = 900 * 1024 * 1024
KEY_PREFIX = "gitdrive-backup"


def db():
    return psycopg2.connect(DB_URL)


def api_base() -> str:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k = 'tunnel_url'")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return (row[0] or "").strip() if row else ""


def resolve_url(url: str, base: str) -> str:
    if url.startswith("/"):
        return base + url
    return url


def build_chunks():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, url, size FROM files WHERE status <> 'deleted' ORDER BY created_at")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    base = api_base()
    os.makedirs("backup", exist_ok=True)
    chunks = []
    current = None
    current_size = 0
    for fid, name, url, size in rows:
        if size > 100 * 1024 * 1024:
            continue  # big files live durably as release assets / pool parts
        if current is None or current_size + (size or len(url)) > CHUNK_MAX:
            current = {"name": f"backup/chunk-{len(chunks):03d}.tar.gz", "files": []}
            chunks.append(current)
            current_size = 0
        current["files"].append((fid, name, url))
        current_size += size or len(url)

    result = []
    with httpx.Client(timeout=600) as client:
        for chunk in chunks:
            out_path = os.path.join(os.getcwd(), chunk["name"])
            with tarfile.open(out_path, "w:gz") as tar:
                for fid, name, url in chunk["files"]:
                    body = None
                    for attempt in range(3):
                        try:
                            r = client.get(resolve_url(url, base))
                            if r.status_code == 200:
                                body = r.content
                            break
                        except httpx.HTTPError:
                            continue
                    if body is None:
                        continue
                    data = tarfile.TarInfo(name=f"{fid}_{name}")
                    data.size = len(body)
                    tar.addfile(data, __import__("io").BytesIO(body))
            result.append({"chunk": chunk["name"].split("chunk-")[1].replace(".tar.gz", ""),
                           "path": os.path.abspath(out_path)})
    return result


def list_caches(client):
    url = f"https://api.github.com/repos/{REPO}/actions/caches?key={KEY_PREFIX}"
    caches = []
    while url:
        r = client.get(url, headers=GH)
        r.raise_for_status()
        body = r.json()
        caches.extend(body.get("actions_caches", []))
        url = body.get("next")  # pagination link (may be absent)
    return caches


def prune(keep_days: int, today: str):
    client = httpx.Client(timeout=60)
    caches = list_caches(client)
    cutoff = today
    # keys look like gitdrive-backup-YYYYMMDD-<chunk>
    for c in caches:
        key = c.get("key", "")
        try:
            day = key.split("-")[2]
        except IndexError:
            continue
        days_old = (datetime.strptime(today, "%Y%m%d") - datetime.strptime(day, "%Y%m%d")).days
        if days_old > keep_days:
            r = client.delete(
                f"https://api.github.com/repos/{REPO}/actions/caches/{c['id']}", headers=GH
            )
            print(f"deleted cache {key} ({r.status_code})")
    client.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-chunks", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--keep-days", type=int, default=5)
    ap.add_argument("--today", default=date.today().strftime("%Y%m%d"))
    args = ap.parse_args()

    if args.build_chunks:
        chunks = build_chunks()
        if not chunks:
            print("null")
            return
        print(json.dumps({"include": chunks}))

    if args.prune:
        prune(args.keep_days, args.today)


if __name__ == "__main__":
    main()
