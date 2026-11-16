CREATE TABLE app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO app_metadata(key, value) VALUES ('product', 'ruminate');

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    input_ref TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'fetched', 'parsed', 'indexed', 'enriched', 'needs_attention'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error_code TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    object_id TEXT,
    created_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
