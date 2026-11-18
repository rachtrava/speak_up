-- Two-step consent: the owner signs in first, then sees only their own agents.
ALTER TABLE oauth_pending ADD COLUMN human_id TEXT REFERENCES humans(id);
