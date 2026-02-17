"""
Step 007: Multi-worker concurrency integration test.

This test hits the REAL Supabase database to verify that:
1. claim_next_job() RPC works end-to-end.
2. Multiple concurrent "workers" (threads) never claim the same job.
3. All queued jobs are eventually claimed (no jobs lost).
4. Each job is claimed exactly once.

Requirements:
  - .env must be configured with valid SUPABASE_URL and SUPABASE_SERVICE_KEY
  - The claim_next_job() SQL function must already exist in Supabase
  - Run with: conda activate FoamAgent && python -m pytest tests/test_step007_concurrency.py -v -s
"""

import os
import sys
import uuid
import threading
import time
import logging

import pytest

# Load .env from cfdqanda-server directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from supabase import create_client

logger = logging.getLogger(__name__)

# --- Test configuration ---
NUM_TEST_JOBS = 5
NUM_WORKERS = 3
# A unique tag so we can identify and clean up test rows
TEST_TAG = f"__test_step007_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def sb():
    """Create a Supabase client for tests."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        pytest.skip("SUPABASE_URL and SUPABASE_SERVICE_KEY required for integration test")
    return create_client(url, key)


@pytest.fixture(scope="module")
def test_user_id(sb):
    """Fetch a real user_id from existing simulations (FK constraint requires it)."""
    resp = sb.table("simulations").select("user_id").limit(1).execute()
    if not resp.data:
        pytest.skip("No existing simulations found; need a real user_id for FK constraint")
    return resp.data[0]["user_id"]


@pytest.fixture(scope="module")
def test_job_ids(sb, test_user_id):
    """Insert NUM_TEST_JOBS queued jobs and return their IDs. Clean up after tests."""
    job_ids = []
    for i in range(NUM_TEST_JOBS):
        resp = sb.table("simulations").insert({
            "prompt": f"{TEST_TAG} concurrency test job {i}",
            "status": "queued",
            "user_id": test_user_id,
        }).execute()
        job_ids.append(resp.data[0]["id"])

    logger.info(f"Created {len(job_ids)} test jobs: {job_ids}")
    yield job_ids

    # Cleanup: delete all test rows
    for jid in job_ids:
        try:
            sb.table("simulations").delete().eq("id", jid).execute()
        except Exception as e:
            logger.warning(f"Cleanup failed for job {jid}: {e}")
    logger.info(f"Cleaned up {len(job_ids)} test jobs")


class TestClaimNextJobLive:
    """Live integration tests against real Supabase."""

    def test_rpc_returns_one_job(self, sb, test_job_ids):
        """A single claim_next_job() call returns exactly one job."""
        resp = sb.rpc("claim_next_job").execute()
        assert resp.data, "claim_next_job should return a job when queued jobs exist"
        assert len(resp.data) == 1, "claim_next_job should return exactly one job"

        claimed = resp.data[0]
        assert claimed["id"] in test_job_ids, "Claimed job should be one of our test jobs"
        assert claimed["status"] == "running", "Claimed job status should be 'running'"

        # Reset it back to queued for subsequent tests
        sb.table("simulations").update({"status": "queued"}).eq("id", claimed["id"]).execute()

    def test_rpc_returns_empty_when_no_queued(self, sb, test_job_ids):
        """claim_next_job() returns empty when no queued jobs exist."""
        # Set all test jobs to 'running' temporarily
        for jid in test_job_ids:
            sb.table("simulations").update({"status": "running"}).eq("id", jid).execute()

        resp = sb.rpc("claim_next_job").execute()
        # May return empty or return a non-test job if other queued jobs exist
        # We just verify it doesn't crash
        if resp.data:
            # If it returned something, it should NOT be one of our test jobs
            # (since they're all 'running')
            claimed_ids = {r["id"] for r in resp.data}
            for jid in test_job_ids:
                if jid in claimed_ids:
                    pytest.fail("claim_next_job should not return a 'running' job")

        # Restore all to queued
        for jid in test_job_ids:
            sb.table("simulations").update({"status": "queued"}).eq("id", jid).execute()

    def test_concurrent_workers_no_duplicate_claims(self, sb, test_job_ids):
        """
        Core test: N workers race to claim M jobs concurrently.
        Each job must be claimed by exactly one worker.
        """
        # Ensure all test jobs are queued
        for jid in test_job_ids:
            sb.table("simulations").update({"status": "queued"}).eq("id", jid).execute()

        # Each worker will repeatedly call claim_next_job until no jobs remain
        claimed_by = {}  # {job_id: worker_id}
        lock = threading.Lock()
        errors = []

        def worker_loop(worker_id):
            """Simulate a worker claiming jobs in a loop."""
            # Each worker gets its own Supabase client to simulate separate processes
            url = os.environ.get("SUPABASE_URL")
            key = os.environ.get("SUPABASE_SERVICE_KEY")
            client = create_client(url, key)

            while True:
                try:
                    resp = client.rpc("claim_next_job").execute()
                    if not resp.data:
                        break  # No more queued jobs

                    job = resp.data[0]
                    job_id = job["id"]

                    with lock:
                        if job_id in claimed_by:
                            errors.append(
                                f"DUPLICATE! Job {job_id} claimed by worker {worker_id} "
                                f"but already claimed by worker {claimed_by[job_id]}"
                            )
                        claimed_by[job_id] = worker_id

                    logger.info(f"Worker {worker_id} claimed job {job_id}")

                except Exception as e:
                    with lock:
                        errors.append(f"Worker {worker_id} error: {e}")
                    break

        # Launch workers concurrently
        threads = []
        for i in range(NUM_WORKERS):
            t = threading.Thread(target=worker_loop, args=(i,), name=f"Worker-{i}")
            threads.append(t)

        # Start all threads as close together as possible
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # --- Assertions ---
        assert not errors, f"Concurrency errors detected:\n" + "\n".join(errors)

        # Check that our test jobs were each claimed at most once
        test_claimed = {jid: wid for jid, wid in claimed_by.items() if jid in test_job_ids}
        logger.info(f"Claims: {test_claimed}")

        # Each test job should appear exactly once
        for jid in test_job_ids:
            count = list(claimed_by.keys()).count(jid)
            assert count <= 1, f"Job {jid} was claimed {count} times (should be <= 1)"

        # All test jobs should have been claimed
        for jid in test_job_ids:
            assert jid in claimed_by, f"Job {jid} was never claimed"

        logger.info(
            f"SUCCESS: {NUM_WORKERS} workers claimed {len(test_claimed)} test jobs "
            f"with zero duplicates"
        )

    def test_claimed_jobs_have_running_status_in_db(self, sb, test_job_ids):
        """After concurrent claiming, all test jobs should be 'running' in DB."""
        for jid in test_job_ids:
            resp = sb.table("simulations").select("status").eq("id", jid).execute()
            assert resp.data, f"Job {jid} should exist"
            assert resp.data[0]["status"] == "running", (
                f"Job {jid} status should be 'running' after claim, "
                f"got '{resp.data[0]['status']}'"
            )

        # Reset for cleanup
        for jid in test_job_ids:
            sb.table("simulations").update({"status": "queued"}).eq("id", jid).execute()
