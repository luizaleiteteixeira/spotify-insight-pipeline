-- Dashboard database (Neon Postgres). Loaded by dashboard/load_db.py from saved pipeline outputs; no model calls.
CREATE TABLE IF NOT EXISTS run_meta (
  run_id TEXT PRIMARY KEY,
  overview JSONB,          -- rank/overview.json (code-computed counts)
  verification JSONB,      -- verify/verify_report.json
  golden JSONB,            -- evals/golden_v2/golden_v2_report.json (headline fields)
  cost JSONB,              -- cost/replay_result.json + full-run ledger totals
  provenance JSONB,        -- label_config, models, prompt versions, input checksum
  loaded_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS records (
  run_id TEXT NOT NULL,
  review_id TEXT NOT NULL,
  status TEXT NOT NULL,            -- completed | quarantined | pending
  reason TEXT,
  topic TEXT, subtopic TEXT, intent TEXT,
  severity SMALLINT, sentiment REAL, needs_review BOOLEAN,
  evidence_quote TEXT,
  review_date DATE, review_rating SMALLINT, review_likes INTEGER, app_version TEXT,
  cache_source_id TEXT,
  issue_id TEXT,
  PRIMARY KEY (run_id, review_id)
);
CREATE INDEX IF NOT EXISTS records_issue ON records (run_id, issue_id, severity DESC);
CREATE INDEX IF NOT EXISTS records_topic ON records (run_id, topic, intent);
CREATE TABLE IF NOT EXISTS issues (
  run_id TEXT NOT NULL, issue_id TEXT NOT NULL, code TEXT, topic TEXT, area TEXT, title TEXT, summary TEXT,
  rank INTEGER, complaint_count INTEGER, severity_sum INTEGER, mean_severity NUMERIC(10,6), priority_score INTEGER,
  PRIMARY KEY (run_id, issue_id)
);
CREATE TABLE IF NOT EXISTS claims (
  run_id TEXT NOT NULL, claim_id TEXT NOT NULL, issue_id TEXT, metric TEXT, value TEXT,
  PRIMARY KEY (run_id, claim_id)
);
CREATE TABLE IF NOT EXISTS recommendation (
  run_id TEXT PRIMARY KEY, title TEXT, recommendation TEXT, alternatives JSONB, memo_markdown TEXT,
  check_result JSONB, model TEXT, prompt_version TEXT, generated_at TEXT
);
CREATE TABLE IF NOT EXISTS active_run (id INTEGER PRIMARY KEY DEFAULT 1, run_id TEXT NOT NULL);
