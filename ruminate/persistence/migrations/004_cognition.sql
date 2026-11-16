CREATE TABLE mutations (
    operation TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(operation, request_id)
);

CREATE TABLE claims (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES topics(id),
    statement TEXT NOT NULL,
    claim_kind TEXT NOT NULL CHECK(claim_kind IN
        ('source_assertion','model_inference','hypothesis','question')),
    origin TEXT NOT NULL CHECK(origin IN ('human','host_model','deterministic')),
    status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','archived')),
    created_at TEXT NOT NULL
);
CREATE INDEX claims_topic ON claims(topic_id, created_at);

CREATE TABLE claim_evidence (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    block_id TEXT NOT NULL REFERENCES blocks(id),
    version_id TEXT NOT NULL REFERENCES source_versions(id),
    relation TEXT NOT NULL CHECK(relation IN ('supports','challenges','mentions','unclear')),
    start_char INTEGER NOT NULL CHECK(start_char >= 0),
    end_char INTEGER NOT NULL CHECK(end_char > start_char),
    quote TEXT NOT NULL,
    verifier_status TEXT NOT NULL DEFAULT 'exact_span' CHECK(verifier_status = 'exact_span'),
    created_at TEXT NOT NULL,
    UNIQUE(claim_id, block_id, relation, start_char, end_char)
);

CREATE TABLE user_beliefs (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES topics(id),
    text TEXT NOT NULL,
    provenance TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('human','host_model')),
    status TEXT NOT NULL CHECK(status IN ('candidate','confirmed','archived')),
    user_confirmed_at TEXT,
    created_at TEXT NOT NULL,
    CHECK((status = 'confirmed' AND user_confirmed_at IS NOT NULL AND origin = 'human')
        OR (status != 'confirmed' AND user_confirmed_at IS NULL))
);
CREATE INDEX beliefs_topic ON user_beliefs(topic_id, created_at);

CREATE TABLE belief_relations (
    id TEXT PRIMARY KEY,
    belief_id TEXT NOT NULL REFERENCES user_beliefs(id),
    claim_id TEXT NOT NULL REFERENCES claims(id),
    relation TEXT NOT NULL CHECK(relation IN ('supports','challenges','mentions','unclear')),
    justification TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(belief_id, claim_id, relation)
);

CREATE TABLE review_candidates (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    topic_id TEXT NOT NULL REFERENCES topics(id),
    claim_id TEXT NOT NULL REFERENCES claims(id),
    belief_id TEXT REFERENCES user_beliefs(id),
    question TEXT NOT NULL,
    reason TEXT NOT NULL,
    priority REAL NOT NULL,
    score_breakdown_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','postponed','dismissed','resolved')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX review_due ON review_candidates(status, due_at);

CREATE TABLE review_events (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES review_candidates(id),
    action TEXT NOT NULL,
    feedback TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX review_exposure ON review_events(action, created_at);

CREATE TABLE output_projects (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES topics(id),
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN
        ('research_note','literature_review','research_proposal','product_spec','article')),
    target_audience TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
    created_at TEXT NOT NULL
);

CREATE TABLE output_revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES output_projects(id),
    version INTEGER NOT NULL CHECK(version > 0),
    markdown TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'needs_review' CHECK(review_status = 'needs_review'),
    UNIQUE(project_id, version)
);
