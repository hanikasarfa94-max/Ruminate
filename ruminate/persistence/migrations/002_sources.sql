CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('file', 'url')),
    canonical_uri TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    current_version_id TEXT REFERENCES source_versions(id)
);

CREATE TABLE source_versions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    blob_path TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    title TEXT NOT NULL,
    authors_json TEXT NOT NULL DEFAULT '[]',
    published_at TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('indexed', 'needs_ocr', 'needs_browser', 'needs_attention')),
    canonical_url TEXT,
    UNIQUE(source_id, sha256)
);

CREATE INDEX versions_sha256 ON source_versions(sha256);
CREATE INDEX versions_source ON source_versions(source_id, retrieved_at);

CREATE TABLE source_aliases (
    uri TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id)
);

CREATE TABLE blocks (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES source_versions(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    text TEXT NOT NULL,
    page INTEGER CHECK (page > 0),
    section TEXT,
    locator_json TEXT NOT NULL,
    parse_quality TEXT NOT NULL,
    UNIQUE(version_id, ordinal)
);

CREATE INDEX blocks_version ON blocks(version_id, ordinal);

CREATE VIRTUAL TABLE block_fts USING fts5(block_id UNINDEXED, indexed_text, tokenize='unicode61');

CREATE TABLE topics (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE source_topics (
    source_id TEXT NOT NULL REFERENCES sources(id),
    topic_id TEXT NOT NULL REFERENCES topics(id),
    PRIMARY KEY(source_id, topic_id)
);

CREATE TABLE job_results (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    version_id TEXT NOT NULL REFERENCES source_versions(id)
);

CREATE TABLE job_inputs (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    topic TEXT,
    snapshot_json TEXT,
    lease_token TEXT,
    lease_expires_at REAL
);
