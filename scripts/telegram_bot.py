"""GitDrive Telegram bot — friendly upload & storage manager.

Commands: /start /help /stats /files /file <id> /delete <id> /set <url> /key <key>
Buttons: main menu, file pagination, per-file details, delete confirmation.
Sending a document uploads it and replies with the link.

Run: python scripts/telegram_bot.py   (env: TELEGRAM_BOT_TOKEN, DB_URL)
"""

import os
import sys
import time
from datetime import datetime

import httpx
import psycopg2

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DB_URL = os.environ.get("DB_URL", "")
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "50"))
ALLOWED = {int(x.strip()) for x in os.environ.get("BOT_ALLOW_CHAT_ID", "").split(",") if x.strip().lstrip("-").isdigit()}
API = "https://api.telegram.org"
PAGE = 8
client = httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0))


def db():
    return psycopg2.connect(DB_URL)


def tg(method: str, **params):
    r = client.post(f"{API}/bot{TOKEN}/{method}", json=params, timeout=httpx.Timeout(600.0, connect=30.0))
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method}: {data.get('description')}")
    return data.get("result")


def send(chat_id: int, text: str, markup=None):
    try:
        tg("sendMessage", chat_id=chat_id, text=text[:4000],
           parse_mode="HTML",
           reply_markup=markup or {"remove_keyboard": True})
    except Exception as e:
        print(f"[bot] send failed: {e}")


def edit(chat_id: int, message_id: int, text: str, markup=None):
    try:
        tg("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4000],
           parse_mode="HTML",
           reply_markup=markup or {"inline_keyboard": []})
    except Exception as e:
        print(f"[bot] edit failed: {e}")


def answer(cb_id: str, text: str = ""):
    try:
        tg("answerCallbackQuery", callback_query_id=cb_id, text=text)
    except Exception:
        pass


def kb(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}


def btn(text: str, data: str):
    return {"text": text, "callback_data": data}


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


def privacy_for(chat_id: int) -> bool:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k = %s", (f"privacy_{chat_id}",))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return (row and row[0] and row[0].strip().lower() == "private") or False


def toggle_privacy(chat_id: int) -> bool:
    new = not privacy_for(chat_id)
    set_meta(f"privacy_{chat_id}", "private" if new else "public")
    return new


def api_call(method: str, path: str, **kwargs):
    base = current_api_url()
    if not base:
        raise RuntimeError("I don't know the API address yet. Wait a minute for the server, or use /set <url>")
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["X-API-Key"] = api_key()
    r = client.request(method, f"{base}{path}", headers=headers, **kwargs)
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"API auth/rate error {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    return r.json()


def fmt_bytes(n):
    if n is None:
        return "–"
    n = float(n)
    if n >= 1e9:
        return f"{n/1e9:.2f} GB"
    if n >= 1e6:
        return f"{n/1e6:.1f} MB"
    if n >= 1e3:
        return f"{n/1e3:.0f} KB"
    return f"{n:.0f} B"


def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_age(iso):
    if not iso:
        return "–"
    s = time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{int(s//60)} min ago"
    if s < 86400:
        return f"{int(s//3600)} h ago"
    return f"{int(s//86400)} d ago"


def menu_markup(chat_id: int | None = None):
    label = "🔒 Visibility: " + ("PRIVATE" if privacy_for(chat_id) else "public") if chat_id else "🔒 Visibility"
    return kb(
        (btn("📊 Status", "stats"), btn("📁 My files", "files:0")),
        (btn("📤 Upload a file", "upload"), btn("❓ Help", "help")),
        (btn(label, "privacy")),
    )


def menu_text(chat_id: int) -> str:
    vis = "🔒 PRIVATE" if privacy_for(chat_id) else "🌍 public"
    return f"What would you like to do?\n\nUploads are currently <b>{vis}</b> — /private to switch."
def welcome(chat_id: int):
    send(chat_id,
         "👋 Welcome to your GitDrive!\n\n"
         "I store your files on GitHub and give you a link to download them from anywhere.\n\n"
         "➡️ Just send me a document and I'll upload it.\n"
         "➡️ Big files (100 MB–12 GB)? I'll route them into the relay pool automatically.\n\n"
         "🔒 Uploads are " + ("<b>PRIVATE</b> — link only you can open via the dashboard." if privacy_for(chat_id)
                              else "<b>public</b> — anyone with the link can download.") +
         " Use /private to switch.",
         markup=menu_markup(chat_id))


def stats_text() -> str:
    d = api_call("GET", "/v1/stats")
    top = d.get("top_files") or []
    lines = [
        "📊 GitDrive status",
        "",
        f"🗂 Files stored: {d.get('files', 0)}",
        f"💾 Storage used: {fmt_bytes(d.get('bytes_total', 0))}",
        f"📦 In small storage (git): {fmt_bytes(d.get('bytes_git', 0))}",
        f"📦 In big storage: {fmt_bytes(d.get('bytes_release', 0))}",
    ]
    if top:
        lines.append("")
        lines.append("🏆 Most downloaded:")
        lines += [f"  • {t.get('name')} — {t.get('downloads', 0)}×" for t in top[:3]]
    return "\n".join(lines)


def files_message(page: int):
    data = api_call("GET", f"/v1/files?limit={PAGE}&offset={page*PAGE}")
    items = data.get("items") or []
    total = data.get("total", 0)
    if not items:
        return "📁 No files here yet.\n\nSend me a document to upload the first one!", menu_markup()
    pages = max(1, (total + PAGE - 1) // PAGE)
    lines = [f"📁 Your files (page {page+1} of {pages})", ""]
    rows = []
    for i, f in enumerate(items, 1):
        lock = " 🔒" if f.get("private") else ""
        lines.append(f"{i}. {esc(f.get('name'))}{lock} — {fmt_bytes(f.get('size'))} — {fmt_age(f.get('created_at'))}")
        rows.append((btn(str(i), f"file:{f.get('id')}"),))
    lines.append("")
    lines.append(f"Tap a number for details. {total} files total.")
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"files:{page-1}"))
    nav.append(btn("🏠", "menu"))
    if page < pages - 1:
        nav.append(btn("▶️", f"files:{page+1}"))
    rows.append(tuple(nav))
    return "\n".join(lines), kb(*rows)


def file_text(f) -> str:
    ready = f.get("status") == "ready"
    status = "✅ ready" if ready else ("⏳ " + (f.get("status") or "pending"))
    vis = "🔒 private" if f.get("private") else "🌍 public"
    return (
        f"📄 {esc(f.get('name'))}\n\n"
        f"Size: {fmt_bytes(f.get('size'))}\n"
        f"Visibility: {vis}\n"
        f"Status: {status}\n"
        f"Uploaded: {fmt_age(f.get('created_at'))}\n"
        f"Downloads: {f.get('downloads', 0)}\n"
        f"ID: {f.get('id')}\n"
    )


def file_markup(f):
    rows = []
    if f.get("url") and not f.get("private"):
        rows.append(({"text": "🔗 Open link", "url": f["url"]},))
    elif f.get("private"):
        rows.append((btn("ℹ️ Private file", "prvinfo"),))
    rows.append((btn("🗑 Delete", f"del:{f.get('id')}"), btn("🏠", "menu")))
    return kb(*rows)


def delete_confirm_markup(fid: str):
    return kb((btn("🗑 Yes, delete", f"delyes:{fid}"), btn("✖ No", "delno")))


def handle_callback(chat_id: int, message_id: int, data: str):
    if data == "menu":
        edit(chat_id, message_id, menu_text(chat_id), markup=menu_markup(chat_id))
        return
    if data == "privacy":
        priv = toggle_privacy(chat_id)
        vis = "🔒 PRIVATE — uploads need your API key to download (dashboard only)." if priv else \
              "🌍 public — anyone with the link can download."
        edit(chat_id, message_id, f"Visibility switched to {vis}\n\nNext upload will be "
                                  f"<b>{'PRIVATE' if priv else 'public'}</b>.", markup=menu_markup(chat_id))
        return
    if data == "prvinfo":
        edit(chat_id, message_id,
             "🔒 This file is <b>private</b>.\n\n"
             "It's stored in a private GitHub repo and can only be downloaded with your API key, "
             "from the dashboard (gitdrive.vikashbuilds.in). There is no public link — that's the point. 🙂",
             markup=kb((btn("🏠", "menu"),)))
        return
    if data == "help":
        edit(chat_id, message_id, help_text(), markup=menu_markup())
        return
    if data == "upload":
        edit(chat_id, message_id,
             "📤 To upload a file, just send it to me as a document.\n\n"
             "• Files up to 100 MB upload here directly.\n"
             "• Bigger files are stored as release parts, re-assembled on download.\n"
             "• Send a photo as a file (paperclip → document) so I keep the name.",
             markup=menu_markup())
        return
    if data == "stats":
        try:
            t = stats_text()
        except Exception as e:
            t = f"⚠️ Couldn't reach the server: {e}"
        edit(chat_id, message_id, t, markup=kb((btn("🔄 Refresh", "stats"), btn("🏠", "menu"))))
        return
    if data.startswith("files:"):
        try:
            page = int(data.split(":", 1)[1])
            text, markup = files_message(page)
            edit(chat_id, message_id, text, markup=markup)
        except Exception as e:
            edit(chat_id, message_id, f"⚠️ Couldn't list files: {e}", markup=menu_markup())
        return
    if data.startswith("file:"):
        fid = data.split(":", 1)[1]
        try:
            f = api_call("GET", f"/v1/file/{fid}")
            edit(chat_id, message_id, file_text(f), markup=file_markup(f))
        except Exception as e:
            edit(chat_id, message_id, f"⚠️ Couldn't load that file: {e}", markup=menu_markup())
        return
    if data.startswith("del:"):
        fid = data.split(":", 1)[1]
        edit(chat_id, message_id, "Are you sure? This can't be undone.", markup=delete_confirm_markup(fid))
        return
    if data.startswith("delyes:"):
        fid = data.split(":", 1)[1]
        try:
            api_call("DELETE", f"/v1/file/{fid}")
            edit(chat_id, message_id, "🗑 Deleted.", markup=menu_markup())
        except Exception as e:
            edit(chat_id, message_id, f"⚠️ Delete failed: {e}", markup=menu_markup())
        return
    if data == "delno":
        edit(chat_id, message_id, "OK, nothing deleted. 🙂", markup=menu_markup())


def handle_command(chat_id: int, text: str):
    parts = text.split()
    cmd = parts[0].lower()
    if cmd in ("/start", "/menu"):
        welcome(chat_id)
        return
    if cmd in ("/help", "/?"):
        send(chat_id, help_text(), markup=menu_markup())
        return
    if cmd == "/stats":
        try:
            send(chat_id, stats_text(), markup=kb((btn("🔄 Refresh", "stats"), btn("🏠", "menu"))))
        except Exception as e:
            send(chat_id, f"⚠️ Couldn't reach the server: {e}", markup=menu_markup())
        return
    if cmd == "/files":
        try:
            text, markup = files_message(0)
            send(chat_id, text, markup=markup)
        except Exception as e:
            send(chat_id, f"⚠️ Couldn't list files: {e}", markup=menu_markup())
        return
    if cmd == "/file":
        if len(parts) < 2:
            send(chat_id, "Usage: /file <id>\nYou can find ids in /files.", markup=menu_markup())
            return
        try:
            f = api_call("GET", f"/v1/file/{parts[1]}")
            send(chat_id, file_text(f), markup=file_markup(f))
        except Exception as e:
            send(chat_id, f"⚠️ Couldn't load that file: {e}", markup=menu_markup())
        return
    if cmd == "/delete":
        if len(parts) < 2:
            send(chat_id, "Usage: /delete <id>\nYou can find ids in /files.", markup=menu_markup())
            return
        send(chat_id, "Are you sure? This can't be undone.", markup=delete_confirm_markup(parts[1]))
        return
    if cmd == "/private":
        priv = toggle_privacy(chat_id)
        vis = "🔒 PRIVATE — next uploads go to your private repos; downloads need your API key from the dashboard." if priv else \
              "🌍 public — next uploads get public links."
        send(chat_id, f"Uploads are now <b>{vis}</b>", markup=menu_markup(chat_id))
        return
    if cmd == "/set":
        if len(parts) < 2:
            send(chat_id, "Usage: /set <api-base-url>")
            return
        set_meta("api_base_url", parts[1].strip().rstrip("/"))
        send(chat_id, "✅ API address saved.")
        return
    if cmd == "/key":
        if len(parts) < 2:
            send(chat_id, "Usage: /key <api-key>")
            return
        set_meta("telegram_api_key", parts[1].strip())
        send(chat_id, "✅ API key saved.")
        return
    send(chat_id, "Hmm, I don't know that command. Try /help 🙂", markup=menu_markup())


def help_text() -> str:
    return (
        "❓ How to use me\n\n"
        "📤 <b>Upload:</b> just send me a document.\n"
        "📁 <b>Browse:</b> press “My files”, tap a number for details, or use /files\n"
        "🗑 <b>Delete:</b> open a file and press Delete.\n"
        "📊 <b>Status:</b> press “Status” or /stats\n\n"
        "Commands:\n"
        "  /files — list your files\n"
        "  /file &lt;id&gt; — details of one file\n"
        "  /delete &lt;id&gt; — delete a file\n"
        "  /private — toggle public/private uploads\n"
        "  /set &lt;url&gt; — set API address (advanced)\n"
        "  /key &lt;key&gt; — set API key (advanced)"
    )


def handle_document(chat_id: int, msg: dict):
    doc = msg.get("document")
    if not doc:
        photo = (msg.get("photo") or [])
        if photo:
            return "📷 I got a photo — send it as a <b>file</b> (paperclip → Document) so I can keep the original name and quality."
        return "Hmm, I couldn't read that file."
    file_id = doc.get("file_id")
    name = doc.get("file_name") or "file.bin"
    size_hint = doc.get("file_size") or 0
    if size_hint > 20 * 1024 * 1024:
        return (f"⚠️ This file is <b>{fmt_bytes(size_hint)}</b>, but Telegram only lets me "
                f"receive files up to 20 MB — that's a Telegram limit, not GitDrive's.\n\n"
                f"💡 <b>Big files:</b> open the dashboard and drag & drop the file there — "
                f"it uploads up to 12 GB in chunks automatically:\n"
                f"gitdrive.vikashbuilds.in")
    info = tg("getFile", file_id=file_id)
    if not info or not info.get("file_path"):
        return f"⚠️ Couldn't fetch the file from Telegram (it may be over 20 MB). Try again with a smaller file."
    dl = client.get(f"{API}/file/bot{TOKEN}/{info['file_path']}", follow_redirects=True)
    dl.raise_for_status()
    size = len(dl.content)
    if size > 100 * 1024 * 1024:
        return f"⚠️ This file is {fmt_bytes(size)} — over the 100 MB tunnel limit. Use the chunked uploader for big files."
    with open(f"/tmp/tg-{chat_id}.bin", "wb") as fh:
        fh.write(dl.content)
    try:
        base = current_api_url()
        if not base:
            raise RuntimeError("I don't know the API address yet — wait a minute for the server, or use /set <url>")
        with open(f"/tmp/tg-{chat_id}.bin", "rb") as fh:
            r = client.post(
                f"{base}/v1/upload",
                headers={"X-API-Key": api_key()},
                files={"file": (name, fh, "application/octet-stream")},
                data={"private": "true" if privacy_for(chat_id) else "false"},
                timeout=httpx.Timeout(1800.0, connect=30.0),
            )
        r.raise_for_status()
        res = r.json()
        vis = "🔒 <b>private</b> (dashboard download only)" if res.get("private") else "🌍 <b>public</b>"
        return (
            f"✅ Uploaded <b>{esc(res.get('name'))}</b>\n\n"
            f"Size: {fmt_bytes(res.get('size') or size)}\n"
            f"Visibility: {vis}\n"
            f"Status: {res.get('status')}\n"
            f"Deduped: {'yes ✔' if res.get('deduped') else 'no'}\n"
            f"ID: {res.get('id')}\n\n"
            f"{'🔗 ' + (res.get('url') or '(link ready soon — it takes a minute)') if not res.get('private') else ''}"
        )
    except Exception as e:
        return f"⚠️ Upload failed: {e}"
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
            cb = up.get("callback_query")
            if cb:
                try:
                    cb_chat = cb["message"]["chat"]["id"]
                    if ALLOWED and cb_chat not in ALLOWED:
                        answer(cb.get("id"), "⛔ This bot is private.")
                        continue
                    handle_callback(cb_chat, cb["message"]["message_id"], cb.get("data", ""))
                except Exception as e:
                    print(f"[bot] callback error: {e}")
                answer(cb.get("id"), "")
                continue
            msg = up.get("message") or up.get("channel_post") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            if not chat_id or msg.get("date", 0) < int(time.time()) - 120:
                continue
            if ALLOWED and chat_id not in ALLOWED:
                print(f"[bot] ignoring chat {chat_id} (not on allowlist)")
                continue
            text = msg.get("text") or ""
            if text.startswith("/"):
                try:
                    handle_command(chat_id, text)
                except Exception as e:
                    send(chat_id, f"⚠️ error: {e}")
            elif msg.get("document") or msg.get("photo"):
                ack = "⏳ Uploading… (big files can take a minute)"
                if msg.get("document"):
                    ack = f"⏳ Uploading <b>{esc(msg['document'].get('file_name') or 'file')}</b>…"
                send(chat_id, ack)
                try:
                    reply = handle_document(chat_id, msg)
                except Exception as e:
                    reply = f"⚠️ error: {e}"
                send(chat_id, reply, markup=kb((btn("📁 My files", "files:0"), btn("🏠", "menu"))))
        time.sleep(1)
    print("[bot] shift over")


if __name__ == "__main__":
    main()
