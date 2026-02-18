"""
Soft-delete and restore endpoint tests.

Verifies:
1. DELETE /api/v1/simulations/{job_id} sets deleted_at (soft delete).
2. POST /api/v1/simulations/{job_id}/restore clears deleted_at.
3. JWT is required for both endpoints.
4. Ownership is enforced (user can only delete their own simulations).
5. Cannot delete a running simulation (409).
6. Cannot restore a simulation that is not deleted (400).
7. Returns 404 for non-existent simulation.
"""

import os
import sys
import pytest
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

TEST_JWT_SECRET = "test-jwt-secret-for-unit-tests"
TEST_USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_USER_ID = "11111111-2222-3333-4444-555555555555"
TEST_FOAM_AGENT_DIR = "/tmp/test-foam-agent"
TEST_JOB_ID = "deadbeef-1234-5678-abcd-000000000001"


def create_test_token(user_id=TEST_USER_ID, secret=TEST_JWT_SECRET, expired=False):
    """Create a valid or expired Supabase-like JWT for testing."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "aud": "authenticated",
        "role": "authenticated",
        "iat": now,
        "exp": now + (timedelta(hours=-1) if expired else timedelta(hours=1)),
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture()
def app_client():
    """Create a FastAPI TestClient with mocked Supabase."""
    env_vars = {
        "FOAM_AGENT_DIR": TEST_FOAM_AGENT_DIR,
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_KEY": "test-service-key",
        "SUPABASE_JWT_SECRET": TEST_JWT_SECRET,
    }

    mock_supabase = MagicMock()

    with patch.dict(os.environ, env_vars, clear=False):
        with patch("supabase.create_client", return_value=mock_supabase):
            if "api_server" in sys.modules:
                del sys.modules["api_server"]

            from fastapi.testclient import TestClient
            import api_server

            client = TestClient(api_server.app)
            yield client, mock_supabase

            if "api_server" in sys.modules:
                del sys.modules["api_server"]


# --- DELETE endpoint tests ---

class TestSoftDelete:
    """DELETE /api/v1/simulations/{job_id} tests."""

    def test_delete_requires_auth(self, app_client):
        """DELETE without token -> 401."""
        client, _ = app_client
        resp = client.delete(f"/api/v1/simulations/{TEST_JOB_ID}")
        assert resp.status_code == 401

    def test_delete_not_found(self, app_client):
        """DELETE non-existent job -> 404."""
        client, mock_supabase = app_client
        mock_resp = MagicMock()
        mock_resp.data = []
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_resp

        token = create_test_token()
        resp = client.delete(
            f"/api/v1/simulations/{TEST_JOB_ID}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_delete_wrong_owner(self, app_client):
        """DELETE by non-owner -> 403."""
        client, mock_supabase = app_client
        mock_resp = MagicMock()
        mock_resp.data = [{"id": TEST_JOB_ID, "user_id": OTHER_USER_ID, "status": "completed"}]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_resp

        token = create_test_token()
        resp = client.delete(
            f"/api/v1/simulations/{TEST_JOB_ID}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_delete_running_job_blocked(self, app_client):
        """DELETE a running job -> 409."""
        client, mock_supabase = app_client
        mock_resp = MagicMock()
        mock_resp.data = [{"id": TEST_JOB_ID, "user_id": TEST_USER_ID, "status": "running"}]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_resp

        token = create_test_token()
        resp = client.delete(
            f"/api/v1/simulations/{TEST_JOB_ID}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409
        assert "running" in resp.json()["detail"].lower()

    def test_delete_success(self, app_client):
        """DELETE completed job -> 200 and calls update with deleted_at."""
        client, mock_supabase = app_client

        # Mock select
        mock_select_resp = MagicMock()
        mock_select_resp.data = [{"id": TEST_JOB_ID, "user_id": TEST_USER_ID, "status": "completed"}]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_select_resp

        # Mock update
        mock_update_resp = MagicMock()
        mock_supabase.table.return_value.update.return_value.eq.return_value.execute.return_value = mock_update_resp

        token = create_test_token()
        resp = client.delete(
            f"/api/v1/simulations/{TEST_JOB_ID}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        # Verify update was called with deleted_at
        update_call = mock_supabase.table.return_value.update
        update_call.assert_called_once()
        update_data = update_call.call_args[0][0]
        assert "deleted_at" in update_data
        assert update_data["deleted_at"] is not None

    def test_delete_failed_job_allowed(self, app_client):
        """DELETE a failed job -> 200 (allowed)."""
        client, mock_supabase = app_client
        mock_select_resp = MagicMock()
        mock_select_resp.data = [{"id": TEST_JOB_ID, "user_id": TEST_USER_ID, "status": "failed"}]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_select_resp
        mock_supabase.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        token = create_test_token()
        resp = client.delete(
            f"/api/v1/simulations/{TEST_JOB_ID}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200


# --- RESTORE endpoint tests ---

class TestRestore:
    """POST /api/v1/simulations/{job_id}/restore tests."""

    def test_restore_requires_auth(self, app_client):
        """Restore without token -> 401."""
        client, _ = app_client
        resp = client.post(f"/api/v1/simulations/{TEST_JOB_ID}/restore")
        assert resp.status_code == 401

    def test_restore_not_found(self, app_client):
        """Restore non-existent job -> 404."""
        client, mock_supabase = app_client
        mock_resp = MagicMock()
        mock_resp.data = []
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_resp

        token = create_test_token()
        resp = client.post(
            f"/api/v1/simulations/{TEST_JOB_ID}/restore",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_restore_wrong_owner(self, app_client):
        """Restore by non-owner -> 403."""
        client, mock_supabase = app_client
        mock_resp = MagicMock()
        mock_resp.data = [{"id": TEST_JOB_ID, "user_id": OTHER_USER_ID, "deleted_at": "2026-02-17T00:00:00Z"}]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_resp

        token = create_test_token()
        resp = client.post(
            f"/api/v1/simulations/{TEST_JOB_ID}/restore",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_restore_not_deleted(self, app_client):
        """Restore a simulation that is not deleted -> 400."""
        client, mock_supabase = app_client
        mock_resp = MagicMock()
        mock_resp.data = [{"id": TEST_JOB_ID, "user_id": TEST_USER_ID, "deleted_at": None}]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_resp

        token = create_test_token()
        resp = client.post(
            f"/api/v1/simulations/{TEST_JOB_ID}/restore",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400
        assert "not deleted" in resp.json()["detail"].lower()

    def test_restore_success(self, app_client):
        """Restore a deleted simulation -> 200."""
        client, mock_supabase = app_client
        mock_select_resp = MagicMock()
        mock_select_resp.data = [{"id": TEST_JOB_ID, "user_id": TEST_USER_ID, "deleted_at": "2026-02-17T00:00:00Z"}]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_select_resp
        mock_supabase.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        token = create_test_token()
        resp = client.post(
            f"/api/v1/simulations/{TEST_JOB_ID}/restore",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        # Verify update was called with deleted_at = None
        update_call = mock_supabase.table.return_value.update
        update_call.assert_called_once()
        update_data = update_call.call_args[0][0]
        assert update_data["deleted_at"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
