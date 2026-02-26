"""
Step 006: CORS restriction tests.

Verifies that:
1. Allowed origins receive proper CORS headers for GET/POST.
2. Preflight (OPTIONS) for allowed methods (GET, POST) succeeds.
3. Preflight for disallowed methods (PUT) is rejected; DELETE and PATCH are allowed.
4. Disallowed origins are rejected.
5. Only Content-Type and Authorization headers are allowed.
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock


# Test constants
TEST_FOAM_AGENT_DIR = "/tmp/test-foam-agent"
ALLOWED_ORIGIN = "http://localhost:5173"
DISALLOWED_ORIGIN = "http://evil.example.com"


@pytest.fixture()
def app_client():
    """Create a FastAPI TestClient with mocked Supabase."""
    env_vars = {
        "FOAM_AGENT_DIR": TEST_FOAM_AGENT_DIR,
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_KEY": "test-service-key",
        "SUPABASE_JWT_SECRET": "test-jwt-secret",
    }

    mock_supabase = MagicMock()

    with patch.dict(os.environ, env_vars, clear=False):
        with patch("supabase.create_client", return_value=mock_supabase):
            if "api_server" in sys.modules:
                del sys.modules["api_server"]

            from fastapi.testclient import TestClient
            import api_server

            client = TestClient(api_server.app)
            yield client

            if "api_server" in sys.modules:
                del sys.modules["api_server"]


class TestPreflightAllowedMethods:
    """Preflight requests for allowed methods should succeed."""

    def test_preflight_post_allowed(self, app_client):
        """OPTIONS preflight for POST from allowed origin → 200 with CORS headers."""
        resp = app_client.options(
            "/api/v1/simulations",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, Authorization",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
        assert "POST" in resp.headers.get("access-control-allow-methods", "")

    def test_preflight_get_allowed(self, app_client):
        """OPTIONS preflight for GET from allowed origin → 200 with CORS headers."""
        resp = app_client.options(
            "/",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
        assert "GET" in resp.headers.get("access-control-allow-methods", "")


class TestPreflightMethodFiltering:
    """Preflight requests: allowed methods succeed, disallowed methods are rejected."""

    def test_preflight_put_rejected(self, app_client):
        """OPTIONS preflight for PUT → 400 (method not allowed by CORS)."""
        resp = app_client.options(
            "/api/v1/simulations",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "PUT",
            },
        )
        assert resp.status_code == 400

    def test_preflight_delete_allowed(self, app_client):
        """OPTIONS preflight for DELETE → 200 (DELETE is an allowed CORS method)."""
        resp = app_client.options(
            "/api/v1/simulations/123",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "DELETE",
            },
        )
        assert resp.status_code == 200
        assert "DELETE" in resp.headers.get("access-control-allow-methods", "")

    def test_preflight_patch_allowed(self, app_client):
        """OPTIONS preflight for PATCH → 200 (PATCH is used by /rating endpoint)."""
        resp = app_client.options(
            "/api/v1/simulations/123/rating",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "PATCH",
            },
        )
        assert resp.status_code == 200
        assert "PATCH" in resp.headers.get("access-control-allow-methods", "")


class TestDisallowedOrigin:
    """Requests from disallowed origins should not receive CORS headers."""

    def test_disallowed_origin_no_cors_header(self, app_client):
        """GET from disallowed origin → no access-control-allow-origin header."""
        resp = app_client.get(
            "/",
            headers={"Origin": DISALLOWED_ORIGIN},
        )
        # Server still responds (CORS is browser-enforced),
        # but the access-control-allow-origin header should be absent
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers

    def test_preflight_disallowed_origin(self, app_client):
        """Preflight from disallowed origin → 400."""
        resp = app_client.options(
            "/api/v1/simulations",
            headers={
                "Origin": DISALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp.status_code == 400


class TestAllowedHeaders:
    """Only Content-Type and Authorization headers should be allowed."""

    def test_allowed_headers_in_preflight(self, app_client):
        """Preflight response should list allowed headers."""
        resp = app_client.options(
            "/api/v1/simulations",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, Authorization",
            },
        )
        assert resp.status_code == 200
        allowed_headers = resp.headers.get("access-control-allow-headers", "").lower()
        assert "content-type" in allowed_headers
        assert "authorization" in allowed_headers

    def test_disallowed_header_rejected(self, app_client):
        """Preflight requesting a non-allowed header → 400."""
        resp = app_client.options(
            "/api/v1/simulations",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Custom-Evil-Header",
            },
        )
        assert resp.status_code == 400


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
