import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import traceback
from urllib.parse import quote

import httpx
import psycopg2
import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

DB_URL = os.environ["DB_URL"]
API_KEYS = [k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()]
_API_KEY_PLAIN = set()
_API_KEY_SCOPES = {}
for _k in API_KEYS:
    if ":" in _k:
        _key, _scope = _k.split(":", 1)
        _API_KEY_PLAIN.add(_key)
        _API_KEY_SCOPES[_key] = _scope
    else:
        _API_KEY_PLAIN.add(_k)
STORAGE_REPOS = [r.strip() for r in os.environ.get("STORAGE_REPOS", os.environ.get("STORAGE_REPO", "")).split(",") if r.strip()]
PRIVATE_STORAGE_REPOS = [r.strip() for r in os.environ.get("PRIVATE_STORAGE_REPOS", "VikashBuilds/private-p1,VikashBuilds/private-p2").split(",") if r.strip()]
GH_TOKEN = os.environ["GH_TOKEN"]
GIT_THRESHOLD = int(os.environ.get("UPLOAD_LIMIT_MB", "25")) * 1024 * 1024
RELEASE_MAX = 2 * 1024 * 1024 * 1024          # GitHub release asset cap
POOL_PART_MAX = int(1.8 * 1024 * 1024 * 1024)  # durable pool buffer part size
POOL_MAX_GB = int(os.environ.get("POOL_MAX_GB", "12"))
POOL_MAX = POOL_MAX_GB * 1024 * 1024 * 1024    # max file that fits a 14 GB pool node
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "")
DASHBOARD_REPO = os.environ.get("DASHBOARD_REPO", "VikashBuilds/GitDrive")
BUFFER_DIR = "/tmp/gitdrive-buffer"
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_HOUR", "60"))
SET_COUNT = int(os.environ.get("SET_COUNT", "20"))
STORE_DIRS = {repo: f"/tmp/gitdrive-store-{i}" for i, repo in enumerate(STORAGE_REPOS + PRIVATE_STORAGE_REPOS)}

GH = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

BLOCKED_EXT = {".exe", ".sh", ".bat", ".cmd", ".htm", ".html", ".svg", ".js", ".hta", ".ps1", ".vbs", ".jar"}
BLOCKED_MIME = {"text/html", "application/x-msdownload", "application/x-sh"}

app = FastAPI(title="GitDrive API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse({"error": str(exc)}, status_code=500)


_uploads = defaultdict(deque)
_gh = httpx.Client(timeout=300)
_gh_async = httpx.AsyncClient(timeout=600, follow_redirects=True)


def db():
    return psycopg2.connect(DB_URL)


def check_key(key: str, need: str = "admin"):
    if not _API_KEY_PLAIN or key not in _API_KEY_PLAIN:
        raise HTTPException(401, "invalid api key")
    scope = _API_KEY_SCOPES.get(key, "admin")
    if need == "admin" and scope != "admin":
        raise HTTPException(403, "key is upload-scoped; admin action rejected")
    now = time.time()
    q = _uploads[key]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "rate limited")
    q.append(now)


def repo_for(set_id: str) -> str:
    return STORAGE_REPOS[int(set_id.split("-")[1]) % len(STORAGE_REPOS)]


def private_repo_for(set_id: str) -> str:
    if not PRIVATE_STORAGE_REPOS:
        return repo_for(set_id)
    return PRIVATE_STORAGE_REPOS[int(set_id.split("-")[1]) % len(PRIVATE_STORAGE_REPOS)]


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
    if r2.status_code == 422 and "Repository is empty" in r2.text:
        _seed_empty_repo(repo)
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
        f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets?name={quote(name)}",
        headers={**GH, "Content-Type": mime or "application/octet-stream"},
        content=data,
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
        if not _repo_has_commits(repo):
            _seed_empty_repo(repo)


def _repo_has_commits(repo: str) -> bool:
    r = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=STORE_DIRS[repo], capture_output=True)
    return r.returncode == 0


def _seed_empty_repo(repo: str):
    """GitHub refuses to create Releases on empty repos — seed an initial commit."""
    dest = os.path.join(STORE_DIRS[repo], "README.md")
    with open(dest, "w") as f:
        f.write(f"# GitDrive storage repo\nInitialized {datetime.now(timezone.utc).isoformat()}.\n")
    git(["add", "README.md"], repo)
    git(["commit", "-m", "chore: init storage repo"], repo)
    for attempt in range(3):
        try:
            git(["push"], repo)
            print(f"[startup] seeded empty repo {repo}")
            return
        except RuntimeError:
            git(["pull", "--rebase"], repo)
    print(f"[startup] WARNING: could not seed {repo}")


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
                        private: bool = False, x_api_key: str = Header("")):
    """Begin a chunked archive upload. Returns the file id + release tag."""
    check_key(x_api_key, need="upload")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if total_parts < 1 or total_parts > 1000:
        raise HTTPException(400, "total_parts must be 1-1000")
    fid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    set_id = f"set-{int(fid[:2], 16) % SET_COUNT:02d}"
    repo = private_repo_for(set_id) if private else repo_for(set_id)
    tag = f"archive-{now.strftime('%Y%m%d%H')}-{fid}"
    ensure_release(tag, repo)
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO files (id, sha, name, size, mime, store, path, release_tag, url, status, set_id, repo, parts_json, private) "
        "VALUES (%s, %s, %s, 0, %s, 'archive', %s, %s, '', 'archiving', %s, %s, '[]', %s)",
        (fid, f"archive-{fid}", name, mime, f"parts/{fid}", tag, set_id, repo, private),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"id": fid, "tag": tag, "total_parts": total_parts, "repo": repo}


@app.post("/v1/archive/{file_id}/part")
async def archive_part(file_id: str, index: int, file: UploadFile = File(...),
                       x_api_key: str = Header("")):
    """Upload one ≤2 GB part. index starts at 0."""
    check_key(x_api_key, need="upload")
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
        f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets?name={quote(pname)}",
        headers={**GH, "Content-Type": "application/octet-stream"},
        content=data,
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
    check_key(x_api_key, need="upload")
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
                (size, url, f"archive-{file_id}-{name}", file_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"id": file_id, "parts": len(parts), "size": size, "url": url}


def _asset_id(repo: str, tag: str, url: str | None = None, name: str | None = None) -> int:
    """Resolve a private release asset to its API asset id (auth required)."""
    r = _gh.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=GH)
    if r.status_code != 200:
        raise HTTPException(404, "release not found")
    for a in r.json().get("assets", []):
        if url and a.get("browser_download_url") == url:
            return a["id"]
        if name and a["name"] == name:
            return a["id"]
    raise HTTPException(404, "asset not found")


def _stream_private_asset(repo: str, asset_id: int, name: str, total: int):
    async def gen():
        async with _gh_async.stream(
            "GET", f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}",
            headers={**GH, "Accept": "application/octet-stream"},
        ) as r:
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


def _download_private(store: str, name: str, repo: str, tag: str, path: str,
                      parts_json: str | None, size: int):
    """Serve a private file via authenticated GitHub API (never a public URL)."""
    if store == "git":
        r = _gh.get(
            f"https://api.github.com/repos/{repo}/contents/{quote(path)}",
            headers={**GH, "Accept": "application/vnd.github.raw"},
        )
        if r.status_code != 200:
            raise HTTPException(404, "private file content not found")
        return Response(
            content=r.content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    if store == "release":
        aid = _asset_id(repo, tag, name=name)
        return _stream_private_asset(repo, aid, name, size)
    parts = json.loads(parts_json or "[]")
    if not parts:
        raise HTTPException(404, "no parts")
    total = sum(p["size"] for p in parts)
    ids = [_asset_id(repo, tag, url=p.get("url")) for p in parts]

    async def gen():
        for aid in ids:
            async with _gh_async.stream(
                "GET", f"https://api.github.com/repos/{repo}/releases/assets/{aid}",
                headers={**GH, "Accept": "application/octet-stream"},
            ) as r:
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


@app.get("/v1/download/{file_id}")
async def download(file_id: str, x_api_key: str = Header("")):
    """Stream any file. Archives are concatenated from their release parts.

    Private files are proxied through authenticated GitHub API endpoints so
    the underlying storage URL is never exposed — and require a valid API key.
    """
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT store, url, name, parts_json, status, repo, path, size, release_tag, private "
        "FROM files WHERE id = %s",
        (file_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(404, "not found")
    store, url, name, parts_json, status, repo, path, size, tag, private = row
    if private and x_api_key not in _API_KEY_PLAIN:
        raise HTTPException(401, "private file — X-API-Key required")
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE files SET download_count = download_count + 1 WHERE id = %s", (file_id,))
    conn.commit()
    cur.close()
    conn.close()
    if store == "pool" and status == "deleted":
        raise HTTPException(410, "file deleted — copy pruned from pool")
    if private:
        return _download_private(store, name, repo, tag, path, parts_json, size)
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


def finalize_upload(name: str, mime: str, data_path: str, expires: datetime | None = None,
                    fid: str | None = None, private: bool = False):
    """Shared tail of upload: hash, dedupe, tier decision, record row.

    Works on a spool file so big uploads never sit fully in RAM.
    Private files land in PRIVATE_STORAGE_REPOS and are served through
    /v1/download (authenticated proxy) instead of public GitHub URLs.
    """
    size = os.path.getsize(data_path)
    sha = hashlib.sha256()
    with open(data_path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            sha.update(block)
    digest = sha.hexdigest()

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, url, size, mime, status, expires_at, download_count FROM files "
        "WHERE sha = %s AND private = %s",
        (digest, private),
    )
    row = cur.fetchone()
    if row:
        cur.close()
        conn.close()
        return {"id": row[0], "url": row[1], "size": row[2], "mime": row[3], "deduped": True,
                "expires_at": row[5].isoformat() if row[5] else None, "private": private}

    fid = fid or uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    rel = f"files/{now.strftime('%Y/%m')}/{digest[:2]}/{fid}-{name}"
    set_id = f"set-{int(digest[:2], 16) % SET_COUNT:02d}"
    repo = private_repo_for(set_id) if private else repo_for(set_id)

    parts_json = None
    if size <= GIT_THRESHOLD:
        with open(data_path, "rb") as fh:
            git_store_bytes(fh.read(), rel, repo)
        url = f"/v1/download/{fid}" if private else f"https://raw.githubusercontent.com/{repo}/main/{rel}"
        store = "git"
        tag = None
        status = "ready"
    elif size <= RELEASE_MAX:
        with open(data_path, "rb") as fh:
            data = fh.read()
        tag = "assets-" + now.strftime("%Y%m%d%H")
        url = f"/v1/download/{fid}" if private else release_upload(data, name, mime, tag, repo)
        store = "release"
        status = "ready"
    else:
        # Big file (2-12 GB): only the relay pool can hold it. Buffer it
        # durably as release-asset parts (survives runner rotation), then a
        # carousel node drains the parts into its set whenever it boots.
        # Parts are staged to disk and pushed with curl -T: uploads.github.com
        # rejects chunked/streamed bodies (needs a real Content-Length).
        tag = "pool-" + fid
        release_id = ensure_release(tag, repo)
        r = _gh.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=GH)
        existing = {}
        if r.status_code == 200:
            existing = {a["name"]: a["browser_download_url"] for a in r.json().get("assets", [])}
        parts = []
        part_tmp = os.path.join(BUFFER_DIR, f"{fid}.part")
        try:
            with open(data_path, "rb") as fh:
                idx = 0
                while True:
                    base = idx * POOL_PART_MAX
                    if base >= size:
                        break
                    end = min(base + POOL_PART_MAX, size)
                    part_name = f"{name}.p{idx:03d}"
                    if part_name in existing:
                        part_url = existing[part_name]
                    else:
                        fh.seek(base)
                        with open(part_tmp, "wb") as pf:
                            remaining = end - base
                            while remaining > 0:
                                chunk = fh.read(min(1 << 20, remaining))
                                if not chunk:
                                    break
                                remaining -= len(chunk)
                                pf.write(chunk)
                        up = subprocess.run(
                            ["curl", "-sS", "-X", "POST",
                             "-H", f"Authorization: Bearer {GH_TOKEN}",
                             "-H", "Accept: application/vnd.github+json",
                             "-H", "X-GitHub-Api-Version: 2022-11-28",
                             "-H", "Content-Type: application/octet-stream",
                             "-T", part_tmp,
                             f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets?name={quote(part_name)}"],
                            capture_output=True, text=True, timeout=1800,
                        )
                        if up.returncode != 0 or "browser_download_url" not in up.stdout:
                            raise RuntimeError(f"part upload failed ({up.returncode}): {up.stderr[-300:] or up.stdout[-300:]}")
                        part_url = json.loads(up.stdout)["browser_download_url"]
                        os.remove(part_tmp)
                    parts.append({"url": part_url, "size": end - base})
                    idx += 1
        finally:
            if os.path.exists(part_tmp):
                os.remove(part_tmp)
        parts_json = json.dumps(parts)
        os.remove(data_path)
        url = ""
        store = "pool"
        status = "buffered"

    cur.execute(
        "INSERT INTO files (id, sha, name, size, mime, store, path, release_tag, url, expires_at, set_id, repo, status, parts_json, private) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (fid, digest, name, size, mime, store, rel, tag, url, expires, set_id, repo, status, parts_json, private),
    )
    conn.commit()
    cur.close()
    conn.close()

    if store == "pool":
        conn2 = db()
        conn2.autocommit = True
        cur2 = conn2.cursor()
        cur2.execute(
            "INSERT INTO jobs (type, target_id) VALUES ('pool-store', %s)", (fid,),
        )
        cur2.close()
        conn2.close()

    enqueue_compress(fid, size, mime)
    note = "queued for relay pool" if store == "pool" else None
    return {"id": fid, "name": name, "size": size, "mime": mime, "url": url,
            "deduped": False, "status": status, "note": note, "private": private,
            "expires_at": expires.isoformat() if expires else None}


@app.post("/v1/upload")
async def upload(file: UploadFile = File(...), private: bool = Form(False),
                 x_api_key: str = Header(""), expire_days: int | None = Form(None)):
    check_key(x_api_key, need="upload")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "file")
    ext = os.path.splitext(name)[1].lower()
    mime = (file.content_type or "").lower()
    if ext in BLOCKED_EXT or mime in BLOCKED_MIME:
        raise HTTPException(415, "mime type not allowed")

    os.makedirs(BUFFER_DIR, exist_ok=True)
    spool = os.path.join(BUFFER_DIR, f"up-{uuid.uuid4().hex[:12]}")
    total = 0
    with open(spool, "wb") as fh:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            fh.write(chunk)
    try:
        if total > POOL_MAX:
            raise HTTPException(413, f"file too large (max {POOL_MAX_GB} GB — pool limit)")
        expires = datetime.now(timezone.utc) + timedelta(days=expire_days) if expire_days else None
        result = finalize_upload(name, mime, spool, expires=expires, private=private)
    finally:
        if os.path.exists(spool):
            os.remove(spool)
    return result


@app.post("/v1/upload/start")
async def upload_start(name: str, total_size: int, mime: str = "", private: bool = False,
                       x_api_key: str = Header("")):
    """Open a chunked upload session (bypasses the 100 MB edge cap)."""
    check_key(x_api_key, need="upload")
    if total_size > POOL_MAX:
        raise HTTPException(413, f"file too large (max {POOL_MAX_GB} GB — pool limit)")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name or "file")
    ext = os.path.splitext(safe)[1].lower()
    if ext in BLOCKED_EXT or (mime or "").lower() in BLOCKED_MIME:
        raise HTTPException(415, "mime type not allowed")
    if total_size <= 0:
        raise HTTPException(400, "total_size must be positive")
    fid = uuid.uuid4().hex[:12]
    os.makedirs(BUFFER_DIR, exist_ok=True)
    with open(os.path.join(BUFFER_DIR, f"{fid}.up"), "wb"):
        pass
    with open(os.path.join(BUFFER_DIR, f"{fid}.meta"), "w") as fh:
        json.dump({"name": safe, "mime": mime or "", "total_size": total_size, "private": private, "created": time.time()}, fh)
    return {"id": fid, "total_size": total_size}


@app.post("/v1/upload/chunk/{fid}")
async def upload_chunk(fid: str, offset: int, request: Request, x_api_key: str = Header("")):
    """Append a ≤90 MB chunk at a fixed offset; 409 with current offset if stale."""
    check_key(x_api_key, need="upload")
    path = os.path.join(BUFFER_DIR, f"{fid}.up")
    if not os.path.exists(path):
        raise HTTPException(404, "upload session not found")
    size = os.path.getsize(path)
    if offset != size:
        raise HTTPException(409, f"offset mismatch: have {size}")
    received = 0
    with open(path, "r+b") as fh:
        fh.seek(offset)
        async for chunk in request.stream():
            fh.write(chunk)
            received += len(chunk)
    return {"offset": offset + received, "received": received}


@app.post("/v1/upload/complete/{fid}")
async def upload_complete(fid: str, background: BackgroundTasks, x_api_key: str = Header("")):
    """Finalize a chunked upload in the background; returns immediately.

    The heavy work (hash + release-asset parts) exceeds the edge timeout, so
    the client should poll GET /v1/file/{fid} for status='ready'.
    """
    check_key(x_api_key, need="upload")
    path = os.path.join(BUFFER_DIR, f"{fid}.up")
    meta_path = os.path.join(BUFFER_DIR, f"{fid}.meta")
    if not os.path.exists(path) or not os.path.exists(meta_path):
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM files WHERE id = %s", (fid,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return {"id": fid, "status": row[0], "note": "already finalized"}
        raise HTTPException(404, "upload session not found")
    with open(meta_path) as fh:
        meta = json.load(fh)
    if os.path.getsize(path) != meta["total_size"]:
        raise HTTPException(400, f"incomplete upload: {os.path.getsize(path)}/{meta['total_size']}")
    background.add_task(finalize_upload, meta["name"], meta["mime"], path, None, fid, meta.get("private", False))
    return {"id": fid, "status": "processing", "note": "finalizing in background; poll /v1/file/{id}"}


@app.get("/v1/file/{file_id}")
def get_file(file_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, size, mime, url, status, expires_at, download_count, created_at, private FROM files WHERE id = %s", (file_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(404, "not found")
    return {"id": row[0], "name": row[1], "size": row[2], "mime": row[3], "url": row[4],
            "status": row[5], "expires_at": row[6].isoformat() if row[6] else None,
            "downloads": row[7], "created_at": row[8].isoformat(), "private": row[9]}


@app.get("/v1/files")
def list_files(limit: int = 50, offset: int = 0, mime: str | None = None):
    conn = db()
    cur = conn.cursor()
    if mime:
        cur.execute("SELECT id, name, size, mime, url, created_at, private FROM files WHERE mime LIKE %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (mime + "%", limit, offset))
        items = [{"id": r[0], "name": r[1], "size": r[2], "mime": r[3], "url": r[4],
                  "created_at": r[5].isoformat(), "private": r[6]} for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM files WHERE mime LIKE %s", (mime + "%",))
    else:
        cur.execute("SELECT id, name, size, mime, url, created_at, private FROM files ORDER BY created_at DESC LIMIT %s OFFSET %s", (limit, offset))
        items = [{"id": r[0], "name": r[1], "size": r[2], "mime": r[3], "url": r[4],
                  "created_at": r[5].isoformat(), "private": r[6]} for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM files")
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
        # Durable buffer lives as release-asset parts; delete them, then mark
        # the row deleted so the next carousel check-in prunes it from disk.
        release_delete_parts(parts_json, tag, repo)
        cur.execute("UPDATE files SET status = 'deleted' WHERE id = %s", (file_id,))
        conn.commit()
        cur.close()
        conn.close()
        return {"deleted": True, "note": "pool parts removed; disk copy pruned at next carousel check-in"}
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
            "bytes_release": total - git_bytes, "top_files": top,
            "repos": STORAGE_REPOS, "private_repos": PRIVATE_STORAGE_REPOS}


WORKFLOW_FILES = {
    "upload-service": "upload-service.yml",
    "verify": "verify-cycle.yml",
    "telegram": "telegram-bot.yml",
    "carousel": "carousel-node.yml",
    "prune": "prune.yml",
    "compress": "compress-workers.yml",
    "cache-backup": "cache-backup.yml",
    "supervisor": "carousel-supervisor.yml",
}
_workflows_cache = {"t": 0.0, "data": []}


@app.get("/v1/dashboard")
def dashboard():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT count(*), coalesce(sum(size), 0) FROM files WHERE status <> 'deleted'")
    fcount, fbytes = cur.fetchone()
    cur.execute("SELECT count(*) FROM files WHERE status = 'deleted'")
    deleted = cur.fetchone()[0]
    cur.execute(
        "SELECT id, name, size, store, status, url, created_at, private FROM files "
        "WHERE status <> 'deleted' ORDER BY created_at DESC LIMIT 20"
    )
    files = [
        {"id": r[0], "name": r[1], "size": r[2], "store": r[3], "status": r[4],
         "url": r[5], "created_at": r[6].isoformat() if r[6] else None, "private": r[7]}
        for r in cur.fetchall()
    ]
    cur.execute("SELECT id, type, status, attempts, target_id, updated_at FROM jobs ORDER BY id DESC LIMIT 15")
    jobs = [
        {"id": r[0], "type": r[1], "status": r[2], "attempts": r[3], "target": r[4],
         "updated_at": r[5].isoformat() if r[5] else None}
        for r in cur.fetchall()
    ]
    cur.execute("SELECT node_id, instance, tunnel_url, last_seen, sets_held, status FROM nodes ORDER BY last_seen DESC")
    nodes = [
        {"node_id": r[0], "instance": r[1], "tunnel_url": r[2],
         "last_seen": r[3].isoformat() if r[3] else None, "sets_held": r[4], "status": r[5]}
        for r in cur.fetchall()
    ]
    cur.execute("SELECT set_id, holder, holder_url, size_bytes, status, last_anchor_at FROM sets ORDER BY set_id")
    sets = [
        {"set_id": r[0], "holder": r[1], "holder_url": r[2], "size_bytes": r[3], "status": r[4],
         "last_anchor_at": r[5].isoformat() if r[5] else None}
        for r in cur.fetchall()
    ]
    cur.execute("SELECT v FROM meta WHERE k = 'tunnel_url'")
    row = cur.fetchone()
    tunnel = (row[0] or "").strip() if row else ""
    cur.close()
    conn.close()

    if time.time() - _workflows_cache["t"] > 30:
        try:
            r = _gh.get(
                f"https://api.github.com/repos/{DASHBOARD_REPO}/actions/runs",
                headers=GH,
                params={"per_page": 30},
            )
            runs = r.json().get("workflow_runs", []) if r.status_code == 200 else []
            by_wf = {}
            for run in runs:
                by_wf.setdefault(run.get("name", "?"), []).append({
                    "id": run["id"],
                    "status": run["status"],
                    "conclusion": run.get("conclusion"),
                    "created_at": run["created_at"],
                })
            _workflows_cache["data"] = [
                {"workflow": k, "runs": v[:5]} for k, v in sorted(by_wf.items())
            ]
            _workflows_cache["t"] = time.time()
        except Exception:
            pass

    return {
        "stats": {"files": fcount, "bytes_total": fbytes, "deleted": deleted,
                  "repos": STORAGE_REPOS, "private_repos": PRIVATE_STORAGE_REPOS},
        "tunnel_url": tunnel,
        "nodes": nodes,
        "sets": sets,
        "jobs": jobs,
        "files": files,
        "workflows": _workflows_cache["data"],
        "workflow_options": sorted(WORKFLOW_FILES),
        "server_uptime_s": int(time.time() - start_time),
    }


@app.post("/v1/dashboard/dispatch")
def dispatch_workflow(workflow: str, node_id: str = "", run_minutes: str = "",
                      x_api_key: str = Header("")):
    check_key(x_api_key)
    wf_file = WORKFLOW_FILES.get(workflow)
    if not wf_file:
        raise HTTPException(400, f"unknown workflow; choose from {sorted(WORKFLOW_FILES)}")
    payload = {"ref": "main"}
    if workflow == "carousel":
        payload["inputs"] = {"node_id": node_id or "carousel-04",
                             "run_minutes": run_minutes or "30"}
    r = _gh.post(
        f"https://api.github.com/repos/{DASHBOARD_REPO}/actions/workflows/{wf_file}/dispatches",
        headers=GH,
        json=payload,
    )
    if r.status_code not in (201, 204):
        raise HTTPException(r.status_code, r.text[:200])
    return {"dispatched": workflow, "file": wf_file}


start_time = time.time()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
