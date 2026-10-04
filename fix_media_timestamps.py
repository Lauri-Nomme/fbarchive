#!/usr/bin/env python3
"""
Fix media file timestamps in a Facebook/Messenger export.

Facebook's exports name each media file with its timestamp only inside the JSON
(`creation_timestamp`), while the files themselves keep whatever mtime they got
when the archive was created/unzipped. This walks the message JSON, finds every
media file it references, and sets the file's mtime+atime to the time the
message was actually sent.

Handles both export formats:
  * DYI  : <root>/your_facebook_activity/messages/<folder>/<thread>/message_N.json
           media uri is relative to the export root.
  * e2ee : <dir>/<Name>_N.json + <dir>/media/<uuid>.<ext>   (uri "./media/...")

Usage:
    python3 fix_media_timestamps.py /path/to/facebook-export --dry-run
    python3 fix_media_timestamps.py /path/to/facebook-export
    python3 fix_media_timestamps.py /path/to/message-storage-export   # E2EE dir
"""
import argparse
import json
import os
import sys
from datetime import datetime

MEDIA_KEYS = ("photos", "videos", "audio_files", "gifs", "files", "media")


def find_media_files(root):
    """Yield absolute paths of every json under root."""
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.lower().endswith(".json"):
                yield os.path.join(dirpath, name)


def resolve(root, json_dir, uri):
    """Resolve a media uri to a path on disk, tolerating the different prefixes."""
    if not uri or "://" in uri:
        return None
    clean = uri.lstrip("./")
    candidates = [
        os.path.join(json_dir, clean),                       # e2ee: ./media/x -> <dir>/media/x
        os.path.join(root, clean),                            # DYI: your_facebook_activity/...
        os.path.join(root, "your_facebook_activity", clean),  # DYI if uri omits the prefix
        os.path.join(os.path.dirname(json_dir), clean),       # fallback
    ]
    for c in candidates:
        if os.path.isfile(c):
            return os.path.normpath(c)
    return None


def collect(root, prefer_create):
    """Return {abspath: (epoch_seconds, source_note)} of desired timestamps."""
    wanted, missing, refs = {}, 0, 0
    for jf in find_media_files(root):
        try:
            data = json.load(open(jf, encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or "messages" not in data:
            continue
        jdir = os.path.dirname(jf)
        for msg in data.get("messages") or []:
            msg_ms = msg.get("timestamp_ms", msg.get("timestamp"))
            msg_ts = msg_ms / 1000.0 if msg_ms else None
            for key in MEDIA_KEYS:
                for att in msg.get(key) or []:
                    if not isinstance(att, dict):
                        continue
                    uri = att.get("uri")
                    create = att.get("creation_timestamp")
                    ts = None
                    if prefer_create and create:
                        ts = float(create)
                    elif msg_ts is not None:
                        ts = msg_ts
                    elif create:
                        ts = float(create)
                    if ts is None:
                        continue
                    for u in filter(None, (uri, (att.get("thumbnail") or {}).get("uri") if isinstance(att.get("thumbnail"), dict) else None)):
                        path = resolve(root, jdir, u)
                        refs += 1
                        if path is None:
                            missing += 1
                            continue
                        # keep the earliest send time for a file referenced more than once
                        if path not in wanted or ts < wanted[path][0]:
                            wanted[path] = (ts, f"msg {datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}")
    return wanted, missing, refs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="Export root (or the e2ee dir)")
    ap.add_argument("--dry-run", action="store_true", help="Only show what would change")
    ap.add_argument("--create-ts", action="store_true",
                    help="Prefer the attachment creation_timestamp over the message timestamp")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    wanted, missing, refs = collect(root, args.create_ts)
    print(f"references: {refs} | media files: {len(wanted)} | unresolved: {missing}", file=sys.stderr)

    changed = skipped = 0
    for path, (ts, note) in sorted(wanted.items(), key=lambda kv: kv[1][0]):
        try:
            st = os.stat(path)
        except OSError:
            missing += 1
            continue
        if abs(st.st_mtime - ts) < 1:
            skipped += 1
            continue
        if args.verbose or args.dry_run:
            print(f"{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M:%S}  {note:<22}  {path}")
        if not args.dry_run:
            os.utime(path, (ts, ts))
        changed += 1

    verb = "would change" if args.dry_run else "changed"
    print(f"Done: {verb} {changed}, already correct {skipped}, missing {missing}.", file=sys.stderr)


if __name__ == "__main__":
    main()
