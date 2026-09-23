-- simulation_archive — permanent, non-identifying record of every simulation.
-- Run in Supabase SQL Editor to apply (same convention as
-- list_invitation_codes_with_replenish.sql).
--
-- Why this exists (2026-09-22):
--   worker.py expires simulations by TTL — failed/cancelled after 30 days,
--   completed after 90, hard-deleted 7 days later. By 2026-09-22 that had
--   removed ~90% of all rows ever created (766 tasks ever, 76 left in the
--   table), so any question about historical usage could no longer be
--   answered from the database. Supabase's own backups do not help: Pro
--   keeps 7 days, and the lost rows are months old.
--
-- What it keeps: one small row per task, written just before the hard delete.
-- Deliberately NO prompt text and NO result payload — only derived shape
-- (length, language, domain tags). That keeps the user-data retention policy
-- intact (prompts and results still disappear on schedule) while usage
-- statistics survive. Storage cost is a few dozen bytes per task.

CREATE TABLE IF NOT EXISTS public.simulation_archive (
  id              bigint PRIMARY KEY,          -- original simulations.id
  user_id         uuid,                        -- FK intentionally omitted: rows
                                               -- must survive account deletion
  created_at      timestamptz,
  finished_at     timestamptz,
  status          text,
  error_category  text,
  model_provider  text,
  model_version   text,
  pipeline_mode   text,
  timeout_minutes smallint,
  prompt_len      integer,
  prompt_lang     text,                        -- 'zh' | 'en'
  domain_tags     text[],                      -- see taxonomy.py
  is_platform_test boolean DEFAULT false,
  files_uploaded  integer,
  bytes_uploaded  bigint,
  user_rating     smallint,
  archived_at     timestamptz DEFAULT now(),
  archive_source  text DEFAULT 'purge'         -- 'purge' | 'backfill_db' | 'backfill_storage'
);

CREATE INDEX IF NOT EXISTS simulation_archive_created_at_idx ON public.simulation_archive (created_at);
CREATE INDEX IF NOT EXISTS simulation_archive_user_id_idx    ON public.simulation_archive (user_id);

-- Statistics only: no end user ever reads this table. RLS on with no policy
-- means anon/authenticated get nothing; the service_role key bypasses RLS.
ALTER TABLE public.simulation_archive ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.simulation_archive IS
  'Permanent per-task usage record written before TTL hard-delete. No prompt text, no results. See cfdqanda-server/taxonomy.py and docs/active/usage-metrics.md.';
