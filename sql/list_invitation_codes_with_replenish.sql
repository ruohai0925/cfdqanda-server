-- list_invitation_codes_with_replenish — board RPC for the invitation page.
-- Run in Supabase SQL Editor to apply. Source of truth for this DB function
-- (the original was created directly in the dashboard and not version-controlled).
--
-- Fixes (2026-06-14):
--   1) ORDER BY: surface UNUSED codes first (was: newest-first, which returned
--      a batch of all-used codes whenever the newest batch was used up, hiding
--      hundreds of older unused codes -> board showed 0/20 despite 444 free).
--   2) Replenish: top up to `batch_size` available (was: only when 0 available),
--      so the board self-heals and never silently runs dry.
--   3) Code format: 4 segments CFDQ-XXXX-XXXX-XXXX to match existing codes.

CREATE OR REPLACE FUNCTION list_invitation_codes_with_replenish(batch_size int DEFAULT 20)
  RETURNS TABLE(code text, is_used boolean)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public
  AS $$
  DECLARE
    available_count int;
    to_generate int;
  BEGIN
    SELECT count(*) INTO available_count
    FROM invitation_codes WHERE email IS NULL;

    -- (1) Top up so there are always at least `batch_size` unclaimed codes.
    to_generate := batch_size - available_count;
    IF to_generate > 0 THEN
      FOR i IN 1..to_generate LOOP
        INSERT INTO invitation_codes (code)
        VALUES (
          'CFDQ-' ||
          upper(substr(md5(random()::text), 1, 4)) || '-' ||
          upper(substr(md5(random()::text), 1, 4)) || '-' ||
          upper(substr(md5(random()::text), 1, 4))
        );
      END LOOP;
    END IF;

    -- (2) Always return UNUSED codes first (drain oldest backlog first), so the
    --     board shows available codes even when many used codes share created_at.
    RETURN QUERY
      SELECT ic.code, (ic.email IS NOT NULL) AS is_used
      FROM invitation_codes ic
      ORDER BY (ic.email IS NOT NULL) ASC, ic.created_at ASC
      LIMIT batch_size;
  END;
  $$;

GRANT EXECUTE ON FUNCTION list_invitation_codes_with_replenish(int) TO anon;
GRANT EXECUTE ON FUNCTION list_invitation_codes_with_replenish(int) TO authenticated;
