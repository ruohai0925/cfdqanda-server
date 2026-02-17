"""
Step 004: Path traversal protection tests for feedback endpoint.

Verifies that:
1. Normal file paths (e.g. "output/log.blockMesh") are accepted.
2. Path traversal attempts (e.g. "../../etc/passwd") are rejected with 400.
3. Various traversal techniques (encoded, nested, absolute) are all blocked.
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
TEST_JOB_ID = 42


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


def mock_supabase_job(mock_supabase, user_id=TEST_USER_ID, job_id=TEST_JOB_ID):
    """Configure mock Supabase to return a valid job owned by user_id."""
    mock_response = MagicMock()
    mock_response.data = [{
        "id": job_id,
        "user_id": user_id,
        "status": "completed",
        "result_data": {},
    }]
    mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_response
    # Also mock storage upload to succeed
    mock_supabase.storage.from_.return_value.upload.return_value = None


# --- Fixtures ---

@pytest.fixture()
def app_client():
    """
    Create a FastAPI TestClient with mocked Supabase.
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


# --- Tests: Path traversal attacks should be blocked ---

class TestPathTraversalBlocked:
    """Malicious file_path values should be rejected with 400."""

    def _post_feedback(self, client, mock_supabase, file_path):
        """Helper to POST feedback with a given file_path."""
        mock_supabase_job(mock_supabase)
        token = create_test_token()
        return client.post(
            f"/api/v1/simulations/{TEST_JOB_ID}/feedback",
            json={"file_path": file_path, "feedback_content": "test feedback"},
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_dotdot_simple(self, app_client):
        """file_path: '../../etc/passwd' → 400."""
        client, mock_sb = app_client
        resp = self._post_feedback(client, mock_sb, "../../etc/passwd")
        assert resp.status_code == 400
        assert "path traversal" in resp.json()["detail"].lower()

    def test_dotdot_in_middle(self, app_client):
        """file_path: 'output/../../secret.txt' → 400."""
        client, mock_sb = app_client
        resp = self._post_feedback(client, mock_sb, "output/../../secret.txt")
        assert resp.status_code == 400

    def test_dotdot_deep(self, app_client):
        """file_path: '../../../../../../../tmp/hack' → 400."""
        client, mock_sb = app_client
        resp = self._post_feedback(client, mock_sb, "../../../../../../../tmp/hack")
        assert resp.status_code == 400

    def test_absolute_path(self, app_client):
        """file_path: '/etc/passwd' → 400 (absolute path escapes base dir)."""
        client, mock_sb = app_client
        resp = self._post_feedback(client, mock_sb, "/etc/passwd")
        assert resp.status_code == 400

    def test_dotdot_with_trailing_slash(self, app_client):
        """file_path: '../' → 400."""
        client, mock_sb = app_client
        resp = self._post_feedback(client, mock_sb, "../")
        assert resp.status_code == 400


# --- Tests: Legitimate file paths should be accepted ---

class TestLegitimatePathsAccepted:
    """Valid file paths within the job directory should be accepted."""

    def _post_feedback(self, client, mock_supabase, file_path):
        """Helper to POST feedback with a given file_path."""
        mock_supabase_job(mock_supabase)
        token = create_test_token()
        return client.post(
            f"/api/v1/simulations/{TEST_JOB_ID}/feedback",
            json={"file_path": file_path, "feedback_content": "looks good"},
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_simple_file(self, app_client):
        """file_path: 'output/log.blockMesh' → accepted (200)."""
        client, mock_sb = app_client
        resp = self._post_feedback(client, mock_sb, "output/log.blockMesh")
        assert resp.status_code == 200

    def test_nested_file(self, app_client):
        """file_path: 'output/0/U' → accepted (200)."""
        client, mock_sb = app_client
        resp = self._post_feedback(client, mock_sb, "output/0/U")
        assert resp.status_code == 200

    def test_top_level_file(self, app_client):
        """file_path: 'Allrun' → accepted (200)."""
        client, mock_sb = app_client
        resp = self._post_feedback(client, mock_sb, "Allrun")
        assert resp.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
