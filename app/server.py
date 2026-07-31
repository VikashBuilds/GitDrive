import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import psycopg2
import uvicorn
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

DB_URL = os.environ["DB_URL"]
API_KEYS = [k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()]
STORAGE_REPOS = [r.strip() for r in os.environ.get("STORAGE_REPOS", os.environ.get("STORAGE_REPO", "")).split(",") if r.strip()]
GH_TOKEN = os.environ["GH_TOKEN"]
GIT_THRESHOLD = int(os.environ.get("UPLOAD_LIMIT_MB", "25")) * 1024 * 1024
RELEASE_MAX = 2 * 1024 * 1024 * 1024          # GitHub release asset cap
POOL_MAX_GB = int(os.environ.get("POOL_MAX_GB", "12"))
POOL_MAX = POOL_MAX_GB * 1024 * 1024 * 1024    # max file that fits a 14 GB pool node
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "")
BUFFER_DIR = "/tmp/gitdrive-buffer"
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_HOUR", "60"))
SET_COUNT = int(os.environ.get("SET_COUNT", "20"))
STORE_DIRS = {repo: f"/tmp/gitdrive-store-{i}" for i, repo in enumerate(STORAGE_REPOS)}

GH = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

BLOCKED_EXT = {".exe", ".sh", ".bat", ".cmd", ".htm", ".html", ".svg", ".js", ".hta", ".ps1", ".vbs", ".jar"}
BLOCKED_MIME = {"text/html", "application/x-msdownload", "application/x-sh"}

app = FastAPI(title="GitDrive API")
_uploads = defaultdict(deque)
_gh = httpx.Client(timeout=300)
_gh_async = httpx.AsyncClient(timeout=600)


def db():
    return psycopg2.connect(DB_URL)


def check_key(key: str):
    if API_KEYS and key not in API_KEYS:
        raise HTTPException(401, "invalid api key")
    now = time.time()
    q = _uploads[key]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "rate limited")
    q.append(now)


def repo_for(set_id: str) -> str:
    return STORAGE_REPOS[int(set_id.split("-")[1]) % len(STORAGE_REPOS)]


def git(args: list, repo: str):
    r = subprocess.run(["git"] + args, cwd=STORE_DIRS[repo], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr[-500:]}")
    return r.stdout


def git_store_bytes(data: bytes, rel: str, repo: str):
    dest = os.path.join(STORE_DIRS[repo], rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    git(["add", rel], repo)
    git(["commit", "-m", f"upload: {rel}"], repo)
    for attempt in range(3):
        try:
            git(["push"], repo)
            return
        except RuntimeError:
            git(["pull", "--rebase"], repo)


def git_delete_path(rel: str, repo: str):
    git(["rm", "--ignore-unmatch", rel], repo)
    try:
        git(["commit", "-m", f"delete: {rel}"], repo)
    except RuntimeError:
        return
    for attempt in range(3):
        try:
            git(["push"], repo)
            return
        except RuntimeError:
            git(["pull", "--rebase"], repo)


def ensure_release(tag: str, repo: str) -> int:
    r = _gh.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=GH)
    if r.status_code == 200:
        return r.json()["id"]
    r2 = _gh.post(
        f"https://api.github.com/repos/{repo}/releases",
        headers=GH,
        json={"tag_name": tag, "name": tag, "prerelease": True, "target_commitish": "main"},
    )
    r2.raise_for_status()
    return r2.json()["id"]


def release_upload(data: bytes, name: str, mime: str, tag: str, repo: str) -> str:
    release_id = ensure_release(tag, repo)
    r = _gh.post(
        f"https://api.github.com/repos/{repo}/releases/{release_id}/assets?name={quote(name)}",
        headers=GH,
        files={"file": (name, data, mime or "application/octet-stream")},
    )
    r.raise_for_status()
    return f"https://github.com/{repo}/releases/download/{tag}/{quote(name)}"


def release_delete(name: str, tag: str, repo: str):
    r = _gh.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=GH)
    if r.status_code != 200:
        return
    for asset in r.json().get("assets", []):
        if asset["name"] == name:
            _gh.delete(f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}", headers=GH)
            return


def release_delete_parts(parts_json: str, tag: str, repo: str):
    """Delete every release asset referenced by an archive file's parts list."""
    if not parts_json:
        return
    r = _gh.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=GH)
    if r.status_code != 200:
        return
    part_urls = {p.get("url") for p in json.loads(parts_json)}
    for asset in r.json().get("assets", []):
        if asset.get("browser_download_url") in part_urls:
            _gh.delete(f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}", headers=GH)


def enqueue_compress(file_id: str, size: int, mime: str):
    if not mime:
        return
    if size < 200_000:
        return
    if mime.startswith("image/") or mime.startswith("video/"):
        conn = db()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("INSERT INTO jobs (type, target_id) VALUES ('compress', %s)", (file_id,))
        cur.close()
        conn.close()


@app.on_event("startup")
def startup():
    for repo, store_dir in STORE_DIRS.items():
        os.makedirs(store_dir, exist_ok=True)
        if not os.path.exists(os.path.join(store_dir, ".git")):
            subprocess.run(
                ["git", "clone", "--depth", "1", f"https://x-access-token:{GH_TOKEN}@github.com/{repo}.git", store_dir],
                check=True, capture_output=True,
            )
        git(["config", "user.name", "GitDrive Bot"], repo)
        git(["config", "user.email", "gitdrive@users.noreply.github.com"], repo)


@app.get("/v1/health")
def health():
    return {"ok": True, "uptime_s": int(time.time() - start_time)}


@app.get("/v1/buffer/{file_id}")
def buffer_download(file_id: str, x_relay_token: str = Header("")):
    """Carousel nodes pull big buffered files from here (relay-token auth)."""
    if x_relay_token != RELAY_TOKEN:
        raise HTTPException(403, "bad relay token")
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT path, name FROM files WHERE id = %s AND store = 'pool'", (file_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(404, "not found")
    f = os.path.join(BUFFER_DIR, row[0])
    if not os.path.isfile(f):
        raise HTTPException(404, "buffer expired")
    return FileResponse(f, filename=row[1])


@app.delete("/v1/buffer/{file_id}")
def buffer_delete(file_id: str, x_relay_token: str = Header("")):
    """Carousel nodes confirm a successful drain; buffer is freed."""
    if x_relay_token != RELAY_TOKEN:
        raise HTTPException(403, "bad relay token")
    f = os.path.join(BUFFER_DIR, file_id)
    if os.path.isfile(f):
        os.remove(f)
    return {"deleted": True}


# ---------- archive tier: unlimited storage via chunked Releases ----------

@app.post("/v1/archive/start")
async def archive_start(name: str, total_parts: int, mime: str = "application/octet-stream",
                        x_api_key: str = Header("")):
    """Begin a chunked archive upload. Returns the file id + release tag."""
    check_key(x_api_key)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if total_parts < 1 or total_parts > 1000:
        raise HTTPException(400, "total_parts must be 1-1000")
    fid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    set_id = f"set-{int(fid[:2], 16) % SET_COUNT:02d}"
    repo = repo_for(set_id)
    tag = f"archive-{now.strftime('%Y%m%d%H')}-{fid}"
    ensure_release(tag, repo)
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO files (id, sha, name, size, mime, store, path, release_tag, url, status, set_id, repo, parts_json) "
        "VALUES (%s, %s, %s, 0, %s, 'archive', %s, %s, '', 'archiving', %s, %s, '[]')",
        (fid, f"archive-{fid}", name, mime, f"parts/{fid}", tag, set_id, repo),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"id": fid, "tag": tag, "total_parts": total_parts, "repo": repo}


@app.post("/v1/archive/{file_id}/part")
async def archive_part(file_id: str, index: int, file: UploadFile = File(...),
                       x_api_key: str = Header("")):
    """Upload one ≤2 GB part. index starts at 0."""
    check_key(x_api_key)
    data = await file.read()
    if len(data) > RELEASE_MAX:
        raise HTTPException(413, "part too large (max 2 GB)")
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT release_tag, repo, name, parts_json FROM files WHERE id = %s AND store = 'archive'", (file_id,))
    row = cur.fetchone()
    if not row or row[3] is None:
        cur.close()
        conn.close()
        raise HTTPException(404, "archive not found or already complete")
    tag, repo, name, parts_json = row
    parts = json.loads(parts_json or "[]")
    if index != len(parts):
        cur.close()
        conn.close()
        raise HTTPException(409, f"expected part {len(parts)}, got {index}")
    release_id = ensure_release(tag, repo)
    pname = f"{name}.part{index:03d}"
    r = _gh.post(
        f"https://api.github.com/repos/{repo}/releases/{release_id}/assets?name={quote(pname)}",
        headers=GH,
        files={"file": (pname, data, "application/octet-stream")},
    )
    r.raise_for_status()
    parts.append({"url": r.json()["browser_download_url"], "size": len(data)})
    cur.execute("UPDATE files SET parts_json = %s WHERE id = %s", (json.dumps(parts), file_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"index": index, "parts": len(parts), "url": parts[-1]["url"]}


@app.post("/v1/archive/{file_id}/complete")
async def archive_complete(file_id: str, x_api_key: str = Header("")):
    """Finalize the archive; the file gets its permanent download URL."""
    check_key(x_api_key)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT name, parts_json FROM files WHERE id = %s AND store = 'archive' AND status = 'archiving'", (file_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(404, "archive not found")
    name, parts_json = row
    parts = json.loads(parts_json or "[]")
    if not parts:
        cur.close()
        conn.close()
        raise HTTPException(400, "no parts uploaded")
    size = sum(p["size"] for p in parts)
    url = f"/v1/download/{file_id}"
    cur.execute("UPDATE files SET status = 'ready', size = %s, url = %s, sha = %s WHERE id = %s",
                (size, url, f"archive-{size}-{name}", file_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"id": file_id, "parts": len(parts), "size": size, "url": url}


@app.get("/v1/download/{file_id}")
async def download(file_id: str):
    """Stream any file. Archives are concatenated from their release parts."""
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT store, url, name, parts_json, status FROM files WHERE id = %s", (file_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(404, "not found")
    store, url, name, parts_json, status = row
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE files SET download_count = download_count + 1 WHERE id = %s", (file_id,))
    conn.commit()
    cur.close()
    conn.close()
    if store == "pool" and status == "deleted":
        raise HTTPException(410, "file deleted — copy pruned from pool")
    if store == "archive":
        parts = json.loads(parts_json or "[]")
        if not parts:
            raise HTTPException(404, "no parts")
        total = sum(p["size"] for p in parts)

        async def gen():
            for p in parts:
                async with _gh_async.stream("GET", p["url"]) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(1024 * 256):
                        yield chunk

        return StreamingResponse(
            gen(),
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(total),
                "Content-Disposition": f'attachment; filename="{name}"',
            },
        )
    return RedirectResponse(url)


@app.post("/v1/upload")
async def upload(file: UploadFile = File(...), x_api_key: str = Header(""), expire_days: int | None = None):
    check_key(x_api_key)
    data = await file.read()
    if len(data) > POOL_MAX:
        raise HTTPException(413, f"file too large (max {POOL_MAX_GB} GB — pool limit)")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "file")
    ext = os.path.splitext(name)[1].lower()
    mime = (file.content_type or "").lower()
    if ext in BLOCKED_EXT or mime in BLOCKED_MIME:
        raise HTTPException(415, "mime type not allowed")

    sha = hashlib.sha256(data).hexdigest()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, url, size, mime, status, expires_at, download_count FROM files WHERE sha = %s", (sha,))
    row = cur.fetchone()
    if row:
        cur.close()
        conn.close()
        return {"id": row[0], "url": row[1], "size": row[2], "mime": row[3], "deduped": True,
                "expires_at": row[5].isoformat() if row[5] else None}

    fid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    rel = f"files/{now.strftime('%Y/%m')}/{sha[:2]}/{fid}-{name}"
    set_id = f"set-{int(sha[:2], 16) % SET_COUNT:02d}"
    repo = repo_for(set_id)

    expires = None
    if expire_days:
        expires = now + timedelta(days=expire_days)

    if len(data) <= GIT_THRESHOLD:
        git_store_bytes(data, rel, repo)
        url = f"https://raw.githubusercontent.com/{repo}/main/{rel}"
        store = "git"
        tag = None
        status = "ready"
    elif len(data) <= RELEASE_MAX:
        tag = "assets-" + now.strftime("%Y%m%d%H")
        url = release_upload(data, name, mime, tag, repo)
        store = "release"
        status = "ready"
    else:
        # Big file (2-12 GB): only the relay pool can hold it.
        # Buffer on the API runner; a carousel node drains it into its set.
        os.makedirs(BUFFER_DIR, exist_ok=True)
        with open(os.path.join(BUFFER_DIR, fid), "wb") as fh:
            fh.write(data)
        url = ""
        store = "pool"
        status = "buffered"
        tag = None
        conn2 = db()
        conn2.autocommit = True
        cur2 = conn2.cursor()
        cur2.execute(
            "INSERT INTO jobs (type, target_id) VALUES ('pool-store', %s)", (fid,),
        )
        cur2.close()
        conn2.close()

    cur.execute(
        "INSERT INTO files (id, sha, name, size, mime, store, path, release_tag, url, expires_at, set_id, repo, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (fid, sha, name, len(data), mime, store, rel, tag, url, expires, set_id, repo, status),
    )
    conn.commit()
    cur.close()
    conn.close()

    enqueue_compress(fid, len(data), mime)
    note = "queued for relay pool" if store == "pool" else None
    return {"id": fid, "name": name, "size": len(data), "mime": mime, "url": url,
            "deduped": False, "status": status, "note": note,
            "expires_at": expires.isoformat() if expires else None}


@app.get("/v1/file/{file_id}")
def get_file(file_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, size, mime, url, status, expires_at, download_count, created_at FROM files WHERE id = %s", (file_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(404, "not found")
    return {"id": row[0], "name": row[1], "size": row[2], "mime": row[3], "url": row[4],
            "status": row[5], "expires_at": row[6].isoformat() if row[6] else None,
            "downloads": row[7], "created_at": row[8].isoformat()}


@app.get("/v1/files")
def list_files(limit: int = 50, offset: int = 0, mime: str | None = None):
    conn = db()
    cur = conn.cursor()
    if mime:
        cur.execute("SELECT id, name, size, mime, url, created_at FROM files WHERE mime LIKE %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (mime + "%", limit, offset))
        cur.execute("SELECT count(*) FROM files WHERE mime LIKE %s", (mime + "%",))
    else:
        cur.execute("SELECT id, name, size, mime, url, created_at FROM files ORDER BY created_at DESC LIMIT %s OFFSET %s", (limit, offset))
        cur.execute("SELECT count(*) FROM files")
    items = [{"id": r[0], "name": r[1], "size": r[2], "mime": r[3], "url": r[4],
              "created_at": r[5].isoformat()} for r in cur.fetchall()]
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    return {"items": items, "total": total}


@app.delete("/v1/file/{file_id}")
def delete_file(file_id: str, x_api_key: str = Header("")):
    check_key(x_api_key)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT store, path, name, release_tag, repo, parts_json FROM files WHERE id = %s", (file_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(404, "not found")
    store, path, name, tag, repo, parts_json = row
    if store == "git":
        git_delete_path(path, repo)
    elif store == "archive":
        release_delete_parts(parts_json, tag, repo)
    elif store == "pool":
        # disk copy lives on a carousel runner; mark deleted and the next
        # check-in of that set prunes the file from disk + manifest.
        cur.execute("UPDATE files SET status = 'deleted' WHERE id = %s", (file_id,))
        conn.commit()
        cur.close()
        conn.close()
        return {"deleted": True, "note": "pool copy pruned at next carousel check-in"}
    else:
        release_delete(name, tag, repo)
    cur.execute("DELETE FROM files WHERE id = %s", (file_id,))
    conn.commit()
    cur.close()
    conn.close()
    return {"deleted": True}


@app.get("/v1/stats")
def stats():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT count(*), coalesce(sum(size), 0) FROM files")
    count, total = cur.fetchone()
    cur.execute("SELECT coalesce(sum(size), 0) FROM files WHERE store = 'git'")
    git_bytes = cur.fetchone()[0]
    cur.execute("SELECT name, download_count FROM files ORDER BY download_count DESC LIMIT 5")
    top = [{"name": r[0], "downloads": r[1]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"files": count, "bytes_total": total, "bytes_git": git_bytes,
            "bytes_release": total - git_bytes, "top_files": top, "repos": STORAGE_REPOS}


start_time = time.time()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
