-- TBox schema v1. Timestamps are INTEGER epoch milliseconds (UTC) unless noted.

CREATE TABLE companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL
);

CREATE TABLE sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  address TEXT DEFAULT '',
  timezone TEXT DEFAULT 'UTC',
  created_at INTEGER NOT NULL,
  UNIQUE (company_id, name)
);

CREATE TABLE buildings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  UNIQUE (site_id, name)
);

CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  full_name TEXT DEFAULT '',
  email TEXT DEFAULT '',
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','engineer','operator')),
  company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL, -- NULL on an admin = platform-wide
  active INTEGER NOT NULL DEFAULT 1,
  must_change_password INTEGER NOT NULL DEFAULT 0,
  token_version INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  last_login INTEGER
);

CREATE TABLE user_sites (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, site_id)
);

CREATE TABLE user_dashboards (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  layout TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE gateways (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  token_hash TEXT NOT NULL UNIQUE,
  last_seen INTEGER,
  version TEXT DEFAULT '',
  host_info TEXT DEFAULT '',
  protocols TEXT DEFAULT '[]',
  buffer_depth INTEGER NOT NULL DEFAULT 0,
  clock_skew_ms INTEGER,
  config_version INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  UNIQUE (site_id, name)
);

CREATE TABLE devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  building_id INTEGER REFERENCES buildings(id) ON DELETE SET NULL,
  gateway_id INTEGER REFERENCES gateways(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  location TEXT DEFAULT '',
  manufacturer TEXT DEFAULT '',
  model TEXT DEFAULT '',
  category TEXT DEFAULT 'other',
  profile_id TEXT DEFAULT '',
  source TEXT NOT NULL DEFAULT 'gateway' CHECK (source IN ('gateway','simulated')),
  protocol TEXT NOT NULL,
  connection TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  archived INTEGER NOT NULL DEFAULT 0,
  reported_status TEXT NOT NULL DEFAULT 'unknown', -- what the gateway / simulator last said
  status TEXT NOT NULL DEFAULT 'offline',          -- effective status last evaluated by the server
  status_ts INTEGER,
  last_comm_ts INTEGER,
  last_error TEXT DEFAULT '',
  diagnostics TEXT DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX devices_name_uq ON devices(site_id, name) WHERE archived = 0;
CREATE INDEX devices_gateway ON devices(gateway_id);

CREATE TABLE device_secrets (
  device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  secret TEXT NOT NULL
);

CREATE TABLE tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  category TEXT DEFAULT '',
  unit TEXT DEFAULT '',
  table_type TEXT NOT NULL DEFAULT 'holding' CHECK (table_type IN ('coil','discrete','holding','input','node','topic')),
  address INTEGER NOT NULL DEFAULT 0,
  address_ref TEXT DEFAULT '',          -- OPC UA node id / MQTT topic+json path
  data_type TEXT NOT NULL DEFAULT 'uint16',
  byte_order TEXT NOT NULL DEFAULT 'ABCD',
  bit INTEGER,
  scale REAL NOT NULL DEFAULT 1,
  offset REAL NOT NULL DEFAULT 0,
  poll_interval_ms INTEGER,             -- NULL = device default
  access TEXT NOT NULL DEFAULT 'R' CHECK (access IN ('R','RW')),
  aggregation TEXT NOT NULL DEFAULT 'avg' CHECK (aggregation IN ('avg','counter','level')),
  log_interval_s INTEGER NOT NULL DEFAULT 60,
  disp_min REAL,
  disp_max REAL,
  lolo REAL, lo REAL, hi REAL, hihi REAL,
  deadband REAL NOT NULL DEFAULT 0,
  alarm_priority TEXT NOT NULL DEFAULT 'medium' CHECK (alarm_priority IN ('low','medium','high','critical')),
  alarm_enabled INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  sim TEXT,                             -- JSON generator params, simulated devices only
  UNIQUE (device_id, name)
);

CREATE TABLE tag_state (
  tag_id INTEGER PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE,
  value REAL,
  quality TEXT NOT NULL DEFAULT 'bad',
  ts INTEGER NOT NULL,
  received_ts INTEGER NOT NULL,
  last_logged_ts INTEGER,
  last_logged_quality TEXT
);

CREATE TABLE samples (
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  ts INTEGER NOT NULL,
  value REAL,
  quality TEXT NOT NULL,
  PRIMARY KEY (tag_id, ts)
) WITHOUT ROWID;
CREATE INDEX samples_ts ON samples(ts);

CREATE TABLE alarms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                   -- hihi | hi | lo | lolo | comm_lost
  priority TEXT NOT NULL,
  message TEXT NOT NULL,
  value REAL,
  raised_ts INTEGER NOT NULL,
  cleared_ts INTEGER,
  acked_ts INTEGER,
  acked_by TEXT,
  active INTEGER NOT NULL DEFAULT 1     -- 1 while the condition is true
);
CREATE UNIQUE INDEX alarms_active_uq ON alarms(device_id, COALESCE(tag_id, 0), kind) WHERE active = 1;
CREATE INDEX alarms_site ON alarms(site_id, raised_ts);

CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  category TEXT NOT NULL,               -- system | device | alarm | audit | security
  severity TEXT NOT NULL DEFAULT 'info',
  site_id INTEGER,
  device_id INTEGER,
  tag_id INTEGER,
  username TEXT,
  message TEXT NOT NULL,
  detail TEXT
);
CREATE INDEX events_ts ON events(ts);
CREATE INDEX events_site ON events(site_id, ts);

CREATE TABLE notification_channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('webhook','email')),
  config TEXT NOT NULL DEFAULT '{}',
  min_priority TEXT NOT NULL DEFAULT 'high',
  site_id INTEGER REFERENCES sites(id) ON DELETE CASCADE,
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE gateway_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gateway_id INTEGER NOT NULL REFERENCES gateways(id) ON DELETE CASCADE,
  device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  command TEXT NOT NULL,                -- test | reconnect
  status TEXT NOT NULL DEFAULT 'pending', -- pending | sent | done | failed | expired
  result TEXT,
  requested_by TEXT,
  created_at INTEGER NOT NULL,
  done_at INTEGER
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
