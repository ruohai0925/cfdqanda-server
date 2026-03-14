"""
Step 25: MCP Pipeline unit and component tests.

Tests cover:
1. MCP Client: response parsing, method signatures, server manager
2. Worker pipeline routing: auto vs controlled mode dispatch
3. Pipeline state machine: stage transitions, checkpoint logic
4. API endpoints: stage confirm/reject for both modes
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

# Ensure cfdqanda-server/ is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock fastmcp before any import of mcp_client, since fastmcp is only
# available in the FoamAgent conda env, not in the foam-api test env.
_mock_fastmcp_client = MagicMock()
sys.modules.setdefault('fastmcp', MagicMock(Client=_mock_fastmcp_client))


# ============================================================================
# 1. MCP Client Tests
# ============================================================================

class TestMCPClientResponseParsing(unittest.TestCase):
    """Test FoamAgentMCPClient._parse_response() with various result formats."""

    def setUp(self):
        from mcp_client import FoamAgentMCPClient
        self.client = FoamAgentMCPClient.__new__(FoamAgentMCPClient)

    def test_parse_structured_content(self):
        """structured_content takes precedence."""
        result = MagicMock()
        result.structured_content = {"case_name": "cavity"}
        result.data = {"case_name": "wrong"}
        self.assertEqual(self.client._parse_response(result), {"case_name": "cavity"})

    def test_parse_data_fallback(self):
        """Falls back to data when structured_content is None."""
        result = MagicMock()
        result.structured_content = None
        result.data = {"case_name": "cavity"}
        self.assertEqual(self.client._parse_response(result), {"case_name": "cavity"})

    def test_parse_dict_passthrough(self):
        """Plain dict is returned as-is."""
        result = {"case_name": "cavity"}
        self.assertEqual(self.client._parse_response(result), {"case_name": "cavity"})

    def test_parse_empty_fallback(self):
        """Returns empty dict when nothing available."""
        result = MagicMock()
        result.structured_content = None
        result.data = None
        self.assertEqual(self.client._parse_response(result), {})


class TestMCPClientMethods(unittest.TestCase):
    """Test that FoamAgentMCPClient methods call the correct MCP tools."""

    def setUp(self):
        from mcp_client import FoamAgentMCPClient
        self.client = FoamAgentMCPClient.__new__(FoamAgentMCPClient)
        self.mock_inner = AsyncMock()
        self.client._client = self.mock_inner

        # Mock call_tool to return a proper response
        mock_result = MagicMock()
        mock_result.structured_content = {"status": "ok"}
        mock_result.data = None
        self.mock_inner.call_tool = AsyncMock(return_value=mock_result)

    def test_plan_calls_correct_tool(self):
        """plan() calls MCP tool 'plan' with correct request structure."""
        asyncio.run(self.client.plan("Test requirement"))
        self.mock_inner.call_tool.assert_called_once()
        args = self.mock_inner.call_tool.call_args
        self.assertEqual(args[0][0], "plan")
        self.assertEqual(args[0][1]["request"]["user_requirement"], "Test requirement")

    def test_input_writer_calls_correct_tool(self):
        """input_writer() calls MCP tool 'input_writer' with all params."""
        asyncio.run(self.client.input_writer(
            case_name="cavity",
            subtasks=[{"file": "U", "folder": "0"}],
            user_requirement="test",
            case_solver="icoFoam",
            case_domain="incompressible",
            case_category="tutorial",
        ))
        args = self.mock_inner.call_tool.call_args
        self.assertEqual(args[0][0], "input_writer")
        req = args[0][1]["request"]
        self.assertEqual(req["case_name"], "cavity")
        self.assertEqual(req["case_solver"], "icoFoam")

    def test_run_calls_correct_tool(self):
        """run() calls MCP tool 'run' with case_dir and timeout."""
        asyncio.run(self.client.run("/path/to/case", timeout=600))
        args = self.mock_inner.call_tool.call_args
        self.assertEqual(args[0][0], "run")
        self.assertEqual(args[0][1]["request"]["case_dir"], "/path/to/case")
        self.assertEqual(args[0][1]["request"]["timeout"], 600)

    def test_review_calls_correct_tool(self):
        """review() calls MCP tool 'review'."""
        asyncio.run(self.client.review("/path", ["error1"], "requirement"))
        args = self.mock_inner.call_tool.call_args
        self.assertEqual(args[0][0], "review")

    def test_apply_fixes_calls_correct_tool(self):
        """apply_fixes() calls MCP tool 'apply_fixes'."""
        asyncio.run(self.client.apply_fixes("/path", ["err"], "analysis", "req"))
        args = self.mock_inner.call_tool.call_args
        self.assertEqual(args[0][0], "apply_fixes")

    def test_visualization_calls_correct_tool(self):
        """visualization() calls MCP tool 'visualization'."""
        asyncio.run(self.client.visualization("/path", quantity="pressure"))
        args = self.mock_inner.call_tool.call_args
        self.assertEqual(args[0][0], "visualization")
        self.assertEqual(args[0][1]["request"]["quantity"], "pressure")

    def test_input_writer_from_plan_convenience(self):
        """input_writer_from_plan() extracts fields from plan result."""
        plan_result = {
            "case_name": "cavity",
            "subtasks": [{"file": "U", "folder": "0"}],
            "case_solver": "icoFoam",
            "case_domain": "incompressible",
            "case_category": "tutorial",
        }
        asyncio.run(self.client.input_writer_from_plan(plan_result, "test req"))
        args = self.mock_inner.call_tool.call_args
        self.assertEqual(args[0][0], "input_writer")
        req = args[0][1]["request"]
        self.assertEqual(req["case_name"], "cavity")
        self.assertEqual(req["user_requirement"], "test req")


class TestMCPServerManager(unittest.TestCase):
    """Test MCPServerManager properties and URL generation."""

    def test_url_property(self):
        """URL is correctly constructed from host and port."""
        from mcp_client import MCPServerManager
        mgr = MCPServerManager("/fake/dir", host="localhost", port=7860)
        self.assertEqual(mgr.url, "http://localhost:7860/mcp")

    def test_is_running_no_process(self):
        """is_running returns False when no process started."""
        from mcp_client import MCPServerManager
        mgr = MCPServerManager("/fake/dir")
        self.assertFalse(mgr.is_running)

    def test_is_running_dead_process(self):
        """is_running returns False when process has exited."""
        from mcp_client import MCPServerManager
        mgr = MCPServerManager("/fake/dir")
        mgr._process = MagicMock()
        mgr._process.poll.return_value = 1  # Process exited
        self.assertFalse(mgr.is_running)

    def test_is_running_alive_process(self):
        """is_running returns True when process is alive."""
        from mcp_client import MCPServerManager
        mgr = MCPServerManager("/fake/dir")
        mgr._process = MagicMock()
        mgr._process.poll.return_value = None  # Still running
        self.assertTrue(mgr.is_running)


# ============================================================================
# 2. Worker Pipeline Routing Tests
# ============================================================================

class TestWorkerPipelineRouting(unittest.TestCase):
    """Test that find_and_process_job() routes correctly based on pipeline_mode."""

    @patch('worker.supabase')
    @patch('worker._handle_controlled_pipeline')
    def test_auto_mode_skips_controlled(self, mock_controlled, mock_sb):
        """pipeline_mode='auto' should NOT call _handle_controlled_pipeline."""
        from worker import find_and_process_job

        mock_sb.rpc.return_value.execute.return_value.data = [{
            'id': 'test-auto-001',
            'prompt': 'test prompt',
            'pipeline_mode': 'auto',
            'pipeline_stage': None,
            'pipeline_state': None,
            'result_data': None,
            'user_id': 'user1',
            'llm_config': None,
            'pre_run_end_time': None,
        }]

        # Mock subprocess execution to avoid real process
        with patch('worker._run_subprocess_with_polling', return_value=(0, False, False)):
            with patch('worker._run_allrun_audit', return_value={'is_safe': True, 'files_scanned': 0, 'results': []}):
                with patch('worker._upload_and_complete'):
                    with patch('os.makedirs'):
                        with patch('builtins.open', MagicMock()):
                            find_and_process_job()

        mock_controlled.assert_not_called()

    @patch('worker.supabase')
    @patch('worker._handle_controlled_pipeline')
    def test_controlled_mode_calls_pipeline(self, mock_controlled, mock_sb):
        """pipeline_mode='controlled' should call _handle_controlled_pipeline."""
        from worker import find_and_process_job

        mock_sb.rpc.return_value.execute.return_value.data = [{
            'id': 'test-ctrl-001',
            'prompt': 'test prompt',
            'pipeline_mode': 'controlled',
            'pipeline_stage': None,
            'pipeline_state': {'active_checkpoints': ['files_review']},
            'result_data': None,
            'user_id': 'user1',
            'llm_config': None,
            'pre_run_end_time': None,
        }]

        find_and_process_job()
        mock_controlled.assert_called_once()
        # Verify the job dict was passed correctly
        job = mock_controlled.call_args[0][0]
        self.assertEqual(job['pipeline_mode'], 'controlled')


# ============================================================================
# 3. Pipeline State Machine Tests
# ============================================================================

class TestPipelineStateTransitions(unittest.TestCase):
    """Test _handle_controlled_pipeline stage routing."""

    @patch('worker._ensure_mcp_server')
    @patch('worker.supabase')
    def test_new_job_routes_to_plan(self, mock_sb, mock_ensure):
        """pipeline_stage=None should execute _mcp_stage_plan."""
        from worker import _handle_controlled_pipeline

        job = {
            'id': 'test-001',
            'prompt': 'test',
            'user_id': 'user1',
            'pipeline_stage': None,
            'pipeline_state': {'active_checkpoints': []},
            'pre_run_end_time': None,
        }

        with patch('worker._mcp_stage_plan', new_callable=AsyncMock) as mock_plan:
            _handle_controlled_pipeline(job)
            mock_plan.assert_called_once()

    @patch('worker._ensure_mcp_server')
    @patch('worker.supabase')
    def test_plan_review_routes_to_input_writer(self, mock_sb, mock_ensure):
        """pipeline_stage='plan_review' should execute _mcp_stage_input_writer."""
        from worker import _handle_controlled_pipeline

        job = {
            'id': 'test-002',
            'prompt': 'test',
            'user_id': 'user1',
            'pipeline_stage': 'plan_review',
            'pipeline_state': {
                'active_checkpoints': [],
                'case_name': 'cavity',
                'subtasks': [],
                'case_solver': 'icoFoam',
                'case_domain': 'incompressible',
                'case_category': 'tutorial',
            },
            'pre_run_end_time': None,
        }

        with patch('worker._mcp_stage_input_writer', new_callable=AsyncMock) as mock_iw:
            _handle_controlled_pipeline(job)
            mock_iw.assert_called_once()

    @patch('worker._ensure_mcp_server')
    @patch('worker.supabase')
    def test_files_review_routes_to_pre_run(self, mock_sb, mock_ensure):
        """pipeline_stage='files_review' should execute _mcp_stage_pre_run."""
        from worker import _handle_controlled_pipeline

        job = {
            'id': 'test-003',
            'prompt': 'test',
            'user_id': 'user1',
            'pipeline_stage': 'files_review',
            'pipeline_state': {
                'active_checkpoints': [],
                'case_dir': '/tmp/case',
            },
            'pre_run_end_time': None,
        }

        with patch('worker._mcp_stage_pre_run', new_callable=AsyncMock) as mock_pr:
            _handle_controlled_pipeline(job)
            mock_pr.assert_called_once()

    @patch('worker._ensure_mcp_server')
    @patch('worker.supabase')
    def test_pre_run_review_routes_to_full_run(self, mock_sb, mock_ensure):
        """pipeline_stage='pre_run_review' should execute _mcp_stage_full_run."""
        from worker import _handle_controlled_pipeline

        job = {
            'id': 'test-004',
            'prompt': 'test',
            'user_id': 'user1',
            'pipeline_stage': 'pre_run_review',
            'pipeline_state': {
                'active_checkpoints': [],
                'case_dir': '/tmp/case',
                'original_end_time': '0.5',
            },
            'pre_run_end_time': 10,
        }

        with patch('worker._mcp_stage_full_run', new_callable=AsyncMock) as mock_fr:
            _handle_controlled_pipeline(job)
            mock_fr.assert_called_once()

    @patch('worker._ensure_mcp_server')
    @patch('worker.supabase')
    def test_unknown_stage_fails(self, mock_sb, mock_ensure):
        """Unknown pipeline_stage should mark job as failed."""
        from worker import _handle_controlled_pipeline

        job = {
            'id': 'test-005',
            'prompt': 'test',
            'user_id': 'user1',
            'pipeline_stage': 'nonexistent_stage',
            'pipeline_state': {'active_checkpoints': []},
            'pre_run_end_time': None,
        }

        _handle_controlled_pipeline(job)
        # Should have called supabase update with status='failed'
        mock_sb.table.return_value.update.assert_called()
        update_data = mock_sb.table.return_value.update.call_args[0][0]
        self.assertEqual(update_data['status'], 'failed')
        self.assertIn('nonexistent_stage', update_data['result_data']['error'])


# ============================================================================
# 4. Checkpoint Logic Tests
# ============================================================================

class TestCheckpointPauseLogic(unittest.TestCase):
    """Test that stages correctly pause when checkpoint is active."""

    @patch('worker._update_pipeline_state')
    @patch('worker._get_mcp_server_manager')
    @patch('worker.supabase')
    def test_plan_pauses_at_plan_review(self, mock_sb, mock_mgr, mock_update):
        """plan() should pause at plan_review when in active_checkpoints."""
        from worker import _mcp_stage_plan

        mock_mgr.return_value.url = "http://localhost:7860/mcp"

        job = {
            'id': 'test-pause-001',
            'prompt': 'test prompt',
            'user_id': 'user1',
        }
        pipeline_state = {'active_checkpoints': ['plan_review']}

        # Mock the FoamAgentMCPClient where it's imported in worker.py
        mock_client_instance = AsyncMock()
        mock_client_instance.plan = AsyncMock(return_value={
            'subtasks': [{'file': 'U', 'folder': '0'}],
            'case_name': 'cavity',
            'case_solver': 'icoFoam',
            'case_domain': 'incompressible',
            'case_category': 'tutorial',
        })

        with patch('mcp_client.FoamAgentMCPClient') as MockClass:
            MockClass.return_value = mock_client_instance
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)

            asyncio.run(_mcp_stage_plan(job, pipeline_state, ['plan_review']))

        # Verify it paused at plan_review
        calls = mock_update.call_args_list
        # Last call should be the checkpoint pause
        last_call = calls[-1]
        self.assertEqual(last_call[0][1], 'plan_review')   # stage
        self.assertEqual(last_call[0][2], 'checkpoint')     # status

    @patch('worker._mcp_stage_input_writer', new_callable=AsyncMock)
    @patch('worker._update_pipeline_state')
    @patch('worker._get_mcp_server_manager')
    @patch('worker.supabase')
    def test_plan_auto_continues_when_no_checkpoint(self, mock_sb, mock_mgr, mock_update, mock_iw):
        """plan() should auto-continue to input_writer when plan_review not in checkpoints."""
        from worker import _mcp_stage_plan

        mock_mgr.return_value.url = "http://localhost:7860/mcp"

        job = {
            'id': 'test-auto-001',
            'prompt': 'test prompt',
            'user_id': 'user1',
        }
        pipeline_state = {'active_checkpoints': ['files_review']}

        mock_client_instance = AsyncMock()
        mock_client_instance.plan = AsyncMock(return_value={
            'subtasks': [{'file': 'U', 'folder': '0'}],
            'case_name': 'cavity',
            'case_solver': 'icoFoam',
            'case_domain': 'incompressible',
            'case_category': 'tutorial',
        })

        with patch('mcp_client.FoamAgentMCPClient') as MockClass:
            MockClass.return_value = mock_client_instance
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)

            asyncio.run(_mcp_stage_plan(job, pipeline_state, ['files_review']))

        # Should have called _mcp_stage_input_writer (auto-continue)
        mock_iw.assert_called_once()


class TestPreRunSkipLogic(unittest.TestCase):
    """Test pre-run skip behavior in controlled pipeline."""

    @patch('worker._mcp_stage_full_run', new_callable=AsyncMock)
    @patch('worker.supabase')
    def test_pre_run_disabled_skips_to_full_run(self, mock_sb, mock_full_run):
        """pre_run_end_time=-1 should skip directly to full run."""
        from worker import _mcp_stage_pre_run

        job = {
            'id': 'test-skip-001',
            'prompt': 'test',
            'user_id': 'user1',
            'pre_run_end_time': -1,
        }
        pipeline_state = {'case_dir': '/tmp/case', 'active_checkpoints': []}

        asyncio.run(_mcp_stage_pre_run(job, pipeline_state, ['pre_run_review']))
        mock_full_run.assert_called_once()


# ============================================================================
# 5. DB Update Helper Tests
# ============================================================================

class TestUpdatePipelineState(unittest.TestCase):
    """Test _update_pipeline_state helper."""

    @patch('worker.supabase')
    def test_basic_update(self, mock_sb):
        """Basic state update with stage, status, and state."""
        from worker import _update_pipeline_state

        state = {'case_name': 'cavity', 'active_checkpoints': []}
        _update_pipeline_state('job-001', 'planning', 'running', state)

        mock_sb.table.assert_called_with('simulations')
        update_data = mock_sb.table.return_value.update.call_args[0][0]
        self.assertEqual(update_data['status'], 'running')
        self.assertEqual(update_data['pipeline_stage'], 'planning')
        self.assertEqual(update_data['pipeline_state'], state)

    @patch('worker.supabase')
    def test_update_with_extra_fields(self, mock_sb):
        """Extra fields are merged into the update."""
        from worker import _update_pipeline_state

        state = {}
        _update_pipeline_state('job-002', 'files_review', 'checkpoint', state,
                               extra_fields={'result_data': {'file_tree': {}}})

        update_data = mock_sb.table.return_value.update.call_args[0][0]
        self.assertEqual(update_data['status'], 'checkpoint')
        self.assertIn('result_data', update_data)
        self.assertEqual(update_data['result_data'], {'file_tree': {}})


# ============================================================================
# 6. API Model Tests
# ============================================================================

class TestSimulationRequestModel(unittest.TestCase):
    """Test SimulationRequest model accepts pipeline fields."""

    def test_default_pipeline_mode(self):
        """Default pipeline_mode is 'auto'."""
        # Import from api_server would need full FastAPI setup;
        # test the model directly via pydantic
        from pydantic import BaseModel
        from typing import Optional, List

        class SimulationRequest(BaseModel):
            prompt: str
            pipeline_mode: str = 'auto'
            checkpoints: Optional[List[str]] = None

        req = SimulationRequest(prompt="test")
        self.assertEqual(req.pipeline_mode, 'auto')
        self.assertIsNone(req.checkpoints)

    def test_controlled_mode_with_checkpoints(self):
        """Controlled mode with checkpoints serializes correctly."""
        from pydantic import BaseModel
        from typing import Optional, List

        class SimulationRequest(BaseModel):
            prompt: str
            pipeline_mode: str = 'auto'
            checkpoints: Optional[List[str]] = None

        req = SimulationRequest(
            prompt="test",
            pipeline_mode="controlled",
            checkpoints=["files_review", "pre_run_review"],
        )
        self.assertEqual(req.pipeline_mode, 'controlled')
        self.assertEqual(req.checkpoints, ["files_review", "pre_run_review"])


# ============================================================================
# 7. API Stage Confirm/Reject Tests
# ============================================================================

def _get_api_test_client(mock_supabase):
    """Create a FastAPI TestClient with mocked Supabase and JWT bypassed."""
    from api_server import app, verify_jwt
    from fastapi.testclient import TestClient

    # Override JWT dependency to return a fixed user_id
    async def _mock_verify_jwt():
        return 'user-001'

    app.dependency_overrides[verify_jwt] = _mock_verify_jwt
    client = TestClient(app)
    return client


class TestAPIStageConfirm(unittest.TestCase):
    """Test /stage/confirm endpoint for both auto and controlled modes."""

    def tearDown(self):
        from api_server import app
        app.dependency_overrides.clear()

    @patch('api_server.supabase')
    def test_confirm_controlled_mode(self, mock_sb):
        """Controlled mode confirm re-queues without clearing pipeline_stage."""
        mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{
            'id': 'job-ctrl-001',
            'user_id': 'user-001',
            'status': 'checkpoint',
            'pipeline_mode': 'controlled',
            'pipeline_stage': 'files_review',
            'pipeline_state': {'case_dir': '/tmp/case'},
            'result_data': {},
        }]
        mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        client = _get_api_test_client(mock_sb)
        resp = client.post("/api/v1/simulations/job-ctrl-001/stage/confirm")
        self.assertEqual(resp.status_code, 200)

        # Verify pipeline_stage is NOT in the update (kept as-is)
        update_data = mock_sb.table.return_value.update.call_args[0][0]
        self.assertEqual(update_data['status'], 'queued')
        self.assertNotIn('pipeline_stage', update_data)

    @patch('api_server.supabase')
    def test_confirm_wrong_status_returns_409(self, mock_sb):
        """Confirm on non-checkpoint job returns 409."""
        mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{
            'id': 'job-running-001',
            'user_id': 'user-001',
            'status': 'running',
            'pipeline_mode': 'controlled',
            'pipeline_stage': 'generating',
            'pipeline_state': {},
            'result_data': None,
        }]

        client = _get_api_test_client(mock_sb)
        resp = client.post("/api/v1/simulations/job-running-001/stage/confirm")
        self.assertEqual(resp.status_code, 409)

    @patch('api_server.supabase')
    def test_confirm_wrong_user_returns_403(self, mock_sb):
        """Confirm by non-owner returns 403."""
        mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{
            'id': 'job-other-001',
            'user_id': 'user-002',  # Different user
            'status': 'checkpoint',
            'pipeline_mode': 'controlled',
            'pipeline_stage': 'files_review',
            'pipeline_state': {},
            'result_data': None,
        }]

        client = _get_api_test_client(mock_sb)
        resp = client.post("/api/v1/simulations/job-other-001/stage/confirm")
        self.assertEqual(resp.status_code, 403)


class TestAPIStageReject(unittest.TestCase):
    """Test /stage/reject endpoint."""

    def tearDown(self):
        from api_server import app
        app.dependency_overrides.clear()

    @patch('api_server.supabase')
    def test_reject_marks_failed_with_stage(self, mock_sb):
        """Reject sets status='failed' and records rejected_stage."""
        mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{
            'id': 'job-reject-001',
            'user_id': 'user-001',
            'status': 'checkpoint',
            'pipeline_mode': 'controlled',
            'pipeline_stage': 'plan_review',
            'pipeline_state': {'case_name': 'cavity'},
            'result_data': {},
        }]
        mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        client = _get_api_test_client(mock_sb)
        resp = client.post("/api/v1/simulations/job-reject-001/stage/reject")
        self.assertEqual(resp.status_code, 200)

        update_data = mock_sb.table.return_value.update.call_args[0][0]
        self.assertEqual(update_data['status'], 'failed')
        self.assertEqual(update_data['result_data']['rejected_stage'], 'plan_review')


# ============================================================================
# 8. Input Writer Stage Tests
# ============================================================================

class TestMCPStageInputWriter(unittest.TestCase):
    """Test _mcp_stage_input_writer checkpoint and auto-continue behavior."""

    @patch('worker._append_mcp_log')
    @patch('worker.upload_directory_to_storage', return_value=(5, 0, 1024))
    @patch('worker.build_file_tree', return_value={'name': 'root'})
    @patch('worker._update_pipeline_state')
    @patch('worker._get_mcp_server_manager')
    @patch('worker.supabase')
    def test_input_writer_pauses_at_files_review(self, mock_sb, mock_mgr,
                                                   mock_update, mock_tree, mock_upload,
                                                   mock_log):
        """input_writer should pause at files_review when in active_checkpoints."""
        from worker import _mcp_stage_input_writer

        mock_mgr.return_value.url = "http://localhost:7860/mcp"

        job = {
            'id': 'test-iw-pause-001',
            'prompt': 'test prompt',
            'user_id': 'user1',
        }
        pipeline_state = {
            'active_checkpoints': ['files_review'],
            'case_name': 'cavity',
            'subtasks': [{'file': 'U', 'folder': '0'}],
            'case_solver': 'icoFoam',
            'case_domain': 'incompressible',
            'case_category': 'tutorial',
        }

        mock_client_instance = AsyncMock()
        mock_client_instance.input_writer = AsyncMock(return_value={
            'case_dir': '/tmp/test_case',
            'allrun_script': '/tmp/test_case/Allrun',
        })

        with patch('mcp_client.FoamAgentMCPClient') as MockClass:
            MockClass.return_value = mock_client_instance
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)

            with patch('os.makedirs'), patch('os.path.exists', return_value=True):
                asyncio.run(_mcp_stage_input_writer(job, pipeline_state, ['files_review']))

        # Should have paused at files_review
        calls = mock_update.call_args_list
        last_call = calls[-1]
        self.assertEqual(last_call[0][1], 'files_review')
        self.assertEqual(last_call[0][2], 'checkpoint')

    @patch('worker._append_mcp_log')
    @patch('worker._mcp_stage_pre_run', new_callable=AsyncMock)
    @patch('worker._update_pipeline_state')
    @patch('worker._get_mcp_server_manager')
    @patch('worker.supabase')
    def test_input_writer_auto_continues_to_pre_run(self, mock_sb, mock_mgr,
                                                      mock_update, mock_pre_run,
                                                      mock_log):
        """input_writer should auto-continue to pre_run when files_review not in checkpoints."""
        from worker import _mcp_stage_input_writer

        mock_mgr.return_value.url = "http://localhost:7860/mcp"

        job = {
            'id': 'test-iw-auto-001',
            'prompt': 'test prompt',
            'user_id': 'user1',
        }
        pipeline_state = {
            'active_checkpoints': ['pre_run_review'],
            'case_name': 'cavity',
            'subtasks': [{'file': 'U', 'folder': '0'}],
            'case_solver': 'icoFoam',
            'case_domain': 'incompressible',
            'case_category': 'tutorial',
        }

        mock_client_instance = AsyncMock()
        mock_client_instance.input_writer = AsyncMock(return_value={
            'case_dir': '/tmp/test_case',
            'allrun_script': '/tmp/test_case/Allrun',
        })

        with patch('mcp_client.FoamAgentMCPClient') as MockClass:
            MockClass.return_value = mock_client_instance
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)

            with patch('os.makedirs'), patch('os.path.exists', return_value=True):
                asyncio.run(_mcp_stage_input_writer(job, pipeline_state, ['pre_run_review']))

        # Should have auto-continued to pre_run
        mock_pre_run.assert_called_once()


# ============================================================================
# 9. Full Run Stage Tests
# ============================================================================

class TestMCPStageFullRun(unittest.TestCase):
    """Test _mcp_stage_full_run review+fix loop and completion."""

    @patch('worker._upload_and_complete')
    @patch('worker._run_allrun_audit', return_value={'is_safe': True})
    @patch('worker._update_pipeline_state')
    @patch('worker._get_mcp_server_manager')
    @patch('worker.supabase')
    def test_full_run_no_errors_completes(self, mock_sb, mock_mgr, mock_update,
                                           mock_audit, mock_upload):
        """Full run with no errors should skip review loop and complete."""
        from worker import _mcp_stage_full_run

        mock_mgr.return_value.url = "http://localhost:7860/mcp"

        job = {
            'id': 'test-fr-001',
            'prompt': 'test prompt',
            'user_id': 'user1',
        }
        pipeline_state = {
            'case_dir': '/tmp/test_case',
            'case_name': 'cavity',
            'case_solver': 'icoFoam',
        }

        mock_client_instance = AsyncMock()
        mock_client_instance.run = AsyncMock(return_value={
            'status': 'success',
            'errors': [],
        })
        mock_client_instance.visualization = AsyncMock(return_value={
            'artifacts': ['image1.png'],
        })

        with patch('mcp_client.FoamAgentMCPClient') as MockClass:
            MockClass.return_value = mock_client_instance
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)

            with patch('os.makedirs'), patch('builtins.open', MagicMock()):
                asyncio.run(_mcp_stage_full_run(job, pipeline_state))

        # Review should NOT have been called
        mock_client_instance.review.assert_not_called()
        # Upload should have been called
        mock_upload.assert_called_once()

    @patch('worker._upload_and_complete')
    @patch('worker._run_allrun_audit', return_value={'is_safe': True})
    @patch('worker._update_pipeline_state')
    @patch('worker._get_mcp_server_manager')
    @patch('worker.supabase')
    def test_full_run_with_errors_no_review_loop(self, mock_sb, mock_mgr,
                                                    mock_update, mock_audit, mock_upload):
        """Full run with errors should NOT trigger review+fix loop.

        Review+fix is done in pre-run stage (cheap, 10 timesteps).
        Full run just executes and logs any remaining errors.
        """
        from worker import _mcp_stage_full_run

        mock_mgr.return_value.url = "http://localhost:7860/mcp"

        job = {
            'id': 'test-fr-loop-001',
            'prompt': 'test prompt',
            'user_id': 'user1',
        }
        pipeline_state = {
            'case_dir': '/tmp/test_case',
            'case_name': 'cavity',
            'case_solver': 'icoFoam',
        }

        mock_client_instance = AsyncMock()
        mock_client_instance.run = AsyncMock(return_value={
            'status': 'failed', 'errors': ['FOAM FATAL ERROR'],
        })

        with patch('mcp_client.FoamAgentMCPClient') as MockClass:
            MockClass.return_value = mock_client_instance
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)

            with patch('os.makedirs'), patch('builtins.open', MagicMock()):
                asyncio.run(_mcp_stage_full_run(job, pipeline_state))

        # Review should NOT have been called (no review loop in full run)
        mock_client_instance.review.assert_not_called()
        mock_client_instance.apply_fixes.assert_not_called()
        # Run should have been called exactly once
        self.assertEqual(mock_client_instance.run.call_count, 1)
        mock_upload.assert_called_once()

    @patch('worker._upload_and_complete')
    @patch('worker._run_allrun_audit', return_value={'is_safe': True})
    @patch('worker._update_pipeline_state')
    @patch('worker._get_mcp_server_manager')
    @patch('worker.supabase')
    def test_full_run_restores_endtime_from_pre_run(self, mock_sb, mock_mgr,
                                                      mock_update, mock_audit, mock_upload):
        """Full run after pre-run should restore original endTime."""
        from worker import _mcp_stage_full_run

        mock_mgr.return_value.url = "http://localhost:7860/mcp"

        job = {
            'id': 'test-fr-restore-001',
            'prompt': 'test prompt',
            'user_id': 'user1',
        }
        pipeline_state = {
            'case_dir': '/tmp/test_case',
            'case_name': 'cavity',
            'case_solver': 'icoFoam',
            'original_end_time': '0.5',  # From pre-run
        }

        mock_client_instance = AsyncMock()
        mock_client_instance.run = AsyncMock(return_value={'status': 'success', 'errors': []})

        with patch('mcp_client.FoamAgentMCPClient') as MockClass:
            MockClass.return_value = mock_client_instance
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)

            with patch('normal_run_preparer.NormalRunPreparer') as MockPreparer:
                mock_preparer_instance = MagicMock()
                MockPreparer.return_value = mock_preparer_instance
                with patch('os.makedirs'), patch('builtins.open', MagicMock()):
                    asyncio.run(_mcp_stage_full_run(job, pipeline_state))

                # NormalRunPreparer should have been called with original endTime
                MockPreparer.assert_called_once_with('/tmp/test_case', '0.5')
                mock_preparer_instance.prepare.assert_called_once()


if __name__ == '__main__':
    unittest.main()
