#!/usr/bin/env python3
"""
Import Facebook/Messenger chat exports into PostgreSQL (schema fbarchive).

Handles TWO source formats, auto-detected per file:

1. "DYI"  - Download Your Information export
            <root>/messages/{inbox,archived_threads,filtered_threads,message_requests,
                             e2ee_cutover}/<thread>/message_N.json
            { "participants": [{"name":...}], "messages": [
                {"sender_name","timestamp_ms","content","type","reactions",
                 "photos"/"videos"/"audio_files"/"gifs"/"files"/"sticker"/"share"} ] }

2. "e2ee" - E2EE "message storage data" download (Messenger -> Message storage -> Download)
            <dir>/<Name>_N.json  +  <dir>/media/<uuid>.<ext>
            { "participants": ["Name",...], "threadName": "Name_11", "messages": [
                {"senderName","timestamp","text","type":"text|media",
                 "media":[{"uri":"./media/x.jpeg"}],"reactions":[...],"isUnsent"} ] }

Both are merged into the same conversation via a participant-derived
`conversation_key`, so e.g. the DYI + e2ee parts of the same chat end up in one
thread. Re-importing is idempotent.

Usage:
    psql -d fbarchive -f schema.sql
    python3 import_fb.py <dir-or-zip> --dsn "dbname=fbarchive user=fbarchive_app"
"""
import argparse
import hashlib
import json
import os
import re
import sys
import zipfile

import psycopg2
from psycopg2.extras import execute_values

EXT_KIND = {
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo", ".bmp": "photo",
    ".gif": "gif",
    ".mp4": "video", ".mov": "video", ".m4v": "video", ".webm": "video",
    ".wav": "audio", ".mp3": "audio", ".m4a": "audio", ".ogg": "audio", ".aac": "audio",
    ".pdf": "file", ".docx": "file", ".doc": "file", ".mobi": "file", ".txt": "file",
    ".tcx": "file", ".torrent": "file", ".zip": "file",
}
DYI_ATTACHMENTS = {
    "photos": "photo", "videos": "video", "audio_files": "audio",
    "gifs": "gif", "files": "file",
}


def fix_mojibake(s):
    """FB JSON sometimes stores UTF-8 bytes re-decoded as latin-1 ('Ã¤' -> 'ä')."""
    if not isinstance(s, str):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def sha1(*parts):
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode("utf-8")).hexdigest()


def conversation_key(names):
    names = sorted(n.strip().lower() for n in names if n and n.strip())
    return "|".join(names) if names else None


def slugify(text):
    text = re.sub(r"_\d+$", "", text or "")
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "thread"


def kind_from_uri(uri):
    return EXT_KIND.get(os.path.splitext(uri or "")[1].lower(), "file")


def resolve_thread(cur, conv_key, default_path, title, source):
    """Return the canonical thread_path for a conversation, merging across export formats."""
    if conv_key:
        cur.execute(
            "SELECT thread_path, source FROM fbarchive.threads WHERE conversation_key = %s "
            "ORDER BY message_count DESC LIMIT 1",
            (conv_key,),
        )
        row = cur.fetchone()
        if row:
            path, cur_source = row
            tokens = {t for t in (cur_source or "").split("+") if t} | {source}
            cur.execute(
                "UPDATE fbarchive.threads SET title = COALESCE(NULLIF(title, ''), %s), source = %s "
                "WHERE thread_path = %s",
                (title, "+".join(sorted(tokens)), path),
            )
            return path
    cur.execute(
        "INSERT INTO fbarchive.threads (thread_path, conversation_key, source, title) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (thread_path) DO NOTHING",
        (default_path, conv_key, source, title),
    )
    return default_path


# --------------------------------------------------------------------------- DYI

def import_dyi(cur, thread_path, data, base_dir):
    conv_key = conversation_key([fix_mojibake(p.get("name")) for p in data.get("participants") or []])
    path = resolve_thread(cur, conv_key, thread_path, fix_mojibake(data.get("title")), "dyi")
    names = [fix_mojibake(p.get("name")) for p in data.get("participants") or []]
    names = [n for n in names if n]
    if names:
        execute_values(
            cur,
            "INSERT INTO fbarchive.participants (thread_path, name) VALUES %s ON CONFLICT DO NOTHING",
            [(path, n) for n in names],
        )

    cur.execute(
        "UPDATE fbarchive.threads SET is_still_participant = %s WHERE thread_path = %s",
        (data.get("is_still_participant"), path),
    )

    n = 0
    for msg in data.get("messages") or []:
        mid = sha1(path, msg.get("timestamp_ms"), msg.get("sender_name"), msg.get("type"), msg.get("content"))
        ts_ms = msg.get("timestamp_ms")
        cur.execute(
            """
            INSERT INTO fbarchive.messages
              (id, thread_path, timestamp_ms, ts, sender_name, body, message_type,
               is_unsent, call_duration, raw)
            VALUES (%s, %s, %s,
                    CASE WHEN %s IS NULL THEN NULL ELSE to_timestamp(%s / 1000.0) END,
                    %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (mid, path, ts_ms, ts_ms, ts_ms,
             fix_mojibake(msg.get("sender_name")), fix_mojibake(msg.get("content")),
             msg.get("type"), bool(msg.get("is_unsent", False)), msg.get("call_duration"),
             json.dumps(msg, ensure_ascii=False)),
        )
        for key, kind in DYI_ATTACHMENTS.items():
            for a in msg.get(key) or []:
                if not isinstance(a, dict):
                    continue
                uri = a.get("uri")
                cur.execute(
                    "INSERT INTO fbarchive.attachments (message_id, kind, uri, creation_ms, extra) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (mid, kind, uri, a.get("creation_timestamp"),
                     json.dumps({k: v for k, v in a.items() if k not in ("uri", "creation_timestamp")},
                                ensure_ascii=False)),
                )
        st = msg.get("sticker")
        if isinstance(st, dict):
            cur.execute(
                "INSERT INTO fbarchive.attachments (message_id, kind, uri, extra) "
                "VALUES (%s, 'sticker', %s, %s) ON CONFLICT DO NOTHING",
                (mid, st.get("uri"), json.dumps(st, ensure_ascii=False)),
            )
        sh = msg.get("share")
        if isinstance(sh, dict):
            cur.execute(
                "INSERT INTO fbarchive.attachments (message_id, kind, uri, extra) "
                "VALUES (%s, 'share', %s, %s) ON CONFLICT DO NOTHING",
                (mid, sh.get("link"), json.dumps(sh, ensure_ascii=False)),
            )
        for r in msg.get("reactions") or []:
            cur.execute(
                "INSERT INTO fbarchive.reactions (message_id, actor, reaction) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (mid, fix_mojibake(r.get("actor")), fix_mojibake(r.get("reaction"))),
            )
        n += 1
    return path, n


# --------------------------------------------------------------------------- e2ee

def import_e2ee(cur, data, base_dir):
    thread_name = data.get("threadName") or "thread"
    title = re.sub(r"_\d+$", "", fix_mojibake(thread_name))
    conv_key = conversation_key([fix_mojibake(p) for p in data.get("participants") or []])
    default_path = "e2ee/" + slugify(thread_name)
    path = resolve_thread(cur, conv_key, default_path, title, "e2ee")

    names = [fix_mojibake(p) for p in data.get("participants") or []]
    names = [n for n in names if n]
    if names:
        execute_values(
            cur,
            "INSERT INTO fbarchive.participants (thread_path, name) VALUES %s ON CONFLICT DO NOTHING",
            [(path, n) for n in names],
        )

    n = 0
    for msg in data.get("messages") or []:
        media = [m.get("uri") for m in (msg.get("media") or []) if isinstance(m, dict)]
        mid = sha1(path, msg.get("timestamp"), msg.get("senderName"), msg.get("type"),
                   msg.get("text"), ",".join(media))
        ts_ms = msg.get("timestamp")
        cur.execute(
            """
            INSERT INTO fbarchive.messages
              (id, thread_path, timestamp_ms, ts, sender_name, body, message_type,
               is_unsent, raw)
            VALUES (%s, %s, %s,
                    CASE WHEN %s IS NULL THEN NULL ELSE to_timestamp(%s / 1000.0) END,
                    %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (mid, path, ts_ms, ts_ms, ts_ms,
             fix_mojibake(msg.get("senderName")), fix_mojibake(msg.get("text")),
             msg.get("type"), bool(msg.get("isUnsent", False)),
             json.dumps(msg, ensure_ascii=False)),
        )
        for m in msg.get("media") or []:
            if not isinstance(m, dict):
                continue
            uri = m.get("uri")
            local = None
            if uri:
                local = os.path.normpath(os.path.join(base_dir, uri.lstrip("./")))
                if not os.path.exists(local):
                    local = None
            cur.execute(
                "INSERT INTO fbarchive.attachments (message_id, kind, uri, local_path, extra) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (mid, kind_from_uri(uri), uri, local, json.dumps(m, ensure_ascii=False)),
            )
        for r in msg.get("reactions") or []:
            cur.execute(
                "INSERT INTO fbarchive.reactions (message_id, actor, reaction) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (mid, fix_mojibake(r.get("actor")), fix_mojibake(r.get("reaction"))),
            )
        n += 1
    return path, n


# --------------------------------------------------------------------------- driver

def detect(data):
    if not isinstance(data, dict) or "messages" not in data:
        return None
    if "threadName" in data and data.get("participants") and isinstance(data["participants"][0], str):
        return "e2ee"
    if "participants" in data and data.get("participants") and isinstance(data["participants"][0], dict):
        return "dyi"
    if "thread_path" in data:
        return "dyi"
    return None


def ensure_extracted(path):
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        out = path[:-4] + ".extracted"
        if not os.path.isdir(out):
            os.makedirs(out, exist_ok=True)
            with zipfile.ZipFile(path) as z:
                z.extractall(out)
            print(f"extracted {path} -> {out}", file=sys.stderr)
        return out
    return path


def json_files(root):
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.lower().endswith(".json"):
                yield os.path.join(dirpath, name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="DYI root, e2ee dir, or a .zip of either")
    ap.add_argument("--dsn", default=os.environ.get("FBARCHIVE_DSN", "dbname=fbarchive"))
    args = ap.parse_args()

    root = ensure_extracted(os.path.abspath(args.path))
    conn = psycopg2.connect(args.dsn)
    conn.autocommit = False
    touched, totals = set(), {"dyi": 0, "e2ee": 0}
    try:
        with conn.cursor() as cur:
            for jf in sorted(json_files(root)):
                try:
                    data = json.load(open(jf, encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                fmt = detect(data)
                if fmt == "dyi":
                    thread_path = data.get("thread_path") or os.path.relpath(os.path.dirname(jf), root)
                    path, n = import_dyi(cur, thread_path, data, os.path.dirname(jf))
                elif fmt == "e2ee":
                    path, n = import_e2ee(cur, data, os.path.dirname(jf))
                else:
                    continue
                touched.add(path)
                totals[fmt] += n
                print(f"[{fmt}] {os.path.relpath(jf, root)} -> {path} (+{n})", file=sys.stderr)

            # recompute thread aggregates
            for path in touched:
                cur.execute(
                    """
                    UPDATE fbarchive.threads t SET
                      message_count = (SELECT count(*) FROM fbarchive.messages m WHERE m.thread_path = t.thread_path),
                      first_ts      = (SELECT min(ts) FROM fbarchive.messages m WHERE m.thread_path = t.thread_path),
                      last_ts       = (SELECT max(ts) FROM fbarchive.messages m WHERE m.thread_path = t.thread_path),
                      is_group      = (SELECT count(*) > 2 FROM fbarchive.participants p WHERE p.thread_path = t.thread_path)
                     WHERE t.thread_path = %s
                    """,
                    (path,),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"Done: dyi={totals['dyi']} e2ee={totals['e2ee']} messages, {len(touched)} thread(s).", file=sys.stderr)


if __name__ == "__main__":
    main()
