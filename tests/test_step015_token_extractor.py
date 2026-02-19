"""
Unit tests for Step 015: Token usage extraction from simulation logs.

Tests the token_extractor module which parses the <LLM Service Statistics>
block output by Foam-Agent's LLMService.print_statistics().
"""

import os
import sys
import tempfile

import pytest

# Ensure the parent directory (cfdqanda-server/) is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from token_extractor import extract_token_usage


# --- Fixtures ---

TYPICAL_LOG = """\
2026-02-19 10:00:00 - INFO - Starting simulation...
Some output from Foam-Agent planner node
blockMesh output here
simpleFoam output here

<LLM Service Statistics>
Total calls: 7
Failed calls: 0
Total retries: 0
Total prompt tokens: 3147
Total completion tokens: 15
Total tokens: 3162
Average prompt tokens per call: 449.57
Average completion tokens per call: 2.14
Average tokens per call: 451.71

</LLM Service Statistics>
"""

MINIMAL_LOG = """\
<LLM Service Statistics>
Total tokens: 100
</LLM Service Statistics>
"""

NO_STATS_LOG = """\
2026-02-19 10:00:00 - INFO - Simulation failed
blockMesh: FOAM FATAL ERROR
"""

EMPTY_BLOCK_LOG = """\
<LLM Service Statistics>

</LLM Service Statistics>
"""

MULTIPLE_BLOCKS_LOG = """\
<LLM Service Statistics>
Total calls: 3
Total tokens: 500
</LLM Service Statistics>

Some intermediate output

<LLM Service Statistics>
Total calls: 7
Total tokens: 3162
</LLM Service Statistics>
"""

ZERO_CALLS_LOG = """\
<LLM Service Statistics>
Total calls: 0
Failed calls: 0
Total retries: 0
Total prompt tokens: 0
Total completion tokens: 0
Total tokens: 0
Average prompt tokens per call: 0
Average completion tokens per call: 0
Average tokens per call: 0
</LLM Service Statistics>
"""


def _write_temp_log(content):
    """Write content to a temporary file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".log")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


# --- Tests: Typical usage ---

class TestExtractTypical:
    """Test extraction from a typical simulation log."""

    def test_all_fields_extracted(self):
        path = _write_temp_log(TYPICAL_LOG)
        try:
            result = extract_token_usage(path)
            assert result is not None
            assert result["total_calls"] == 7
            assert result["failed_calls"] == 0
            assert result["total_retries"] == 0
            assert result["total_prompt_tokens"] == 3147
            assert result["total_completion_tokens"] == 15
            assert result["total_tokens"] == 3162
            assert result["avg_prompt_tokens"] == pytest.approx(449.57)
            assert result["avg_completion_tokens"] == pytest.approx(2.14)
            assert result["avg_tokens"] == pytest.approx(451.71)
        finally:
            os.unlink(path)

    def test_integer_fields_are_int(self):
        path = _write_temp_log(TYPICAL_LOG)
        try:
            result = extract_token_usage(path)
            assert isinstance(result["total_calls"], int)
            assert isinstance(result["total_tokens"], int)
            assert isinstance(result["total_prompt_tokens"], int)
        finally:
            os.unlink(path)

    def test_float_fields_are_float(self):
        path = _write_temp_log(TYPICAL_LOG)
        try:
            result = extract_token_usage(path)
            assert isinstance(result["avg_prompt_tokens"], float)
            assert isinstance(result["avg_completion_tokens"], float)
            assert isinstance(result["avg_tokens"], float)
        finally:
            os.unlink(path)


# --- Tests: Edge cases ---

class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_file_not_found(self):
        result = extract_token_usage("/nonexistent/path/simulation.log")
        assert result is None

    def test_no_stats_block(self):
        path = _write_temp_log(NO_STATS_LOG)
        try:
            result = extract_token_usage(path)
            assert result is None
        finally:
            os.unlink(path)

    def test_empty_block(self):
        path = _write_temp_log(EMPTY_BLOCK_LOG)
        try:
            result = extract_token_usage(path)
            assert result is None  # No fields parsed -> None
        finally:
            os.unlink(path)

    def test_empty_file(self):
        path = _write_temp_log("")
        try:
            result = extract_token_usage(path)
            assert result is None
        finally:
            os.unlink(path)

    def test_minimal_block(self):
        path = _write_temp_log(MINIMAL_LOG)
        try:
            result = extract_token_usage(path)
            assert result is not None
            assert result["total_tokens"] == 100
            # Other fields not present
            assert "total_calls" not in result
        finally:
            os.unlink(path)

    def test_multiple_blocks_uses_last(self):
        """When multiple stat blocks exist, use the last one."""
        path = _write_temp_log(MULTIPLE_BLOCKS_LOG)
        try:
            result = extract_token_usage(path)
            assert result is not None
            assert result["total_calls"] == 7
            assert result["total_tokens"] == 3162
        finally:
            os.unlink(path)

    def test_zero_values(self):
        path = _write_temp_log(ZERO_CALLS_LOG)
        try:
            result = extract_token_usage(path)
            assert result is not None
            assert result["total_calls"] == 0
            assert result["total_tokens"] == 0
        finally:
            os.unlink(path)

    def test_start_tag_without_end_tag(self):
        content = "<LLM Service Statistics>\nTotal tokens: 100\n"
        path = _write_temp_log(content)
        try:
            result = extract_token_usage(path)
            assert result is None
        finally:
            os.unlink(path)

    def test_binary_content_in_log(self):
        """Log files with some binary content should not crash."""
        content = (
            "Some binary \x00\xff data\n"
            "<LLM Service Statistics>\n"
            "Total tokens: 42\n"
            "</LLM Service Statistics>\n"
        )
        path = _write_temp_log(content)
        try:
            result = extract_token_usage(path)
            assert result is not None
            assert result["total_tokens"] == 42
        finally:
            os.unlink(path)


# --- Tests: Real log files from Foam-Agent/runs/ ---

class TestRealLogs:
    """Test against real simulation logs if they exist."""

    RUNS_DIR = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "Foam-Agent", "runs"
    )

    def _find_real_logs(self):
        """Find all simulation.log files in Foam-Agent/runs/."""
        logs = []
        if not os.path.isdir(self.RUNS_DIR):
            return logs
        for entry in os.listdir(self.RUNS_DIR):
            log_path = os.path.join(self.RUNS_DIR, entry, "simulation.log")
            if os.path.isfile(log_path):
                logs.append(log_path)
        return logs

    def test_real_logs_parse_without_error(self):
        """All real simulation logs should parse without exceptions."""
        logs = self._find_real_logs()
        if not logs:
            pytest.skip("No real simulation logs found in Foam-Agent/runs/")
        for log_path in logs:
            # Should not raise any exception
            result = extract_token_usage(log_path)
            # result can be None (if no stats block) or a dict

    def test_real_logs_with_stats_have_total_tokens(self):
        """Real logs that have a stats block should include total_tokens."""
        logs = self._find_real_logs()
        if not logs:
            pytest.skip("No real simulation logs found in Foam-Agent/runs/")
        found_any = False
        for log_path in logs:
            result = extract_token_usage(log_path)
            if result is not None:
                found_any = True
                assert "total_tokens" in result
                assert isinstance(result["total_tokens"], int)
                assert result["total_tokens"] >= 0
        if not found_any:
            pytest.skip("No simulation logs contained LLM Service Statistics")


# --- Tests: Worker integration (source code verification) ---

class TestWorkerIntegration:
    """Verify worker.py properly imports and uses token_extractor."""

    WORKER_PATH = os.path.join(os.path.dirname(__file__), "..", "worker.py")

    def test_worker_imports_extract_token_usage(self):
        with open(self.WORKER_PATH) as f:
            source = f.read()
        assert "from token_extractor import extract_token_usage" in source

    def test_worker_calls_extract_token_usage(self):
        with open(self.WORKER_PATH) as f:
            source = f.read()
        assert "extract_token_usage(log_path)" in source

    def test_worker_adds_token_usage_to_result_data(self):
        with open(self.WORKER_PATH) as f:
            source = f.read()
        assert '"token_usage"' in source or "'token_usage'" in source
