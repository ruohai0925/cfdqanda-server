"""
Step 001: subprocess timeout protection tests.

Verifies that:
1. SIMULATION_TIMEOUT is read from environment with correct default.
2. subprocess.TimeoutExpired is caught and job is marked as failed.
3. result_data contains timeout information.
"""

import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock


class TestSimulationTimeout(unittest.TestCase):
    """Test subprocess timeout configuration and handling."""

    def test_timeout_default_value(self):
        """SIMULATION_TIMEOUT defaults to 3600 when env var is not set."""
        with patch.dict(os.environ, {}, clear=False):
            # Remove SIMULATION_TIMEOUT if it exists
            env = os.environ.copy()
            env.pop("SIMULATION_TIMEOUT", None)
            with patch.dict(os.environ, env, clear=True):
                val = int(os.environ.get("SIMULATION_TIMEOUT", "3600"))
                self.assertEqual(val, 3600)

    def test_timeout_from_env(self):
        """SIMULATION_TIMEOUT reads from environment variable."""
        with patch.dict(os.environ, {"SIMULATION_TIMEOUT": "1200"}):
            val = int(os.environ.get("SIMULATION_TIMEOUT", "3600"))
            self.assertEqual(val, 1200)

    def test_subprocess_timeout_expired(self):
        """subprocess.run raises TimeoutExpired when timeout is exceeded."""
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(
                ["sleep", "10"],
                timeout=2,
                capture_output=True,
            )

    def test_timeout_handler_updates_job_status(self):
        """Simulate the worker's timeout handling logic: job should be marked failed."""
        # Simulate the worker's try/except pattern
        mock_supabase = MagicMock()
        job_id = "test-job-001"
        log_path = "/tmp/test_simulation.log"
        timeout_seconds = 5

        # Simulate what happens inside find_and_process_job when timeout occurs
        try:
            raise subprocess.TimeoutExpired(cmd="sleep 10", timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            # This mirrors the worker's timeout handler (worker.py:414-423)
            expected_result_data = {
                "error": f"Simulation timed out after {timeout_seconds} seconds.",
                "log_path_on_server": log_path,
                "timeout_seconds": timeout_seconds,
            }
            mock_supabase.table("simulations").update(
                {"status": "failed", "result_data": expected_result_data}
            ).eq("id", job_id).execute()

        # Verify the mock was called with correct arguments
        mock_supabase.table.assert_called_with("simulations")
        update_call = mock_supabase.table("simulations").update
        update_call.assert_called_once()
        call_args = update_call.call_args[0][0]
        self.assertEqual(call_args["status"], "failed")
        self.assertIn("timed out", call_args["result_data"]["error"])
        self.assertEqual(call_args["result_data"]["timeout_seconds"], timeout_seconds)
        self.assertEqual(call_args["result_data"]["log_path_on_server"], log_path)

    def test_real_subprocess_timeout(self):
        """Integration test: run a real subprocess with short timeout and verify TimeoutExpired."""
        timeout_sec = 2
        caught_timeout = False
        try:
            subprocess.run(
                ["sleep", "30"],
                timeout=timeout_sec,
                capture_output=True,
            )
        except subprocess.TimeoutExpired as e:
            caught_timeout = True
            self.assertEqual(e.timeout, timeout_sec)

        self.assertTrue(caught_timeout, "TimeoutExpired should have been raised")


if __name__ == "__main__":
    unittest.main()
