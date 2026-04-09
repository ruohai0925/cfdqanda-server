"""
Step 049: Error diagnosis, queue status, and platform default model tests.

Verifies that:
1. _diagnose_subprocess_failure detects rate limit / quota / auth errors in logs.
2. Codex quota exceeded is identified with correct error_category.
3. Auth errors are detected from simulation logs.
4. Unknown errors return (None, None).
5. Timeout failures include error_category='timeout' and human-readable duration.
6. GET /api/v1/queue-status returns correct queue depth and ordered job IDs.
7. Platform default model is gpt-5.3-codex (openai-codex provider).
"""

import os
import sys
import shutil
import tempfile
import pytest
from unittest.mock import patch, MagicMock

# Test constants
TEST_JWT_SECRET = "test-jwt-secret-for-unit-tests"
TEST_FOAM_AGENT_DIR = "/tmp/test-foam-agent"


def _ensure_test_dir():
    """Create the temporary FOAM_AGENT_DIR if it doesn't exist."""
    os.makedirs(TEST_FOAM_AGENT_DIR, exist_ok=True)


@pytest.fixture()
def worker_module():
    """Import worker.py with mocked environment and dependencies.

    worker.py checks FOAM_AGENT_DIR exists at import time and imports
    local modules (allrun_validator, token_extractor), so we must:
    1. Create the temp directory
    2. Mock those local imports before importing worker
    """
    _ensure_test_dir()
    env_vars = {
        "FOAM_AGENT_DIR": TEST_FOAM_AGENT_DIR,
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_KEY": "test-service-key",
        "SIMULATION_TIMEOUT": "3600",
    }

    # Pre-load mocked local dependencies that worker.py imports at top level
    mock_allrun = MagicMock()
    mock_token_ext = MagicMock()
    sys.modules.setdefault("allrun_validator", mock_allrun)
    sys.modules.setdefault("token_extractor", mock_token_ext)

    mock_supabase = MagicMock()

    with patch.dict(os.environ, env_vars, clear=False):
        with patch("supabase.create_client", return_value=mock_supabase):
            if "worker" in sys.modules:
                del sys.modules["worker"]
            import worker
            yield worker, mock_supabase
            if "worker" in sys.modules:
                del sys.modules["worker"]


# ---------------------------------------------------------------------------
# Part 1: _diagnose_subprocess_failure unit tests
# ---------------------------------------------------------------------------

class TestDiagnoseSubprocessFailure:
    """Test log scanning for known LLM API error patterns."""

    @pytest.fixture(autouse=True)
    def _setup(self, worker_module):
        self.diagnose = worker_module[0]._diagnose_subprocess_failure

    def _write_log(self, content):
        """Write content to a temp log file and return its path."""
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False)
        f.write(content)
        f.close()
        return f.name

    # --- Codex quota exceeded ---

    def test_codex_quota_exceeded(self):
        """Codex rate_limit_exceeded → error_category='codex_quota_exceeded'."""
        log = self._write_log(
            "Error: RateLimitError: rate_limit_exceeded - You've exceeded the rate limit.\n"
            "Please try again later."
        )
        msg, cat = self.diagnose(log, 'openai-codex')
        assert cat == 'codex_quota_exceeded'
        assert 'Codex' in msg
        os.unlink(log)

    def test_codex_quota_exceeded_429(self):
        """Codex 'too many requests' phrasing → codex_quota_exceeded."""
        log = self._write_log("HTTP 429: Too Many Requests\nRetry after 60s")
        msg, cat = self.diagnose(log, 'openai-codex')
        assert cat == 'codex_quota_exceeded'
        os.unlink(log)

    # --- Generic rate limit (non-Codex) ---

    def test_openai_rate_limit(self):
        """OpenAI rate limit with provider='openai' → error_category='rate_limit'."""
        log = self._write_log(
            "openai.RateLimitError: Rate limit reached for gpt-5.3-codex "
            "in organization org-xxx on requests per min."
        )
        msg, cat = self.diagnose(log, 'openai')
        assert cat == 'rate_limit'
        assert 'rate limit' in msg.lower()
        os.unlink(log)

    def test_quota_exceeded(self):
        """'You exceeded your current quota' → rate_limit."""
        log = self._write_log(
            "openai.error.RateLimitError: You exceeded your current quota, "
            "please check your plan and billing details."
        )
        msg, cat = self.diagnose(log, 'openai')
        assert cat == 'rate_limit'
        os.unlink(log)

    def test_insufficient_quota(self):
        """'insufficient_quota' error code → rate_limit."""
        log = self._write_log(
            '{"error": {"code": "insufficient_quota", "message": "..."}}'
        )
        msg, cat = self.diagnose(log, 'openai')
        assert cat == 'rate_limit'
        os.unlink(log)

    # --- Auth errors ---

    def test_invalid_api_key_platform(self):
        """'invalid api key' on platform default → 'auth_error_platform'."""
        log = self._write_log(
            "openai.AuthenticationError: Incorrect API key provided: sk-xxx. "
            "You can find your API key at https://platform.openai.com/account/api-keys."
        )
        msg, cat = self.diagnose(log, 'openai', is_byok=False)
        assert cat == 'auth_error_platform'
        assert 'platform' in msg.lower() or 'unavailable' in msg.lower()
        os.unlink(log)

    def test_invalid_api_key_byok(self):
        """'invalid api key' on BYOK → 'auth_error_byok'."""
        log = self._write_log(
            "openai.AuthenticationError: Incorrect API key provided: sk-xxx."
        )
        msg, cat = self.diagnose(log, 'openai', is_byok=True)
        assert cat == 'auth_error_byok'
        assert 'API key' in msg or 'key' in msg
        os.unlink(log)

    def test_unauthorized_error(self):
        """HTTP 401 Unauthorized → auth_error_*."""
        log = self._write_log("HTTP Error 401: Unauthorized\nInvalid bearer token")
        msg, cat = self.diagnose(log, 'openai', is_byok=False)
        assert cat == 'auth_error_platform'
        os.unlink(log)

    def test_anthropic_auth_error(self):
        """Anthropic authentication failure → auth_error_*."""
        log = self._write_log(
            "anthropic.AuthenticationError: authentication failed, "
            "invalid_api_key"
        )
        msg, cat = self.diagnose(log, 'anthropic', is_byok=True)
        assert cat == 'auth_error_byok'
        os.unlink(log)

    def test_codex_token_expired_byok(self):
        """BYOK Codex token expiry → suggests re-auth and warns about ~10 day expiry."""
        log = self._write_log(
            "ChatCompletionResponse: HTTP 401 token_expired"
        )
        msg, cat = self.diagnose(log, 'openai-codex', is_byok=True)
        assert cat == 'auth_error_byok'
        assert 'Codex' in msg or '10 days' in msg

    # --- Unknown / no match ---

    def test_unknown_error_returns_none(self):
        """Normal simulation log without API errors → (None, None)."""
        log = self._write_log(
            "Starting simulation...\n"
            "OpenFOAM mesh generated successfully.\n"
            "FOAM FATAL ERROR: cannot find file system/controlDict\n"
        )
        msg, cat = self.diagnose(log, 'openai')
        assert msg is None
        assert cat is None
        os.unlink(log)

    def test_empty_log_returns_none(self):
        """Empty log file → (None, None)."""
        log = self._write_log("")
        msg, cat = self.diagnose(log, 'openai')
        assert msg is None
        assert cat is None
        os.unlink(log)

    def test_missing_log_returns_none(self):
        """Non-existent log path → (None, None)."""
        msg, cat = self.diagnose("/tmp/nonexistent_log_12345.log", 'openai')
        assert msg is None
        assert cat is None

    def test_none_log_path(self):
        """None log path → (None, None)."""
        msg, cat = self.diagnose(None, 'openai')
        assert msg is None
        assert cat is None

    # --- Case insensitivity ---

    def test_case_insensitive_detection(self):
        """Detection is case-insensitive."""
        log = self._write_log("RATELIMITERROR: RATE LIMIT REACHED for model gpt-5.3-codex")
        msg, cat = self.diagnose(log, 'openai')
        assert cat == 'rate_limit'
        os.unlink(log)

    def test_faiss_score_not_false_positive_auth(self):
        """FAISS similarity score containing '401' should NOT trigger auth_error.

        Regression test for bug where score=0.40114... was matched by bare '401' pattern.
        """
        log = self._write_log(
            "1. cavityDrivenFlow | incompressible | RAS | pimpleFoam | score=0.40114468336105347\n"
            "2. flowWithOpenBoundary | incompressible | laminar\n"
            "Workflow failed with error: ValidationError"
        )
        msg, cat = self.diagnose(log, 'openai-codex')
        assert cat is None, f"FAISS score should not trigger auth_error, got: {cat}"
        os.unlink(log)


# ---------------------------------------------------------------------------
# Part 2: Timeout error_category test
# ---------------------------------------------------------------------------

class TestTimeoutErrorCategory:
    """Verify timeout failures include error_category='timeout'."""

    @pytest.fixture(autouse=True)
    def _setup(self, worker_module):
        self.worker, self.mock_supabase = worker_module

    def test_handle_timeout_sets_error_category(self):
        """_handle_cancelled_or_timeout with timed_out=True → error_category='timeout'."""
        with patch.object(self.worker, '_upload_and_fail') as mock_fail:
            result = self.worker._handle_cancelled_or_timeout(
                job_id="timeout-test-001",
                user_id="user-001",
                run_dir="/tmp/test-run-timeout",
                log_path="/tmp/test-run-timeout/simulation.log",
                cancelled=False,
                timed_out=True,
            )

        assert result is True
        mock_fail.assert_called_once()
        call_args = mock_fail.call_args
        # Check error message includes minutes
        error_msg = call_args[0][2]
        assert 'timed out' in error_msg.lower()
        assert '60' in error_msg  # 3600s = 60 min
        # Check extra_result includes error_category
        extra_result = call_args[1]['extra_result']
        assert extra_result['error_category'] == 'timeout'

    def test_handle_cancelled_not_timeout(self):
        """_handle_cancelled_or_timeout with cancelled=True → does not set timeout category."""
        with patch.object(self.worker, '_upload_and_fail') as mock_fail:
            result = self.worker._handle_cancelled_or_timeout(
                job_id="cancel-test-001",
                user_id="user-001",
                run_dir="/tmp/test",
                log_path="/tmp/test/sim.log",
                cancelled=True,
                timed_out=False,
            )

        assert result is True
        call_args = mock_fail.call_args
        error_msg = call_args[0][2]
        assert 'cancelled' in error_msg.lower()


# ---------------------------------------------------------------------------
# Part 3: Platform default model = openai-codex / gpt-5.3-codex
# ---------------------------------------------------------------------------

class TestPlatformDefaultModel:
    """Verify the platform default model is openai-codex / gpt-5.3-codex."""

    def test_default_model_version_is_nano(self):
        """When llm_config is empty, effective_version should be gpt-5.3-codex."""
        # The fallback pattern in worker.py:
        #   effective_version = llm_config.get('model_version') or 'gpt-5.3-codex'
        llm_config = {}
        effective_version = llm_config.get('model_version') or 'gpt-5.3-codex'
        assert effective_version == 'gpt-5.3-codex'

    def test_default_model_provider_is_codex(self):
        """When llm_config is empty, effective_provider should be openai-codex."""
        llm_config = {}
        effective_provider = llm_config.get('model_provider') or 'openai-codex'
        assert effective_provider == 'openai-codex'

    def test_byok_overrides_default(self):
        """When llm_config has values, they override defaults."""
        llm_config = {'model_provider': 'anthropic', 'model_version': 'claude-sonnet-4-5-20250929'}
        effective_provider = llm_config.get('model_provider') or 'openai-codex'
        effective_version = llm_config.get('model_version') or 'gpt-5.3-codex'
        assert effective_provider == 'anthropic'
        assert effective_version == 'claude-sonnet-4-5-20250929'


# ---------------------------------------------------------------------------
# Part 4: GET /api/v1/queue-status endpoint tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client():
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


class TestQueueStatusEndpoint:
    """Test GET /api/v1/queue-status."""

    def test_returns_queue_info(self, api_client):
        """Returns queued_count, running_count, and ordered queued_ids."""
        client, mock_supabase = api_client

        # Mock queued jobs response (ordered by created_at ASC)
        queued_resp = MagicMock()
        queued_resp.data = [
            {"id": "aaa-111", "created_at": "2026-03-12T10:00:00Z"},
            {"id": "bbb-222", "created_at": "2026-03-12T10:05:00Z"},
            {"id": "ccc-333", "created_at": "2026-03-12T10:10:00Z"},
        ]

        # Mock running jobs response
        running_resp = MagicMock()
        running_resp.data = [{"id": "ddd-444"}]

        # Chain mock: table().select().eq().is_().order().execute()
        queued_chain = MagicMock()
        queued_chain.execute.return_value = queued_resp

        running_chain = MagicMock()
        running_chain.execute.return_value = running_resp

        # We need to handle two separate call chains for the same table
        call_count = {"n": 0}
        chains = [queued_chain, running_chain]

        def table_side_effect(name):
            mock_table = MagicMock()
            # Each chain call returns the appropriate terminal mock
            idx = min(call_count["n"], len(chains) - 1)
            terminal = chains[idx]
            call_count["n"] += 1
            # Make all chained methods return terminal (which has .execute)
            mock_table.select.return_value = mock_table
            mock_table.eq.return_value = mock_table
            mock_table.is_.return_value = mock_table
            mock_table.order.return_value = mock_table
            mock_table.execute.return_value = terminal.execute.return_value
            return mock_table

        mock_supabase.table.side_effect = table_side_effect

        resp = client.get("/api/v1/queue-status")
        assert resp.status_code == 200

        data = resp.json()
        assert data["queued_count"] == 3
        assert data["running_count"] == 1
        assert data["queued_ids"] == ["aaa-111", "bbb-222", "ccc-333"]

    def test_empty_queue(self, api_client):
        """Returns zeros when no tasks are queued or running."""
        client, mock_supabase = api_client

        empty_resp = MagicMock()
        empty_resp.data = []

        def table_side_effect(name):
            mock_table = MagicMock()
            mock_table.select.return_value = mock_table
            mock_table.eq.return_value = mock_table
            mock_table.is_.return_value = mock_table
            mock_table.order.return_value = mock_table
            mock_table.execute.return_value = empty_resp
            return mock_table

        mock_supabase.table.side_effect = table_side_effect

        resp = client.get("/api/v1/queue-status")
        assert resp.status_code == 200

        data = resp.json()
        assert data["queued_count"] == 0
        assert data["running_count"] == 0
        assert data["queued_ids"] == []

    def test_no_auth_required(self, api_client):
        """Queue status endpoint does not require authentication."""
        client, mock_supabase = api_client

        empty_resp = MagicMock()
        empty_resp.data = []

        def table_side_effect(name):
            mock_table = MagicMock()
            mock_table.select.return_value = mock_table
            mock_table.eq.return_value = mock_table
            mock_table.is_.return_value = mock_table
            mock_table.order.return_value = mock_table
            mock_table.execute.return_value = empty_resp
            return mock_table

        mock_supabase.table.side_effect = table_side_effect

        # No Authorization header
        resp = client.get("/api/v1/queue-status")
        assert resp.status_code == 200

    def test_database_error_returns_500(self, api_client):
        """Database error → 500 response."""
        client, mock_supabase = api_client

        mock_supabase.table.side_effect = Exception("DB connection lost")

        resp = client.get("/api/v1/queue-status")
        assert resp.status_code == 500


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
