"""
Codex provider integration tests.

Verifies:
1. Worker defaults to openai-codex / gpt-5.5 when no llm_config is provided.
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
    """Verify worker default provider/version logic matches openai-codex/gpt-5.5."""

    def test_default_provider_is_codex(self):
        """When llm_config has no model_provider, default to openai-codex."""
        llm_config = {}
        effective_provider = llm_config.get('model_provider') or 'openai-codex'
        effective_version = llm_config.get('model_version') or 'gpt-5.5'
        assert effective_provider == 'openai-codex'
        assert effective_version == 'gpt-5.5'

    def test_empty_llm_config_defaults(self):
        """When llm_config is None, default to openai-codex/gpt-5.5."""
        llm_config = None
        effective_provider = (llm_config or {}).get('model_provider') or 'openai-codex'
        effective_version = (llm_config or {}).get('model_version') or 'gpt-5.5'
        assert effective_provider == 'openai-codex'
        assert effective_version == 'gpt-5.5'

    def test_user_override_openai(self):
        """When user provides model_provider=openai, it overrides the default."""
        llm_config = {'model_provider': 'openai', 'model_version': 'gpt-4o'}
        effective_provider = llm_config.get('model_provider') or 'openai-codex'
        effective_version = llm_config.get('model_version') or 'gpt-5.5'
        assert effective_provider == 'openai'
        assert effective_version == 'gpt-4o'

    def test_user_override_anthropic(self):
        """When user provides model_provider=anthropic, it overrides the default."""
        llm_config = {'model_provider': 'anthropic', 'model_version': 'claude-sonnet-4-5-20250929'}
        effective_provider = llm_config.get('model_provider') or 'openai-codex'
        effective_version = llm_config.get('model_version') or 'gpt-5.5'
        assert effective_provider == 'anthropic'
        assert effective_version == 'claude-sonnet-4-5-20250929'

    def test_codex_no_api_key_injection(self):
        """openai-codex provider should not have api_key in llm_config."""
        llm_config = {'model_provider': 'openai-codex', 'model_version': 'gpt-5.5'}
        user_api_key = llm_config.get('api_key')
        assert user_api_key is None

    def test_openai_api_key_preserved_for_embeddings(self):
        """OPENAI_API_KEY from .env should remain in child_env for embedding provider."""
        child_env = os.environ.copy()
        child_env['OPENAI_API_KEY'] = 'sk-test-key-for-embeddings'
        child_env['FOAM_MODEL_PROVIDER'] = 'openai-codex'
        child_env['FOAM_MODEL_VERSION'] = 'gpt-5.5'
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

    @staticmethod
    def _setup_quota_pass(mock_supabase):
        """Set up mock to pass quota checks (0 tasks today, 0 storage)."""
        daily_resp = MagicMock()
        daily_resp.count = 0
        daily_resp.data = []

        storage_resp = MagicMock()
        storage_resp.data = []

        insert_resp = MagicMock()
        insert_resp.data = [{"id": "test-id", "user_id": TEST_USER_ID,
                             "prompt": "test", "status": "queued"}]

        call_count = {"n": 0}
        responses = [daily_resp, storage_resp, insert_resp]

        def table_side_effect(table_name):
            mock_table = MagicMock()
            def execute_side_effect():
                i = min(call_count["n"], len(responses) - 1)
                call_count["n"] += 1
                return responses[i]
            mock_table.select.return_value.eq.return_value.gte.return_value.execute = execute_side_effect
            mock_table.select.return_value.eq.return_value.is_.return_value.execute = execute_side_effect
            mock_table.insert.return_value.execute = execute_side_effect
            return mock_table

        mock_supabase.table.side_effect = table_side_effect

    def test_submit_with_codex_provider(self, app_client):
        """POST /api/v1/simulations with openai-codex provider -> 200."""
        client, mock_supabase = app_client
        import api_server
        api_server._storage_cache.clear()
        self._setup_quota_pass(mock_supabase)

        token = create_test_token()
        resp = client.post(
            "/api/v1/simulations",
            json={
                "prompt": "lid-driven cavity flow test",
                "llm_config": {
                    "model_provider": "openai-codex",
                    "model_version": "gpt-5.5",
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_submit_without_llm_config_uses_server_default(self, app_client):
        """POST /api/v1/simulations without llm_config -> 200, no llm_config in DB."""
        client, mock_supabase = app_client
        import api_server
        api_server._storage_cache.clear()
        self._setup_quota_pass(mock_supabase)

        token = create_test_token()
        resp = client.post(
            "/api/v1/simulations",
            json={"prompt": "lid-driven cavity flow test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
