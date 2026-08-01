"""GitDrive Telegram bot — upload files & query storage status.

- /start /help        usage
- /stats              storage totals + top files
- /files [limit]      recent files
- /file <id>          one file's details
- /delete <id>        delete a file (requires API key)
- /set <api-url>      override API base URL (persisted in DB)
- /key <key>          set API key for uploads (persisted in DB)
- sending any document uploads it to GitDrive and replies with the URL

Run: python scripts/telegram_bot.py   (env: TELEGRAM_BOT_TOKEN, DB_URL)
"""

import os
import sys
import time

import httpx
import psycopg2

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DB_URL = os.environ.get("DB_URL", "")
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "50"))
API = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(600.0, connect=30.0)
client = httpx.Client(timeout=TIMEOUT)


def db():
    return psycopg2.connect(DB_URL)


def tg(method: str, **params):
    r = client.post(f"{API}/bot{TOKEN}/{method}", json=params, timeout=httpx.Timeout(600.0, connect=30.0))
    r.raise_for_status()
    return r.json().get("result")


def send(chat_id: int, text: str):
    try:
        tg("sendMessage", chat_id=chat_id, text=text[:4000])
    except Exception as e:
        print(f"[bot] send failed: {e}")


def current_api_url() -> str:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k = 'api_base_url'")
    row = cur.fetchone()
    if row and row[0]:
        url = row[0]
    else:
        cur.execute("SELECT v FROM meta WHERE k = 'tunnel_url'")
        row = cur.fetchone()
        url = (row[0] or "").strip() if row else ""
    cur.close()
    conn.close()
    return url


def set_meta(k: str, v: str):
    conn = db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO meta (k, v) VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = now()",
        (k, v),
    )
    cur.close()
    conn.close()


def api_key() -> str:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k = 'telegram_api_key'")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and row[0]:
        return row[0]
    return os.environ.get("GITDRIVE_API_KEYS", "").split(",")[0].strip()


def api_call(method: str, path: str, **kwargs):
    base = current_api_url()
    if not base:
        raise RuntimeError("no API URL — set one with /set <url> (or wait for the tunnel to publish)")
    headers = dict(kwargs.pop("headers", {}) or {})
    if "auth" not in kwargs:
        headers["X-API-Key"] = api_key()
    r = client.request(method, f"{base}{path}", headers=headers, **kwargs)
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"API auth/rate error {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    return r.json()


def handle_command(chat_id: int, text: str) -> str:
    parts = text.split()
    cmd = parts[0].lower()
    if cmd in ("/start", "/help"):
        return ("GitDrive bot: send any document to upload it.\n"
                "/stats  /files [n]  /file <id>  /delete <id>  /set <url>  /key <key>")
    if cmd == "/stats":
        return str(api_call("GET", "/v1/stats"))
    if cmd == "/files":
        limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
        data = api_call("GET", f"/v1/files?limit={min(limit, 25)}")
        if not data["items"]:
            return "no files yet"
        return "\n".join(f"{i['id']}  {i['name']}  {i['size']/1024:.0f} KB  {i['url'][:60] or '(pending)'}"
                         for i in data["items"]) + f"\n-- {data['total']} total"
    if cmd == "/file":
        if len(parts) < 2:
            return "usage: /file <id>"
        return str(api_call("GET", f"/v1/file/{parts[1]}"))
    if cmd == "/delete":
        if len(parts) < 2:
            return "usage: /delete <id>"
        return str(api_call("DELETE", f"/v1/file/{parts[1]}"))
    if cmd == "/set":
        if len(parts) < 2:
            return "usage: /set <api-base-url>"
        set_meta("api_base_url", parts[1].strip().rstrip("/"))
        return f"API base set to {parts[1]}"
    if cmd == "/key":
        if len(parts) < 2:
            return "usage: /key <api-key>"
        set_meta("telegram_api_key", parts[1].strip())
        return "API key saved"
    return "unknown command — /help"


def handle_document(chat_id: int, msg: dict) -> str:
    doc = msg.get("document") or {}
    file_id = doc.get("file_id")
    name = doc.get("file_name") or "file.bin"
    if not file_id:
        return "no file_id in this message"
    info = tg("getFile", file_id=file_id)
    if not info or not info.get("file_path"):
        return f"could not fetch file (size over Telegram API limit?): {info}"
    dl = client.get(f"{API}/file/bot{TOKEN}/{info['file_path']}", follow_redirects=True)
    dl.raise_for_status()
    size = len(dl.content)
    if size > 100 * 1024 * 1024:
        return f"file too big for the API path ({size/1024/1024:.0f} MB > 100 MB)"
    with open(f"/tmp/tg-{chat_id}.bin", "wb") as fh:
        fh.write(dl.content)
    try:
        base = current_api_url()
        if not base:
            raise RuntimeError("no API URL — use /set <url>")
        with open(f"/tmp/tg-{chat_id}.bin", "rb") as fh:
            r = client.post(
                f"{base}/v1/upload",
                headers={"X-API-Key": api_key()},
                files={"file": (name, fh, "application/octet-stream")},
                timeout=httpx.Timeout(1800.0, connect=30.0),
            )
        r.raise_for_status()
        res = r.json()
        return (f"Uploaded: {res.get('name')}\nid={res.get('id')} size={res.get('size', 0)/1024/1024:.1f} MB\n"
                f"deduped={res.get('deduped')} status={res.get('status')}\n"
                f"url={res.get('url') or '(queued)'}")
    except Exception as e:
        return f"upload failed: {e}"
    finally:
        try:
            os.remove(f"/tmp/tg-{chat_id}.bin")
        except OSError:
            pass


def main():
    if not TOKEN:
        print("[bot] TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    offset = 0
    deadline = time.time() + RUN_MINUTES * 60
    while time.time() < deadline:
        try:
            updates = tg("getUpdates", offset=offset, timeout=50) or []
        except Exception as e:
            print(f"[bot] poll error: {e}")
            time.sleep(5)
            continue
        for up in updates:
            offset = up["update_id"] + 1
            msg = up.get("message") or up.get("channel_post") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            if not chat_id or msg.get("date", 0) < int(time.time()) - 120:
                continue
            text = msg.get("text") or ""
            if text.startswith("/"):
                try:
                    reply = handle_command(chat_id, text)
                except Exception as e:
                    reply = f"error: {e}"
                send(chat_id, reply)
            elif msg.get("document") or msg.get("photo"):
                try:
                    reply = handle_document(chat_id, msg)
                except Exception as e:
                    reply = f"error: {e}"
                send(chat_id, reply)
        time.sleep(1)
    print("[bot] shift over")


if __name__ == "__main__":
    main()
