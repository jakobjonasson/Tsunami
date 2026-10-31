-- Devices Claude added to the inventory over MCP (add_devices), with the host it gave.
-- MCP connections never record a first-contact host key for such a device while its host
-- is unchanged, even with trust_new_host_keys on: a person trusts the key first, so a host
-- Claude chose never receives a login.
CREATE TABLE mcp_devices (
    customer TEXT NOT NULL,
    device TEXT NOT NULL,
    host TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (customer, device)
);
