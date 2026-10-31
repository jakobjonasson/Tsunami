-- Show-allowlist additions Claude asked for over MCP (request_allowlist_addition). Only a
-- person's approval in Settings changes the policy file; this table is just the queue.
CREATE TABLE allowlist_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family TEXT NOT NULL,
    command TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
);
