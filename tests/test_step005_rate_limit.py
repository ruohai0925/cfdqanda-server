"""
Step 005: Rate limiting tests.

Verifies that:
1. POST /api/v1/simulations is limited to 5 requests/minute per IP.
2. POST /api/v1/simulations/{id}/feedback is limited to 10 requests/minute per IP.
3. Exceeding the limit returns 429 Too Many Requests.
4. GET endpoints are not rate-limited.
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

            # Reset the limiter storage between tests to avoid state leakage
            api_server.limiter.reset()

            client = TestClient(api_server.app)
            yield client, mock_supabase, api_server

            # Cleanup
            if "api_server" in sys.modules:
                del sys.modules["api_server"]


def _mock_supabase_insert_success(mock_supabase):
    """Configure mock Supabase to return a successful insert."""
    mock_response = MagicMock()
    mock_response.data = [{
        "id": "fake-job-id",
        "user_id": TEST_USER_ID,
        "prompt": "test simulation",
        "status": "queued",
    }]
    mock_supabase.table.return_value.insert.return_value.execute.return_value = mock_response


# --- Tests: Rate limiting on POST /api/v1/simulations ---

class TestSimulationRateLimit:
    """POST /api/v1/simulations should be limited to 5/minute per IP."""

    def test_within_limit_succeeds(self, app_client):
        """5 requests within the limit should all succeed."""
        client, mock_supabase, _ = app_client
        _mock_supabase_insert_success(mock_supabase)
        token = create_test_token()
        headers = {"Authorization": f"Bearer {token}"}

        for i in range(5):
            resp = client.post(
                "/api/v1/simulations",
                json={"prompt": f"test simulation {i}"},
                headers=headers,
            )
            assert resp.status_code == 200, f"Request {i+1} failed with {resp.status_code}"

    def test_exceeding_limit_returns_429(self, app_client):
        """6th request within a minute should return 429."""
        client, mock_supabase, _ = app_client
        _mock_supabase_insert_success(mock_supabase)
        token = create_test_token()
        headers = {"Authorization": f"Bearer {token}"}

        # Send 5 requests (all should succeed)
        for i in range(5):
            resp = client.post(
                "/api/v1/simulations",
                json={"prompt": f"test simulation {i}"},
                headers=headers,
            )
            assert resp.status_code == 200

        # 6th request should be rate limited
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "one too many"},
            headers=headers,
        )
        assert resp.status_code == 429


# --- Tests: Rate limiting on POST /api/v1/simulations/{id}/feedback ---

class TestFeedbackRateLimit:
    """POST /api/v1/simulations/{id}/feedback should be limited to 10/minute per IP."""

    def _mock_feedback_dependencies(self, mock_supabase):
        """Set up mocks for the feedback endpoint's DB queries and storage."""
        # Mock: job lookup returns a job owned by TEST_USER_ID
        mock_select_response = MagicMock()
        mock_select_response.data = [{
            "id": "fake-job-id",
            "user_id": TEST_USER_ID,
            "status": "completed",
        }]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_select_response

        # Mock: storage upload succeeds
        mock_supabase.storage.from_.return_value.upload.return_value = None

    def test_within_limit_succeeds(self, app_client):
        """10 feedback requests within the limit should all succeed."""
        client, mock_supabase, _ = app_client
        self._mock_feedback_dependencies(mock_supabase)
        token = create_test_token()
        headers = {"Authorization": f"Bearer {token}"}

        for i in range(10):
            resp = client.post(
                "/api/v1/simulations/123/feedback",
                json={"file_path": "output/log", "feedback_content": f"feedback {i}"},
                headers=headers,
            )
            # 200 or 500 from storage mock — but NOT 429
            assert resp.status_code != 429, f"Request {i+1} was rate-limited at 429"

    def test_exceeding_limit_returns_429(self, app_client):
        """11th feedback request within a minute should return 429."""
        client, mock_supabase, _ = app_client
        self._mock_feedback_dependencies(mock_supabase)
        token = create_test_token()
        headers = {"Authorization": f"Bearer {token}"}

        # Send 10 requests
        for i in range(10):
            client.post(
                "/api/v1/simulations/123/feedback",
                json={"file_path": "output/log", "feedback_content": f"feedback {i}"},
                headers=headers,
            )

        # 11th should be rate limited
        resp = client.post(
            "/api/v1/simulations/123/feedback",
            json={"file_path": "output/log", "feedback_content": "one too many"},
            headers=headers,
        )
        assert resp.status_code == 429


# --- Tests: GET endpoints are NOT rate-limited ---

class TestGetEndpointsNotLimited:
    """GET endpoints should not be affected by rate limiting."""

    def test_health_check_not_limited(self, app_client):
        """GET / should work even after many requests."""
        client, _, _ = app_client
        for i in range(20):
            resp = client.get("/")
            assert resp.status_code == 200, f"Request {i+1} failed with {resp.status_code}"


# --- Tests: 429 response format ---

class TestRateLimitResponse:
    """Verify the 429 response includes useful information."""

    def test_429_response_body(self, app_client):
        """429 response should include an error message."""
        client, mock_supabase, _ = app_client
        _mock_supabase_insert_success(mock_supabase)
        token = create_test_token()
        headers = {"Authorization": f"Bearer {token}"}

        # Exhaust the limit
        for i in range(5):
            client.post(
                "/api/v1/simulations",
                json={"prompt": f"test {i}"},
                headers=headers,
            )

        # 6th request
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "over limit"},
            headers=headers,
        )
        assert resp.status_code == 429
        body = resp.json()
        assert "error" in body or "detail" in body


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
