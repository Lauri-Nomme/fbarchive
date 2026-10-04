-- fbarchive: schema for importing Facebook/Messenger data exports.
-- Handles two source formats:
--   1. "DYI"  : Download Your Information  (messages/<folder>/<thread>/message_N.json)
--   2. "e2ee" : E2EE "message storage data" download (one <Name>_N.json per chat + media/)
-- Kept separate from any pre-existing tables.
-- Safe to run repeatedly.

CREATE SCHEMA IF NOT EXISTS fbarchive;

CREATE TABLE IF NOT EXISTS fbarchive.threads (
    thread_path            text PRIMARY KEY,      -- e.g. "inbox/alice_100000000000001"
    conversation_key       text,                  -- stable key from participant set; merges formats
    source                 text,                  -- 'dyi', 'e2ee', or 'dyi+e2ee'
    title                  text,
    is_group               boolean,
    is_still_participant   boolean,
    message_count          integer NOT NULL DEFAULT 0,
    first_ts               timestamptz,
    last_ts                timestamptz,
    raw                    jsonb,
    imported_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fbarchive.participants (
    thread_path  text NOT NULL REFERENCES fbarchive.threads(thread_path) ON DELETE CASCADE,
    name         text NOT NULL,
    PRIMARY KEY (thread_path, name)
);

CREATE TABLE IF NOT EXISTS fbarchive.messages (
    id            text PRIMARY KEY,               -- deterministic hash (idempotent re-imports)
    thread_path   text NOT NULL REFERENCES fbarchive.threads(thread_path) ON DELETE CASCADE,
    timestamp_ms  bigint,
    ts            timestamptz,
    sender_name   text,
    body          text,
    message_type  text,
    is_unsent     boolean NOT NULL DEFAULT false,
    call_duration integer,
    raw           jsonb,
    imported_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_thread_ts_idx ON fbarchive.messages (thread_path, ts);
CREATE INDEX IF NOT EXISTS messages_ts_idx        ON fbarchive.messages (ts);

CREATE TABLE IF NOT EXISTS fbarchive.attachments (
    id            bigserial PRIMARY KEY,
    message_id    text NOT NULL REFERENCES fbarchive.messages(id) ON DELETE CASCADE,
    kind          text,                            -- photo|video|audio|gif|file|sticker|share|link
    uri           text,
    local_path    text,                            -- resolved path on disk, if the export was extracted
    mime_type     text,
    creation_ms   bigint,
    extra         jsonb
);
CREATE INDEX IF NOT EXISTS attachments_message_idx ON fbarchive.attachments (message_id);
-- makes attachment inserts idempotent across repeated exports (NULL-safe)
CREATE UNIQUE INDEX IF NOT EXISTS attachments_uniq
    ON fbarchive.attachments (message_id, kind, COALESCE(uri, ''), COALESCE(creation_ms, 0));

CREATE TABLE IF NOT EXISTS fbarchive.reactions (
    message_id  text NOT NULL REFERENCES fbarchive.messages(id) ON DELETE CASCADE,
    actor       text NOT NULL,
    reaction    text NOT NULL,
    PRIMARY KEY (message_id, actor, reaction)
);

-- Upgrade path for schemas created before the multi-format support.
ALTER TABLE fbarchive.threads     ADD COLUMN IF NOT EXISTS conversation_key text;
ALTER TABLE fbarchive.threads     ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE fbarchive.attachments ADD COLUMN IF NOT EXISTS local_path text;
CREATE INDEX IF NOT EXISTS threads_conversation_key_idx ON fbarchive.threads (conversation_key);

-- Backfill conversation_key for already-imported threads (idempotent).
UPDATE fbarchive.threads t
   SET conversation_key = sub.k
  FROM (
        SELECT thread_path,
               string_agg(lower(trim(name)), '|' ORDER BY lower(trim(name))) AS k
          FROM fbarchive.participants
      GROUP BY thread_path
       ) sub
 WHERE t.thread_path = sub.thread_path
   AND t.conversation_key IS NULL;
UPDATE fbarchive.threads SET source = 'dyi' WHERE source IS NULL;

-- Optional: grant access to the role that will run the importer (create it first).
-- GRANT USAGE ON SCHEMA fbarchive TO fbarchive_app;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA fbarchive TO fbarchive_app;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA fbarchive TO fbarchive_app;
