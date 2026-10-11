-- Drafts written over MCP, by content hash. Text Claude wrote is its own input, so
-- reading it back is not exact-redacted while the file is unchanged; a draft a person
-- saved or edited is redacted like any other file (design.md, Redaction).
CREATE TABLE mcp_drafts (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    written_at TEXT NOT NULL
);
