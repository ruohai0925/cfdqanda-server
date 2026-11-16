-- storage_upload_policies — let signed-in users upload their own simulation inputs.
-- Run in Supabase SQL Editor to apply.
--
-- Why (found 2026-11-15): there is no policy on the simulation_results bucket
-- that lets an authenticated user INSERT, so every mesh upload from the web UI
-- was refused with "new row violates row-level security policy". Evidence:
--   * an authenticated upload to mesh_uploads/<uid>/x.msh is rejected by RLS,
--     even with a full GoTrue claim set, while the same JWT works on PostgREST;
--   * no task in the last 90 days has a mesh_file;
--   * a user twice described "the uploaded .msh" (jobs 779, 780) — the platform
--     received no mesh either time, and one of those runs "completed" on a mesh
--     Foam-Agent generated itself, contrary to the request.
-- The new existing-case import (case_uploads/) needs the same permission.
--
-- Scope: INSERT only, only into the caller's own folder, only for the two
-- input prefixes. Results are written by the worker with the service key, and
-- reading/deleting uploads is done server-side, so nothing else is opened up.
-- RLS policies for the same command are OR-ed, so this cannot narrow anything
-- that already works.

DROP POLICY IF EXISTS "users upload own simulation inputs" ON storage.objects;

CREATE POLICY "users upload own simulation inputs"
  ON storage.objects
  FOR INSERT
  TO authenticated
  WITH CHECK (
    bucket_id = 'simulation_results'
    AND (storage.foldername(name))[1] IN ('mesh_uploads', 'case_uploads')
    AND (storage.foldername(name))[2] = auth.uid()::text
  );
