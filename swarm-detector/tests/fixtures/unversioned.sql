-- Frozen schema from the original unversioned application, before repositories/migrations.
CREATE TABLE datasets (
    dataset_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE messages (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id), message_key TEXT NOT NULL,
    post_key TEXT NOT NULL, account_key TEXT, channel_key TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL, text_original TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (dataset_id, message_key)
);
CREATE INDEX idx_message_post ON messages(dataset_id, post_key);
CREATE INDEX idx_message_account ON messages(dataset_id, account_key);
CREATE TABLE reviews (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id), case_id TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX idx_review_case ON reviews(run_id, case_id, revision DESC);
CREATE VIRTUAL TABLE messages_fts USING fts5(
    dataset_id UNINDEXED, message_key UNINDEXED, text_original, tokenize='unicode61'
);
