CREATE TABLE visual_observations (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES source_versions(id),
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    blob_path TEXT NOT NULL,
    page INTEGER NOT NULL CHECK (page > 0),
    locator_json TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('visual_transcription', 'visual_interpretation')),
    text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 20000),
    host TEXT NOT NULL CHECK (length(host) BETWEEN 1 AND 100),
    request_id TEXT NOT NULL UNIQUE CHECK (length(request_id) BETWEEN 1 AND 100),
    origin TEXT NOT NULL DEFAULT 'host_model' CHECK (origin = 'host_model'),
    status TEXT NOT NULL DEFAULT 'unverified' CHECK (status = 'unverified'),
    created_at TEXT NOT NULL
);

CREATE INDEX observations_version_page ON visual_observations(version_id, page, created_at);
