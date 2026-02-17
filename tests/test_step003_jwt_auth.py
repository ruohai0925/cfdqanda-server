"""
Step 003: API Server JWT authentication tests.

Verifies that:
1. Requests without Authorization header are rejected (403).
2. Requests with an invalid/forged token are rejected (401).
3. Requests with an expired token are rejected (401).
4. Requests with a valid Supabase JWT succeed and use token's user_id.
5. GET endpoints remain unauthenticated.
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


# --- Fixtures ---

@pytest.fixture()
def app_client():
    """
    Create a FastAPI TestClient with mocked Supabase.
    Environment variables and Supabase client are mocked before importing.
    """
    env_vars = {
        "FOAM_AGENT_DIR": TEST_FOAM_AGENT_DIR,
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_KEY": "test-service-key",
        "SUPABASE_JWT_SECRET": TEST_JWT_SECRET,
    }

    mock_supabase = MagicMock()

    with patch.dict(os.environ, env_vars, clear=False):
        with patch("supabase.create_client", return_value=mock_supabase):
            # Remove cached module to force re-import with patched env
            if "api_server" in sys.modules:
                del sys.modules["api_server"]

            from fastapi.testclient import TestClient
            import api_server

            client = TestClient(api_server.app)
            yield client, mock_supabase

            # Cleanup
            if "api_server" in sys.modules:
                del sys.modules["api_server"]


# --- Tests: No token / missing header ---

class TestNoToken:
    """Requests without Authorization header should be rejected."""

    def test_post_simulations_no_token(self, app_client):
        """POST /api/v1/simulations without token → 401."""
        client, _ = app_client
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test simulation"},
        )
        # HTTPBearer returns 401 when no credentials are provided
        assert resp.status_code == 401

    def test_post_feedback_no_token(self, app_client):
        """POST /api/v1/simulations/{id}/feedback without token → 401."""
        client, _ = app_client
        resp = client.post(
            "/api/v1/simulations/123/feedback",
            json={"file_path": "output/log", "feedback_content": "good"},
        )
        assert resp.status_code == 401


# --- Tests: Invalid / forged token ---

class TestInvalidToken:
    """Requests with forged or invalid tokens should be rejected."""

    def test_forged_token(self, app_client):
        """Token signed with wrong secret → 401."""
        client, _ = app_client
        forged_token = create_test_token(secret="wrong-secret")
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test simulation"},
            headers={"Authorization": f"Bearer {forged_token}"},
        )
        assert resp.status_code == 401
        assert "Invalid token" in resp.json()["detail"]

    def test_garbage_token(self, app_client):
        """Completely invalid token string → 401."""
        client, _ = app_client
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test simulation"},
            headers={"Authorization": "Bearer not.a.valid.jwt"},
        )
        assert resp.status_code == 401

    def test_expired_token(self, app_client):
        """Expired token → 401."""
        client, _ = app_client
        expired_token = create_test_token(expired=True)
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test simulation"},
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    def test_wrong_audience(self, app_client):
        """Token with wrong audience → 401."""
        client, _ = app_client
        now = datetime.now(timezone.utc)
        payload = {
            "sub": TEST_USER_ID,
            "aud": "wrong-audience",
            "iat": now,
            "exp": now + timedelta(hours=1),
        }
        token = pyjwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test simulation"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    def test_missing_sub_claim(self, app_client):
        """Token without 'sub' claim → 401."""
        client, _ = app_client
        now = datetime.now(timezone.utc)
        payload = {
            "aud": "authenticated",
            "iat": now,
            "exp": now + timedelta(hours=1),
        }
        token = pyjwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test simulation"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
        assert "missing user ID" in resp.json()["detail"]


# --- Tests: Valid token ---

class TestValidToken:
    """Requests with valid tokens should succeed."""

    def test_create_simulation_with_valid_token(self, app_client):
        """POST /api/v1/simulations with valid JWT → 200 and uses JWT user_id."""
        client, mock_supabase = app_client

        # Mock Supabase insert response
        mock_response = MagicMock()
        mock_response.data = [{
            "id": "fake-job-id",
            "user_id": TEST_USER_ID,
            "prompt": "test simulation",
            "status": "queued",
        }]
        mock_supabase.table.return_value.insert.return_value.execute.return_value = mock_response

        valid_token = create_test_token()
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test simulation"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["task"]["user_id"] == TEST_USER_ID

        # Verify Supabase was called with JWT user_id, not from body
        insert_call = mock_supabase.table.return_value.insert
        insert_call.assert_called_once()
        inserted_data = insert_call.call_args[0][0]
        assert inserted_data["user_id"] == TEST_USER_ID

    def test_user_id_not_accepted_in_body(self, app_client):
        """Even if user_id is sent in body, JWT user_id takes precedence."""
        client, mock_supabase = app_client

        mock_response = MagicMock()
        mock_response.data = [{
            "id": "fake-job-id",
            "user_id": TEST_USER_ID,
            "prompt": "test",
            "status": "queued",
        }]
        mock_supabase.table.return_value.insert.return_value.execute.return_value = mock_response

        valid_token = create_test_token()
        # Send a user_id in the body (should be ignored since it's not in the model)
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "test", "user_id": "attacker-id"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status_code == 200

        # Verify the inserted user_id is from JWT, not from body
        inserted_data = mock_supabase.table.return_value.insert.call_args[0][0]
        assert inserted_data["user_id"] == TEST_USER_ID


# --- Tests: GET endpoints remain unauthenticated ---

class TestGetEndpointsNoAuth:
    """GET endpoints should work without any Authorization header."""

    def test_health_check_no_auth(self, app_client):
        """GET / (health check) → 200 without token."""
        client, _ = app_client
        resp = client.get("/")
        assert resp.status_code == 200
        assert "running" in resp.json()["message"].lower()

    def test_get_file_tree_no_auth(self, app_client):
        """GET /api/v1/simulations/{id}/files → does not require auth (returns 404 for unknown job, not 403)."""
        client, mock_supabase = app_client
        mock_response = MagicMock()
        mock_response.data = []
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_response

        resp = client.get("/api/v1/simulations/999/files")
        # Should be 404 (not found), not 403 (no auth)
        assert resp.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
