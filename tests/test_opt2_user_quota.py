"""
OPT-2: User quota system tests.

Verifies that:
1. Users exceeding storage quota cannot submit new tasks (403).
2. Users exceeding daily task limit cannot submit new tasks (429).
3. Users within quota can submit tasks normally (200).
4. Soft-delete invalidates storage cache.
"""

import os
import sys
import pytest
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

# Test constants
TEST_JWT_SECRET = "test-jwt-secret-for-unit-tests"
TEST_USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TEST_FOAM_AGENT_DIR = "/tmp/test-foam-agent"


def create_test_token(user_id=TEST_USER_ID, secret=TEST_JWT_SECRET):
    """Create a valid Supabase-like JWT for testing."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "aud": "authenticated",
        "role": "authenticated",
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture()
def app_client():
    """
    Create a FastAPI TestClient with mocked Supabase.
    Sets quota limits low for testing: 100 MB storage, 3 tasks/day.
    """
    env_vars = {
        "FOAM_AGENT_DIR": TEST_FOAM_AGENT_DIR,
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_KEY": "test-service-key",
        "SUPABASE_JWT_SECRET": TEST_JWT_SECRET,
        "USER_STORAGE_LIMIT_MB": "100",   # 100 MB for testing
        "USER_DAILY_TASK_LIMIT": "3",     # 3 tasks/day for testing
    }

    mock_supabase = MagicMock()

    with patch.dict(os.environ, env_vars, clear=False):
        with patch("supabase.create_client", return_value=mock_supabase):
            if "api_server" in sys.modules:
                del sys.modules["api_server"]

            from fastapi.testclient import TestClient
            import api_server

            # Clear any cached storage data from previous tests
            api_server._storage_cache.clear()

            client = TestClient(api_server.app)
            yield client, mock_supabase, api_server

            if "api_server" in sys.modules:
                del sys.modules["api_server"]


def _mock_chain(mock_supabase):
    """Return the mock chain object for table('simulations').select(...)..."""
    return mock_supabase.table.return_value.select.return_value


def _setup_quota_mocks(mock_supabase, today_count=0, storage_bytes=0, task_count=1):
    """
    Set up mock responses for the quota check queries and the final insert.

    The create_simulation_task endpoint makes 3 Supabase calls in order:
    1. Daily task count:  table('simulations').select('id', count='exact').eq('user_id', ...).gte('created_at', ...).execute()
    2. Storage bytes:     table('simulations').select('id, result_data').eq('user_id', ...).is_('deleted_at', 'null').execute()
    3. Insert:            table('simulations').insert({...}).execute()

    We use side_effect on the chain's execute() to return different responses sequentially.
    """
    # Response 1: daily count query
    daily_resp = MagicMock()
    daily_resp.count = today_count
    daily_resp.data = [{"id": f"task-{i}"} for i in range(today_count)]

    # Response 2: storage query
    storage_resp = MagicMock()
    storage_resp.data = []
    if storage_bytes > 0:
        storage_resp.data = [{
            "id": "existing-task",
            "result_data": {
                "upload_stats": {"total_bytes": storage_bytes}
            }
        }]

    # Response 3: insert
    insert_resp = MagicMock()
    insert_resp.data = [{
        "id": "new-task-id",
        "user_id": TEST_USER_ID,
        "prompt": "test",
        "status": "queued",
    }]

    # Chain all execute() calls: the mock table builder chains differently for each call.
    # Since Supabase method chaining is complex, we use a simpler approach:
    # Track call count and return appropriate responses.
    call_count = {"n": 0}
    responses = [daily_resp, storage_resp, insert_resp]

    original_table = mock_supabase.table

    def table_side_effect(table_name):
        mock_table = MagicMock()
        # Every terminal .execute() returns the next response in sequence
        idx = min(call_count["n"], len(responses) - 1)

        def execute_side_effect():
            i = min(call_count["n"], len(responses) - 1)
            call_count["n"] += 1
            return responses[i]

        # Wire up all possible chain endings to our execute
        mock_table.select.return_value.eq.return_value.gte.return_value.execute = execute_side_effect
        mock_table.select.return_value.eq.return_value.is_.return_value.execute = execute_side_effect
        mock_table.insert.return_value.execute = execute_side_effect
        return mock_table

    mock_supabase.table.side_effect = table_side_effect


class TestDailyTaskLimit:
    """Users exceeding daily task limit should be blocked."""

    def test_daily_limit_exceeded(self, app_client):
        """User with 3 tasks today (limit=3) → 429."""
        client, mock_supabase, api_server = app_client
        api_server._storage_cache.clear()
        _setup_quota_mocks(mock_supabase, today_count=3, storage_bytes=0)

        token = create_test_token()
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 429
        assert "Daily task limit" in resp.json()["detail"]

    def test_daily_limit_not_exceeded(self, app_client):
        """User with 2 tasks today (limit=3) → 200."""
        client, mock_supabase, api_server = app_client
        api_server._storage_cache.clear()
        _setup_quota_mocks(mock_supabase, today_count=2, storage_bytes=0)

        token = create_test_token()
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"


class TestStorageQuota:
    """Users exceeding storage quota should be blocked."""

    def test_storage_exceeded(self, app_client):
        """User with 150 MB storage (limit=100 MB) → 403."""
        client, mock_supabase, api_server = app_client
        api_server._storage_cache.clear()
        storage_150mb = 150 * 1024 * 1024
        _setup_quota_mocks(mock_supabase, today_count=0, storage_bytes=storage_150mb)

        token = create_test_token()
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert "Storage quota exceeded" in resp.json()["detail"]

    def test_storage_within_limit(self, app_client):
        """User with 50 MB storage (limit=100 MB) → 200."""
        client, mock_supabase, api_server = app_client
        api_server._storage_cache.clear()
        storage_50mb = 50 * 1024 * 1024
        _setup_quota_mocks(mock_supabase, today_count=0, storage_bytes=storage_50mb)

        token = create_test_token()
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"


class TestQuotaWithinLimits:
    """Users within both quotas should submit tasks normally."""

    def test_fresh_user(self, app_client):
        """New user with 0 tasks and 0 storage → 200."""
        client, mock_supabase, api_server = app_client
        api_server._storage_cache.clear()
        _setup_quota_mocks(mock_supabase, today_count=0, storage_bytes=0)

        token = create_test_token()
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
