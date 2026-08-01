import argparse
import os
import subprocess
import sys
import tempfile
import uuid

import httpx
import psycopg2

DB_URL = os.environ["DB_URL"]
GH_TOKEN = os.environ["GH_TOKEN"]
STORAGE_REPO = os.environ["STORAGE_REPO"]
GH = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

MIN_SAVINGS = 0.10  # only replace if >= 10% smaller


def db():
    return psycopg2.connect(DB_URL)


def get_job(job_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT target_id FROM jobs WHERE id = %s", (job_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def get_file(file_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, mime, size, url, private FROM files WHERE id = %s", (file_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "mime": row[2], "size": row[3], "url": row[4],
            "private": row[5]}


def set_status(job_id: int, status: str):
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("UPDATE jobs SET status = %s, updated_at = now() WHERE id = %s", (status, job_id))
    cur.execute("UPDATE files SET status = 'ready' WHERE status = 'compressing'")
    cur.close()
    conn.close()


def compress_image(src: str, dst_dir: str) -> str | None:
    from PIL import Image

    img = Image.open(src).convert("RGB")
    if max(img.size) > 2048:
        img.thumbnail((2048, 2048))
    out = os.path.join(dst_dir, "out.webp")
    img.save(out, "WEBP", quality=82, method=6)
    return out


def compress_video(src: str, dst_dir: str) -> str | None:
    out = os.path.join(dst_dir, "out.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
        "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
        "-movflags", "+faststart",
        out,
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        return None
    return out


def git_store(new_bytes: bytes, rel_path: str):
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "clone", "--depth", "1", f"https://x-access-token:{GH_TOKEN}@github.com/{STORAGE_REPO}.git", tmp],
                       check=True, capture_output=True)
        dest = os.path.join(tmp, rel_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(new_bytes)
        subprocess.run(["git", "config", "user.name", "GitDrive Bot"], cwd=tmp, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "gitdrive@users.noreply.github.com"], cwd=tmp, check=True, capture_output=True)
        subprocess.run(["git", "add", rel_path], cwd=tmp, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"compress: {rel_path}"], cwd=tmp, check=True, capture_output=True)
        for attempt in range(3):
            r = subprocess.run(["git", "push"], cwd=tmp, capture_output=True)
            if r.returncode == 0:
                return
            subprocess.run(["git", "pull", "--rebase"], cwd=tmp, capture_output=True)


def update_file(file_id: str, new_size: int):
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "UPDATE files SET size = %s, status = 'compressed', mime = %s WHERE id = %s",
        (new_size, "image/webp", file_id),
    )
    cur.close()
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", type=int, required=True)
    args = ap.parse_args()

    target_id = get_job(args.job_id)
    if not target_id:
        sys.exit(0)
    f = get_file(target_id)
    if not f:
        set_status(args.job_id, "failed")
        sys.exit(1)
    if f.get("private"):
        # Private files are never re-hosted: the compressed copy would land
        # in the public storage repo and leak the content.
        set_status(args.job_id, "done")
        print(f"[compress] skipping private file {f['id']}")
        sys.exit(0)

    with httpx.Client(timeout=300) as client:
        r = client.get(f["url"])
        if r.status_code != 200:
            set_status(args.job_id, "failed")
            sys.exit(1)
        src_bytes = r.content

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.bin")
        with open(src, "wb") as fh:
            fh.write(src_bytes)
        out = None
        new_mime = None
        if (f["mime"] or "").startswith("image/") and not f["mime"].endswith("webp"):
            out = compress_image(src, tmp)
            new_mime = "image/webp"
        elif (f["mime"] or "").startswith("video/"):
            out = compress_video(src, tmp)
            new_mime = "video/mp4"
        if not out:
            set_status(args.job_id, "done")
            return
        new_size = os.path.getsize(out)
        if new_size >= f["size"] * (1 - MIN_SAVINGS):
            set_status(args.job_id, "done")
            return
        with open(out, "rb") as fh:
            new_bytes = fh.read()

    rel_path = f["url"].split("/files/", 1)[1] if "/files/" in f["url"] else None
    if not rel_path:
        set_status(args.job_id, "failed")
        sys.exit(1)
    git_store(new_bytes, rel_path)
    update_file(f["id"], new_size)
    set_status(args.job_id, "done")


if __name__ == "__main__":
    main()
