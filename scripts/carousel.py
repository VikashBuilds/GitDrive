"""GridCarousel relay node — rotating hot-pool worker with direct peer handoff.

Two roles, decided at boot from the DB:

  RECEIVER  — a predecessor announced a handoff (sets.handoff_from == NODE_ID
              and handoff_to IS NULL). Claim them, listen on :8080, and let the
              sender stream the data directly to us. Zero downtime.
  HOLDER    — normal mode: claim idle sets, restore from anchor, serve, check-in
              to git/Releases every CHECKIN_MIN (the parachute), and at shift
              end START the successor and stream everything to it.

Shift timeline (run_min = 340 by default):
  t=0        boot, register, claim/restore, serve
  t=320      announce handoff -> dispatch successor (gh workflow run)
  t=322-335  successor boots, claims pending sets, sender streams ~13 GB
  t=335      sender marks done, exits gracefully
  t=340      (receiver now continues its own 340 min)

Concurrency cost: 1 job normally, 2 during the ~20 min overlap.
10 staggered chains -> ~10-11 concurrent jobs, always under the 20 cap.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import psycopg2
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

DB_URL = os.environ["DB_URL"]
GH_TOKEN = os.environ["GH_TOKEN"]
STORAGE_REPO = os.environ["STORAGE_REPO"]
NODE_ID = os.environ["NODE_ID"]
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "")
DISK_BUDGET = int(os.environ.get("DISK_BUDGET_MB", "13000"))
CHECKIN_MIN = int(os.environ.get("CHECKIN_MIN", "20"))
TUNNEL_URL = os.environ.get("TUNNEL_URL", "")
INSTANCE = os.environ.get("GITHUB_RUN_ID", "local")
DATA = Path(os.environ.get("CAROUSEL_DATA", "/tmp/carousel-data"))
GIT_REPO = Path("/tmp/carousel-store")
RUN_MIN = 340
HANDOFF_AT_MIN = 320          # when the sender starts the successor
HANDOFF_TIMEOUT_S = 15 * 60   # how long the sender waits for the receiver
CLAIM_TIMEOUT_S = 12 * 60     # how long the receiver waits for the first byte
CHUNK_MAX = 1900 * 1024 * 1024  # release assets cap at 2 GB — use 1.9 GB chunks

GH = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

client = httpx.Client(timeout=httpx.Timeout(3600.0, connect=30.0), follow_redirects=True)
app = FastAPI(title="GridCarousel Relay Node")


def db():
    return psycopg2.connect(DB_URL)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------- registration ----------

def register():
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO nodes (node_id, instance, tunnel_url, last_seen, sets_held, status) "
        "VALUES (%s, %s, %s, now(), 0, 'active') "
        "ON CONFLICT (node_id) DO UPDATE SET instance = EXCLUDED.instance, "
        "tunnel_url = EXCLUDED.tunnel_url, last_seen = now(), status = 'active'",
        (NODE_ID, INSTANCE, TUNNEL_URL),
    )
    cur.close()
    conn.close()
    print(f"[relay] registered {NODE_ID}/{INSTANCE} @ {TUNNEL_URL}")


def heartbeat():
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("UPDATE nodes SET last_seen = now(), status = 'active' WHERE node_id = %s", (NODE_ID,))
    cur.close()
    conn.close()


# ---------- receiver path ----------

def claim_pending_handoffs() -> list:
    """Atomically claim my chain's pending sets (handoff_from == NODE_ID)."""
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT set_id, manifest_json FROM sets "
        "WHERE handoff_from = %s AND handoff_to IS NULL "
        "ORDER BY size_bytes DESC LIMIT 20 FOR UPDATE SKIP LOCKED",
        (NODE_ID,),
    )
    rows = cur.fetchall()
    claimed = []
    for set_id, manifest in rows:
        cur.execute(
            "UPDATE sets SET handoff_to = %s, handoff_state = 'transferring', "
            "holder_url = %s WHERE set_id = %s AND handoff_to IS NULL",
            (INSTANCE, TUNNEL_URL, set_id),
        )
        claimed.append((set_id, json.loads(manifest or "[]")))
    cur.close()
    conn.close()
    if claimed:
        print(f"[relay] receiver claimed {len(claimed)} sets in handoff")
    return claimed


def wait_for_sender(set_id: str) -> bool:
    """Poll until the sender marks handoff_state = 'done' (or timeout)."""
    conn = db()
    deadline = time.time() + CLAIM_TIMEOUT_S + HANDOFF_TIMEOUT_S
    while time.time() < deadline:
        cur = conn.cursor()
        cur.execute("SELECT handoff_state FROM sets WHERE set_id = %s", (set_id,))
        row = cur.fetchone()
        cur.close()
        if row and row[0] == "done":
            conn.close()
            return True
        time.sleep(5)
    conn.close()
    return False


def finalize_received(set_id: str):
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "UPDATE sets SET status = 'held', holder = %s, holder_url = %s, "
        "handoff_from = NULL, handoff_to = NULL, handoff_state = 'none', held_at = now() "
        "WHERE set_id = %s",
        (NODE_ID, TUNNEL_URL, set_id),
    )
    cur.execute(
        "UPDATE files SET url = %s || '/v1/set/' || set_id || '/' || path "
        "WHERE set_id = %s AND store = 'pool' AND path IS NOT NULL",
        (TUNNEL_URL, set_id),
    )
    cur.close()
    conn.close()
    print(f"[relay] {set_id} now held by {NODE_ID} (pool URLs refreshed)")


# ---------- holder path ----------

def claim_sets() -> list:
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT set_id, size_bytes, manifest_json FROM sets "
        "WHERE status = 'idle' ORDER BY size_bytes DESC LIMIT 50"
    )
    rows = cur.fetchall()
    budget = DISK_BUDGET * 1024 * 1024
    claimed = []
    for set_id, size, manifest in rows:
        if budget - size < 0:
            continue
        budget -= size
        cur.execute(
            "UPDATE sets SET status = 'held', holder = %s, holder_url = %s, held_at = now() "
            "WHERE set_id = %s AND status = 'idle'",
            (NODE_ID, TUNNEL_URL, set_id),
        )
        claimed.append((set_id, json.loads(manifest or "[]")))
    cur.close()
    conn.close()
    print(f"[relay] {NODE_ID} claimed {len(claimed)} sets from idle pool")
    return claimed


def restore_set(set_id: str, manifest: list):
    dst = DATA / set_id
    dst.mkdir(parents=True, exist_ok=True)
    for entry in manifest:
        f = dst / entry["name"]
        if f.exists() and f.stat().st_size == entry.get("size", -1):
            continue
        if "parts" in entry:
            tmp = f.with_suffix(".tmp")
            with open(tmp, "wb") as out:
                for p in entry["parts"]:
                    r = client.get(p)
                    if r.status_code != 200:
                        print(f"[relay] restore part miss {entry['name']}: HTTP {r.status_code}")
                        break
                    out.write(r.content)
            if tmp.stat().st_size == entry.get("size", -1):
                tmp.rename(f)
            continue
        url = entry.get("url")
        if not url:
            continue
        r = client.get(url)
        if r.status_code != 200:
            print(f"[relay] restore miss {set_id}/{entry['name']}: HTTP {r.status_code}")
            continue
        f.write_bytes(r.content)
    print(f"[relay] restored {set_id}: {len(manifest)} files")


# ---------- parachute: git/Releases check-in ----------

def git_setup():
    if not GIT_REPO.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1",
             f"https://x-access-token:{GH_TOKEN}@github.com/{STORAGE_REPO}.git", str(GIT_REPO)],
            check=True, capture_output=True,
        )
    for cfg in (["user.name", "Carousel Bot"], ["user.email", "carousel@users.noreply.github.com"]):
        subprocess.run(["git", "config", *cfg], cwd=GIT_REPO, check=True, capture_output=True)


def git_push_paths(paths):
    if not paths:
        return
    subprocess.run(["git", "add"] + paths, cwd=GIT_REPO, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"carousel checkin {NODE_ID} {now_iso()}"],
                   cwd=GIT_REPO, capture_output=True)
    for _ in range(3):
        r = subprocess.run(["git", "push"], cwd=GIT_REPO, capture_output=True)
        if r.returncode == 0:
            return
        subprocess.run(["git", "pull", "--rebase"], cwd=GIT_REPO, capture_output=True)


def ensure_release(tag: str) -> int:
    r = client.get(f"https://api.github.com/repos/{STORAGE_REPO}/releases/tags/{tag}", headers=GH)
    if r.status_code == 200:
        return r.json()["id"]
    r2 = client.post(
        f"https://api.github.com/repos/{STORAGE_REPO}/releases", headers=GH,
        json={"tag_name": tag, "name": tag, "prerelease": True, "target_commitish": "main"},
    )
    r2.raise_for_status()
    return r2.json()["id"]


def checkin_set(set_id: str, manifest: list) -> list:
    src = DATA / set_id
    if not src.exists():
        return manifest
    new_manifest = []
    git_paths = []
    deleted_names = set()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM files WHERE set_id = %s AND store = 'pool' AND status = 'deleted'", (set_id,))
    deleted_names = {r[0] for r in cur.fetchall()}
    cur.close()
    conn.close()
    for f in src.iterdir():
        if not f.is_file():
            continue
        if f.name in deleted_names:
            f.unlink()
            print(f"[relay] pruned deleted pool file {f.name}")
            continue
        size = f.stat().st_size
        old = next((m for m in manifest if m["name"] == f.name), None)
        if old and old.get("size") == size and (old.get("url") or old.get("parts")):
            new_manifest.append(old)
            continue
        if size <= 100 * 1024 * 1024:
            rel = f"files/{set_id}/{f.name}"
            dest = GIT_REPO / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f.read_bytes())
            git_paths.append(rel)
            new_manifest.append({
                "name": f.name, "size": size,
                "url": f"https://raw.githubusercontent.com/{STORAGE_REPO}/main/{rel}",
            })
        elif size <= CHUNK_MAX:
            tag = f"carousel-{set_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H')}"
            release_id = ensure_release(tag)
            r = client.post(
                f"https://uploads.github.com/repos/{STORAGE_REPO}/releases/{release_id}/assets"
                f"?name={quote(f.name)}",
                headers={**GH, "Content-Type": "application/octet-stream"},
                content=f.read_bytes(),
            )
            r.raise_for_status()
            new_manifest.append({
                "name": f.name, "size": size,
                "url": f"https://github.com/{STORAGE_REPO}/releases/download/{tag}/{quote(f.name)}",
            })
        else:
            tag = f"carousel-{set_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H')}"
            release_id = ensure_release(tag)
            parts = []
            with open(f, "rb") as fh:
                idx = 0
                while True:
                    chunk = fh.read(CHUNK_MAX)
                    if not chunk:
                        break
                    pname = f"{f.name}.part{idx:03d}"
                    r = client.post(
                        f"https://uploads.github.com/repos/{STORAGE_REPO}/releases/{release_id}/assets"
                        f"?name={quote(pname)}",
                        headers={**GH, "Content-Type": "application/octet-stream"},
                        content=chunk,
                    )
                    r.raise_for_status()
                    parts.append(r.json()["browser_download_url"])
                    idx += 1
            new_manifest.append({"name": f.name, "size": size, "parts": parts})
            print(f"[relay] chunked {f.name} -> {len(parts)} parts ({size / (1024**3):.1f} GB)")
    git_push_paths(git_paths)
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "UPDATE sets SET manifest_json = %s, last_anchor_at = now() WHERE set_id = %s",
        (json.dumps(new_manifest), set_id),
    )
    cur.close()
    conn.close()
    print(f"[relay] checked in {set_id}: {len(new_manifest)} files")
    return new_manifest


# ---------- big-file drain (2-12 GB uploads) ----------

def drain_buffer_jobs():
    """Pull buffered big uploads into my sets.

    Buffers are durable release-asset parts (parts_json) when available;
    falls back to the legacy API-runner buffer (/v1/buffer) otherwise.
    """
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT id, target_id FROM jobs WHERE type = 'pool-store' AND status = 'queued' "
        "ORDER BY id LIMIT 2 FOR UPDATE SKIP LOCKED"
    )
    jobs = cur.fetchall()
    if not jobs:
        cur.close()
        conn.close()
        return
    cur.executemany(
        "UPDATE jobs SET status = 'processing', attempts = attempts + 1, updated_at = now() WHERE id = %s",
        [(j[0],) for j in jobs],
    )
    conn.commit()
    cur.close()

    for job_id, fid in jobs:
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT name, set_id, size, parts_json FROM files WHERE id = %s", (fid,))
            frow = cur.fetchone()
            cur.close()
            conn.close()
            if not frow:
                conn = db()
                conn.autocommit = True
                cur = conn.cursor()
                cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
                cur.close()
                conn.close()
                continue
            name, set_id, size, parts_json = frow
            tmp = Path(f"/tmp/drain-{fid}")
            got = 0
            if parts_json:
                with open(tmp, "wb") as fh:
                    for p in json.loads(parts_json):
                        with client.stream("GET", p["url"]) as r:
                            r.raise_for_status()
                            for chunk in r.iter_bytes():
                                fh.write(chunk)
                                got += len(chunk)
            else:
                conn = db()
                conn.autocommit = True
                cur = conn.cursor()
                cur.execute("SELECT v FROM meta WHERE k = 'tunnel_url'")
                row = cur.fetchone()
                cur.close()
                conn.close()
                if not row or not row[0]:
                    raise RuntimeError("no tunnel_url for legacy buffer drain")
                api_url = row[0].strip()
                with client.stream("GET", f"{api_url}/v1/buffer/{fid}",
                                   headers={"X-Relay-Token": RELAY_TOKEN}) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_bytes():
                            fh.write(chunk)
                            got += len(chunk)
            if got != size:
                raise RuntimeError(f"size mismatch: got {got} expected {size}")
            holder_url = None
            conn = db()
            cur = conn.cursor()
            cur.execute(
                "SELECT holder_url FROM sets WHERE set_id = %s AND holder_url IS NOT NULL "
                "AND holder_url <> ''",
                (set_id,),
            )
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row and row[0]:
                try:
                    with open(tmp, "rb") as fh:
                        r = client.put(
                            f"{row[0]}/v1/relay/recv/{quote(set_id)}/{quote(name)}",
                            headers={"X-Relay-Token": RELAY_TOKEN},
                            content=fh,
                        )
                    if r.status_code == 200 and r.json().get("size") == got:
                        holder_url = row[0]
                        print(f"[relay] drained {name} ({got / (1024**3):.1f} GB) "
                              f"straight to holder of {set_id}")
                except Exception as e:
                    print(f"[relay] holder transfer failed ({e}) — writing locally")
            if holder_url:
                url = f"{holder_url}/v1/set/{set_id}/{quote(name)}"
                tmp.unlink(missing_ok=True)
            else:
                dst = DATA / set_id
                dst.mkdir(parents=True, exist_ok=True)
                with open(tmp, "rb") as fh, open(dst / name, "wb") as out:
                    shutil.copyfileobj(fh, out)
                tmp.unlink(missing_ok=True)
                url = f"{TUNNEL_URL}/v1/set/{set_id}/{quote(name)}"
            conn = db()
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "UPDATE files SET url = %s, status = 'ready', path = %s WHERE id = %s",
                (url, f"{set_id}/{name}", fid),
            )
            cur.execute("UPDATE sets SET size_bytes = size_bytes + %s WHERE set_id = %s", (got, set_id))
            cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
            conn.commit()
            cur.close()
            conn.close()
            print(f"[relay] drained {name} ({got / (1024**3):.1f} GB) into {set_id}")
        except Exception as e:
            print(f"[relay] drain failed for {fid}: {e}")
            conn = db()
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "UPDATE jobs SET status = 'queued', updated_at = now() WHERE id = %s AND attempts < 3",
                (job_id,),
            )
            cur.close()
            conn.close()


# ---------- relay: peer transfer (sender side) ----------

def dispatch_successor():
    subprocess.run(
        ["gh", "workflow", "run", "GridCarousel Node",
         "--repo", f"{os.environ.get('GITHUB_REPOSITORY', '')}",
         "-f", f"node_id={NODE_ID}", "-f", f"run_minutes={RUN_MIN}"],
        capture_output=True,
    )
    print("[relay] successor dispatched")


def announce_handoff(sets):
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    for set_id, _ in sets:
        cur.execute(
            "UPDATE sets SET handoff_from = %s, handoff_state = 'pending' "
            "WHERE set_id = %s AND holder = %s",
            (NODE_ID, set_id, NODE_ID),
        )
    cur.close()
    conn.close()


def wait_for_receiver(set_id: str) -> str | None:
    """Wait until a receiver claims the set; return its tunnel URL."""
    conn = db()
    deadline = time.time() + HANDOFF_TIMEOUT_S
    while time.time() < deadline:
        cur = conn.cursor()
        cur.execute(
            "SELECT handoff_to, handoff_state FROM sets "
            "WHERE set_id = %s AND handoff_from = %s",
            (set_id, NODE_ID),
        )
        row = cur.fetchone()
        cur.close()
        if row and row[0]:
            cur2 = conn.cursor()
            cur2.execute("SELECT tunnel_url FROM nodes WHERE instance = %s", (row[0],))
            trow = cur2.fetchone()
            cur2.close()
            conn.close()
            if trow and trow[0]:
                return trow[0]
            return None
        time.sleep(5)
    conn.close()
    return None


def relay_transfer(set_id: str, target: str) -> bool:
    """Stream every file on disk to the receiver. Returns success."""
    src = DATA / set_id
    if not src.exists():
        return True
    total = 0
    try:
        ping = client.get(f"{target}/v1/relay/ping", headers={"X-Relay-Token": RELAY_TOKEN})
        ping.raise_for_status()
    except Exception as e:
        print(f"[relay] receiver not reachable: {e}")
        return False
    for f in src.iterdir():
        if not f.is_file():
            continue
        with open(f, "rb") as fh:
            r = client.put(
                f"{target}/v1/relay/recv/{quote(set_id)}/{quote(f.name)}",
                headers={"X-Relay-Token": RELAY_TOKEN},
                content=fh,
            )
        if r.status_code != 200 or r.json().get("size") != f.stat().st_size:
            print(f"[relay] transfer failed on {f.name} (HTTP {r.status_code})")
            return False
        total += f.stat().st_size
    print(f"[relay] transferred {set_id}: {total / (1024 * 1024):.0f} MB")
    return True


def complete_handoff(set_id: str):
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "UPDATE sets SET handoff_state = 'done' WHERE set_id = %s", (set_id,),
    )
    cur.close()
    conn.close()


def handoff(sets):
    if not sets:
        return
    announce_handoff(sets)
    dispatch_successor()
    ok = True
    for set_id, _ in sets:
        target = wait_for_receiver(set_id)
        if not target:
            print(f"[relay] no receiver claimed {set_id} — leaving it anchored")
            ok = False
            continue
        if not relay_transfer(set_id, target):
            print(f"[relay] transfer failed for {set_id} — parachute anchors it")
            ok = False
            continue
        complete_handoff(set_id)
        print(f"[relay] handoff done: {set_id}")
    # Release holder locks for sets we handed off; failed ones stay anchored in git
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "UPDATE sets SET status = 'idle', holder = NULL, holder_url = NULL, "
        "handoff_from = NULL WHERE holder = %s AND handoff_state = 'pending'",
        (NODE_ID,),
    )
    cur.close()
    conn.close()
    if ok:
        print("[relay] shift complete, successor is live — exiting")


# ---------- HTTP ----------

@app.put("/v1/relay/recv/{set_id}/{filename}")
async def relay_recv(set_id: str, filename: str, request: Request, x_relay_token: str = Header("")):
    if x_relay_token != RELAY_TOKEN:
        raise HTTPException(403, "bad relay token")
    dst = DATA / set_id / filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(dst, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            size += len(chunk)
    return {"ok": True, "size": size}


@app.get("/v1/relay/ping")
def relay_ping(x_relay_token: str = Header("")):
    if x_relay_token != RELAY_TOKEN:
        raise HTTPException(403, "bad relay token")
    return {"ok": True}


@app.get("/v1/set/{set_id}/{filename}")
def serve(set_id: str, filename: str):
    f = DATA / set_id / filename
    if not f.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(f)


@app.get("/v1/health")
def health():
    return {"node": NODE_ID, "instance": INSTANCE, "ok": True}


# ---------- main ----------

def main():
    global RUN_MIN, HANDOFF_AT_MIN
    if "--run-min" in sys.argv:
        RUN_MIN = int(sys.argv[sys.argv.index("--run-min") + 1])
    HANDOFF_AT_MIN = RUN_MIN - 20

    register()
    git_setup()

    thread = threading.Thread(
        target=uvicorn.run, args=(app,),
        kwargs={"host": "0.0.0.0", "port": 8080, "log_level": "warning"},
        daemon=True,
    )
    thread.start()
    print(f"[relay] {NODE_ID}/{INSTANCE} serving on :8080")

    sets = claim_pending_handoffs()
    if sets:
        print("[relay] receiver mode — waiting for sender stream")
        all_done = True
        for set_id, _ in sets:
            if not wait_for_sender(set_id):
                all_done = False
                print(f"[relay] sender never finished {set_id} — will restore from anchor")
        if all_done:
            for set_id, _ in sets:
                finalize_received(set_id)
        else:
            for set_id, _ in sets:
                conn = db()
                conn.autocommit = True
                cur = conn.cursor()
                cur.execute("UPDATE sets SET handoff_to = NULL, handoff_state = 'pending' WHERE set_id = %s", (set_id,))
                cur.close()
                conn.close()
                restore_set(set_id, dict((s, m) for s, m in sets)[set_id])
                finalize_received(set_id)
    else:
        sets = claim_sets()
        for set_id, manifest in sets:
            restore_set(set_id, manifest)

    drain_buffer_jobs()

    deadline = time.time() + RUN_MIN * 60
    handoff_at = time.time() + HANDOFF_AT_MIN * 60
    last_checkin = time.time()
    while time.time() < deadline:
        time.sleep(30)
        if time.time() - last_checkin > CHECKIN_MIN * 60:
            for set_id, manifest in sets:
                checkin_set(set_id, manifest)
            last_checkin = time.time()
            heartbeat()
        if time.time() >= handoff_at:
            print(f"[relay] handoff time reached ({HANDOFF_AT_MIN} min)")
            break

    print("[relay] final parachute check-in")
    for set_id, manifest in sets:
        checkin_set(set_id, manifest)
    handoff(sets)


if __name__ == "__main__":
    main()
