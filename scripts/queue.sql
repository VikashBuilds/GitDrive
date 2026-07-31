-- GitDrive schema — run against the `gitdrive` database (Aiven Postgres)

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY,
  sha TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  size BIGINT NOT NULL,
  mime TEXT,
  store TEXT NOT NULL DEFAULT 'git',          -- 'git' | 'release'
  path TEXT NOT NULL,                          -- path inside storage repo
  release_tag TEXT,                            -- only for release-stored files
  url TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ready',        -- ready | compressing | compressed
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now(),
  download_count INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
  id SERIAL PRIMARY KEY,
  type TEXT NOT NULL,                          -- 'compress'
  target_id TEXT REFERENCES files(id),
  status TEXT NOT NULL DEFAULT 'queued',       -- queued | processing | done | failed
  attempts INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_files_created ON files (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_queued ON jobs (type, status) WHERE status = 'queued';

-- Data-cycle wiring: every file belongs to a set (stable by sha hash)

ALTER TABLE files ADD COLUMN IF NOT EXISTS set_id TEXT;
ALTER TABLE files ADD COLUMN IF NOT EXISTS repo TEXT;   -- which storage repo holds it
ALTER TABLE files ADD COLUMN IF NOT EXISTS parts_json TEXT;  -- archive tier: [{url,size}]
CREATE INDEX IF NOT EXISTS idx_files_set ON files (set_id) WHERE set_id IS NOT NULL;

-- GridCarousel (rotating hot-pool protocol)

CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
  instance TEXT DEFAULT '',                  -- current VM run id (GITHUB_RUN_ID)
  tunnel_url TEXT DEFAULT '',
  last_seen TIMESTAMPTZ DEFAULT now(),
  sets_held INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sets (
  set_id TEXT PRIMARY KEY,
  size_bytes BIGINT NOT NULL DEFAULT 0,
  manifest_json TEXT,                        -- [{name, size, url}]
  status TEXT NOT NULL DEFAULT 'idle',       -- idle | held
  holder TEXT,
  holder_url TEXT,
  held_at TIMESTAMPTZ,
  last_anchor_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Relay handoff (overlapping peer transfer)
ALTER TABLE sets ADD COLUMN IF NOT EXISTS handoff_from TEXT;      -- logical chain id that dispatched the successor
ALTER TABLE sets ADD COLUMN IF NOT EXISTS handoff_to TEXT;        -- successor instance that claimed it
ALTER TABLE sets ADD COLUMN IF NOT EXISTS handoff_state TEXT DEFAULT 'none';  -- none | pending | transferring | done

CREATE TABLE IF NOT EXISTS shifts (
  id SERIAL PRIMARY KEY,
  set_id TEXT REFERENCES sets(set_id),
  runner TEXT NOT NULL,
  started_at TIMESTAMPTZ DEFAULT now(),
  ended_at TIMESTAMPTZ,
  bytes_checked_in BIGINT NOT NULL DEFAULT 0
);
