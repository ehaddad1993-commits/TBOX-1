-- Controlled writes: per-tag write limits and command payloads.
ALTER TABLE tags ADD COLUMN write_min REAL;
ALTER TABLE tags ADD COLUMN write_max REAL;
ALTER TABLE gateway_commands ADD COLUMN payload TEXT;
