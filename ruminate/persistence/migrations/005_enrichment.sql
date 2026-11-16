CREATE TABLE enrichment_inputs (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    request_id TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL REFERENCES sources(id),
    version_id TEXT NOT NULL REFERENCES source_versions(id),
    provider TEXT NOT NULL,
    input_json TEXT NOT NULL,
    result_json TEXT,
    lease_token TEXT,
    lease_expires_at REAL
);
