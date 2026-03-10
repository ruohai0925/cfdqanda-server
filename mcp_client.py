"""Foam-Agent MCP Client for controlled pipeline mode.

Wraps the FastMCP Client to call Foam-Agent's 6 MCP tools
(plan, input_writer, run, review, apply_fixes, visualization)
via HTTP/JSON-RPC 2.0.

Usage:
    client = FoamAgentMCPClient("http://localhost:7860/mcp")
    async with client:
        plan = await client.plan("Simulate lid-driven cavity flow")
        files = await client.input_writer(plan)
        run_result = await client.run(files["case_dir"])
"""

import asyncio
import logging
import os
import signal
import subprocess
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# fastmcp.Client is imported lazily in FoamAgentMCPClient.__init__
# because it is only available in the FoamAgent conda environment,
# not in the foam-api environment used for testing.


class MCPServerManager:
    """Manages the lifecycle of the Foam-Agent MCP server process."""

    def __init__(
        self,
        foam_agent_dir: str,
        host: str = "localhost",
        port: int = 7860,
        conda_env: str = "FoamAgent",
    ):
        self.foam_agent_dir = foam_agent_dir
        self.host = host
        self.port = port
        self.conda_env = conda_env
        self._process: Optional[subprocess.Popen] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    def start(self, timeout: float = 30.0) -> None:
        """Start MCP server as a subprocess and wait until ready."""
        if self._process and self._process.poll() is None:
            logger.info("MCP server already running (pid=%d)", self._process.pid)
            return

        # Use bash -c to cd into Foam-Agent dir before launching,
        # because conda run doesn't always honor Popen's cwd.
        server_cmd = (
            f"cd {self.foam_agent_dir} && "
            f"conda run -n {self.conda_env} "
            f"python -m src.mcp.fastmcp_server "
            f"--transport http --host {self.host} --port {self.port}"
        )

        # Build sanitized env — exclude server-side secrets from subprocess
        from worker import _build_subprocess_env
        clean_env = _build_subprocess_env()

        logger.info("Starting MCP server: %s", server_cmd)
        self._process = subprocess.Popen(
            ["bash", "-c", server_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
            env=clean_env,
        )

        # Wait for server to be ready by trying a TCP connection
        import socket

        start_time = time.time()
        while time.time() - start_time < timeout:
            # Check process hasn't died
            if self._process.poll() is not None:
                stdout = self._process.stdout.read().decode() if self._process.stdout else ""
                raise RuntimeError(
                    f"MCP server exited with code {self._process.returncode}. "
                    f"Output:\n{stdout[:2000]}"
                )
            try:
                sock = socket.create_connection((self.host, self.port), timeout=1)
                sock.close()
                logger.info("MCP server ready on port %d (pid=%d)",
                            self.port, self._process.pid)
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)

        # Timeout — kill and raise
        self.stop()
        raise TimeoutError(
            f"MCP server did not become ready within {timeout}s"
        )

    def stop(self) -> None:
        """Stop the MCP server process."""
        if self._process and self._process.poll() is None:
            logger.info("Stopping MCP server (pid=%d)", self._process.pid)
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                self._process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self._process = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None


class FoamAgentMCPClient:
    """High-level async client for Foam-Agent MCP tools.

    Wraps fastmcp.Client with typed methods for each MCP tool.
    Can optionally manage the MCP server lifecycle.

    Usage:
        # With external MCP server (already running)
        client = FoamAgentMCPClient("http://localhost:7860/mcp")
        async with client:
            plan = await client.plan("Simulate cavity flow")

        # With auto-managed MCP server
        client = FoamAgentMCPClient.with_managed_server(
            foam_agent_dir="/path/to/Foam-Agent"
        )
        async with client:
            plan = await client.plan("Simulate cavity flow")
        # Server is stopped on exit
    """

    def __init__(self, url: str, server_manager: Optional[MCPServerManager] = None):
        from fastmcp import Client
        self._url = url
        self._client = Client(url)
        self._server_manager = server_manager

    @classmethod
    def with_managed_server(
        cls,
        foam_agent_dir: str,
        host: str = "localhost",
        port: int = 7860,
        conda_env: str = "FoamAgent",
    ) -> "FoamAgentMCPClient":
        """Create a client that auto-starts/stops the MCP server."""
        manager = MCPServerManager(
            foam_agent_dir=foam_agent_dir,
            host=host,
            port=port,
            conda_env=conda_env,
        )
        url = manager.url
        return cls(url=url, server_manager=manager)

    async def __aenter__(self) -> "FoamAgentMCPClient":
        if self._server_manager:
            self._server_manager.start()
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._client.__aexit__(exc_type, exc_val, exc_tb)
        if self._server_manager:
            self._server_manager.stop()

    def _parse_response(self, result) -> dict:
        """Extract dict from MCP tool call result."""
        if hasattr(result, 'structured_content') and result.structured_content:
            return result.structured_content
        if hasattr(result, 'data') and result.data:
            return result.data
        # Fallback: try to treat as dict
        if isinstance(result, dict):
            return result
        return {}

    # ------------------------------------------------------------------
    # Tool: plan
    # ------------------------------------------------------------------
    async def plan(self, user_requirement: str) -> Dict[str, Any]:
        """Call plan() — analyze requirements and generate subtasks.

        Returns:
            {
                "subtasks": [{"file": "...", "folder": "..."}, ...],
                "case_name": "lidDrivenCavity",
                "case_solver": "icoFoam",
                "case_domain": "incompressible",
                "case_category": "tutorial"
            }
        """
        logger.info("MCP plan(): user_requirement=%s...", user_requirement[:80])
        result = await self._client.call_tool(
            "plan",
            {"request": {"user_requirement": user_requirement}},
        )
        data = self._parse_response(result)
        logger.info(
            "MCP plan() returned: case_name=%s, solver=%s, %d subtasks",
            data.get("case_name"),
            data.get("case_solver"),
            len(data.get("subtasks", [])),
        )
        return data

    # ------------------------------------------------------------------
    # Tool: input_writer
    # ------------------------------------------------------------------
    async def input_writer(
        self,
        case_name: str,
        subtasks: List[Dict[str, str]],
        user_requirement: str,
        case_solver: str,
        case_domain: str,
        case_category: str,
    ) -> Dict[str, Any]:
        """Call input_writer() — generate OpenFOAM input files.

        Returns:
            {
                "case_dir": "/abs/path/to/case",
                "foamfiles": {...},
                "allrun_script": "/abs/path/to/Allrun"
            }
        """
        logger.info("MCP input_writer(): case=%s, %d subtasks", case_name, len(subtasks))
        result = await self._client.call_tool(
            "input_writer",
            {
                "request": {
                    "case_name": case_name,
                    "subtasks": subtasks,
                    "user_requirement": user_requirement,
                    "case_solver": case_solver,
                    "case_domain": case_domain,
                    "case_category": case_category,
                }
            },
        )
        data = self._parse_response(result)
        logger.info("MCP input_writer() returned: case_dir=%s", data.get("case_dir"))
        return data

    # ------------------------------------------------------------------
    # Tool: run
    # ------------------------------------------------------------------
    async def run(
        self,
        case_dir: str,
        timeout: int = 3600,
    ) -> Dict[str, Any]:
        """Call run() — execute Allrun script.

        Returns:
            {
                "status": "success" | "failed",
                "errors": [...],
                "log_files": {"Allrun.out": "...", ...}
            }
        """
        logger.info("MCP run(): case_dir=%s, timeout=%d", case_dir, timeout)
        result = await self._client.call_tool(
            "run",
            {"request": {"case_dir": case_dir, "timeout": timeout}},
        )
        data = self._parse_response(result)
        logger.info("MCP run() returned: status=%s, errors=%d",
                     data.get("status"), len(data.get("errors", [])))
        return data

    # ------------------------------------------------------------------
    # Tool: review
    # ------------------------------------------------------------------
    async def review(
        self,
        case_dir: str,
        errors: List[str],
        user_requirement: str,
    ) -> Dict[str, Any]:
        """Call review() — analyze simulation errors.

        Returns:
            {"analysis": "..."}
        """
        logger.info("MCP review(): case_dir=%s, %d errors", case_dir, len(errors))
        result = await self._client.call_tool(
            "review",
            {
                "request": {
                    "case_dir": case_dir,
                    "errors": errors,
                    "user_requirement": user_requirement,
                }
            },
        )
        data = self._parse_response(result)
        logger.info("MCP review() returned analysis (%d chars)",
                     len(data.get("analysis", "")))
        return data

    # ------------------------------------------------------------------
    # Tool: apply_fixes
    # ------------------------------------------------------------------
    async def apply_fixes(
        self,
        case_dir: str,
        error_logs: List[str],
        review_analysis: str,
        user_requirement: str,
    ) -> Dict[str, Any]:
        """Call apply_fixes() — rewrite files based on review analysis.

        Returns:
            {
                "updated_files": ["path1", "path2", ...],
                "status": "ok" | "no_changes"
            }
        """
        logger.info("MCP apply_fixes(): case_dir=%s", case_dir)
        result = await self._client.call_tool(
            "apply_fixes",
            {
                "request": {
                    "case_dir": case_dir,
                    "error_logs": error_logs,
                    "review_analysis": review_analysis,
                    "user_requirement": user_requirement,
                }
            },
        )
        data = self._parse_response(result)
        logger.info("MCP apply_fixes() returned: status=%s, %d files updated",
                     data.get("status"), len(data.get("updated_files", [])))
        return data

    # ------------------------------------------------------------------
    # Tool: visualization
    # ------------------------------------------------------------------
    async def visualization(
        self,
        case_dir: str,
        quantity: str = "velocity",
        visualization_type: str = "pyvista",
    ) -> Dict[str, Any]:
        """Call visualization() — generate PyVista visualization.

        Returns:
            {
                "artifacts": ["path_to_image", ...],
                "script": "..."
            }
        """
        logger.info("MCP visualization(): case_dir=%s, quantity=%s", case_dir, quantity)
        result = await self._client.call_tool(
            "visualization",
            {
                "request": {
                    "case_dir": case_dir,
                    "quantity": quantity,
                    "visualization_type": visualization_type,
                }
            },
        )
        data = self._parse_response(result)
        logger.info("MCP visualization() returned: %d artifacts",
                     len(data.get("artifacts", [])))
        return data

    # ------------------------------------------------------------------
    # Convenience: input_writer from plan result
    # ------------------------------------------------------------------
    async def input_writer_from_plan(
        self,
        plan_result: Dict[str, Any],
        user_requirement: str,
    ) -> Dict[str, Any]:
        """Convenience: call input_writer using plan() output directly."""
        return await self.input_writer(
            case_name=plan_result["case_name"],
            subtasks=plan_result["subtasks"],
            user_requirement=user_requirement,
            case_solver=plan_result["case_solver"],
            case_domain=plan_result["case_domain"],
            case_category=plan_result["case_category"],
        )
