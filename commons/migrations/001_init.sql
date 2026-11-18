-- The New Commons: initial schema (portable SQL; written for SQLite, PostgreSQL-compatible
-- apart from AUTOINCREMENT on audit_log.seq, see docs/ADR-001).

CREATE TABLE humans (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  email TEXT UNIQUE,
  password_hash TEXT NOT NULL,
  telegram_user_id TEXT UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE human_sessions (
  token_hash TEXT PRIMARY KEY,
  human_id TEXT NOT NULL REFERENCES humans(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);

-- connection_type distinguishes the four capabilities that must never be presented as equivalent.
CREATE TABLE agents (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES humans(id),
  display_name TEXT NOT NULL,
  provider TEXT NOT NULL,
  connection_type TEXT NOT NULL CHECK (connection_type IN
     ('native_assistant','standalone_api_agent','coding_agent','autonomous_activation','simulated')),
  is_simulated INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  description TEXT,
  created_at TEXT NOT NULL
);

-- One table for every agent credential: static API tokens and OAuth-issued tokens.
CREATE TABLE credentials (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  kind TEXT NOT NULL CHECK (kind IN ('static','oauth_access','oauth_refresh')),
  oauth_client_id TEXT,
  scopes TEXT NOT NULL DEFAULT '[]',
  label TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT,
  last_used_at TEXT
);

CREATE TABLE oauth_clients (
  client_id TEXT PRIMARY KEY,
  info_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE oauth_pending (
  id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  params_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);

CREATE TABLE oauth_codes (
  code_hash TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  data_json TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);

CREATE TABLE rooms (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  purpose TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'private',
  owner_id TEXT NOT NULL REFERENCES humans(id),
  telegram_chat_id TEXT,
  default_max_turns INTEGER NOT NULL DEFAULT 6,
  retention_days INTEGER NOT NULL DEFAULT 90,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

CREATE TABLE room_members (
  room_id TEXT NOT NULL REFERENCES rooms(id),
  member_type TEXT NOT NULL CHECK (member_type IN ('human','agent')),
  member_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',
  added_at TEXT NOT NULL,
  removed_at TEXT,
  PRIMARY KEY (room_id, member_type, member_id)
);

-- Delegations: granted only by humans, never by message content.
CREATE TABLE grants (
  id TEXT PRIMARY KEY,
  granted_by TEXT NOT NULL REFERENCES humans(id),
  agent_id TEXT NOT NULL REFERENCES agents(id),
  room_id TEXT REFERENCES rooms(id),
  actions TEXT NOT NULL,
  info_categories TEXT NOT NULL DEFAULT '["general"]',
  allowed_targets TEXT,
  max_amount REAL NOT NULL DEFAULT 0,
  max_per_hour INTEGER NOT NULL DEFAULT 30,
  max_turns INTEGER,
  requires_approval INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT,
  revoked_by TEXT
);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(id),
  parent_task_id TEXT REFERENCES tasks(id),
  depth INTEGER NOT NULL DEFAULT 0,
  created_by_agent TEXT NOT NULL REFERENCES agents(id),
  assigned_to_agent TEXT NOT NULL REFERENCES agents(id),
  owner_id TEXT NOT NULL REFERENCES humans(id),
  title TEXT NOT NULL,
  brief TEXT NOT NULL,
  info_category TEXT NOT NULL DEFAULT 'general',
  amount REAL NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  deadline TEXT,
  max_turns INTEGER NOT NULL,
  turns_used INTEGER NOT NULL DEFAULT 0,
  result TEXT,
  grant_id TEXT REFERENCES grants(id),
  idempotency_key TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (created_by_agent, idempotency_key)
);

CREATE TABLE task_dependencies (
  task_id TEXT NOT NULL REFERENCES tasks(id),
  depends_on TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE task_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  from_state TEXT,
  to_state TEXT NOT NULL,
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  reason TEXT,
  at TEXT NOT NULL
);

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(id),
  task_id TEXT REFERENCES tasks(id),
  sender_type TEXT NOT NULL CHECK (sender_type IN ('agent','human','system')),
  sender_id TEXT NOT NULL,
  recipient_agent_id TEXT REFERENCES agents(id),
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  correlation_id TEXT,
  idempotency_key TEXT,
  injection_flags TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE (sender_id, idempotency_key)
);

-- Delivery to an inbox, to Telegram and to webhooks are separate records.
-- retrieved_at is set ONLY when the recipient agent actually reads its inbox.
CREATE TABLE deliveries (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES messages(id),
  channel TEXT NOT NULL CHECK (channel IN ('inbox','telegram','webhook')),
  recipient TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','delivered','failed','skipped')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  delivered_at TEXT,
  retrieved_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id),
  requested_by_agent TEXT REFERENCES agents(id),
  owner_id TEXT NOT NULL REFERENCES humans(id),
  kind TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','denied','expired')),
  decided_by TEXT,
  decided_at TEXT,
  decision_note TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE context_items (
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(id),
  task_id TEXT REFERENCES tasks(id),
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  info_category TEXT NOT NULL DEFAULT 'general',
  content TEXT NOT NULL,
  provenance TEXT NOT NULL,
  created_at TEXT NOT NULL,
  retain_until TEXT NOT NULL
);

CREATE TABLE event_subscriptions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  callback_url TEXT NOT NULL,
  secret TEXT NOT NULL,
  event_types TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE outbox (
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  delivery_id TEXT REFERENCES deliveries(id),
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  last_error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  outcome TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);

CREATE TABLE idempotency (
  agent_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  key TEXT NOT NULL,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (agent_id, operation, key)
);

CREATE TABLE rate_events (
  agent_id TEXT NOT NULL,
  bucket TEXT NOT NULL,
  at REAL NOT NULL
);
CREATE INDEX rate_events_idx ON rate_events(agent_id, bucket, at);

CREATE TABLE telegram_updates (
  update_id INTEGER PRIMARY KEY,
  received_at TEXT NOT NULL
);

CREATE TABLE system_flags (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_by TEXT,
  updated_at TEXT NOT NULL
);

-- Pilot evidence: every manual activation and native-vs-simulated observation.
CREATE TABLE pilot_log (
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  step TEXT NOT NULL,
  actor TEXT NOT NULL,
  manual INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX tasks_assignee_idx ON tasks(assigned_to_agent, state);
CREATE INDEX deliveries_recipient_idx ON deliveries(channel, recipient, retrieved_at);
CREATE INDEX outbox_status_idx ON outbox(status, next_attempt_at);
