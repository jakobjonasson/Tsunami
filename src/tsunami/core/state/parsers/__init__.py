"""State table parsers, one module per platform family.

Each module has COMMANDS (table name -> the show command that fills it) and PARSERS
(table name -> a function from that command's output to rows). A table missing from
either is not available on that family, and a play that uses it there fails validation.

A parser returns a list of dicts keyed by the table's column names (state/tables.py),
with values in the words the table documents: `up`, `full`, `established`, lowercase,
the same on every family. It gets the output with `\\n` line ends, returns [] for an
empty table or a feature that is off (`% BGP not active`), and never raises for output
it does not recognize: lines that are not rows are skipped. Interface names are written
the way the platform's running config prints them (helpers.long_interface for IOS).

See design.md, State checks, and docs/research/state-tables.md for the output formats.
"""
