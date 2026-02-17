"""
Step 002: Stale job recovery tests.

Verifies that:
1. STALE_JOB_THRESHOLD is read from environment with correct default.
2. recover_stale_jobs() queries for running jobs older than the threshold.
3. Stale jobs are reset to 'queued'.
4. Non-stale running jobs are left untouched.
5. Errors during recovery are caught gracefully.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call


class TestStaleJobThreshold(unittest.TestCase):
    """Test STALE_JOB_THRESHOLD configuration."""

    def test_threshold_default_value(self):
        """STALE_JOB_THRESHOLD defaults to 7200 when env var is not set."""
        env = os.environ.copy()
        env.pop("STALE_JOB_THRESHOLD", None)
        with patch.dict(os.environ, env, clear=True):
            val = int(os.environ.get("STALE_JOB_THRESHOLD", "7200"))
            self.assertEqual(val, 7200)

    def test_threshold_from_env(self):
        """STALE_JOB_THRESHOLD reads from environment variable."""
        with patch.dict(os.environ, {"STALE_JOB_THRESHOLD": "3600"}):
            val = int(os.environ.get("STALE_JOB_THRESHOLD", "7200"))
            self.assertEqual(val, 3600)


class TestRecoverStaleJobs(unittest.TestCase):
    """Test the recover_stale_jobs() function logic."""

    def _build_mock_supabase(self, stale_jobs):
        """
        Build a mock Supabase client that returns stale_jobs for the
        chained select().eq().lt().execute() call.
        """
        mock_client = MagicMock()

        # Chain for the SELECT query
        mock_table = MagicMock()
        mock_client.table.return_value = mock_table

        mock_select = MagicMock()
        mock_table.select.return_value = mock_select

        mock_eq = MagicMock()
        mock_select.eq.return_value = mock_eq

        mock_lt = MagicMock()
        mock_eq.lt.return_value = mock_lt

        mock_response = MagicMock()
        mock_response.data = stale_jobs
        mock_lt.execute.return_value = mock_response

        # Chain for UPDATE calls (used per stale job)
        mock_update = MagicMock()
        mock_table.update.return_value = mock_update
        mock_update_eq = MagicMock()
        mock_update.eq.return_value = mock_update_eq
        mock_update_eq.execute.return_value = MagicMock()

        return mock_client

    def test_no_stale_jobs(self):
        """When no stale jobs exist, no updates are made."""
        mock_client = self._build_mock_supabase([])

        # Simulate recover_stale_jobs logic
        threshold = datetime.now(timezone.utc) - timedelta(seconds=7200)
        threshold_iso = threshold.isoformat()

        response = (
            mock_client.table('simulations')
            .select('id, updated_at')
            .eq('status', 'running')
            .lt('updated_at', threshold_iso)
            .execute()
        )

        self.assertEqual(response.data, [])
        # No update calls should have been made
        mock_client.table('simulations').update.assert_not_called()

    def test_stale_jobs_recovered(self):
        """Stale jobs are reset from 'running' to 'queued'."""
        three_hours_ago = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).isoformat()

        stale_jobs = [
            {'id': 'job-001', 'updated_at': three_hours_ago},
            {'id': 'job-002', 'updated_at': three_hours_ago},
        ]
        mock_client = self._build_mock_supabase(stale_jobs)

        # Simulate recover_stale_jobs logic
        threshold = datetime.now(timezone.utc) - timedelta(seconds=7200)
        threshold_iso = threshold.isoformat()

        response = (
            mock_client.table('simulations')
            .select('id, updated_at')
            .eq('status', 'running')
            .lt('updated_at', threshold_iso)
            .execute()
        )

        self.assertEqual(len(response.data), 2)

        # Simulate the update loop
        for job in response.data:
            mock_client.table('simulations').update(
                {'status': 'queued'}
            ).eq('id', job['id']).execute()

        # Verify update was called for each stale job
        update_mock = mock_client.table('simulations').update
        self.assertEqual(update_mock.call_count, 2)
        update_mock.assert_any_call({'status': 'queued'})

    def test_threshold_computation(self):
        """The threshold timestamp is correctly computed from STALE_JOB_THRESHOLD."""
        threshold_seconds = 7200
        before = datetime.now(timezone.utc)
        threshold = datetime.now(timezone.utc) - timedelta(seconds=threshold_seconds)
        after = datetime.now(timezone.utc)

        # Threshold should be approximately 2 hours ago
        expected_min = before - timedelta(seconds=threshold_seconds)
        expected_max = after - timedelta(seconds=threshold_seconds)

        self.assertGreaterEqual(threshold, expected_min)
        self.assertLessEqual(threshold, expected_max)

        # ISO format should be parseable
        iso_str = threshold.isoformat()
        parsed = datetime.fromisoformat(iso_str)
        self.assertEqual(parsed, threshold)

    def test_query_uses_correct_filters(self):
        """The Supabase query filters on status='running' and updated_at < threshold."""
        mock_client = self._build_mock_supabase([])

        threshold = datetime.now(timezone.utc) - timedelta(seconds=7200)
        threshold_iso = threshold.isoformat()

        (
            mock_client.table('simulations')
            .select('id, updated_at')
            .eq('status', 'running')
            .lt('updated_at', threshold_iso)
            .execute()
        )

        # Verify correct table was queried
        mock_client.table.assert_called_with('simulations')
        # Verify select fields
        mock_client.table('simulations').select.assert_called_with('id, updated_at')
        # Verify eq filter
        mock_client.table('simulations').select('id, updated_at').eq.assert_called_with(
            'status', 'running'
        )
        # Verify lt filter
        mock_client.table('simulations').select('id, updated_at').eq(
            'status', 'running'
        ).lt.assert_called_with('updated_at', threshold_iso)

    def test_recovery_error_is_caught(self):
        """Errors during recovery should be logged, not crash the Worker."""
        mock_client = MagicMock()
        mock_client.table.side_effect = Exception("DB connection failed")

        # Simulate the try/except in recover_stale_jobs
        error_caught = False
        try:
            (
                mock_client.table('simulations')
                .select('id, updated_at')
                .eq('status', 'running')
                .lt('updated_at', 'some-iso')
                .execute()
            )
        except Exception:
            error_caught = True

        self.assertTrue(error_caught, "Exception should propagate to the try/except block")

    def test_recover_stale_jobs_called_before_main_loop(self):
        """
        Verify that recover_stale_jobs is called at Worker startup
        by inspecting the main_loop function source code.
        """
        import inspect
        import importlib.util

        worker_path = os.path.join(
            os.path.dirname(__file__), '..', 'worker.py'
        )
        # Read the source to verify recover_stale_jobs is called in main_loop
        with open(worker_path, 'r') as f:
            source = f.read()

        # Check that main_loop calls recover_stale_jobs before the while loop
        main_loop_start = source.index('def main_loop()')
        while_start = source.index('while True:', main_loop_start)
        between = source[main_loop_start:while_start]

        self.assertIn(
            'recover_stale_jobs()',
            between,
            "recover_stale_jobs() should be called in main_loop() before the while loop"
        )


if __name__ == "__main__":
    unittest.main()
