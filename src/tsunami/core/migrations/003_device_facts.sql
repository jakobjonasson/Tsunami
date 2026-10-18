-- What each inventory device last reported about itself (show version and friends), so the
-- device list and device page can show a model and software version without a connection.
-- Values are only ever filled in: a facts read that finds no serial keeps the known one.
CREATE TABLE device_facts (
    customer TEXT NOT NULL,
    device TEXT NOT NULL,
    platform TEXT,
    model TEXT,
    version TEXT,
    serial TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (customer, device)
);
