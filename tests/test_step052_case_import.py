"""
Step 052: importing an existing OpenFOAM case (upstream Foam-Agent --case_path).

The worker fetches the user's .zip from Storage and hands it to Foam-Agent,
which does its own path-escape, link and zip-bomb checks. What the platform
adds is a size ceiling matched to its own per-task disk limit (Foam-Agent's is
10 GiB), checked before anything is unpacked.

Also pins the fix for a path traversal in the existing mesh download: the
client-supplied file name was joined onto the run directory as-is.
"""

import io
import os
import sys
import shutil
import tempfile
import zipfile
import pytest
from unittest.mock import patch, MagicMock

TEST_FOAM_AGENT_DIR = "/tmp/test-foam-agent"


@pytest.fixture()
def worker():
    os.makedirs(TEST_FOAM_AGENT_DIR, exist_ok=True)
    env = {"FOAM_AGENT_DIR": TEST_FOAM_AGENT_DIR, "SUPABASE_URL": "https://t.supabase.co",
           "SUPABASE_SERVICE_KEY": "k", "TASK_DISK_LIMIT_MB": "1024"}
    sys.modules.setdefault("allrun_validator", MagicMock())
    sys.modules.setdefault("token_extractor", MagicMock())
    with patch.dict(os.environ, env, clear=False):
        with patch("supabase.create_client", return_value=MagicMock()):
            sys.modules.pop("worker", None)
            import worker as w
            yield w
            sys.modules.pop("worker", None)


@pytest.fixture()
def tmp():
    d = tempfile.mkdtemp(prefix="case-import-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


class TestCaseArchiveCheck:

    def test_ordinary_case_is_accepted(self, worker, tmp):
        z = make_zip(os.path.join(tmp, "c.zip"), {
            "cavity/system/controlDict": "application icoFoam;",
            "cavity/constant/transportProperties": "nu 0.01;",
            "cavity/0/U": "internalField uniform (0 0 0);"})
        assert worker._case_archive_problem(z) is None

    def test_not_a_zip(self, worker, tmp):
        p = os.path.join(tmp, "c.zip")
        open(p, "w").write("this is a tarball, honest")
        assert "not a valid ZIP" in worker._case_archive_problem(p)

    def test_empty_zip(self, worker, tmp):
        assert "empty" in worker._case_archive_problem(make_zip(os.path.join(tmp, "c.zip"), {}))

    def test_unpacked_size_over_task_disk_limit(self, worker, tmp):
        """Compressed 100 MB can unpack far past the per-task disk limit."""
        z = make_zip(os.path.join(tmp, "c.zip"), {"case/0/U": "0" * 4096})
        with patch.object(worker, "TASK_DISK_LIMIT_BYTES", 1024):
            msg = worker._case_archive_problem(z)
        assert msg and "per-task limit" in msg and "processor" in msg


class TestUploadNames:

    def test_mesh_name_cannot_escape_run_dir(self, worker, tmp):
        run_dir = os.path.join(tmp, "runs", "1")
        os.makedirs(run_dir)
        worker.supabase.storage.from_.return_value.download.return_value = b"$MeshFormat"
        path = worker._download_mesh_file(
            1, {"storage_path": "mesh_uploads/u/x.msh", "original_name": "../../evil.msh"}, run_dir)
        assert os.path.dirname(os.path.abspath(path)) == os.path.abspath(run_dir)
        assert not os.path.exists(os.path.join(tmp, "evil.msh"))

    def test_case_archive_saved_under_fixed_name(self, worker, tmp):
        worker.supabase.storage.from_.return_value.download.return_value = b"PK\x05\x06" + b"\0" * 18
        path = worker._download_case_archive(
            1, {"storage_path": "case_uploads/u/c.zip", "original_name": "../../../x.zip"}, tmp)
        assert path == os.path.join(tmp, "imported_case.zip")


class TestImportStopReasons:

    def test_import_reasons_have_messages(self, worker):
        for reason in ("case_import_failed", "case_target_mismatch"):
            assert reason in worker.TERMINATION_REASONS
        assert "Foundation v10" in worker.TERMINATION_REASONS["case_target_mismatch"][1]


# ---------------------------------------------------------------------------
# API: what a create request may reference
# ---------------------------------------------------------------------------

from tests.test_opt2_user_quota import (  # noqa: E402  (shared fixtures/helpers)
    app_client, create_test_token, TEST_USER_ID,
)


def _wire(api, mock_supabase, inserted):
    """Quotas pass (stubbed — they are not what these tests are about); the
    insert payload is captured."""
    api._count_today_billable = lambda uid: {"total": 0, "refunded": 0, "billable": 0}
    api._get_user_storage_bytes = lambda uid: 0

    def insert(payload):
        inserted.append(payload)
        m = MagicMock()
        m.execute.return_value = MagicMock(
            data=[{"id": 1, "user_id": TEST_USER_ID, "prompt": "", "status": "queued"}])
        return m
    mock_supabase.table.return_value.insert = insert


def _post(client, body):
    return client.post("/api/v1/simulations", json=body,
                       headers={"Authorization": f"Bearer {create_test_token()}"})


ARCHIVE = {"storage_path": f"case_uploads/{TEST_USER_ID}/1_cavity.zip",
           "original_name": "cavity.zip", "size_bytes": 3450}


class TestCreateWithCaseArchive:

    def test_valid_archive_is_stored_in_parameters(self, app_client):
        client, sb, api = app_client
        inserted = []
        _wire(api, sb, inserted)
        r = _post(client, {"prompt": "", "case_archive": ARCHIVE})
        assert r.status_code == 200, r.text
        assert inserted[0]["parameters"] == {"case_archive": ARCHIVE}

    @pytest.mark.parametrize("patch_fields,detail", [
        ({"storage_path": "case_uploads/someone-else/1_cavity.zip"}, "Invalid case archive reference"),
        ({"original_name": "cavity.tar.gz"}, ".zip"),
        ({"size_bytes": 101 * 1024 * 1024}, "MB limit"),
    ])
    def test_bad_archive_rejected(self, app_client, patch_fields, detail):
        client, sb, api = app_client
        _wire(api, sb, [])
        r = _post(client, {"prompt": "run it", "case_archive": {**ARCHIVE, **patch_fields}})
        assert r.status_code == 400 and detail in r.json()["detail"]

    def test_controlled_mode_rejected(self, app_client):
        client, sb, api = app_client
        _wire(api, sb, [])
        r = _post(client, {"prompt": "run it", "case_archive": ARCHIVE, "pipeline_mode": "controlled"})
        assert r.status_code == 400 and "auto mode" in r.json()["detail"]

    def test_mesh_reference_must_be_own_upload(self, app_client):
        client, sb, api = app_client
        _wire(api, sb, [])
        r = _post(client, {"prompt": "run it", "mesh_file": {
            "storage_path": "mesh_uploads/someone-else/m.msh", "original_name": "m.msh", "size_bytes": 10}})
        assert r.status_code == 400 and "Invalid mesh file reference" in r.json()["detail"]
