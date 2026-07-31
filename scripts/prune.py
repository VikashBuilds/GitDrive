import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
import psycopg2

DB_URL = os.environ["DB_URL"]
GH_TOKEN = os.environ["GH_TOKEN"]
STORAGE_REPO = os.environ["STORAGE_REPO"]
TELEGRAM_WEBHOOK = os.environ.get("TELEGRAM_WEBHOOK", "")
GH = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def db():
    return psycopg2.connect(DB_URL)


def git_delete_all(paths: list[str]):
    if not paths:
        return
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["git", "clone", "--depth", "1", f"https://x-access-token:{GH_TOKEN}@github.com/{STORAGE_REPO}.git", tmp],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "rm", "--ignore-unmatch"] + paths, cwd=tmp, capture_output=True)
        subprocess.run(["git", "config", "user.name", "GitDrive Bot"], cwd=tmp, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "gitdrive@users.noreply.github.com"], cwd=tmp, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "prune: expire files"], cwd=tmp, capture_output=True)
        for _ in range(3):
            r = subprocess.run(["git", "push"], cwd=tmp, capture_output=True)
            if r.returncode == 0:
                break
            subprocess.run(["git", "pull", "--rebase"], cwd=tmp, capture_output=True)


def release_delete(name: str, tag: str):
    client = httpx.Client(timeout=60)
    r = client.get(f"https://api.github.com/repos/{STORAGE_REPO}/releases/tags/{tag}", headers=GH)
    if r.status_code == 200:
        for asset in r.json().get("assets", []):
            if asset["name"] == name:
                client.delete(f"https://api.github.com/repos/{STORAGE_REPO}/releases/assets/{asset['id']}", headers=GH)
    client.close()


def release_delete_parts(parts_json: str, tag: str):
    if not parts_json:
        return
    client = httpx.Client(timeout=60)
    r = client.get(f"https://api.github.com/repos/{STORAGE_REPO}/releases/tags/{tag}", headers=GH)
    if r.status_code == 200:
        part_urls = {p.get("url") for p in json.loads(parts_json)}
        for asset in r.json().get("assets", []):
            if asset.get("browser_download_url") in part_urls:
                client.delete(f"https://api.github.com/repos/{STORAGE_REPO}/releases/assets/{asset['id']}", headers=GH)
    client.close()


def main():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, store, path, name, release_tag, size, parts_json FROM files WHERE expires_at IS NOT NULL AND expires_at < now()"
    )
    rows = cur.fetchall()
    git_paths = []
    for fid, store, path, name, tag, size, parts_json in rows:
        if store == "git":
            git_paths.append(path)
        elif store == "archive":
            release_delete_parts(parts_json, tag)
        else:
            release_delete(name, tag)
    git_delete_all(git_paths)
    cur.execute("DELETE FROM files WHERE expires_at IS NOT NULL AND expires_at < now()")
    conn.commit()

    cur.execute("SELECT count(*), coalesce(sum(size), 0) FROM files")
    count, total = cur.fetchone()
    cur.execute("SELECT coalesce(sum(size), 0) FROM files WHERE store = 'git'")
    git_bytes = cur.fetchone()[0]
    cur.execute("SELECT name, download_count FROM files ORDER BY download_count DESC LIMIT 5")
    top = [{"name": r[0], "downloads": r[1]} for r in cur.fetchall()]
    cur.close()
    conn.close()

    report = (
        f"GitDrive prune @ {datetime.now(timezone.utc).isoformat()}\n"
        f"expired removed: {len(rows)}\n"
        f"files: {count} | bytes: {total:,} (git {git_bytes:,} / release {total - git_bytes:,})\n"
        f"top: " + ", ".join(f"{t['name']} ({t['downloads']})" for t in top)
    )
    print(report)
    if TELEGRAM_WEBHOOK:
        httpx.post(TELEGRAM_WEBHOOK, json={"text": report})


if __name__ == "__main__":
    main()
