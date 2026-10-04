# fbarchive

Import Facebook/Messenger chat exports into PostgreSQL, using **new tables** in a
dedicated `fbarchive` schema (kept separate from any existing tables).

It merges **two different export formats** for the same conversation via a
participant-derived `conversation_key`, so e.g. the DYI + E2EE parts of one chat
end up in a single thread.

## The two formats

| | DYI ("Download Your Information") | E2EE ("Message storage data") |
|---|---|---|
| Source | facebook.com → Settings → Export your information | Messenger → Privacy & safety → End-to-end encrypted chats → Message storage → **Download message storage data** |
| Layout | `messages/{inbox,e2ee_cutover,...}/<thread>/message_N.json` | `<Name>_N.json` + `media/` |
| Participants | `[{"name": ...}]` | `["Name", ...]` |
| Message fields | `sender_name`, `timestamp_ms`, `content`, `type`, `photos/videos/...` | `senderName`, `timestamp`, `text`, `type`, `media[{uri}]`, `isUnsent` |
| Coverage | full history incl. pre-E2EE | only the E2EE-era messages, incl. ones DYI misses |

The **E2EE download is what fills the gap**: the newest messages of a chat often
only exist there, because the DYI export only contains history up to the point
each chat was migrated to end-to-end encryption.

## Usage

```bash
psql -d fbarchive -f schema.sql
python3 import_fb.py /path/to/export-or.zip --dsn "dbname=fbarchive user=fbarchive_app"
```

`import_fb.py` auto-detects the format per JSON file, accepts a directory **or** a
`.zip`, and is **idempotent** (deterministic message ids + `ON CONFLICT DO NOTHING`).
Re-running is safe.

## Tables (schema `fbarchive`)

- `threads(thread_path PK, conversation_key, source, title, is_group, is_still_participant, message_count, first_ts, last_ts, raw)`
  - `conversation_key` = sorted lowercased participant names; used to merge formats
  - `source` = `dyi`, `e2ee`, or `dyi+e2ee`
- `participants(thread_path, name)`
- `messages(id PK, thread_path, timestamp_ms, ts, sender_name, body, message_type, is_unsent, call_duration, raw)`
- `attachments(message_id, kind, uri, local_path, mime_type, creation_ms, extra)`
- `reactions(message_id, actor, reaction)`

Handled quirks: FB latin-1 mojibake (`Ã¤` → `ä`), photos/videos/audio/gifs/files/
sticker/share attachments, reactions, unsent messages, call records, multi-file
threads, and E2EE `media/` files (resolved to `attachments.local_path`).

## Example

```sql
SELECT ts, sender_name, body
FROM fbarchive.messages
WHERE thread_path IN (SELECT thread_path FROM fbarchive.threads
                      WHERE conversation_key = 'alice example|bob example')
ORDER BY ts DESC LIMIT 20;
```
