"""
Step 051: reading Foam-Agent's verdict after the 2026-11 upstream refactor.

Upstream changed how a run ends. main() no longer raises on a failed workflow;
it returns a state, and the CLI maps that to an exit status (0 ok, 2 failed or
partial, 1 crash) plus a closing <workflow_end> line that names the reason.
The worker used to import main() directly and ignore its return value, so on
the new code every failed run would have exited 0 and been recorded as
completed — caught in testing on 2026-11-15, before it shipped.

Marker strings below are verbatim from runs of upstream 4d5621a.
"""

import os
import re
import sys
import shutil
import tempfile
import pytest
from unittest.mock import patch, MagicMock

TEST_FOAM_AGENT_DIR = "/tmp/test-foam-agent"

OK_END = "<workflow_end>Workflow completed successfully!</workflow_end>"
PARTIAL_END = "<workflow_end>Simulation completed successfully, but visualization failed.</workflow_end>"
STOP = "<workflow_end>Workflow stopped without completing successfully: {}</workflow_end>"
CODEX_401 = ("Error occurred in LLM service: HTTP 401 for "
             "https://chatgpt.com/backend-api/codex/responses. Body: {\"error\": "
             "{\"message\": \"Your authentication token has expired.\"}}")
SOLVER_END = "Time = 0.5\n\nExecutionTime = 0.12 s  ClockTime = 0 s\n\nEnd\n\n"
SOLVER_FATAL = "--> FOAM FATAL ERROR:\ncannot find file \"/x/0/Urel\"\n\nFOAM exiting\n"


@pytest.fixture()
def worker():
    os.makedirs(TEST_FOAM_AGENT_DIR, exist_ok=True)
    env = {"FOAM_AGENT_DIR": TEST_FOAM_AGENT_DIR, "SUPABASE_URL": "https://t.supabase.co",
           "SUPABASE_SERVICE_KEY": "k", "SIMULATION_TIMEOUT": "2400"}
    sys.modules.setdefault("allrun_validator", MagicMock())
    sys.modules.setdefault("token_extractor", MagicMock())
    with patch.dict(os.environ, env, clear=False):
        with patch("supabase.create_client", return_value=MagicMock()):
            sys.modules.pop("worker", None)
            import worker as w
            yield w
            sys.modules.pop("worker", None)


@pytest.fixture()
def run_dir():
    d = tempfile.mkdtemp(prefix="fa-outcome-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_run(run_dir, log_lines, solver_log=None, report=False):
    log_path = os.path.join(run_dir, "simulation.log")
    with open(log_path, "w") as f:
        f.write("\n".join(log_lines) + "\n")
    out = os.path.join(run_dir, "output")
    os.makedirs(out, exist_ok=True)
    if solver_log is not None:
        with open(os.path.join(out, "log.icoFoam"), "w") as f:
            f.write(solver_log)
    if report:
        rep = os.path.join(out, ".foamagent", "workflow_report.html")
        os.makedirs(os.path.dirname(rep), exist_ok=True)
        open(rep, "w").write("<html></html>")
        with open(log_path, "a") as f:
            f.write(f"<workflow_report>{rep}</workflow_report>\n")
    return log_path


class TestDecideOutcome:

    def test_clean_success(self, worker, run_dir):
        log = make_run(run_dir, ["<workflow_start>Starting Foam-Agent...</workflow_start>", OK_END],
                       SOLVER_END, report=True)
        status, cat, msg, extra = worker._decide_outcome(0, log, run_dir, "openai-codex", False)
        assert status == "completed" and cat is None
        assert extra["workflow_report"] == "output/.foamagent/workflow_report.html"
        assert "warning" not in extra

    def test_partial_success_is_completed_with_warning(self, worker, run_dir):
        log = make_run(run_dir, [PARTIAL_END], SOLVER_END)
        status, cat, msg, extra = worker._decide_outcome(2, log, run_dir, "openai-codex", False)
        assert status == "completed"
        assert extra["warning_category"] == "visualization_failed"

    def test_partial_success_needs_solver_evidence(self, worker, run_dir):
        """Never take Foam-Agent's word for it alone (see #713)."""
        log = make_run(run_dir, [PARTIAL_END], SOLVER_FATAL)
        status, *_ = worker._decide_outcome(2, log, run_dir, "openai-codex", False)
        assert status == "failed"

    def test_exit_two_with_reason_is_failure(self, worker, run_dir):
        """The bug this file exists for: exit 2 must not be read as success."""
        log = make_run(run_dir, [STOP.format("repair_made_no_progress")], None)
        status, cat, msg, extra = worker._decide_outcome(2, log, run_dir, "openai-codex", False)
        assert status == "failed"
        assert cat == "repair_no_progress"
        assert extra["termination_reason"] == "repair_made_no_progress"

    def test_platform_llm_failure_beats_generic_reason(self, worker, run_dir):
        """An expired token reaches us as preparation_failed; it is our fault, refund it."""
        log = make_run(run_dir, [CODEX_401, STOP.format("preparation_failed")], None)
        status, cat, *_ = worker._decide_outcome(2, log, run_dir, "openai-codex", False)
        assert status == "failed" and cat == "auth_error_platform"

    def test_preparation_failed_without_llm_error(self, worker, run_dir):
        log = make_run(run_dir, [STOP.format("preparation_failed")], None)
        _, cat, *_ = worker._decide_outcome(2, log, run_dir, "openai-codex", False)
        assert cat == "preparation_failed"

    def test_oom_kill(self, worker, run_dir):
        log = make_run(run_dir, ["<workflow_start>Starting Foam-Agent...</workflow_start>"], None)
        _, cat, *_ = worker._decide_outcome(137, log, run_dir, "openai-codex", False)
        assert cat == "oom"

    def test_unknown_reason_surfaces_foam_agent_text(self, worker, run_dir):
        log = make_run(run_dir, [STOP.format("something_new_upstream")], None)
        status, cat, msg, _ = worker._decide_outcome(2, log, run_dir, "openai-codex", False)
        assert status == "failed" and cat is None
        assert "something_new_upstream" in msg


class TestUpstreamContract:

    # Every stop reason the prompt workflow can produce in upstream 4d5621a
    # (case_import_failed / case_target_mismatch belong to case import only).
    PROMPT_FLOW_REASONS = ["repair_made_no_progress", "max_review_loop_reached",
                           "agent_resources_unavailable", "static_review_failed",
                           "preparation_failed", "invalid_review_response",
                           "mesh_review_stopped", "solver_review_stopped"]

    def test_every_reason_has_a_message(self, worker):
        missing = [r for r in self.PROMPT_FLOW_REASONS if r not in worker.TERMINATION_REASONS]
        assert not missing, f"unmapped Foam-Agent stop reasons: {missing}"

    def test_worker_uses_the_cli_not_internals(self):
        """Importing main() is what hid failures; keep the CLI boundary."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "worker.py")).read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        assert '"src/main.py"' in code
        assert "from main import main" not in code

    def test_refundable_reasons_are_refunded(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "api_server.py")).read()
        block = src.split("PLATFORM_REFUND_CATEGORIES = [")[1].split("]")[0]
        for cat in ("preparation_failed", "platform_resources", "llm_response_error"):
            assert f"'{cat}'" in block, cat
