-- Additive thinking aids. Original claims, evidence and output revisions stay intact.
CREATE TABLE reflection_guides (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    lens TEXT NOT NULL,
    spark TEXT NOT NULL,
    prompts_json TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    links_json TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin='host_model'),
    created_at TEXT NOT NULL
);
CREATE INDEX reflection_guides_claim ON reflection_guides(claim_id,created_at DESC,id);
CREATE TABLE reflection_notes (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    text TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('human','host_model')),
    created_at TEXT NOT NULL
);
CREATE INDEX reflection_notes_claim ON reflection_notes(claim_id,created_at,id);
CREATE TABLE topic_suggestions (
    id TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('source','claim')),
    target_id TEXT NOT NULL,
    version_id TEXT REFERENCES source_versions(id),
    topic_id TEXT NOT NULL REFERENCES topics(id),
    rationale TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin='host_model'),
    status TEXT NOT NULL CHECK(status IN ('pending','applied','dismissed','reverted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX topic_suggestions_target ON topic_suggestions(target_kind,target_id,topic_id);
ALTER TABLE source_topics ADD COLUMN suggestion_id TEXT REFERENCES topic_suggestions(id);
CREATE TABLE claim_topics (
    claim_id TEXT NOT NULL REFERENCES claims(id),
    topic_id TEXT NOT NULL REFERENCES topics(id),
    suggestion_id TEXT NOT NULL REFERENCES topic_suggestions(id),
    PRIMARY KEY(claim_id,topic_id)
);
