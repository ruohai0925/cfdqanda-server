"""
Step 054: controlled-mode affinity needs workers on different machines to have
different identities.

Inside a container the worker runs as PID 1, so the old default
f"worker-{os.getpid()}" named every worker on every machine "worker-1" (seen in
GCP logs, 2026-11-16) and the affinity check never fired. Identity now comes
from WORKER_SITE, one per machine.
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock


def load_worker(extra_env):
    env = {"FOAM_AGENT_DIR": "/tmp/test-foam-agent", "SUPABASE_URL": "https://t.supabase.co",
           "SUPABASE_SERVICE_KEY": "k", **extra_env}
    os.makedirs("/tmp/test-foam-agent", exist_ok=True)
    sys.modules.setdefault("allrun_validator", MagicMock())
    sys.modules.setdefault("token_extractor", MagicMock())
    with patch.dict(os.environ, env, clear=False):
        for k in ("WORKER_ID", "WORKER_SITE"):
            if k not in extra_env:
                os.environ.pop(k, None)
        with patch("supabase.create_client", return_value=MagicMock()):
            sys.modules.pop("worker", None)
            import worker
            wid = worker.WORKER_ID
            sys.modules.pop("worker", None)
            return wid


def test_site_names_the_machine():
    assert load_worker({"WORKER_SITE": "gcp"}) == "gcp"


def test_explicit_worker_id_wins():
    assert load_worker({"WORKER_SITE": "gcp", "WORKER_ID": "gcp-a"}) == "gcp-a"


def test_unset_keeps_previous_behaviour():
    assert load_worker({}) == f"worker-{os.getpid()}"


def test_two_sites_differ():
    assert load_worker({"WORKER_SITE": "gcp"}) != load_worker({"WORKER_SITE": "desktop"})
