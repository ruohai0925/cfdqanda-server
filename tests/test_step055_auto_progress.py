"""
Step 055: auto-mode jobs report which stage Foam-Agent is in.

Before this, an auto job showed only "running" for 5-40+ minutes. The worker
now tails the Foam-Agent log for the marker each node prints and publishes a
coarse stage in `pipeline_stage` (auto_* values, unread by controlled mode).
Markers below are taken from real production logs (jobs 816, 2026-11-16).
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(scope="module")
def worker():
    env = {"FOAM_AGENT_DIR": "/tmp/test-foam-agent", "SUPABASE_URL": "https://t.supabase.co",
           "SUPABASE_SERVICE_KEY": "k"}
    os.makedirs("/tmp/test-foam-agent", exist_ok=True)
    sys.modules.setdefault("allrun_validator", MagicMock())
    sys.modules.setdefault("token_extractor", MagicMock())
    with patch.dict(os.environ, env, clear=False), \
         patch("supabase.create_client", return_value=MagicMock()):
        sys.modules.pop("worker", None)
        import worker as w
        yield w


MESHING = (
    "<workflow_start>Starting Foam-Agent...</workflow_start>\n"
    "<mesh_reference>{'case_name': 'cavity'}: The standard incompressible ...\n"
    "<initial_write_service>\n"
    "<generating_file>blockMeshDict in folder: system</generating_file>\n"
    "<mesh_static_review>\n"
    "Executing script /home/openfoam/Foam-Agent/runs/816/output/Allmesh in /x\n"
)
SETUP = (
    "<solver_planner>\n"
    "Retrieved 10 candidates from FAISS.\n"
    "</solver_planner>\n"
    '<input_writer mode="initial">\n'
    "<generating_file>U in folder: 0</generating_file>\n"
    "<solver_static_review>\n"
)
RUN = "<router>Local run requested. Routing to local_runner node.</router>\n<runner>\n"
REPAIR = '<input_writer mode="rewrite">\n<generating_file>fvSolution in folder: system</generating_file>\n'


def _log(tmp_path, text):
    p = tmp_path / "simulation.log"
    p.write_text(text)
    return p


def test_stages_follow_the_log(worker, tmp_path):
    log = _log(tmp_path, MESHING)
    tracker = worker._AutoProgress(str(log))
    assert tracker.poll() == "auto_meshing"
    with open(log, "a") as f:
        f.write(SETUP)
    assert tracker.poll() == "auto_setup"
    with open(log, "a") as f:
        f.write(RUN)
    assert tracker.poll() == "auto_running"
    with open(log, "a") as f:
        f.write("<visualization>\n")
    assert tracker.poll() == "auto_visualizing"


def test_repair_rounds_are_counted(worker, tmp_path):
    log = _log(tmp_path, MESHING + SETUP + RUN + REPAIR + RUN + REPAIR)
    assert worker._AutoProgress(str(log)).poll() == "auto_repairing:2"


def test_half_written_line_is_not_lost(worker, tmp_path):
    log = _log(tmp_path, MESHING + "<solver_pla")
    tracker = worker._AutoProgress(str(log))
    assert tracker.poll() == "auto_meshing"
    with open(log, "a") as f:
        f.write("nner>\n")
    assert tracker.poll() == "auto_setup"


def test_marker_must_start_the_line(worker, tmp_path):
    # Prompts and LLM output are echoed into the log; a tag quoted mid-line
    # must not move the stage.
    log = _log(tmp_path, MESHING + "the user wrote <runner> in the prompt\n")
    assert worker._AutoProgress(str(log)).poll() == "auto_meshing"


def test_missing_log_is_harmless(worker, tmp_path):
    assert worker._AutoProgress(str(tmp_path / "nope.log")).poll() is None


def test_polling_loop_publishes_only_changes(worker, tmp_path, monkeypatch):
    # A real child process writes the log; the loop must call on_progress once
    # per distinct stage, not once per poll.
    monkeypatch.setattr(worker, "AUTO_PROGRESS_INTERVAL", 0)
    monkeypatch.setattr(worker, "CANCEL_CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(worker, "check_job_cancelled", lambda _id: False)
    script = ("import time,sys\n"
              f"sys.stdout.write({MESHING!r}); sys.stdout.flush(); time.sleep(0.4)\n"
              f"sys.stdout.write({SETUP!r}); sys.stdout.flush(); time.sleep(0.4)\n")
    seen = []
    rc, cancelled, timed_out, disk = worker._run_subprocess_with_polling(
        command=[sys.executable, "-c", script], cwd=str(tmp_path), env=dict(os.environ),
        log_path=str(tmp_path / "simulation.log"), job_id=1, timeout=30,
        on_progress=seen.append)
    assert rc == 0 and not (cancelled or timed_out or disk)
    assert seen == ["auto_meshing", "auto_setup"]


def test_no_callback_means_no_tracking(worker, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CANCEL_CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(worker, "check_job_cancelled", lambda _id: False)
    rc, *_ = worker._run_subprocess_with_polling(
        command=[sys.executable, "-c", "print('<runner>')"], cwd=str(tmp_path),
        env=dict(os.environ), log_path=str(tmp_path / "simulation.log"), job_id=1, timeout=30)
    assert rc == 0
