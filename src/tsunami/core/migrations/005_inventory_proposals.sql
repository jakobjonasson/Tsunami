-- Inventory changes Claude proposed over MCP (propose_inventory_change). Nothing in
-- changes_json touches the inventory until a person approves the proposal in the GUI;
-- a denied, expired or failed proposal changes nothing. changes_json holds each device's
-- fields as they were when proposed (the before side of the diff) and the new values.
CREATE TABLE inventory_proposals (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    customer TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    error TEXT
);
CREATE INDEX inventory_proposals_status ON inventory_proposals (status, created_at);
