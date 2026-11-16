"""
Step 053: no undefined names in the modules that run in production.

2026-11-16: a field added to the auto-mode task_settings was pasted into the
controlled-pipeline copy of that dict too, where its variable does not exist.
Every controlled-mode job then died with NameError — caught only by a
post-deploy check, because the controlled-pipeline tests (step 025) were
already broken and nothing else executes that path. pyflakes finds this class
of bug without running anything. (Install it to run this test:
`pip install pyflakes`; the test skips otherwise.)
"""

import os
import subprocess
import sys

import pytest

pytest.importorskip("pyflakes")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ["worker.py", "api_server.py", "case_lint.py", "taxonomy.py",
           "mcp_client.py", "stats.py", "supabase_rest.py"]


@pytest.mark.parametrize("module", MODULES)
def test_no_undefined_names(module):
    out = subprocess.run([sys.executable, "-m", "pyflakes", os.path.join(ROOT, module)],
                         capture_output=True, text=True).stdout
    bad = [l for l in out.splitlines() if "undefined name" in l or "referenced before assignment" in l]
    assert not bad, "\n".join(bad)
