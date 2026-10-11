CREATE TABLE schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    play_path TEXT NOT NULL,
    play_hash TEXT NOT NULL,
    customer TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    prompts_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]',
    limit_json TEXT NOT NULL DEFAULT '[]',
    approved_by TEXT,
    approved_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    heartbeat_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_customer_created ON jobs(customer, created_at);

CREATE TABLE job_devices (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    device TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    render_hash TEXT,
    render_masked TEXT,
    output_redacted TEXT,
    diff_redacted TEXT,
    failed_line TEXT,
    error_redacted TEXT,
    rollback_status TEXT,
    backup_path TEXT,
    PRIMARY KEY (job_id, device)
);
CREATE INDEX idx_job_devices_job ON job_devices(job_id);

CREATE TABLE mcp_calls (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_redacted_json TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    result_bytes INTEGER NOT NULL,
    job_id TEXT
);
CREATE INDEX idx_mcp_calls_ts ON mcp_calls(ts);

CREATE TABLE compliance_results (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    device TEXT NOT NULL,
    missing_count INTEGER NOT NULL,
    extra_count INTEGER NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX idx_compliance_job ON compliance_results(job_id);

CREATE TABLE device_leases (
    customer TEXT NOT NULL,
    device TEXT NOT NULL,
    job_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    PRIMARY KEY (customer, device)
);
CREATE INDEX idx_device_leases_job ON device_leases(job_id);
