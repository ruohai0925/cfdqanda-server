import os
import re
import sys
import time
import logging
import subprocess
import signal
import json
import shutil
import threading
import fnmatch
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from supabase import create_client, Client
from allrun_validator import audit_allrun_scripts
from token_extractor import extract_token_usage

# --- 1. 初始化与配置 ---

# 配置日志，方便我们观察 Worker 的一举一动
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 新增这两行 ---
from dotenv import load_dotenv
load_dotenv()  # 自动读取同目录下的 .env 文件
# ------------------

# --- Worker identification (for multi-worker setups) ---
WORKER_ID = os.environ.get("WORKER_ID", f"worker-{os.getpid()}")

# --- Foam-Agent 目录配置 ---
FOAM_AGENT_DIR = os.environ.get("FOAM_AGENT_DIR")
if not FOAM_AGENT_DIR:
    logger.error("FATAL: FOAM_AGENT_DIR is not set. Set it in .env to point to the Foam-Agent directory.")
    raise RuntimeError("FOAM_AGENT_DIR is not set in the environment variables.")
FOAM_AGENT_DIR = os.path.abspath(FOAM_AGENT_DIR)
if not os.path.isdir(FOAM_AGENT_DIR):
    logger.error(f"FATAL: FOAM_AGENT_DIR={FOAM_AGENT_DIR} does not exist or is not a directory.")
    raise RuntimeError(f"FOAM_AGENT_DIR={FOAM_AGENT_DIR} does not exist.")
logger.info(f"FOAM_AGENT_DIR resolved to: {FOAM_AGENT_DIR}")

# Simulation subprocess timeout (seconds). Default: 3600 (1 hour).
SIMULATION_TIMEOUT = int(os.environ.get("SIMULATION_TIMEOUT", "3600"))
logger.info(f"Simulation timeout set to {SIMULATION_TIMEOUT} seconds")

# Stale job recovery threshold (seconds). Default: 7200 (2 hours).
# Jobs stuck in 'running' longer than this are reset to 'queued' on Worker startup.
STALE_JOB_THRESHOLD = int(os.environ.get("STALE_JOB_THRESHOLD", "7200"))
logger.info(f"Stale job threshold set to {STALE_JOB_THRESHOLD} seconds")

# How often (seconds) to check DB for cancellation while subprocess is running.
CANCEL_CHECK_INTERVAL = int(os.environ.get("CANCEL_CHECK_INTERVAL", "5"))
logger.info(f"Cancel check interval set to {CANCEL_CHECK_INTERVAL} seconds")

# Pre-run timeout (seconds). Default: 300 (5 minutes). Much shorter than full simulation.
PRE_RUN_TIMEOUT = int(os.environ.get("PRE_RUN_TIMEOUT", "300"))
logger.info(f"Pre-run timeout set to {PRE_RUN_TIMEOUT} seconds")

# Max disk usage per task (bytes). Default: 400 MB. Task is killed if exceeded.
TASK_DISK_LIMIT_BYTES = int(os.environ.get("TASK_DISK_LIMIT_MB", "400")) * 1024 * 1024
logger.info(f"Task disk limit set to {TASK_DISK_LIMIT_BYTES // (1024*1024)} MB")

# How often (seconds) to check disk usage during subprocess run.
DISK_CHECK_INTERVAL = int(os.environ.get("DISK_CHECK_INTERVAL", "30"))
logger.info(f"Disk check interval set to {DISK_CHECK_INTERVAL} seconds")

# Health check HTTP server port. Set to 0 to disable.
HEALTH_CHECK_PORT = int(os.environ.get("HEALTH_CHECK_PORT", "8001"))

# --- Controlled pipeline: exclusive worker lock ---
# When a worker is bound to a controlled-pipeline job, it must not claim
# other jobs until that job completes/fails/is cancelled.
# Stores the job ID (int/str) or None.
_bound_pipeline_job_id = None
# Timestamp (UTC) when the bound job entered its current checkpoint.
_bound_checkpoint_since = None
# How long (seconds) to wait for user confirmation before auto-failing.
CHECKPOINT_TIMEOUT = int(os.environ.get("CHECKPOINT_TIMEOUT", "1800"))  # 30 min

# --- Middleware directory configuration ---
MIDDLEWARE_DIR = os.environ.get("MIDDLEWARE_DIR")
if MIDDLEWARE_DIR:
    MIDDLEWARE_DIR = os.path.abspath(MIDDLEWARE_DIR)
    pre_run_module_path = os.path.join(MIDDLEWARE_DIR, "Foam-Agent", "pre-run")
    if os.path.isdir(pre_run_module_path):
        sys.path.insert(0, pre_run_module_path)
        logger.info(f"Middleware pre-run path added to sys.path: {pre_run_module_path}")
    else:
        logger.warning(f"MIDDLEWARE_DIR set but pre-run path not found: {pre_run_module_path}")
else:
    logger.info("MIDDLEWARE_DIR not set, checkpoint pre-run disabled.")

# 从环境变量加载 Supabase 配置
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    logger.error("FATAL: Supabase credentials are not set in the environment variables.")
    raise RuntimeError("Supabase credentials are not set in the environment variables.")

logger.info("Initializing Supabase client for Worker...")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
logger.info(f"Supabase client initialized successfully. WORKER_ID={WORKER_ID}")

# --- Security: subprocess environment and log sanitization ---

# Server-side env vars that must NOT be passed to Foam-Agent subprocess.
# The subprocess only needs: system vars, OpenFOAM vars, and LLM credentials.
_SUBPROCESS_ENV_BLOCKLIST = frozenset({
    'SUPABASE_URL',
    'SUPABASE_SERVICE_KEY',
    'SUPABASE_JWT_SECRET',
    'EXTRA_CORS_ORIGINS',
    'WORKER_ID',
    'HEALTH_CHECK_PORT',
    'WORKER_HEALTH_URL',
    'USER_STORAGE_LIMIT_MB',
    'USER_DAILY_TASK_LIMIT',
    'FOAM_AGENT_HOST_PATH',
    'MIDDLEWARE_HOST_PATH',
    'MIDDLEWARE_DIR',
    'SIMULATION_TIMEOUT',
    'STALE_JOB_THRESHOLD',
    'TASK_DISK_LIMIT_MB',
    'DISK_CHECK_INTERVAL',
    'MCP_SERVER_PORT',
})

# Patterns to redact from simulation.log before uploading to user-accessible storage
_SENSITIVE_LOG_PATTERNS = [
    (re.compile(r'sk-proj-[A-Za-z0-9_-]{20,}'), '[REDACTED_OPENAI_KEY]'),
    (re.compile(r'sk-ant-[A-Za-z0-9_-]{20,}'), '[REDACTED_ANTHROPIC_KEY]'),
    # Generic sk- keys (OpenAI/DeepSeek format, 30+ chars to catch all variants)
    (re.compile(r'(?<![A-Za-z0-9_-])sk-[A-Za-z0-9]{30,}'), '[REDACTED_API_KEY]'),
    # JWT tokens (three dot-separated base64 segments, e.g. Supabase service key)
    (re.compile(r'eyJ[A-Za-z0-9_/+-]{50,}\.[A-Za-z0-9_/+-]{50,}\.[A-Za-z0-9_/+-]{20,}'),
     '[REDACTED_TOKEN]'),
]


def _build_subprocess_env():
    """Build a sanitized environment dict for simulation subprocesses.

    Starts from the current process environment and removes server-side secrets
    (Supabase credentials, worker config, etc.) so that the subprocess cannot
    read or leak them.
    """
    return {k: v for k, v in os.environ.items() if k not in _SUBPROCESS_ENV_BLOCKLIST}


def _sanitize_log_file(log_path):
    """Redact sensitive patterns (API keys, JWT tokens) from a log file in-place.

    Called before uploading simulation.log to user-accessible Supabase Storage
    to prevent accidental credential exposure through subprocess output.
    """
    if not log_path or not os.path.isfile(log_path):
        return
    try:
        with open(log_path, 'r', errors='replace') as f:
            content = f.read()
        redacted = False
        for pattern, replacement in _SENSITIVE_LOG_PATTERNS:
            new_content = pattern.sub(replacement, content)
            if new_content != content:
                redacted = True
                content = new_content
        if redacted:
            with open(log_path, 'w') as f:
                f.write(content)
            logger.warning(f"Sanitized sensitive patterns from {log_path}")
    except Exception as e:
        logger.warning(f"Failed to sanitize log file {log_path}: {e}")


def _diagnose_subprocess_failure(log_path, effective_provider):
    """Scan simulation log for known error patterns and return a user-friendly message.

    Returns a tuple (user_message, error_category) or (None, None) if no known
    pattern is detected.
    """
    if not log_path or not os.path.isfile(log_path):
        return None, None
    try:
        with open(log_path, 'r', errors='replace') as f:
            content = f.read()
    except Exception:
        return None, None

    lower = content.lower()

    # Rate limit / quota exceeded (OpenAI, Codex, Anthropic, DeepSeek)
    if any(p in lower for p in [
        'rate_limit_exceeded', 'ratelimiterror', 'rate limit reached',
        'too many requests', 'quota exceeded', 'insufficient_quota',
        'you exceeded your current quota',
    ]):
        if effective_provider == 'openai-codex':
            return (
                "Platform Codex quota temporarily exhausted. "
                "It resets every few hours — please wait and try again, or use BYOK with your own API key."
            ), 'codex_quota_exceeded'
        return (
            "LLM API rate limit or quota exceeded. "
            "Please try again later, or use a different API key / model."
        ), 'rate_limit'

    # Authentication errors
    # Note: avoid bare '401' — it false-matches FAISS similarity scores like 0.401...
    if any(p in lower for p in [
        'authenticationerror', 'invalid api key', 'invalid_api_key',
        'incorrect api key', 'unauthorized', 'http 401', 'status 401',
        'error code: 401', '401 unauthorized',
    ]):
        return (
            "LLM API authentication failed. Please check your API key."
        ), 'auth_error'

    return None, None


# --- 2. 辅助函数：文件树构建和上传 ---

def build_file_tree(directory_path):
    """
    递归扫描目录，构建文件树结构。

    返回格式：
    {
        "files": [
            {"path": "prompt.txt", "name": "prompt.txt", "size": 1234, "type": "txt"},
            {"path": "output/Allrun", "name": "Allrun", "size": 5678, "type": "sh"},
            ...
        ],
        "directories": [
            {"path": "output", "name": "output"},
            {"path": "output/0", "name": "0"},
            ...
        ]
    }
    """
    file_tree = {
        "files": [],
        "directories": []
    }

    base_path = Path(directory_path)
    if not base_path.exists():
        logger.warning(f"Directory {directory_path} does not exist")
        return file_tree

    # Directories to exclude from file tree (sensitive or ephemeral)
    excluded_dirs = {'.codex_auth'}

    # 使用os.walk遍历所有文件和目录
    for root, dirs, files in os.walk(directory_path):
        # Skip excluded directories (modifying dirs in-place prunes os.walk)
        dirs[:] = [d for d in dirs if d not in excluded_dirs]

        # 计算相对于base_path的路径
        rel_root = os.path.relpath(root, directory_path)

        # 添加目录信息（排除根目录）
        if rel_root != '.':
            file_tree["directories"].append({
                "path": rel_root.replace('\\', '/'),  # 统一使用正斜杠
                "name": os.path.basename(root)
            })

        # 添加文件信息
        for file in files:
            file_path = os.path.join(root, file)
            rel_file_path = os.path.relpath(file_path, directory_path)

            try:
                file_size = os.path.getsize(file_path)
                # 根据文件扩展名判断文件类型
                _, ext = os.path.splitext(file)
                file_type = ext[1:].lower() if ext else 'unknown'

                file_tree["files"].append({
                    "path": rel_file_path.replace('\\', '/'),  # 统一使用正斜杠
                    "name": file,
                    "size": file_size,
                    "type": file_type
                })
            except Exception as e:
                logger.warning(f"Failed to get info for file {file_path}: {e}")

    # 按路径排序，便于前端显示
    file_tree["files"].sort(key=lambda x: x["path"])
    file_tree["directories"].sort(key=lambda x: x["path"])

    logger.info(f"Built file tree for {directory_path}: {len(file_tree['files'])} files, {len(file_tree['directories'])} directories")
    return file_tree


def get_content_type(file_type):
    """
    根据文件扩展名返回 MIME 类型。
    """
    content_types = {
        'txt': 'text/plain',
        'log': 'text/plain',
        'err': 'text/plain',
        'out': 'text/plain',
        'dict': 'text/plain',
        'boundary': 'text/plain',
        'json': 'application/json',
        'xml': 'application/xml',
        'py': 'text/x-python',
        'sh': 'text/x-shellscript',
        'c': 'text/x-c',
        'cpp': 'text/x-c++',
        'h': 'text/x-c',
        'hpp': 'text/x-c++',
        'csv': 'text/csv',
        'html': 'text/html',
        'md': 'text/markdown',
        'pdf': 'application/pdf',
        'zip': 'application/zip',
        'foam': 'text/plain',  # ParaView文件
        'vtk': 'application/octet-stream',
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'gif': 'image/gif',
        'svg': 'image/svg+xml',
    }
    return content_types.get(file_type, 'application/octet-stream')


def upload_directory_to_storage(local_dir, storage_base_path, supabase_client):
    """
    将本地目录中的所有文件上传到 Supabase Storage。

    参数:
        local_dir: 本地目录路径（如 "runs/10"）
        storage_base_path: Storage中的基础路径（如 "public/{user_id}/10"）
        supabase_client: Supabase客户端实例

    返回:
        (uploaded_count, failed_count, total_bytes): 成功/失败的文件数量和总字节数
    """
    uploaded_count = 0
    failed_count = 0
    total_bytes = 0

    base_path = Path(local_dir)
    if not base_path.exists():
        logger.error(f"Local directory {local_dir} does not exist")
        return uploaded_count, failed_count, total_bytes

    # Directories to exclude from upload (sensitive or ephemeral)
    excluded_dirs = {'.codex_auth'}

    # 遍历所有文件
    for root, dirs, files in os.walk(local_dir):
        # Skip excluded directories (modifying dirs in-place prunes os.walk)
        dirs[:] = [d for d in dirs if d not in excluded_dirs]

        for file in files:
            local_file_path = os.path.join(root, file)

            # 计算相对于local_dir的路径
            rel_file_path = os.path.relpath(local_file_path, local_dir)
            storage_file_path = f"{storage_base_path}/{rel_file_path}".replace('\\', '/')

            try:
                # 读取文件内容
                with open(local_file_path, 'rb') as f:
                    file_content = f.read()
                total_bytes += len(file_content)

                # 获取文件类型
                _, ext = os.path.splitext(file)
                file_type = ext[1:].lower() if ext else 'unknown'
                content_type = get_content_type(file_type)

                # 上传到Storage
                # 注意：如果文件已存在，需要先删除或使用upsert
                try:
                    # 尝试删除已存在的文件（如果有）
                    supabase_client.storage.from_("simulation_results").remove([storage_file_path])
                except Exception:
                    pass  # Ignore if file doesn't exist

                # 上传文件
                supabase_client.storage.from_("simulation_results").upload(
                    path=storage_file_path,
                    file=file_content,
                    file_options={"content-type": content_type}
                )

                uploaded_count += 1
                if uploaded_count % 10 == 0:  # 每上传10个文件记录一次
                    logger.info(f"Uploaded {uploaded_count} files...")

            except Exception as e:
                failed_count += 1
                logger.error(f"Failed to upload file {local_file_path} to {storage_file_path}: {e}")
                # 继续上传其他文件，不因单个文件失败而中断

    logger.info(f"Upload complete: {uploaded_count} files uploaded, {failed_count} files failed, {total_bytes} bytes total")
    return uploaded_count, failed_count, total_bytes


# --- 3. Stale job recovery ---

def recover_stale_jobs():
    """
    Recover jobs stuck in 'running' status due to a previous Worker crash.

    If a job has been 'running' for longer than STALE_JOB_THRESHOLD seconds
    (default: 2 hours), reset it to 'queued' so it can be picked up again.
    Called once at Worker startup.
    """
    threshold = datetime.now(timezone.utc) - timedelta(seconds=STALE_JOB_THRESHOLD)
    threshold_iso = threshold.isoformat()

    try:
        response = (
            supabase.table('simulations')
            .select('id, updated_at')
            .eq('status', 'running')
            .lt('updated_at', threshold_iso)
            .execute()
        )

        stale_jobs = response.data
        if not stale_jobs:
            logger.info("No stale jobs found during startup recovery.")
            return

        logger.warning(f"Found {len(stale_jobs)} stale job(s) stuck in 'running' status.")

        for job in stale_jobs:
            job_id = job['id']
            logger.warning(
                f"Recovering stale job {job_id} "
                f"(updated_at: {job.get('updated_at', 'unknown')}). "
                f"Resetting to 'queued'."
            )
            # Clear worker affinity so any available worker can pick it up.
            # Fetch current pipeline_state to remove assigned_worker_id.
            full_job = supabase.table('simulations').select(
                'pipeline_state'
            ).eq('id', job_id).execute()
            update_data = {'status': 'queued'}
            if full_job.data:
                ps = full_job.data[0].get('pipeline_state') or {}
                if ps.pop('assigned_worker_id', None):
                    update_data['pipeline_state'] = ps
                    logger.info(f"Stale job {job_id}: cleared worker affinity.")
            supabase.table('simulations').update(update_data).eq('id', job_id).execute()
            logger.info(f"Stale job {job_id} reset to 'queued' successfully.")

    except Exception as e:
        logger.error(f"Error during stale job recovery: {e}", exc_info=True)


# --- 4. Purge & TTL auto-expiry ---

# Throttle: run at most once per hour
_last_purge_time = 0.0
PURGE_INTERVAL = 3600       # seconds between purge runs
PURGE_RETENTION_DAYS = 3    # keep soft-deleted rows for 3 days before hard-delete
TTL_FAILED_DAYS = 7         # auto-expire failed/cancelled tasks after 7 days
TTL_COMPLETED_DAYS = 14     # auto-expire completed tasks after 14 days


def run_purge_cycle():
    """
    Single throttled entry point for all purge/TTL work.
    Runs at most once per PURGE_INTERVAL seconds.
    1. Auto-expire old tasks (soft-delete via TTL)
    2. Hard-delete tasks whose deleted_at is older than PURGE_RETENTION_DAYS
    """
    global _last_purge_time
    now = time.time()
    if now - _last_purge_time < PURGE_INTERVAL:
        return
    _last_purge_time = now

    _purge_expired_simulations()
    _purge_deleted_simulations()


def _purge_expired_simulations():
    """
    Auto-expire old simulations by setting deleted_at (soft-delete).
    - failed/cancelled tasks older than TTL_FAILED_DAYS (7 days)
    - completed tasks older than TTL_COMPLETED_DAYS (14 days)
    """
    now_utc = datetime.now(timezone.utc)

    try:
        # 1. Auto-expire failed/cancelled > TTL_FAILED_DAYS
        failed_cutoff = (now_utc - timedelta(days=TTL_FAILED_DAYS)).isoformat()
        resp1 = (
            supabase.table('simulations')
            .update({'deleted_at': now_utc.isoformat()})
            .is_('deleted_at', 'null')
            .in_('status', ['failed', 'cancelled'])
            .lt('created_at', failed_cutoff)
            .execute()
        )
        expired_failed = len(resp1.data) if resp1.data else 0

        # 2. Auto-expire completed > TTL_COMPLETED_DAYS
        completed_cutoff = (now_utc - timedelta(days=TTL_COMPLETED_DAYS)).isoformat()
        resp2 = (
            supabase.table('simulations')
            .update({'deleted_at': now_utc.isoformat()})
            .is_('deleted_at', 'null')
            .eq('status', 'completed')
            .lt('created_at', completed_cutoff)
            .execute()
        )
        expired_completed = len(resp2.data) if resp2.data else 0

        if expired_failed or expired_completed:
            logger.info(f"TTL auto-expire: {expired_failed} failed/cancelled, {expired_completed} completed tasks marked for deletion")

    except Exception as e:
        logger.error(f"TTL auto-expire: error: {e}", exc_info=True)


def _remove_storage_directory(prefix):
    """
    Recursively list and remove all files under a Supabase Storage prefix.
    Supabase list() only returns one level, so we must recurse into subdirectories.
    """
    bucket = supabase.storage.from_('simulation_results')
    listed = bucket.list(prefix)
    if not listed:
        return

    files = []
    for item in listed:
        item_path = f"{prefix}/{item['name']}"
        if item.get('id') is None:
            # Directory entry (no id) — recurse
            _remove_storage_directory(item_path)
        else:
            files.append(item_path)

    if files:
        bucket.remove(files)


def _purge_deleted_simulations():
    """
    Hard-delete simulations where deleted_at is older than PURGE_RETENTION_DAYS.
    Cleanup order: Supabase Storage files -> local runs/ directory -> DB row.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PURGE_RETENTION_DAYS)).isoformat()
    try:
        response = (
            supabase.table('simulations')
            .select('id, user_id, result_data, mesh_file')
            .not_.is_('deleted_at', 'null')
            .lt('deleted_at', cutoff)
            .execute()
        )
        rows = response.data
        if not rows:
            logger.info("Purge: no expired soft-deleted simulations.")
            return

        logger.info(f"Purge: found {len(rows)} simulation(s) to hard-delete.")

        for row in rows:
            job_id = row['id']
            user_id = row.get('user_id')
            result_data = row.get('result_data') or {}

            # 1. Delete files from Supabase Storage (recursive)
            storage_base = result_data.get('storage_base_path')
            if storage_base:
                try:
                    _remove_storage_directory(storage_base)
                    logger.info(f"Purge: removed Storage files for job {job_id}")
                except Exception as e:
                    logger.warning(f"Purge: failed to remove Storage files for job {job_id}: {e}")

            # 2. Delete uploaded mesh file from Supabase Storage
            mesh_file = row.get('mesh_file') or {}
            mesh_path = mesh_file.get('storage_path')
            if mesh_path:
                try:
                    supabase.storage.from_('simulation_results').remove([mesh_path])
                    logger.info(f"Purge: removed mesh file {mesh_path} for job {job_id}")
                except Exception as e:
                    logger.warning(f"Purge: failed to remove mesh file for job {job_id}: {e}")

            # 3. Delete local runs/ directory
            local_run_dir = os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
            if os.path.isdir(local_run_dir):
                try:
                    shutil.rmtree(local_run_dir)
                    logger.info(f"Purge: removed local dir {local_run_dir}")
                except Exception as e:
                    logger.warning(f"Purge: failed to remove local dir {local_run_dir}: {e}")

            # 3. Delete DB row
            try:
                supabase.table('simulations').delete().eq('id', job_id).execute()
                logger.info(f"Purge: hard-deleted simulation {job_id} from DB")
            except Exception as e:
                logger.error(f"Purge: failed to delete DB row for job {job_id}: {e}")

    except Exception as e:
        logger.error(f"Purge: error during purge cycle: {e}", exc_info=True)



# --- 5. Cancellation helpers ---

def check_job_cancelled(job_id):
    """Check if a job has been cancelled by the user (status == 'cancelled' in DB)."""
    try:
        response = supabase.table('simulations').select('status').eq('id', job_id).execute()
        if response.data and response.data[0]['status'] == 'cancelled':
            return True
    except Exception as e:
        logger.warning(f"Job {job_id}: failed to check cancel status: {e}")
    return False


def _kill_process_tree(process):
    """
    Kill a subprocess and all its children by sending signals to the process group.
    Uses SIGTERM first (graceful), then SIGKILL (force) if still alive after 10s.
    Requires the process to have been started with start_new_session=True.
    """
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return  # Already dead
    try:
        os.killpg(pgid, signal.SIGTERM)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(pgid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        pass  # Already dead


# --- 6. Prompt pre-check ---

import re as _re

# OpenFOAM version patterns: "openfoam 13", "OF13", "openfoam v13", "of 13", etc.
# We detect versions that are NOT v10 (the platform version).
_OF_VERSION_RE = _re.compile(
    r'(?:openfoam|of)\s*(?:v|version\s*)?(\d+)',
    _re.IGNORECASE
)

# Convergence tolerance pattern: "1e-7", "10^-8", "10的-7次方", "残差.*1e-7"
_CONVERGENCE_RE = _re.compile(
    r'(?:residual|残差|convergence|收敛).*?'
    r'(?:1e-?(\d+)|10\^-?(\d+)|10的-?(\d+)次)',
    _re.IGNORECASE
)

# Platform capabilities note — always appended so Foam-Agent knows constraints.
_PLATFORM_NOTE = (
    "[PLATFORM CONSTRAINTS] "
    "OpenFOAM v10 (not v11/v12/v13 — use v10-compatible API and syntax). "
    "Single-core only — do NOT use decomposePar or runParallel. "
    "All simulations must run in serial mode. "
    "After meshing, check cell count — if > 2 million cells, "
    "warn user and consider coarsening the mesh. "
    "snappyHexMesh is supported with built-in searchable geometries "
    "(searchableBox, searchableCylinder, searchableSphere, etc.), "
    "but NO STL file generation capability — if complex geometry requires "
    "an STL that cannot be described by searchable primitives, "
    "report that user must upload a custom mesh (.msh). "
    "Users can upload Gmsh .msh files via --custom_mesh_path. "
    f"Max output size: {{}}" "MB. "  # filled at runtime
    "Use purgeWrite to limit stored timesteps."
)


def _check_prompt(prompt, job_id):
    """
    Pre-check user prompt for known issues before running Foam-Agent.

    Returns a list of warning dicts: [{'type': str, 'message': str}, ...]
    Each warning is appended to the prompt as [PLATFORM NOTE] and logged.
    The platform capabilities note is always included.
    """
    warnings = []

    # Always include platform capabilities
    limit_mb = TASK_DISK_LIMIT_BYTES // (1024 * 1024)
    warnings.append({
        'type': 'platform_info',
        'message': _PLATFORM_NOTE.format(limit_mb),
    })

    # 1. OpenFOAM version mismatch
    for m in _OF_VERSION_RE.finditer(prompt):
        version = int(m.group(1))
        if version != 10:
            warnings.append({
                'type': 'of_version_mismatch',
                'message': (
                    f"User prompt mentions OpenFOAM {version}, but this "
                    f"platform runs OpenFOAM v10. Use v10-compatible syntax "
                    f"(e.g. 'stopAt endTime' not 'stopAt maxClockTime', "
                    f"'Gauss upwind' not 'bounded Gauss ...', "
                    f"'compressible::alphatJayatillekeWallFunction' with "
                    f"namespace prefix)."
                ),
            })
            break

    # 2. Overly strict convergence criteria
    for m in _CONVERGENCE_RE.finditer(prompt):
        exponent = int(m.group(1) or m.group(2) or m.group(3))
        if exponent >= 6:
            warnings.append({
                'type': 'strict_convergence',
                'message': (
                    f"Convergence target 1e-{exponent} is very strict for "
                    f"RANS simulations. Residuals of 1e-4 ~ 1e-5 are typically "
                    f"sufficient. Overly strict targets may cause timeout."
                ),
            })
            break

    # Log non-platform warnings
    real_warnings = [w for w in warnings if w['type'] != 'platform_info']
    if real_warnings:
        types = [w['type'] for w in real_warnings]
        logger.info(f"Job {job_id}: prompt pre-check warnings: {types}")
        for w in real_warnings:
            logger.info(f"  [{w['type']}] {w['message']}")

    return warnings


# --- 7. Disk monitoring & subprocess helpers ---

def _get_dir_size_bytes(path):
    """Get total size of a directory in bytes (non-recursive os.scandir for speed)."""
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
            elif entry.is_dir(follow_symlinks=False):
                total += _get_dir_size_bytes(entry.path)
    except (OSError, PermissionError):
        pass
    return total


def _run_subprocess_with_polling(command, cwd, env, log_path, job_id, timeout,
                                 run_dir=None):
    """
    Run a subprocess with polling for timeout, cancellation, and disk usage.

    Returns:
        (returncode, cancelled, timed_out, disk_exceeded) tuple.
        returncode is None if cancelled, timed_out, or disk_exceeded before
        natural completion.
    """
    with open(log_path, 'w') as log_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=log_file,
            stderr=log_file,
            text=True,
            start_new_session=True,
        )

        start_time = time.time()
        last_disk_check = 0
        cancelled = False
        timed_out = False
        disk_exceeded = False

        while True:
            retcode = process.poll()
            if retcode is not None:
                break

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                timed_out = True
                logger.error(f"Job {job_id} timed out after {timeout} seconds.")
                _kill_process_tree(process)
                break

            if check_job_cancelled(job_id):
                cancelled = True
                logger.info(f"Job {job_id}: cancellation detected, terminating subprocess...")
                _kill_process_tree(process)
                break

            # Periodic disk usage check
            if run_dir and (elapsed - last_disk_check) >= DISK_CHECK_INTERVAL:
                last_disk_check = elapsed
                dir_size = _get_dir_size_bytes(run_dir)
                dir_size_mb = dir_size / (1024 * 1024)
                if dir_size > TASK_DISK_LIMIT_BYTES:
                    disk_exceeded = True
                    limit_mb = TASK_DISK_LIMIT_BYTES // (1024 * 1024)
                    logger.error(
                        f"Job {job_id}: disk usage {dir_size_mb:.0f} MB "
                        f"exceeds limit {limit_mb} MB. Terminating."
                    )
                    _kill_process_tree(process)
                    break
                elif dir_size_mb > 100:
                    limit_mb = TASK_DISK_LIMIT_BYTES // (1024 * 1024)
                    logger.info(
                        f"Job {job_id}: disk usage {dir_size_mb:.0f} MB "
                        f"/ {limit_mb} MB"
                    )

            time.sleep(CANCEL_CHECK_INTERVAL)

    return process.returncode, cancelled, timed_out, disk_exceeded


def _handle_cancelled_or_timeout(job_id, user_id, run_dir, log_path,
                                  cancelled, timed_out, disk_exceeded=False):
    """Update DB for cancelled, timed-out, or disk-exceeded jobs. Returns True if handled."""
    if cancelled:
        logger.info(f"Job {job_id} was cancelled by user. Subprocess terminated.")
        _upload_and_fail(job_id, user_id, 'Simulation cancelled by user.',
                         run_dir=run_dir,
                         extra_result={'log_path_on_server': log_path})
        # Override status to 'cancelled' (not 'failed')
        supabase.table('simulations').update({'status': 'cancelled'}).eq('id', job_id).execute()
        return True

    if disk_exceeded:
        limit_mb = TASK_DISK_LIMIT_BYTES // (1024 * 1024)
        _upload_and_fail(
            job_id, user_id,
            f'Simulation terminated: output exceeded {limit_mb} MB disk limit. '
            f'This usually means the mesh is too large or write frequency is too high. '
            f'Please simplify the geometry or reduce output frequency.',
            run_dir=run_dir,
            extra_result={
                'log_path_on_server': log_path,
                'error_category': 'disk_exceeded',
            },
        )
        return True

    if timed_out:
        timeout_min = SIMULATION_TIMEOUT // 60
        _upload_and_fail(
            job_id, user_id,
            f'Simulation timed out after {timeout_min} minutes.',
            run_dir=run_dir,
            extra_result={
                'log_path_on_server': log_path,
                'error_category': 'timeout',
            },
        )
        return True

    return False


def _run_allrun_audit(run_dir, job_id):
    """Run Allrun security audit and log results. Returns audit dict."""
    allrun_audit = audit_allrun_scripts(run_dir)
    if not allrun_audit['is_safe']:
        logger.critical(
            f"SECURITY ALERT: Job {job_id} Allrun contains dangerous commands: "
            f"{allrun_audit['dangerous_summary']}"
        )
    elif allrun_audit['files_scanned'] > 0:
        unknown_count = sum(
            len(r['unknown_commands']) for r in allrun_audit['results']
        )
        if unknown_count > 0:
            logger.warning(
                f"Job {job_id}: Allrun audit found {unknown_count} unknown command(s)"
            )
        else:
            logger.info(
                f"Job {job_id}: Allrun audit passed "
                f"({allrun_audit['files_scanned']} file(s) scanned)"
            )
    return allrun_audit


def _download_mesh_file(job_id, mesh_file_info, run_dir):
    """Download mesh file from Supabase Storage to local run directory.

    Returns local path on success, None on failure.
    """
    storage_path = mesh_file_info.get('storage_path')
    original_name = mesh_file_info.get('original_name', 'mesh.msh')
    if not storage_path:
        return None
    local_mesh_path = os.path.join(run_dir, original_name)
    try:
        data = supabase.storage.from_('simulation_results').download(storage_path)
        with open(local_mesh_path, 'wb') as f:
            f.write(data)
        logger.info(
            f"Job {job_id}: downloaded mesh '{original_name}' "
            f"({len(data)} bytes) to {local_mesh_path}"
        )
        return local_mesh_path
    except Exception as e:
        logger.error(f"Job {job_id}: failed to download mesh file: {e}")
        return None


def _upload_and_fail(job_id, user_id, error_msg, run_dir=None, extra_result=None,
                     extra_fields=None):
    """Upload whatever files exist to Supabase Storage, then mark job as failed.

    This ensures failed tasks also have their files (logs, partial output)
    available in cloud storage for user browsing and storage accounting.

    Args:
        job_id: Simulation task ID.
        user_id: Owner user ID (needed for storage path).
        error_msg: Error description string.
        run_dir: Local run directory. If None or missing, skip upload.
        extra_result: Additional dict entries to merge into result_data.
        extra_fields: Additional DB columns to set (e.g. pipeline_stage).
    """
    # Sanitize simulation.log before uploading to prevent credential leaks
    if run_dir:
        _sanitize_log_file(os.path.join(run_dir, "simulation.log"))
    result_data = {'error': error_msg}
    if extra_result:
        result_data.update(extra_result)

    # Upload files if run_dir exists and has content
    if run_dir and os.path.isdir(run_dir) and os.listdir(run_dir):
        storage_base_path = f"public/{user_id}/{job_id}"
        try:
            file_tree = build_file_tree(run_dir)
            uploaded_count, failed_count, total_bytes = upload_directory_to_storage(
                run_dir, storage_base_path, supabase
            )
            # Create and upload ZIP archive
            zip_storage_path = f"{storage_base_path}/result.zip"
            try:
                zip_path_base = os.path.join(run_dir, "result")
                shutil.make_archive(zip_path_base, 'zip', run_dir)
                zip_file_path = f"{zip_path_base}.zip"
                with open(zip_file_path, 'rb') as f:
                    supabase.storage.from_("simulation_results").upload(
                        path=zip_storage_path, file=f)
                os.remove(zip_file_path)
                result_data['zip_storage_path'] = zip_storage_path
                logger.info(f"Job {job_id} (failed): uploaded ZIP to {zip_storage_path}")
            except Exception as e:
                logger.warning(f"Job {job_id}: failed to create/upload ZIP on failure: {e}")

            result_data['storage_base_path'] = storage_base_path
            result_data['file_tree'] = file_tree
            result_data['upload_stats'] = {
                'uploaded': uploaded_count,
                'failed': failed_count,
                'total_bytes': total_bytes,
            }
            logger.info(f"Job {job_id} (failed): uploaded {uploaded_count} files "
                        f"({total_bytes} bytes) to storage before marking failed")
        except Exception as e:
            logger.warning(f"Job {job_id}: failed to upload files on failure: {e}")

    # Preserve accumulated stage feedback before clearing pipeline_state
    try:
        ps_resp = supabase.table('simulations').select('pipeline_state').eq('id', job_id).execute()
        ps = (ps_resp.data[0].get('pipeline_state') or {}) if ps_resp.data else {}
        if ps.get('stage_feedback'):
            result_data['stage_feedback'] = ps['stage_feedback']
    except Exception as e:
        logger.warning(f"Job {job_id}: failed to read pipeline_state for stage_feedback: {e}")

    update_data = {'status': 'failed', 'result_data': result_data}
    if extra_fields:
        update_data.update(extra_fields)

    supabase.table('simulations').update(update_data).eq('id', job_id).execute()
    _increment_stat('jobs_processed')
    _increment_stat('jobs_failed')
    _update_stats(current_job_id=None)

    # Clean up local files after successful upload to Supabase Storage
    if run_dir and os.path.isdir(run_dir):
        try:
            shutil.rmtree(run_dir)
            logger.info(f"Job {job_id}: cleaned up local run directory {run_dir}")
        except Exception as e:
            logger.warning(f"Job {job_id}: failed to clean up local dir: {e}")


def _upload_and_complete(job_id, user_id, run_dir, output_path, log_path, allrun_audit,
                         upload_dir=None):
    """Upload results to Supabase Storage and update DB status to completed.

    Args:
        upload_dir: Directory to use for file tree + individual file uploads.
                    Defaults to run_dir.  Controlled pipeline passes case_dir
                    here because the generated OpenFOAM files live outside run_dir.
    """
    # Sanitize simulation.log before uploading to prevent credential leaks
    _sanitize_log_file(os.path.join(run_dir, "simulation.log"))

    if upload_dir is None:
        upload_dir = run_dir
    storage_base_path = f"public/{user_id}/{job_id}"

    logger.info(f"Building file tree for run directory: {upload_dir}")
    file_tree = build_file_tree(upload_dir)

    logger.info(f"Uploading ZIP file for job {job_id}...")
    zip_path_base = os.path.join(run_dir, "result")
    shutil.make_archive(zip_path_base, 'zip', output_path)
    zip_file_path = f"{zip_path_base}.zip"

    zip_storage_path = f"{storage_base_path}/result.zip"
    try:
        with open(zip_file_path, 'rb') as f:
            supabase.storage.from_("simulation_results").upload(path=zip_storage_path, file=f)
        os.remove(zip_file_path)
        logger.info(f"Uploaded and removed local zip file: {zip_file_path}")
    except Exception as e:
        logger.error(f"Failed to upload ZIP file: {e}")

    logger.info(f"Uploading all files from {upload_dir} to Storage...")
    uploaded_count, failed_count, total_bytes = upload_directory_to_storage(
        upload_dir,
        storage_base_path,
        supabase
    )

    if failed_count > 0:
        logger.warning(f"Some files failed to upload: {failed_count} files failed")

    # Extract token usage from simulation log
    token_usage = extract_token_usage(log_path)

    final_result = {
        "log_path_on_server": log_path,
        "output_path_on_server": output_path,
        "zip_storage_path": zip_storage_path,
        "storage_base_path": storage_base_path,
        "file_tree": file_tree,
        "upload_stats": {
            "uploaded": uploaded_count,
            "failed": failed_count,
            "total_bytes": total_bytes
        },
        "allrun_audit": allrun_audit,
    }
    if token_usage:
        final_result["token_usage"] = token_usage

    # Preserve accumulated stage feedback before clearing pipeline_state
    try:
        ps_resp = supabase.table('simulations').select('pipeline_state').eq('id', job_id).execute()
        ps = (ps_resp.data[0].get('pipeline_state') or {}) if ps_resp.data else {}
        if ps.get('stage_feedback'):
            final_result['stage_feedback'] = ps['stage_feedback']
    except Exception as e:
        logger.warning(f"Job {job_id}: failed to read pipeline_state for stage_feedback: {e}")

    supabase.table('simulations').update({
        'status': 'completed',
        'result_data': final_result,
        'pipeline_stage': None,
        'pipeline_state': None,
    }).eq('id', job_id).execute()

    logger.info(f"Job {job_id} completed and all files uploaded successfully. "
               f"Total files: {uploaded_count}, Failed: {failed_count}")

    # Clean up local files after successful upload to Supabase Storage
    if run_dir and os.path.isdir(run_dir):
        try:
            shutil.rmtree(run_dir)
            logger.info(f"Job {job_id}: cleaned up local run directory {run_dir}")
        except Exception as e:
            logger.warning(f"Job {job_id}: failed to clean up local dir: {e}")
    _increment_stat('jobs_processed')
    _increment_stat('jobs_succeeded')
    _update_stats(current_job_id=None)


# --- 7. MCP controlled pipeline ---

# MCP server port for controlled pipeline mode
MCP_SERVER_PORT = int(os.environ.get("MCP_SERVER_PORT", "7860"))

# Global MCP server manager (started once, shared across jobs)
_mcp_server_manager = None


def _get_mcp_server_manager():
    """Lazily create and return the global MCPServerManager."""
    global _mcp_server_manager
    if _mcp_server_manager is None:
        from mcp_client import MCPServerManager
        _mcp_server_manager = MCPServerManager(
            foam_agent_dir=FOAM_AGENT_DIR,
            host="localhost",
            port=MCP_SERVER_PORT,
        )
    return _mcp_server_manager


def _ensure_mcp_server():
    """Ensure MCP server is running, start it if not."""
    mgr = _get_mcp_server_manager()
    if not mgr.is_running:
        logger.info("Starting MCP server for controlled pipeline...")
        mgr.start(timeout=60.0)


def _append_mcp_log(job_id, stage, message):
    """Append a timestamped entry to the MCP pipeline simulation.log."""
    run_dir = os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
    os.makedirs(run_dir, exist_ok=True)
    log_path = os.path.join(run_dir, "simulation.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, 'a') as f:
        f.write(f"[{timestamp}] [{stage}] {message}\n")


def _collect_case_errors(case_dir):
    """Collect OpenFOAM error messages from log files in the case directory.

    Reads log.* files and Allrun.err / Allrun.pre-run.err looking for FOAM FATAL
    ERROR blocks and non-empty stderr output.  Returns a list of error strings
    suitable for passing to MCP review().
    """
    errors = []
    if not case_dir or not os.path.isdir(case_dir):
        return errors

    for entry in os.listdir(case_dir):
        entry_path = os.path.join(case_dir, entry)
        if not os.path.isfile(entry_path):
            continue

        # Check log.* files for FOAM FATAL ERROR
        if entry.startswith("log."):
            try:
                with open(entry_path, 'r') as f:
                    content = f.read()
                if "FOAM FATAL ERROR" in content or "FOAM FATAL IO ERROR" in content:
                    # Extract last 2000 chars for context
                    snippet = content[-2000:] if len(content) > 2000 else content
                    errors.append(f"{entry}: {snippet}")
            except Exception:
                pass

        # Check stderr files
        if entry in ("Allrun.err", "Allrun.pre-run.err"):
            try:
                with open(entry_path, 'r') as f:
                    content = f.read().strip()
                if content:
                    errors.append(f"{entry}: {content[:2000]}")
            except Exception:
                pass

    return errors


def _sanitize_case_dir(case_dir):
    """Rename case directory to remove shell-unsafe characters like ().

    Foam-Agent's utils.run_command() builds an unquoted bash command from the
    directory path.  Characters such as () cause bash syntax errors and are not
    modifiable because Foam-Agent code is read-only.  Sanitizing the directory
    name right after input_writer() returns prevents all downstream failures.
    """
    if not case_dir or not os.path.isdir(case_dir):
        return case_dir
    parent = os.path.dirname(case_dir)
    basename = os.path.basename(case_dir)
    sanitized = re.sub(r'[()&;|`$!]', '', basename)
    sanitized = re.sub(r'_+', '_', sanitized).strip('_')
    if sanitized != basename:
        new_case_dir = os.path.join(parent, sanitized)
        if os.path.exists(new_case_dir):
            # Avoid collision — find a free suffix
            suffix = 1
            while os.path.exists(f"{new_case_dir}_{suffix}"):
                suffix += 1
            new_case_dir = f"{new_case_dir}_{suffix}"
        os.rename(case_dir, new_case_dir)
        logger.info(f"Sanitized case dir: {basename} -> {os.path.basename(new_case_dir)}")
        return new_case_dir
    return case_dir


def _update_pipeline_state(job_id, stage, status, pipeline_state, extra_fields=None):
    """Helper to update pipeline stage, status, and state in DB."""
    update_data = {
        'status': status,
        'pipeline_stage': stage,
        'pipeline_state': pipeline_state,
    }
    if extra_fields:
        update_data.update(extra_fields)
    supabase.table('simulations').update(update_data).eq('id', job_id).execute()


def _handle_controlled_pipeline(job):
    """
    Drive the MCP controlled pipeline state machine.

    Pipeline stages:
        None         → plan()          → plan_review (if checkpoint) or generating
        plan_review  → input_writer()  → files_review (if checkpoint) or pre_running
        files_review → pre-run         → pre_run_review (if checkpoint) or running
        pre_run_review → full run()    → completed
        running      → full run done   → completed

    Review+fix loop is in pre-run stage (cheap, 10 timesteps), not full run.

    The stage transitions are driven by the DB pipeline_stage value:
    - None / new job: start from plan
    - Any *_review stage: user confirmed, continue from next stage

    Worker exclusivity: once a worker starts a controlled-pipeline job, it is
    exclusively bound to that job until completion/failure. The binding is
    tracked via the module-level _bound_pipeline_job_id variable and the
    pipeline_state['assigned_worker_id'] field in the DB.
    """
    global _bound_pipeline_job_id, _bound_checkpoint_since
    import asyncio
    from mcp_client import FoamAgentMCPClient

    job_id = job['id']
    pipeline_state = job.get('pipeline_state') or {}
    pipeline_stage = job.get('pipeline_stage')
    active_checkpoints = pipeline_state.get('active_checkpoints', [])

    logger.info(f"Job {job_id}: controlled pipeline, stage={pipeline_stage}")

    # --- Worker affinity: bind job to this worker on first touch ---
    if not pipeline_state.get('assigned_worker_id'):
        pipeline_state['assigned_worker_id'] = WORKER_ID
        logger.info(f"Job {job_id}: assigned to {WORKER_ID}")

    # --- Exclusive lock: this worker is now reserved for this job ---
    _bound_pipeline_job_id = job_id
    logger.info(f"Job {job_id}: worker {WORKER_ID} exclusively bound")

    # Write task_settings.json and download mesh for new jobs (first stage only)
    if pipeline_stage is None:
        llm_config = job.get('llm_config') or {}
        run_dir = os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
        os.makedirs(run_dir, exist_ok=True)

        # Download custom mesh if provided
        mesh_file_info = job.get('mesh_file')
        if mesh_file_info:
            mesh_path = _download_mesh_file(job_id, mesh_file_info, run_dir)
            if not mesh_path:
                _upload_and_fail(job_id, job['user_id'],
                                 'Failed to download uploaded mesh file.',
                                 run_dir=run_dir,
                                 extra_result={'error_category': 'mesh_download_failed'})
                _bound_pipeline_job_id = None
                return
            # Store in pipeline_state so MCP stages can find it
            pipeline_state['custom_mesh_path'] = mesh_path

        task_settings = {
            'job_id': str(job_id),
            'user_id': job.get('user_id'),
            'created_at': job.get('created_at'),
            'model_provider': llm_config.get('model_provider') or 'openai-codex',
            'model_version': llm_config.get('model_version') or 'gpt-5.3-codex',
            'has_user_api_key': bool(llm_config.get('api_key')),
            'has_codex_token': bool(llm_config.get('codex_token')),
            'base_url': llm_config.get('base_url'),
            'pipeline_mode': 'controlled',
            'pre_run_end_time': job.get('pre_run_end_time'),
            'checkpoints': active_checkpoints,
            'worker_id': WORKER_ID,
            'has_custom_mesh': bool(mesh_file_info),
            'mesh_original_name': (mesh_file_info or {}).get('original_name'),
        }
        try:
            with open(os.path.join(run_dir, "task_settings.json"), "w") as f:
                json.dump(task_settings, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"Job {job_id}: failed to write task_settings.json: {e}")

    try:
        # Ensure MCP server is running
        _ensure_mcp_server()

        # Determine which stage to execute based on current pipeline_stage
        if pipeline_stage is None:
            # New job — start from plan
            asyncio.run(_mcp_stage_plan(job, pipeline_state, active_checkpoints))
        elif pipeline_stage == 'plan_review':
            # User confirmed plan — continue to input_writer
            asyncio.run(_mcp_stage_input_writer(job, pipeline_state, active_checkpoints))
        elif pipeline_stage == 'files_review':
            # User confirmed files — continue to pre-run or full run
            asyncio.run(_mcp_stage_pre_run(job, pipeline_state, active_checkpoints))
        elif pipeline_stage == 'pre_run_review':
            # User confirmed pre-run — continue to full run
            asyncio.run(_mcp_stage_full_run(job, pipeline_state))
        else:
            logger.error(f"Job {job_id}: unknown pipeline_stage '{pipeline_stage}'")
            run_dir = pipeline_state.get('case_dir') or os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
            _upload_and_fail(job_id, job['user_id'],
                             f"Unknown pipeline_stage: {pipeline_stage}",
                             run_dir=run_dir)

    except Exception as e:
        logger.error(f"Job {job_id}: controlled pipeline error: {e}", exc_info=True)
        run_dir = pipeline_state.get('case_dir') or os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
        _upload_and_fail(job_id, job['user_id'],
                         f"Pipeline error at stage '{pipeline_stage}': {str(e)}",
                         run_dir=run_dir,
                         extra_fields={'pipeline_stage': pipeline_stage})

    # --- Check if job reached a terminal state → release the lock ---
    # Terminal states: completed, failed, cancelled (no more stages to run).
    # Checkpoint state: worker stays bound, waiting for user confirm.
    try:
        final = supabase.table('simulations').select('status').eq('id', job_id).execute()
        final_status = final.data[0]['status'] if final.data else 'unknown'
    except Exception:
        final_status = 'unknown'

    if final_status in ('completed', 'failed', 'cancelled'):
        _bound_pipeline_job_id = None
        _bound_checkpoint_since = None
        logger.info(f"Job {job_id}: final status={final_status}, worker lock released")
    elif final_status == 'checkpoint':
        # Record when the checkpoint wait started
        _bound_checkpoint_since = datetime.now(timezone.utc)
        logger.info(f"Job {job_id}: entered checkpoint, timeout in {CHECKPOINT_TIMEOUT}s")
    else:
        _bound_checkpoint_since = None
        logger.info(f"Job {job_id}: status={final_status}, worker stays bound")


async def _mcp_stage_plan(job, pipeline_state, active_checkpoints):
    """Execute plan() and decide whether to checkpoint."""
    job_id = job['id']
    mgr = _get_mcp_server_manager()

    from mcp_client import FoamAgentMCPClient
    client = FoamAgentMCPClient(mgr.url)

    _update_pipeline_state(job_id, 'planning', 'running', pipeline_state)

    _append_mcp_log(job_id, 'plan', 'Starting plan() ...')

    async with client:
        plan_result = await client.plan(job['prompt'])

    # Save plan result into pipeline_state
    pipeline_state.update({
        'subtasks': plan_result.get('subtasks', []),
        'case_name': plan_result.get('case_name', ''),
        'case_solver': plan_result.get('case_solver', ''),
        'case_domain': plan_result.get('case_domain', ''),
        'case_category': plan_result.get('case_category', ''),
    })

    _append_mcp_log(job_id, 'plan',
                    f"Completed: case={pipeline_state['case_name']}, "
                    f"solver={pipeline_state['case_solver']}, "
                    f"{len(pipeline_state['subtasks'])} subtasks")

    logger.info(
        f"Job {job_id}: plan() completed — case={pipeline_state['case_name']}, "
        f"solver={pipeline_state['case_solver']}, {len(pipeline_state['subtasks'])} subtasks"
    )

    if 'plan_review' in active_checkpoints:
        # Pause for user review
        _update_pipeline_state(job_id, 'plan_review', 'checkpoint', pipeline_state)
        logger.info(f"Job {job_id}: paused at plan_review checkpoint")
    else:
        # Auto-continue to input_writer
        await _mcp_stage_input_writer(job, pipeline_state, active_checkpoints)


async def _mcp_stage_input_writer(job, pipeline_state, active_checkpoints):
    """Execute input_writer() and decide whether to checkpoint."""
    job_id = job['id']
    mgr = _get_mcp_server_manager()

    from mcp_client import FoamAgentMCPClient
    client = FoamAgentMCPClient(mgr.url)

    _update_pipeline_state(job_id, 'generating', 'running', pipeline_state)
    _append_mcp_log(job_id, 'input_writer', 'Starting input_writer() ...')

    async with client:
        files_result = await client.input_writer(
            case_name=pipeline_state['case_name'],
            subtasks=pipeline_state['subtasks'],
            user_requirement=job['prompt'],
            case_solver=pipeline_state['case_solver'],
            case_domain=pipeline_state['case_domain'],
            case_category=pipeline_state['case_category'],
        )

    # Save file generation result (sanitize directory name for bash safety)
    case_dir = files_result.get('case_dir', '')
    case_dir = _sanitize_case_dir(case_dir)
    pipeline_state['case_dir'] = case_dir
    pipeline_state['allrun_script'] = files_result.get('allrun_script', '')

    _append_mcp_log(job_id, 'input_writer', f"Completed: case_dir={case_dir}")
    logger.info(f"Job {job_id}: input_writer() completed — case_dir={case_dir}")

    # Upload generated files so user can browse them
    run_dir = os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
    os.makedirs(run_dir, exist_ok=True)

    # Write prompt file to run_dir for consistency
    prompt_path = os.path.join(run_dir, "prompt.txt")
    if not os.path.exists(prompt_path):
        with open(prompt_path, "w") as f:
            f.write(job['prompt'])

    # Create symlink: runs/{job_id}/output -> case_dir (if not already)
    output_link = os.path.join(run_dir, "output")
    if not os.path.exists(output_link) and case_dir:
        try:
            os.symlink(case_dir, output_link)
        except OSError:
            # Fallback: just record the path
            pass

    if 'files_review' in active_checkpoints:
        # Upload generated OpenFOAM files (from case_dir) for browsing
        storage_base_path = f"public/{job['user_id']}/{job_id}"
        upload_src = case_dir if case_dir else run_dir
        file_tree = build_file_tree(upload_src)
        uploaded_count, failed_count, total_bytes = upload_directory_to_storage(
            upload_src, storage_base_path, supabase
        )
        _update_pipeline_state(job_id, 'files_review', 'checkpoint', pipeline_state,
                               extra_fields={
                                   'result_data': {
                                       'storage_base_path': storage_base_path,
                                       'file_tree': file_tree,
                                       'upload_stats': {'uploaded': uploaded_count, 'failed': failed_count, 'total_bytes': total_bytes},
                                   }
                               })
        logger.info(f"Job {job_id}: paused at files_review checkpoint ({uploaded_count} files uploaded)")
    else:
        # Auto-continue to pre-run
        await _mcp_stage_pre_run(job, pipeline_state, active_checkpoints)


async def _mcp_stage_pre_run(job, pipeline_state, active_checkpoints):
    """Execute pre-run (short simulation) using middleware, then checkpoint or continue."""
    job_id = job['id']
    case_dir = pipeline_state.get('case_dir', '')

    pre_run_end_time = job.get('pre_run_end_time')
    if pre_run_end_time is None:
        pre_run_end_time = 10  # Default

    if pre_run_end_time == -1 or not MIDDLEWARE_DIR:
        # Pre-run disabled — skip directly to full run
        if pre_run_end_time == -1:
            logger.info(f"Job {job_id}: pre-run disabled, skipping to full run")
        else:
            logger.info(f"Job {job_id}: MIDDLEWARE_DIR not set, skipping pre-run")
        await _mcp_stage_full_run(job, pipeline_state)
        return

    _update_pipeline_state(job_id, 'pre_running', 'running', pipeline_state)

    try:
        from pre_run_executor import PreRunExecutor
    except ImportError:
        logger.error(f"Job {job_id}: cannot import PreRunExecutor, skipping pre-run")
        await _mcp_stage_full_run(job, pipeline_state)
        return

    logger.info(f"Job {job_id}: starting pre-run (endTime={pre_run_end_time})")
    _append_mcp_log(job_id, 'pre_run', f"Starting pre-run (endTime={pre_run_end_time})")

    max_pre_run_fix_loops = 5
    pre_run_fix_count = 0
    executor = PreRunExecutor(case_dir, pre_run_end_time)
    checkpoint_data = executor.run(timeout=PRE_RUN_TIMEOUT)

    # Review+fix loop: if pre-run fails, use MCP review/apply_fixes and retry
    while (not checkpoint_data.get('execution_result', {}).get('success', False)
           and pre_run_fix_count < max_pre_run_fix_loops):
        pre_run_fix_count += 1
        errors = _collect_case_errors(case_dir)
        if not errors:
            _append_mcp_log(job_id, 'pre_run',
                            f"Pre-run failed but no parseable errors found, cannot auto-fix")
            break

        logger.info(f"Job {job_id}: pre-run failed with {len(errors)} errors, "
                    f"review+fix {pre_run_fix_count}/{max_pre_run_fix_loops}")
        _append_mcp_log(job_id, 'pre_run',
                        f"FAILED — review+fix loop {pre_run_fix_count}/{max_pre_run_fix_loops} "
                        f"({len(errors)} errors)")
        _update_pipeline_state(job_id, 'reviewing', 'running', pipeline_state)

        mgr = _get_mcp_server_manager()
        from mcp_client import FoamAgentMCPClient
        client = FoamAgentMCPClient(mgr.url)

        async with client:
            review_result = await client.review(
                case_dir=case_dir,
                errors=errors,
                user_requirement=job['prompt'],
            )
            analysis = review_result.get('analysis', '')

            fix_result = await client.apply_fixes(
                case_dir=case_dir,
                error_logs=errors,
                review_analysis=analysis,
                user_requirement=job['prompt'],
            )
            _append_mcp_log(job_id, 'pre_run',
                            f"Applied fixes ({fix_result.get('status')}), retrying pre-run")
            logger.info(f"Job {job_id}: applied fixes ({fix_result.get('status')}), "
                        f"retrying pre-run")

        _update_pipeline_state(job_id, 'pre_running', 'running', pipeline_state)
        executor = PreRunExecutor(case_dir, pre_run_end_time)
        checkpoint_data = executor.run(timeout=PRE_RUN_TIMEOUT)

    pipeline_state['pre_run_fix_count'] = pre_run_fix_count

    if not checkpoint_data.get('execution_result', {}).get('success', False):
        _append_mcp_log(job_id, 'pre_run',
                        f'Pre-run FAILED after {pre_run_fix_count} fix attempts')
        logger.error(f"Job {job_id}: pre-run failed after {pre_run_fix_count} fix attempts")
        _upload_and_fail(
            job_id, job['user_id'],
            f'Pre-run simulation failed after {pre_run_fix_count} fix attempts.',
            run_dir=case_dir or os.path.join(FOAM_AGENT_DIR, "runs", str(job_id)),
            extra_result={'checkpoint_data': checkpoint_data},
            extra_fields={'pipeline_stage': 'pre_running'},
        )
        return

    pipeline_state['checkpoint_data'] = checkpoint_data
    pipeline_state['original_end_time'] = checkpoint_data.get('original_end_time')

    _append_mcp_log(job_id, 'pre_run',
                    f"Completed: original_endTime={checkpoint_data.get('original_end_time')}")

    # Write detailed diagnostics to simulation.log for engineer review
    diag = checkpoint_data.get('diagnostics') or {}

    solver_log = diag.get('solver_log')
    if solver_log:
        lines = [f"--- Solver Log Summary ({solver_log.get('log_file', '?')}) ---"]
        if solver_log.get('last_time'):
            lines.append(f"  Last timestep: {solver_log['last_time']}")
        if solver_log.get('courant'):
            co = solver_log['courant']
            lines.append(f"  Courant Number — mean: {co.get('mean')}, max: {co.get('max')}")
        for field, vals in solver_log.get('residuals', {}).items():
            lines.append(f"  Residual {field}: initial={vals['initial']:.6e}, final={vals['final']:.6e}")
        if solver_log.get('continuity'):
            ct = solver_log['continuity']
            lines.append(f"  Continuity errors — local: {ct.get('local'):.6e}, "
                         f"global: {ct.get('global'):.6e}, cumulative: {ct.get('cumulative'):.6e}")
        _append_mcp_log(job_id, 'pre_run', '\n'.join(lines))

    field_mm = diag.get('field_min_max')
    if field_mm and isinstance(field_mm, dict):
        lines = ["--- Field Min/Max ---"]
        # Structure: {cellMax: {time_dir: {fname: content}}, cellMin: {...}}
        for func_name, func_data in field_mm.items():
            if isinstance(func_data, dict):
                for time_dir, files in func_data.items():
                    if isinstance(files, dict):
                        for fname, content in files.items():
                            preview = content.strip()[:500] if isinstance(content, str) else str(content)[:500]
                            lines.append(f"  [{func_name}/{time_dir}/{fname}] {preview}")
        _append_mcp_log(job_id, 'pre_run', '\n'.join(lines))

    field_avg = diag.get('field_average')
    if field_avg and isinstance(field_avg, dict):
        lines = ["--- Field Average ---"]
        for time_dir, files in field_avg.items():
            for fname, content in files.items():
                preview = content.strip()[:500] if isinstance(content, str) else str(content)[:500]
                lines.append(f"  [{time_dir}/{fname}] {preview}")
        _append_mcp_log(job_id, 'pre_run', '\n'.join(lines))

    if 'pre_run_review' in active_checkpoints:
        # Upload pre-run results (from case_dir) and pause
        run_dir = os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
        storage_base_path = f"public/{job['user_id']}/{job_id}"
        upload_src = case_dir if case_dir else run_dir
        file_tree = build_file_tree(upload_src)
        uploaded_count, failed_count, total_bytes = upload_directory_to_storage(
            upload_src, storage_base_path, supabase
        )
        _update_pipeline_state(job_id, 'pre_run_review', 'checkpoint', pipeline_state,
                               extra_fields={
                                   'result_data': {
                                       'checkpoint_data': checkpoint_data,
                                       'checkpoint_phase': 'pre-run',
                                       'storage_base_path': storage_base_path,
                                       'file_tree': file_tree,
                                       'upload_stats': {'uploaded': uploaded_count, 'failed': failed_count, 'total_bytes': total_bytes},
                                   }
                               })
        logger.info(f"Job {job_id}: paused at pre_run_review checkpoint")
    else:
        # Auto-continue to full run
        await _mcp_stage_full_run(job, pipeline_state)


async def _mcp_stage_full_run(job, pipeline_state):
    """Execute full simulation via MCP run().

    No review+fix loop here — errors are caught and fixed during pre-run
    (which is cheap at 10 timesteps).  Full run just executes the final
    simulation with the already-validated configuration.
    """
    job_id = job['id']
    case_dir = pipeline_state.get('case_dir', '')

    # If coming from pre-run, restore original endTime first.
    # Try pipeline_state first; fall back to backup file if pipeline_state lost.
    original_end_time = pipeline_state.get('original_end_time')
    backup_path = os.path.join(case_dir, 'system', 'controlDict.pre-run-backup') if case_dir else ''

    if not original_end_time and backup_path and os.path.exists(backup_path):
        # Recover original_end_time from backup file
        try:
            from controldict_manager import ControlDictManager
            mgr = ControlDictManager(case_dir)
            # Read endTime from the backup (which has the original value)
            import re
            with open(backup_path, 'r') as f:
                backup_content = f.read()
            match = re.search(r'endTime\s+([^;]+?)\s*;', backup_content)
            if match:
                original_end_time = match.group(1).strip()
                logger.warning(f"Job {job_id}: original_end_time recovered from backup: {original_end_time}")
        except Exception as e:
            logger.warning(f"Job {job_id}: failed to recover original_end_time from backup: {e}")

    if original_end_time:
        try:
            from normal_run_preparer import NormalRunPreparer
            preparer = NormalRunPreparer(case_dir, original_end_time)
            preparer.prepare()
            logger.info(f"Job {job_id}: restored endTime={original_end_time} for full run")
        except Exception as e:
            logger.error(f"Job {job_id}: failed to prepare normal-run: {e}", exc_info=True)
            _upload_and_fail(
                job_id, job['user_id'],
                f'Normal-run preparation failed: {str(e)}',
                run_dir=case_dir or os.path.join(FOAM_AGENT_DIR, "runs", str(job_id)),
                extra_fields={'pipeline_stage': 'running'},
            )
            return
    elif backup_path and os.path.exists(backup_path):
        # Backup exists but couldn't extract endTime — restore the whole file
        import shutil
        controldict_path = os.path.join(case_dir, 'system', 'controlDict')
        shutil.copy2(backup_path, controldict_path)
        os.remove(backup_path)
        logger.warning(f"Job {job_id}: restored controlDict from backup (full file copy)")

    _update_pipeline_state(job_id, 'running', 'running', pipeline_state)
    _append_mcp_log(job_id, 'run', f"Starting full simulation run (timeout={SIMULATION_TIMEOUT}s)")

    mgr = _get_mcp_server_manager()
    from mcp_client import FoamAgentMCPClient
    client = FoamAgentMCPClient(mgr.url)

    # Run simulation — no review+fix loop; errors already fixed during pre-run
    async with client:
        run_result = await client.run(case_dir, timeout=SIMULATION_TIMEOUT)

        errors = run_result.get('errors', [])
        _append_mcp_log(job_id, 'run',
                        f"Completed: status={run_result.get('status')}, errors={len(errors)}")

        if errors:
            _append_mcp_log(job_id, 'run',
                            f"WARNING: {len(errors)} errors in full run")
            logger.warning(f"Job {job_id}: full run had {len(errors)} errors")

    # Upload results and complete
    run_dir = os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
    os.makedirs(run_dir, exist_ok=True)
    output_path = case_dir
    log_path = os.path.join(run_dir, "simulation.log")

    # Append final summary to the MCP pipeline log
    _append_mcp_log(job_id, 'summary',
                    f"Case={pipeline_state.get('case_name')}, "
                    f"Solver={pipeline_state.get('case_solver')}, "
                    f"PreRunFixLoops={pipeline_state.get('pre_run_fix_count', 0)}, "
                    f"FinalStatus={run_result.get('status')}, "
                    f"RemainingErrors={len(errors)}")

    allrun_audit = _run_allrun_audit(run_dir, job_id)
    _upload_and_complete(job_id, job['user_id'], run_dir, output_path, log_path, allrun_audit,
                         upload_dir=case_dir)


# --- 8. 核心工作逻辑 ---

def _poll_bound_pipeline_job():
    """Check if the exclusively-bound controlled-pipeline job is ready to resume.

    Returns the job dict if it has been re-queued (user confirmed a checkpoint),
    or None if still waiting / terminal / no binding.
    """
    global _bound_pipeline_job_id, _bound_checkpoint_since
    if _bound_pipeline_job_id is None:
        return None

    job_id = _bound_pipeline_job_id
    try:
        resp = supabase.table('simulations').select('*').eq('id', job_id).execute()
        if not resp.data:
            logger.warning(f"Bound job {job_id} not found in DB — releasing lock")
            _bound_pipeline_job_id = None
            _bound_checkpoint_since = None
            return None

        job = resp.data[0]
        status = job.get('status')

        # --- Checkpoint timeout: auto-fail if user doesn't confirm in time ---
        if status == 'checkpoint' and _bound_checkpoint_since is not None:
            elapsed = (datetime.now(timezone.utc) - _bound_checkpoint_since).total_seconds()
            if elapsed > CHECKPOINT_TIMEOUT:
                pipeline_stage = job.get('pipeline_stage', 'unknown')
                logger.warning(
                    f"Bound job {job_id}: checkpoint '{pipeline_stage}' timed out "
                    f"after {int(elapsed)}s (limit={CHECKPOINT_TIMEOUT}s). Auto-failing."
                )
                run_dir = (job.get('pipeline_state') or {}).get('case_dir') or \
                          os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
                _upload_and_fail(
                    job_id, job['user_id'],
                    f"Checkpoint '{pipeline_stage}' timed out: no confirmation "
                    f"received within {CHECKPOINT_TIMEOUT // 60} minutes.",
                    run_dir=run_dir,
                    extra_fields={'pipeline_stage': pipeline_stage},
                )
                _bound_pipeline_job_id = None
                _bound_checkpoint_since = None
                return None

        # Check if user confirmed the checkpoint (flag in pipeline_state).
        # The confirm API sets pipeline_state.confirmed=True while keeping
        # status='checkpoint', so claim_next_job() cannot steal this job.
        pipeline_state = job.get('pipeline_state') or {}
        if status == 'checkpoint' and pipeline_state.get('confirmed'):
            # Clear the confirmed flag and set status to running
            pipeline_state.pop('confirmed', None)
            pipeline_state.pop('confirmed_at', None)
            supabase.table('simulations').update({
                'status': 'running',
                'pipeline_state': pipeline_state,
            }).eq('id', job_id).execute()
            # Update job dict with latest pipeline_state for downstream use
            job['pipeline_state'] = pipeline_state
            job['status'] = 'running'
            _bound_checkpoint_since = None
            logger.info(f"Bound job {job_id}: checkpoint confirmed, resuming")
            return job

        # Legacy support: if confirm endpoint set status='queued' (old behavior)
        if status == 'queued':
            supabase.table('simulations').update(
                {'status': 'running'}
            ).eq('id', job_id).eq('status', 'queued').execute()
            _bound_checkpoint_since = None
            logger.info(f"Bound job {job_id}: checkpoint confirmed (legacy queued), resuming")
            return job

        if status in ('completed', 'failed', 'cancelled'):
            logger.info(f"Bound job {job_id}: reached terminal status={status}, releasing lock")
            _bound_pipeline_job_id = None
            _bound_checkpoint_since = None
            return None

        # Still at checkpoint (not confirmed) or running — keep waiting
        return None

    except Exception as e:
        logger.error(f"Error polling bound job {job_id}: {e}", exc_info=True)
        return None


def find_and_process_job():
    """
    查找一个'queued'状态的任务并处理它。
    如果没有任务，则返回 False。如果处理了任务，则返回 True。

    Uses the claim_next_job() PostgreSQL RPC function which atomically
    selects and locks the next queued job using FOR UPDATE SKIP LOCKED,
    preventing multiple Workers from claiming the same job.

    Worker exclusivity: if this worker is bound to a controlled-pipeline job
    (waiting at a checkpoint), it will ONLY poll that job and refuse to claim
    any other work until the bound job completes or fails.

    Routes based on pipeline_mode:
    - 'controlled': MCP stage-by-stage pipeline with optional checkpoints
    - 'auto' (default): Foam-Agent subprocess one-shot execution
    """
    # --- Exclusive binding: if we're waiting on a checkpoint job, only poll that ---
    if _bound_pipeline_job_id is not None:
        bound_job = _poll_bound_pipeline_job()
        if bound_job is not None:
            logger.info(f"Job {bound_job['id']}: resuming bound controlled pipeline")
            _handle_controlled_pipeline(bound_job)
            return True
        # Still waiting on bound job — do NOT claim other work
        return False

    # --- Normal path: claim the next available queued job ---
    # Atomically claim the next queued job via RPC.
    response = supabase.rpc('claim_next_job').execute()

    if not response.data:
        return False

    job = response.data[0]
    job_id = job['id']
    _update_stats(current_job_id=job_id)
    logger.info(f"[{WORKER_ID}] Claimed job {job_id} via claim_next_job() RPC. Processing...")

    # --- Controlled pipeline mode: MCP stage-by-stage ---
    pipeline_mode = job.get('pipeline_mode', 'auto')
    if pipeline_mode == 'controlled':
        # --- Worker affinity check for checkpoint-resumed jobs ---
        pipeline_state = job.get('pipeline_state') or {}
        assigned_worker = pipeline_state.get('assigned_worker_id')
        if assigned_worker and assigned_worker != WORKER_ID:
            # This job belongs to a different worker. Re-queue it so the
            # correct worker (with access to the same filesystem / MCP state)
            # can pick it up, preventing cross-machine path mismatches.
            logger.warning(
                f"Job {job_id}: worker affinity mismatch — assigned to "
                f"{assigned_worker}, but claimed by {WORKER_ID}. Re-queuing."
            )
            supabase.table('simulations').update(
                {'status': 'queued'}
            ).eq('id', job_id).execute()
            return False

        logger.info(f"Job {job_id}: controlled pipeline mode")
        _handle_controlled_pipeline(job)
        return True

    # --- Auto mode: standard flow → Foam-Agent subprocess ---

    # Read user LLM config and build subprocess environment
    llm_config = job.get('llm_config') or {}
    child_env = _build_subprocess_env()
    temp_codex_dir = None  # Track temp dir for cleanup

    # Inject user-provided API key
    user_api_key = llm_config.get('api_key')
    if user_api_key:
        provider = llm_config.get('model_provider', 'openai')
        if provider == 'anthropic':
            child_env['ANTHROPIC_API_KEY'] = user_api_key
        else:
            child_env['OPENAI_API_KEY'] = user_api_key
        logger.info(f"Job {job_id}: using user-provided API key for provider={provider}")

    # Inject user-provided Codex OAuth token
    codex_token = llm_config.get('codex_token')
    if codex_token:
        # Write token to a temp auth.json and set CODEX_HOME so
        # Foam-Agent's _load_codex_oauth() finds it automatically
        temp_codex_dir = os.path.join(FOAM_AGENT_DIR, "runs", str(job_id), ".codex_auth")
        os.makedirs(temp_codex_dir, exist_ok=True)
        auth_json_path = os.path.join(temp_codex_dir, "auth.json")
        with open(auth_json_path, "w") as f:
            json.dump({"access_token": codex_token}, f)
        child_env['CODEX_HOME'] = temp_codex_dir
        logger.info(f"Job {job_id}: wrote Codex OAuth token to {auth_json_path}")

    # Immediately clear sensitive tokens from DB (privacy)
    sensitive_keys = {'api_key', 'codex_token', 'base_url'}
    if any(llm_config.get(k) for k in sensitive_keys):
        try:
            safe_config = {k: v for k, v in llm_config.items() if k not in sensitive_keys}
            supabase.table('simulations').update(
                {'llm_config': safe_config if safe_config else None}
            ).eq('id', job_id).execute()
            logger.info(f"Job {job_id}: cleared sensitive tokens from database")
        except Exception as e:
            logger.warning(f"Job {job_id}: failed to clear tokens from DB: {e}")

    # Prepare run directory and files
    run_dir = os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
    os.makedirs(run_dir, exist_ok=True)

    prompt_text = job['prompt']
    prompt_warnings = _check_prompt(prompt_text, job_id)

    # Append platform context to prompt so Foam-Agent is aware of constraints
    if prompt_warnings:
        prompt_text = prompt_text + "\n\n" + "\n".join(
            f"[PLATFORM NOTE] {w['message']}" for w in prompt_warnings
        )

    prompt_path = os.path.join(run_dir, "prompt.txt")
    with open(prompt_path, "w") as f:
        f.write(prompt_text)

    output_path = os.path.join(run_dir, "output")
    log_path = os.path.join(run_dir, "simulation.log")

    # Download custom mesh file if provided
    custom_mesh_path = None
    mesh_file_info = job.get('mesh_file')
    if mesh_file_info:
        custom_mesh_path = _download_mesh_file(job_id, mesh_file_info, run_dir)
        if not custom_mesh_path:
            _upload_and_fail(job_id, job['user_id'],
                             'Failed to download uploaded mesh file.',
                             run_dir=run_dir,
                             extra_result={'error_category': 'mesh_download_failed'})
            return True

    # Write task_settings.json — records all non-secret settings for post-hoc analysis.
    # Uploaded to Supabase Storage alongside other run files.
    task_settings = {
        'job_id': str(job_id),
        'user_id': job.get('user_id'),
        'created_at': job.get('created_at'),
        'model_provider': llm_config.get('model_provider') or 'openai-codex',
        'model_version': llm_config.get('model_version') or 'gpt-5.3-codex',
        'has_user_api_key': bool(llm_config.get('api_key')),
        'has_codex_token': bool(llm_config.get('codex_token')),
        'base_url': llm_config.get('base_url'),
        'pipeline_mode': job.get('pipeline_mode', 'auto'),
        'pre_run_end_time': job.get('pre_run_end_time'),
        'checkpoints': (job.get('pipeline_state') or {}).get('active_checkpoints'),
        'worker_id': WORKER_ID,
        'prompt_warnings': prompt_warnings if prompt_warnings else None,
        'has_custom_mesh': bool(custom_mesh_path),
        'mesh_original_name': (mesh_file_info or {}).get('original_name'),
    }
    try:
        with open(os.path.join(run_dir, "task_settings.json"), "w") as f:
            json.dump(task_settings, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Job {job_id}: failed to write task_settings.json: {e}")

    try:
        # Set env vars for Foam-Agent's Config.__post_init__() to read natively
        effective_provider = llm_config.get('model_provider') or 'openai-codex'
        effective_version = llm_config.get('model_version') or 'gpt-5.3-codex'

        # OpenAI-compatible providers (DeepSeek, Qwen): map to 'openai' for Foam-Agent
        # and set OPENAI_API_BASE so LangChain's ChatOpenAI routes to the correct endpoint.
        base_url = llm_config.get('base_url')
        if base_url:
            child_env['OPENAI_API_BASE'] = base_url
            logger.info(f"Job {job_id}: OPENAI_API_BASE={base_url}")
        if effective_provider in ('deepseek', 'qwen'):
            logger.info(f"Job {job_id}: mapping provider '{effective_provider}' → 'openai' (OpenAI-compatible)")
            effective_provider = 'openai'

        child_env['FOAMAGENT_MODEL_PROVIDER'] = effective_provider
        child_env['FOAMAGENT_MODEL_VERSION'] = effective_version
        child_env['FOAM_OUTPUT_DIR'] = os.path.abspath(output_path)
        child_env['FOAM_PROMPT_PATH'] = os.path.abspath(prompt_path)
        if custom_mesh_path:
            child_env['FOAM_CUSTOM_MESH_PATH'] = os.path.abspath(custom_mesh_path)
        logger.info(f"Job {job_id}: using provider={effective_provider}, model={effective_version}")
        if custom_mesh_path:
            logger.info(f"Job {job_id}: custom mesh → {custom_mesh_path}")

        # Config.__post_init__() reads FOAMAGENT_MODEL_PROVIDER/VERSION natively,
        # so no need for the inspect-based patching hack anymore.
        command = [
            "python", "-c",
            "import os,sys; sys.path.insert(0,'src'); "
            "from config import Config; from main import main; "
            "c=Config(); c.case_dir=os.environ['FOAM_OUTPUT_DIR']; "
            "main(open(os.environ['FOAM_PROMPT_PATH']).read(),c,"
            "os.environ.get('FOAM_CUSTOM_MESH_PATH'))"
        ]

        logger.info(f"Executing command for job {job_id}: {' '.join(command)}")
        logger.info(f"Log file for this run will be at: {log_path}")

        # Per-job timeout (user-configurable) or global default
        job_timeout = SIMULATION_TIMEOUT
        if job.get('timeout_minutes'):
            job_timeout = int(job['timeout_minutes']) * 60
            logger.info(f"Job {job_id}: using custom timeout {job['timeout_minutes']} min")

        # Run Foam-Agent subprocess with polling
        returncode, cancelled, timed_out, disk_exceeded = _run_subprocess_with_polling(
            command=command,
            cwd=FOAM_AGENT_DIR,
            env=child_env,
            log_path=log_path,
            job_id=job_id,
            timeout=job_timeout,
            run_dir=run_dir,
        )

        if _handle_cancelled_or_timeout(job_id, job['user_id'], run_dir, log_path,
                                        cancelled, timed_out, disk_exceeded):
            return True

        # Post-execution Allrun security audit
        allrun_audit = _run_allrun_audit(run_dir, job_id)

        # Handle completion based on return code
        if returncode == 0:
            logger.info(f"Job {job_id} Foam-Agent completed successfully.")
            _upload_and_complete(
                job_id, job['user_id'], run_dir, output_path, log_path, allrun_audit
            )
        else:
            logger.error(f"Job {job_id} failed. Check log file for details: {log_path}")
            # Diagnose known error patterns from log
            user_msg, err_cat = _diagnose_subprocess_failure(log_path, effective_provider)
            if user_msg:
                logger.info(f"Job {job_id}: diagnosed failure as '{err_cat}'")
                error_msg = user_msg
            else:
                error_msg = f"Foam-Agent script failed with return code {returncode}."
                # Classify OOM (killed by signal 9)
                if returncode == -9 or returncode == 137:
                    err_cat = 'oom'
                    error_msg = (
                        "Simulation was killed due to out-of-memory (OOM). "
                        "The mesh may be too large for single-core execution."
                    )
            _upload_and_fail(
                job_id, job['user_id'],
                error_msg,
                run_dir=run_dir,
                extra_result={
                    'log_path_on_server': log_path,
                    'allrun_audit': allrun_audit,
                    'error_category': err_cat,
                },
            )

    except Exception as e:
        logger.error(f"A critical error occurred while processing job {job_id}: {e}", exc_info=True)
        _upload_and_fail(
            job_id, job['user_id'],
            f"Worker script encountered an exception: {str(e)}",
            run_dir=run_dir,
        )

    finally:
        # Clean up temp Codex auth directory (contains OAuth token)
        if temp_codex_dir and os.path.isdir(temp_codex_dir):
            try:
                shutil.rmtree(temp_codex_dir)
                logger.info(f"Job {job_id}: cleaned up temp Codex auth dir")
            except Exception as e:
                logger.warning(f"Job {job_id}: failed to clean up Codex auth dir: {e}")

    return True


# --- 8. Health check HTTP server ---

_worker_stats = {
    'start_time': datetime.now(timezone.utc).isoformat(),
    'jobs_processed': 0,
    'jobs_succeeded': 0,
    'jobs_failed': 0,
    'current_job_id': None,
}
_stats_lock = threading.Lock()


def _update_stats(**kwargs):
    with _stats_lock:
        _worker_stats.update(kwargs)


def _increment_stat(key):
    with _stats_lock:
        _worker_stats[key] = _worker_stats.get(key, 0) + 1


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/health':
            self.send_response(404)
            self.end_headers()
            return
        with _stats_lock:
            stats = dict(_worker_stats)
        stats['worker_id'] = WORKER_ID
        stats['bound_pipeline_job_id'] = _bound_pipeline_job_id
        stats['status'] = 'busy' if stats.get('current_job_id') or _bound_pipeline_job_id else 'idle'
        start = datetime.fromisoformat(stats['start_time'])
        uptime = datetime.now(timezone.utc) - start
        stats['uptime_seconds'] = int(uptime.total_seconds())
        body = json.dumps(stats).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress default access logs


def _start_health_server():
    """Start health check HTTP server in a daemon thread."""
    if HEALTH_CHECK_PORT == 0:
        logger.info("Health check server disabled (HEALTH_CHECK_PORT=0)")
        return
    try:
        server = HTTPServer(('0.0.0.0', HEALTH_CHECK_PORT), _HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"[{WORKER_ID}] Health check server listening on port {HEALTH_CHECK_PORT}")
    except OSError as e:
        logger.warning(f"Failed to start health check server on port {HEALTH_CHECK_PORT}: {e}")


# --- 9. 主循环 ---

def _warmup_mcp_server():
    """Pre-start MCP server at Worker boot to avoid cold-start latency on first job."""
    try:
        _ensure_mcp_server()
        logger.info("MCP server pre-warmed successfully")
    except Exception as e:
        logger.warning(f"MCP server pre-warm failed (will retry on first job): {e}")


def main_loop():
    """
    无限循环，不断地寻找并处理任务。
    """
    # Start health check HTTP server (daemon thread)
    _start_health_server()

    # Recover any stale jobs from previous Worker crashes
    recover_stale_jobs()

    # Pre-start MCP server so the first controlled-pipeline job doesn't wait
    _warmup_mcp_server()

    logger.info(f"[{WORKER_ID}] Worker started. Looking for jobs...")
    while True:
        try:
            # Purge cycle: auto-expire old tasks + hard-delete expired soft-deleted (throttled internally)
            run_purge_cycle()

            processed_a_job = find_and_process_job()
            if not processed_a_job:
                # 如果没有任务，就休息一下
                time.sleep(2)  # 等待 2 秒（缩短以降低 checkpoint 确认后的延迟）
        except Exception as e:
            logger.error(f"An error occurred in the main loop: {e}", exc_info=True)
            time.sleep(30) # 如果主循环出错，等待更长时间再重试


# --- 9. 脚本入口 ---
if __name__ == "__main__":
    main_loop()
