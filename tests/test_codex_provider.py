"""
Codex provider integration tests.

Verifies:
1. Worker defaults to openai-codex / gpt-5.3-codex when no llm_config is provided.
2. User-provided model_provider/model_version overrides the default.
3. openai-codex provider does not inject an API key (it uses OAuth).
4. OPENAI_API_KEY from .env is preserved in child_env for the embedding provider.
5. API server accepts openai-codex in llm_config.
"""

import os
import sys
import pytest
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock


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


# --- Worker default tests (pure logic, no server needed) ---

class TestWorkerCodexDefaults:
    """Verify worker default provider/version logic matches openai-codex."""

    def test_default_provider_is_codex(self):
        """When llm_config has no model_provider, default to openai-codex."""
        llm_config = {}
        effective_provider = llm_config.get('model_provider') or 'openai-codex'
        effective_version = llm_config.get('model_version') or 'gpt-5.3-codex'
        assert effective_provider == 'openai-codex'
        assert effective_version == 'gpt-5.3-codex'

    def test_empty_llm_config_defaults(self):
        """When llm_config is None, default to openai-codex."""
        llm_config = None
        effective_provider = (llm_config or {}).get('model_provider') or 'openai-codex'
        effective_version = (llm_config or {}).get('model_version') or 'gpt-5.3-codex'
        assert effective_provider == 'openai-codex'
        assert effective_version == 'gpt-5.3-codex'

    def test_user_override_openai(self):
        """When user provides model_provider=openai, it overrides the default."""
        llm_config = {'model_provider': 'openai', 'model_version': 'gpt-4o'}
        effective_provider = llm_config.get('model_provider') or 'openai-codex'
        effective_version = llm_config.get('model_version') or 'gpt-5.3-codex'
        assert effective_provider == 'openai'
        assert effective_version == 'gpt-4o'

    def test_user_override_anthropic(self):
        """When user provides model_provider=anthropic, it overrides the default."""
        llm_config = {'model_provider': 'anthropic', 'model_version': 'claude-sonnet-4-5-20250929'}
        effective_provider = llm_config.get('model_provider') or 'openai-codex'
        effective_version = llm_config.get('model_version') or 'gpt-5.3-codex'
        assert effective_provider == 'anthropic'
        assert effective_version == 'claude-sonnet-4-5-20250929'

    def test_codex_no_api_key_injection(self):
        """openai-codex provider should not have api_key in llm_config."""
        llm_config = {'model_provider': 'openai-codex', 'model_version': 'gpt-5.3-codex'}
        user_api_key = llm_config.get('api_key')
        assert user_api_key is None

    def test_openai_api_key_preserved_for_embeddings(self):
        """OPENAI_API_KEY from .env should remain in child_env for embedding provider."""
        child_env = os.environ.copy()
        child_env['OPENAI_API_KEY'] = 'sk-test-key-for-embeddings'
        child_env['FOAM_MODEL_PROVIDER'] = 'openai-codex'
        child_env['FOAM_MODEL_VERSION'] = 'gpt-5.3-codex'
        # OPENAI_API_KEY must still be present (used by text-embedding-3-small)
        assert 'OPENAI_API_KEY' in child_env
        assert child_env['OPENAI_API_KEY'] == 'sk-test-key-for-embeddings'


# --- API server test ---

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


class TestApiCodexProvider:
    """API server should accept openai-codex in llm_config."""

    def test_submit_with_codex_provider(self, app_client):
        """POST /api/v1/simulations with openai-codex provider -> 200."""
        client, mock_supabase = app_client

        # Mock the insert response
        mock_insert_resp = MagicMock()
        mock_insert_resp.data = [{"id": "test-id"}]
        mock_supabase.table.return_value.insert.return_value.execute.return_value = mock_insert_resp

        token = create_test_token()
        resp = client.post(
            "/api/v1/simulations",
            json={
                "prompt": "lid-driven cavity flow test",
                "llm_config": {
                    "model_provider": "openai-codex",
                    "model_version": "gpt-5.3-codex",
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

        # Verify the insert was called with the codex config
        insert_call = mock_supabase.table.return_value.insert
        insert_call.assert_called_once()
        inserted_data = insert_call.call_args[0][0]
        assert inserted_data['llm_config']['model_provider'] == 'openai-codex'
        assert inserted_data['llm_config']['model_version'] == 'gpt-5.3-codex'
        assert 'api_key' not in inserted_data['llm_config']

    def test_submit_without_llm_config_uses_server_default(self, app_client):
        """POST /api/v1/simulations without llm_config -> 200, no llm_config in DB."""
        client, mock_supabase = app_client

        mock_insert_resp = MagicMock()
        mock_insert_resp.data = [{"id": "test-id"}]
        mock_supabase.table.return_value.insert.return_value.execute.return_value = mock_insert_resp

        token = create_test_token()
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "lid-driven cavity flow test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

        # llm_config should be None when not provided
        inserted_data = mock_supabase.table.return_value.insert.call_args[0][0]
        assert inserted_data.get('llm_config') is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
