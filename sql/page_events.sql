-- page_events — self-hosted, anonymous web analytics for foam-agent.com.
-- Run in Supabase SQL Editor to apply.
--
-- Why not a third-party tool (2026-09-22): most users reach the site from
-- mainland China (100 accounts on qq.com, 45 on 163.com), where GA4 and
-- Vercel Analytics collect almost nothing. This keeps the data in the project's
-- own Postgres, reachable from everywhere the app itself is reachable, and
-- joinable with auth.users.
--
-- What it stores: a random per-tab session id, the in-app page, the referring
-- ORIGIN only, and two timestamps. No IP address, no user agent, no URL query
-- strings. Dwell time = last_seen_at - started_at, driven by a 15s heartbeat
-- that only fires while the tab is visible.

CREATE TABLE IF NOT EXISTS public.page_events (
  session_id   uuid        NOT NULL,      -- random, sessionStorage, per tab
  path         text        NOT NULL,      -- in-app page: main / guide / privacy / invite
  user_id      uuid,                      -- set once the visitor signs in
  referrer     text,                      -- origin only, e.g. https://www.google.com
  started_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, path)
);

CREATE INDEX IF NOT EXISTS page_events_started_at_idx ON public.page_events (started_at);
CREATE INDEX IF NOT EXISTS page_events_user_id_idx    ON public.page_events (user_id);

-- The table takes no direct traffic: RLS on with no policy denies anon and
-- authenticated outright, and every write goes through the function below.
ALTER TABLE public.page_events ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.track_page_view(
  p_session  uuid,
  p_path     text,
  p_referrer text DEFAULT NULL
) RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public
AS $$
BEGIN
  INSERT INTO public.page_events (session_id, path, user_id, referrer)
  VALUES (p_session, left(p_path, 120), auth.uid(), left(nullif(p_referrer, ''), 200))
  ON CONFLICT (session_id, path) DO UPDATE
    SET last_seen_at = now(),
        -- a visit that starts anonymous and then signs in keeps its identity
        user_id = COALESCE(public.page_events.user_id, auth.uid());
END;
$$;

GRANT EXECUTE ON FUNCTION public.track_page_view(uuid, text, text) TO anon, authenticated;

COMMENT ON TABLE public.page_events IS
  'Anonymous visit/dwell tracking written only via track_page_view(). No IP, no user agent. See cfdqanda-client/src/analytics.js and docs/active/usage-metrics.md.';
