"""
Step 050: the solver log is the only trustworthy record of a run (2026-09-19).

Job #713 timed out while Foam-Agent's own workflow log said "Allrun executed
successfully without errors" — and `output/cases/*/log.SRFSimpleFoam` showed
the solver had aborted immediately on a missing 0/Urel field. The platform
reported "timed out", the lint returned nothing, and the case review initially
recorded it as a successful run killed by post-processing. It wasn't.

These tests pin the two rules that came out of that:
1. _detect_solver_outcome() reads solver logs, ignores utility logs, and finds
   them at any depth (multi-case parameter sweeps live in output/cases/<name>/).
2. A timeout is only salvaged into a success when the solver log really ended
   with OpenFOAM's "End" and the stage still running was visualization.
"""

import os
import sys
import tempfile
import shutil
import pytest
from unittest.mock import patch, MagicMock

TEST_FOAM_AGENT_DIR = "/tmp/test-foam-agent"

FATAL_LOG = """/*--------------------------------*- C++ -*----------------------------------*\\
Build  : 10-c4cf895ad8fa
Exec   : SRFSimpleFoam
\\*---------------------------------------------------------------------------*/
Create mesh for time = 0

SIMPLE: No convergence criteria found

Reading field p

Reading field Urel


--> FOAM FATAL ERROR:
cannot find file "/home/openfoam/Foam-Agent/runs/713/output/cases/Q05/0/Urel"

    From function virtual Foam::autoPtr<Foam::ISstream> Foam::fileOperations::
    in file global/fileOperations/uncollatedFileOperation.C at line 539.

FOAM exiting

"""

COMPLETED_LOG = """Starting time loop

Time = 0.5

DICPCG:  Solving for p, Initial residual = 0.000109455, Final residual = 7.2e-07
ExecutionTime = 0.122731 s  ClockTime = 0 s

End

"""

RUNNING_LOG = """Starting time loop

Time = 0.00121528s

DILUPBiCGStab:  Solving for alpha.water, Initial residual = 2.28e-09
Phase-1 volume fraction = 0.00051942336
"""


@pytest.fixture()
def worker_module():
    os.makedirs(TEST_FOAM_AGENT_DIR, exist_ok=True)
    env_vars = {
        "FOAM_AGENT_DIR": TEST_FOAM_AGENT_DIR,
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_KEY": "test-service-key",
        "SIMULATION_TIMEOUT": "2400",
    }
    sys.modules.setdefault("allrun_validator", MagicMock())
    sys.modules.setdefault("token_extractor", MagicMock())
    with patch.dict(os.environ, env_vars, clear=False):
        with patch("supabase.create_client", return_value=MagicMock()):
            if "worker" in sys.modules:
                del sys.modules["worker"]
            import worker
            yield worker
            if "worker" in sys.modules:
                del sys.modules["worker"]


@pytest.fixture()
def run_dir():
    d = tempfile.mkdtemp(prefix="solver-outcome-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _write(run_dir, rel, content):
    path = os.path.join(run_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path


class TestDetectSolverOutcome:

    def test_clean_run_is_completed(self, worker_module, run_dir):
        _write(run_dir, "output/log.icoFoam", COMPLETED_LOG)
        r = worker_module._detect_solver_outcome(run_dir)
        assert r["status"] == "completed"
        assert r["solver"] == "icoFoam"

    def test_fatal_beats_everything(self, worker_module, run_dir):
        """#713's shape: a utility log that looks fine plus a dead solver."""
        _write(run_dir, "output/cases/Q05/log.blockMesh", COMPLETED_LOG)
        _write(run_dir, "output/cases/Q05/log.SRFSimpleFoam", FATAL_LOG)
        r = worker_module._detect_solver_outcome(run_dir)
        assert r["status"] == "fatal"
        assert r["solver"] == "SRFSimpleFoam"
        assert "0/Urel" in r["fatal"]
        assert "From function" not in r["fatal"], "stack noise must be trimmed"

    def test_killed_mid_run_is_incomplete(self, worker_module, run_dir):
        _write(run_dir, "output/log.interFoam", RUNNING_LOG)
        assert worker_module._detect_solver_outcome(run_dir)["status"] == "incomplete"

    def test_utility_logs_alone_are_not_a_solver(self, worker_module, run_dir):
        """blockMesh also prints 'End' — it must not count as a finished run."""
        _write(run_dir, "output/log.blockMesh", COMPLETED_LOG)
        _write(run_dir, "output/log.setFields", COMPLETED_LOG)
        r = worker_module._detect_solver_outcome(run_dir)
        assert r["status"] == "unknown"
        assert r["logs_checked"] == 0

    def test_finds_solver_in_nested_case_dirs(self, worker_module, run_dir):
        """Parameter sweeps put each case under output/cases/<name>/."""
        _write(run_dir, "output/cases/Q20mlmin_RPM300/log.simpleFoam", COMPLETED_LOG)
        assert worker_module._detect_solver_outcome(run_dir)["status"] == "completed"

    def test_missing_dir_is_safe(self, worker_module):
        assert worker_module._detect_solver_outcome("/nonexistent")["status"] == "unknown"


class TestVisualizationTimeoutSalvage:
    """Only a genuinely finished solver turns a timeout into a success."""

    def _run(self, worker, run_dir, log_content):
        log_path = _write(run_dir, "simulation.log", log_content)
        completed, failed = {}, {}
        with patch.object(worker, "_upload_and_complete",
                          side_effect=lambda *a, **k: completed.update(k)), \
             patch.object(worker, "_upload_and_fail",
                          side_effect=lambda *a, **k: failed.update(k)), \
             patch.object(worker, "_run_allrun_audit", return_value={}), \
             patch.object(worker, "lint_hints", return_value=[]):
            worker._handle_cancelled_or_timeout(
                1, "u", run_dir, log_path, cancelled=False, timed_out=True)
        return completed, failed

    def test_solver_done_visualization_timed_out_completes(self, worker_module, run_dir):
        _write(run_dir, "output/log.icoFoam", COMPLETED_LOG)
        completed, failed = self._run(
            worker_module, run_dir, "planner\ninput_writer\nlocal_runner\nvisualization\n")
        assert completed and not failed
        extra = completed["extra_result"]
        assert extra["warning_category"] == "visualization_timeout"
        assert "icoFoam" in extra["warning"]

    def test_solver_fatal_still_fails(self, worker_module, run_dir):
        """#713: never salvage a run whose solver aborted."""
        _write(run_dir, "output/cases/Q05/log.SRFSimpleFoam", FATAL_LOG)
        completed, failed = self._run(
            worker_module, run_dir, "planner\ninput_writer\nvisualization\n")
        assert failed and not completed
        assert failed["extra_result"]["error_category"] == "timeout"
        assert failed["extra_result"]["solver_outcome"]["status"] == "fatal"

    def test_timeout_in_reviewer_is_not_salvaged(self, worker_module, run_dir):
        """A finished solver mid-fix-loop is not a deliverable result."""
        _write(run_dir, "output/log.icoFoam", COMPLETED_LOG)
        completed, failed = self._run(
            worker_module, run_dir, "planner\ninput_writer\nreviewer\n")
        assert failed and not completed


class TestLintScansMultiCaseLayout:

    def test_surfaces_solver_fatal_from_nested_case(self, run_dir):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from case_lint import lint_hints
        _write(run_dir, "output/cases/Q05/log.SRFSimpleFoam", FATAL_LOG)
        hints = lint_hints(run_dir)
        assert len(hints) == 1
        assert "SRFSimpleFoam" in hints[0] and "Urel" in hints[0]

    def test_clean_case_yields_no_hint(self, run_dir):
        from case_lint import lint_hints
        _write(run_dir, "output/log.icoFoam", COMPLETED_LOG)
        assert lint_hints(run_dir) == []
