-- Every press-to-reveal in the GUI (run preview and backups viewer): who, when, which
-- secret or backup token, and the preview or backup it was viewed in. Only the masked
-- line is kept; the revealed value is never written anywhere (core/reveal.py).
CREATE TABLE reveals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revealed_at TEXT NOT NULL,
    revealed_by TEXT NOT NULL,
    kind TEXT NOT NULL,          -- preview | backup
    customer TEXT NOT NULL,
    device TEXT,
    item TEXT NOT NULL,          -- e.g. secret('snmpv3', 'auth_key'), or backup line 12, token 1
    context TEXT NOT NULL,       -- the play previewed, or the backup file
    masked_line TEXT NOT NULL    -- the line as shown, with the value masked
);
CREATE INDEX reveals_by_time ON reveals (revealed_at);
