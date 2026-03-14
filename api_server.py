import os
import json
import urllib.request
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
# --- 新增这两行 ---
from dotenv import load_dotenv
load_dotenv()  # 自动读取同目录下的 .env 文件
# ------------------
from supabase import create_client, Client
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone
import logging
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
import jwt
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# --- 1. 初始化与配置 ---

# 配置日志记录，方便我们调试
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Thread pool for running sync I/O (file writes, storage uploads) without blocking the event loop
_io_executor = ThreadPoolExecutor(max_workers=4)

# Worker health check URL (for admin status endpoint).
# Docker Compose: http://worker:8001/health; bare metal: http://localhost:8001/health
WORKER_HEALTH_URL = os.environ.get("WORKER_HEALTH_URL", "http://localhost:8001/health")

# --- User quota configuration ---
USER_STORAGE_LIMIT_BYTES = int(os.environ.get("USER_STORAGE_LIMIT_MB", "2048")) * 1024 * 1024  # default 2 GB
USER_DAILY_TASK_LIMIT = int(os.environ.get("USER_DAILY_TASK_LIMIT", "5"))  # default 5 tasks/day
# Admin emails exempt from daily task limit (comma-separated)
_QUOTA_EXEMPT_EMAILS = set(
    e.strip().lower() for e in os.environ.get("QUOTA_EXEMPT_EMAILS", "").split(",") if e.strip()
)

# --- Foam-Agent 目录配置 ---
FOAM_AGENT_DIR = os.environ.get("FOAM_AGENT_DIR")
if not FOAM_AGENT_DIR:
    logger.error("FATAL: FOAM_AGENT_DIR is not set. Set it in .env to point to the Foam-Agent directory.")
    raise RuntimeError("FOAM_AGENT_DIR is not set in the environment variables.")
FOAM_AGENT_DIR = os.path.abspath(FOAM_AGENT_DIR)
logger.info(f"FOAM_AGENT_DIR resolved to: {FOAM_AGENT_DIR}")

# 从我们之前在 Part A 设置的环境变量中加载 Supabase 的配置
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

# 检查环境变量是否已设置，如果缺失则程序无法运行
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    logger.error("FATAL: Supabase credentials are not set in the environment variables.")
    raise RuntimeError("Supabase credentials are not set in the environment variables.")

# 创建 Supabase 客户端实例
# 注意：在后端，我们使用权限更高的 service_role key
# 因为 API 服务器需要有权限无视 RLS 策略来写入数据
logger.info("Initializing Supabase client...")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
logger.info("Supabase client initialized successfully.")

# --- JWT Authentication ---
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")
if not SUPABASE_JWT_SECRET:
    logger.warning("SUPABASE_JWT_SECRET is not set. POST endpoints will reject all requests.")

security = HTTPBearer()

async def verify_jwt(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    Verify Supabase JWT from Authorization header.
    Returns the authenticated user_id (from token's 'sub' claim).
    """
    if not SUPABASE_JWT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="JWT verification is not configured (SUPABASE_JWT_SECRET missing)"
        )

    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated"
        )
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token: missing user ID")
        return user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

# 创建 FastAPI 应用实例
app = FastAPI()

# --- Rate Limiting ---
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- 在这里添加 CORS 中间件 ---

# 1. 定义一个 "白名单" 列表，包含所有我们允许的来源
#    生产/本地域名写在这里；额外来源可通过环境变量 EXTRA_CORS_ORIGINS 添加（逗号分隔）
_origins_base = [
    "https://cfdqanda.com",
    "https://www.cfdqanda.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
_extra = os.environ.get("EXTRA_CORS_ORIGINS", "")
origins = _origins_base + [o.strip() for o in _extra.split(",") if o.strip()]

# 2. 将 CORS 中间件添加到我们的 FastAPI 应用中
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,       # 允许 "白名单" 中的来源
    allow_credentials=True,    # 允许携带 cookie
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],  # Only methods our API uses
    allow_headers=["Content-Type", "Authorization"],  # JSON body + JWT Bearer token
)
# ----------------------------------

# --- 2. 定义数据模型 ---

# LLM 配置模型 — 通用设计，不与特定 Agent 耦合
class LLMConfig(BaseModel):
    model_provider: Optional[str] = None   # e.g. "openai", "openai-codex", "anthropic"
    model_version: Optional[str] = None    # e.g. "gpt-5.3-codex", "gpt-4o", "claude-sonnet-4-5-20250929"
    api_key: Optional[str] = None          # User-provided API key (for openai/anthropic providers)
    codex_token: Optional[str] = None      # ChatGPT/Codex OAuth access token (for openai-codex provider)

# 使用 Pydantic 定义前端发送过来的请求体(body)应该长什么样
# 这可以提供自动的数据验证和生成 API 文档
# Note: user_id is no longer in the body — it comes from JWT (verify_jwt dependency)
class SimulationRequest(BaseModel):
    prompt: str
    llm_config: Optional[LLMConfig] = None  # 用户可选的 LLM 配置
    pre_run_end_time: Optional[int] = None  # Pre-run timesteps: None=default(10), -1=disabled, positive=custom
    pipeline_mode: str = 'auto'  # 'auto' (subprocess one-shot) or 'controlled' (MCP stage-by-stage)
    checkpoints: Optional[List[str]] = None  # Active checkpoints: ['files_review', 'pre_run_review', 'plan_review']

class FeedbackRequest(BaseModel):
    file_path: str  # 文件路径，如 "output/log.blockMesh"
    feedback_content: str  # 反馈内容

class RatingRequest(BaseModel):
    rating: int  # 1=success, 2=partial success, 3=failed
    comment: Optional[str] = None
    stage: Optional[str] = None  # pipeline stage (e.g. 'files_review', 'pre_run_review', 'completed')

# --- 3. 创建 API 端点 (Endpoint) ---

@app.post("/api/v1/simulations")
@limiter.limit("5/minute")
async def create_simulation_task(request: Request, sim_request: SimulationRequest, user_id: str = Depends(verify_jwt)):
    """
    接收一个新的仿真请求，并将其作为任务插入数据库，状态为 'queued'
    Requires a valid Supabase JWT in the Authorization header.
    """
    logger.info(f"Received new simulation request for user: {user_id}")
    try:
        # --- Quota checks ---

        # 1. Daily task limit (count tasks created today by this user)
        # Check if user is exempt (admin/test accounts)
        is_exempt = False
        if _QUOTA_EXEMPT_EMAILS:
            try:
                auth_header = request.headers.get("authorization", "")
                if auth_header.startswith("Bearer "):
                    token_payload = jwt.decode(
                        auth_header[7:], SUPABASE_JWT_SECRET,
                        algorithms=["HS256"], audience="authenticated"
                    )
                    user_email = token_payload.get("email", "").lower()
                    is_exempt = user_email in _QUOTA_EXEMPT_EMAILS
            except Exception:
                pass  # if decode fails, not exempt

        if not is_exempt:
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            today_resp = (
                supabase.table('simulations')
                .select('id', count='exact')
                .eq('user_id', user_id)
                .gte('created_at', today_start)
                .execute()
            )
            today_count = today_resp.count if today_resp.count is not None else len(today_resp.data)
            if today_count >= USER_DAILY_TASK_LIMIT:
                logger.warning(f"User {user_id} hit daily task limit ({today_count}/{USER_DAILY_TASK_LIMIT})")
                raise HTTPException(
                    status_code=429,
                    detail=f"Daily task limit reached ({USER_DAILY_TASK_LIMIT} tasks/day). Please try again tomorrow."
                )

        # 2. Storage quota (reuse cached storage calculation)
        storage_total = _get_user_storage_bytes(user_id)
        if storage_total >= USER_STORAGE_LIMIT_BYTES:
            logger.warning(f"User {user_id} exceeded storage quota ({_format_bytes(storage_total)} / {_format_bytes(USER_STORAGE_LIMIT_BYTES)})")
            raise HTTPException(
                status_code=403,
                detail=f"Storage quota exceeded ({_format_bytes(storage_total)} / {_format_bytes(USER_STORAGE_LIMIT_BYTES)}). "
                       f"Please delete old tasks to free space."
            )

        # 构建插入数据
        insert_data = {
            'prompt': sim_request.prompt,
            'user_id': user_id,
            'status': 'queued'  # 将初始状态明确设置为 '排队中'
        }
        # 如果用户提供了 LLM 配置，写入 llm_config JSONB 列
        if sim_request.llm_config:
            insert_data['llm_config'] = sim_request.llm_config.model_dump(exclude_none=True)
        # Pre-run end time for checkpoint mechanism
        if sim_request.pre_run_end_time is not None:
            insert_data['pre_run_end_time'] = sim_request.pre_run_end_time
        # Pipeline mode: 'auto' (default) or 'controlled' (MCP stage-by-stage)
        if sim_request.pipeline_mode != 'auto':
            insert_data['pipeline_mode'] = sim_request.pipeline_mode
            if sim_request.checkpoints:
                insert_data['pipeline_state'] = {
                    'active_checkpoints': sim_request.checkpoints
                }

        # 将新任务插入到 'simulations' 表中
        response = supabase.table('simulations').insert(insert_data).execute()

        # 检查 Supabase 的响应，看是否有数据被返回
        if response.data:
            new_task = response.data[0]
            logger.info(f"Successfully queued task {new_task['id']} for user {user_id}")
            # 将新创建的任务记录返回给前端，这是一个好的实践
            return {"status": "success", "message": "Simulation task queued successfully.", "task": new_task}
        else:
            # 如果 Supabase 返回了错误（即使没有抛出异常）
            error_message = response.error.message if response.error else "Unknown error from Supabase"
            logger.error(f"Failed to insert task into database: {error_message}")
            raise HTTPException(status_code=500, detail=f"Failed to insert task into database: {error_message}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An internal server error occurred: {str(e)}")

# --- 4. (可选) 创建一个根端点用于测试 ---

@app.get("/")
def read_root():
    """
    一个简单的"健康检查"端点，用于确认服务器是否正在运行。
    """
    return {"message": "Foam-Agent API Server is running!"}


# --- Daily usage endpoint ---

@app.get("/api/v1/user/daily-usage")
async def get_daily_usage(request: Request, user_id: str = Depends(verify_jwt)):
    """Return the user's daily task usage and limit."""
    try:
        # Check if user is exempt
        is_exempt = False
        if _QUOTA_EXEMPT_EMAILS:
            try:
                auth_header = request.headers.get("authorization", "")
                if auth_header.startswith("Bearer "):
                    token_payload = jwt.decode(
                        auth_header[7:], SUPABASE_JWT_SECRET,
                        algorithms=["HS256"], audience="authenticated"
                    )
                    is_exempt = token_payload.get("email", "").lower() in _QUOTA_EXEMPT_EMAILS
            except Exception:
                pass

        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        today_resp = (
            supabase.table('simulations')
            .select('id', count='exact')
            .eq('user_id', user_id)
            .gte('created_at', today_start)
            .execute()
        )
        today_count = today_resp.count if today_resp.count is not None else len(today_resp.data)
        effective_limit = 999999 if is_exempt else USER_DAILY_TASK_LIMIT
        return {
            "used": today_count,
            "limit": effective_limit,
            "remaining": max(0, effective_limit - today_count),
            "exempt": is_exempt,
        }
    except Exception as e:
        logger.error(f"Failed to fetch daily usage: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch daily usage")


# --- Queue status endpoint ---

@app.get("/api/v1/queue-status")
async def get_queue_status():
    """Return current queue depth and ordered list of queued job IDs.

    Used by the frontend to show queue position for waiting tasks.
    No auth required — only returns job IDs and counts, no sensitive data.
    """
    try:
        queued = supabase.table('simulations') \
            .select('id, created_at') \
            .eq('status', 'queued') \
            .is_('deleted_at', None) \
            .order('created_at', desc=False) \
            .execute()
        running = supabase.table('simulations') \
            .select('id') \
            .eq('status', 'running') \
            .is_('deleted_at', None) \
            .execute()
        return {
            "queued_count": len(queued.data),
            "running_count": len(running.data),
            "queued_ids": [str(j["id"]) for j in queued.data],
        }
    except Exception as e:
        logger.error(f"Failed to fetch queue status: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch queue status")


# --- 5. 文件浏览相关端点（可选，用于前端快速获取文件树）---

@app.get("/api/v1/simulations/{job_id}/files")
async def get_file_tree(job_id: int):
    """
    获取任务的文件树结构。
    这个端点是可选的，因为前端可以直接使用Supabase Storage的list() API。
    但提供这个端点可以让前端更快地获取文件树结构（无需遍历Storage）。
    """
    try:
        # 从数据库查询任务信息
        response = supabase.table('simulations').select('*').eq('id', job_id).execute()

        if not response.data:
            raise HTTPException(status_code=404, detail=f"Simulation {job_id} not found")

        job = response.data[0]

        # Check if task has files to browse
        if job['status'] not in ['completed', 'failed', 'checkpoint', 'cancelled']:
            raise HTTPException(
                status_code=400,
                detail=f"Simulation {job_id} has no files yet. Current status: {job['status']}"
            )

        # 从result_data中获取文件树
        result_data = job.get('result_data', {})

        if 'file_tree' not in result_data:
            # 如果文件树不存在，返回错误
            raise HTTPException(
                status_code=404,
                detail=f"File tree not found for simulation {job_id}. This might be an old task."
            )

        # 返回文件树和存储路径信息
        return {
            "job_id": job_id,
            "status": job['status'],
            "storage_base_path": result_data.get('storage_base_path'),
            "file_tree": result_data.get('file_tree'),
            "upload_stats": result_data.get('upload_stats', {})
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"An error occurred while getting file tree for job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An internal server error occurred: {str(e)}")


# --- 修改 api_server.py 中的 submit_feedback 函数 ---

@app.post("/api/v1/simulations/{job_id}/feedback")
@limiter.limit("10/minute")
async def submit_feedback(request: Request, job_id: int, fb_request: FeedbackRequest, user_id: str = Depends(verify_jwt)):
    """
    提交文件反馈。
    Requires a valid Supabase JWT in the Authorization header.
    1. [新增] 保存到本地 WSL 文件系统 (Foam-Agent/runs/{job_id}/...)
    2. 上传到 Supabase Storage (云端备份)
    """
    try:
        # 1. 验证任务是否存在（只查必要字段，避免传输大量 result_data）
        response = supabase.table('simulations').select('id, user_id').eq('id', job_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail=f"Simulation {job_id} not found")

        job = response.data[0]

        # 2. 验证权限 (user_id from JWT, not from request body)
        if job['user_id'] != user_id:
            raise HTTPException(status_code=403, detail="Permission denied")

        # 3. 验证大小 (限制 5KB)
        feedback_size = len(fb_request.feedback_content.encode('utf-8'))
        if feedback_size > 5120 or feedback_size == 0:
            raise HTTPException(status_code=400, detail="Invalid feedback size")

        # 4. 构建文件名
        # 逻辑：原文件 "output/log.blockMesh" -> 反馈文件 "output/log.blockMesh_feedback"
        feedback_file_path = f"{fb_request.file_path}_feedback"

        # 5. Path traversal protection: ensure the resolved path stays within runs/{job_id}/
        job_base_dir = Path(FOAM_AGENT_DIR, "runs", str(job_id)).resolve()
        resolved_feedback_path = (job_base_dir / feedback_file_path).resolve()
        if not str(resolved_feedback_path).startswith(str(job_base_dir) + os.sep) and resolved_feedback_path != job_base_dir:
            logger.warning(f"Path traversal attempt blocked: file_path='{fb_request.file_path}' resolved to '{resolved_feedback_path}'")
            raise HTTPException(status_code=400, detail="Invalid file_path: path traversal is not allowed")

        # ==========================================
        # 并行执行：本地写入 + 云端上传（避免串行阻塞）
        # ==========================================
        local_file_path = str(resolved_feedback_path)
        storage_base_path = f"public/{user_id}/{job_id}"
        storage_feedback_path = f"{storage_base_path}/{feedback_file_path}"
        loop = asyncio.get_event_loop()

        def _write_local():
            os.makedirs(os.path.dirname(local_file_path), exist_ok=True)
            with open(local_file_path, "w", encoding="utf-8") as f:
                f.write(fb_request.feedback_content)

        def _upload_cloud():
            supabase.storage.from_("simulation_results").upload(
                path=storage_feedback_path,
                file=fb_request.feedback_content.encode('utf-8'),
                file_options={"content-type": "text/plain", "upsert": "true"}
            )

        local_task = loop.run_in_executor(_io_executor, _write_local)
        cloud_task = loop.run_in_executor(_io_executor, _upload_cloud)

        # 等待两个任务完成，收集结果
        results = await asyncio.gather(local_task, cloud_task, return_exceptions=True)

        # 处理本地写入结果（失败不中断）
        if isinstance(results[0], Exception):
            logger.error(f"Failed to write local feedback file: {results[0]}")
        else:
            logger.info(f"Feedback saved locally to: {local_file_path}")

        # 处理云端上传结果（失败则报错）
        if isinstance(results[1], Exception):
            logger.error(f"Storage upload failed: {results[1]}")
            raise HTTPException(status_code=500, detail=f"Storage upload failed: {str(results[1])}")
        else:
            logger.info(f"Feedback uploaded to Supabase: {storage_feedback_path}")

        return {
            "status": "success",
            "message": "Feedback submitted successfully (Local & Cloud)",
            "local_path": local_file_path,
            "cloud_path": storage_feedback_path
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in submit_feedback: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# --- 6. Task-level rating endpoint ---

@app.patch("/api/v1/simulations/{job_id}/rating")
@limiter.limit("10/minute")
async def submit_rating(request: Request, job_id: str, rating_request: RatingRequest, user_id: str = Depends(verify_jwt)):
    """
    Submit a task-level rating (1=success, 2=partial, 3=failed) with optional comment.
    Requires a valid Supabase JWT. Only the task owner can rate.
    """
    if rating_request.rating not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="Rating must be 1 (success), 2 (partial), or 3 (failed)")

    if rating_request.comment and len(rating_request.comment) > 500:
        raise HTTPException(status_code=400, detail="Comment must be 500 characters or less")

    try:
        response = supabase.table('simulations').select(
            'id, user_id, stage_ratings, pipeline_mode, pipeline_stage, status'
        ).eq('id', job_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail=f"Simulation {job_id} not found")

        job = response.data[0]
        if job['user_id'] != user_id:
            raise HTTPException(status_code=403, detail="Permission denied")

        stage = rating_request.stage
        # Auto-derive stage for controlled pipeline when frontend doesn't pass it
        if not stage and job.get('pipeline_mode') == 'controlled':
            if job.get('status') == 'checkpoint':
                stage = job.get('pipeline_stage')
            elif job.get('status') == 'completed':
                stage = 'completed'
            elif job.get('status') == 'failed':
                stage = job.get('pipeline_stage') or 'failed'

        if stage:
            # Per-stage rating: store in stage_ratings JSONB (accumulative, never overwritten)
            stage_ratings = job.get('stage_ratings') or {}
            stage_ratings[stage] = {
                'rating': rating_request.rating,
                'comment': rating_request.comment or '',
            }
            # Also update user_rating/user_comment so the latest rating is always accessible
            update_data = {
                'stage_ratings': stage_ratings,
                'user_rating': rating_request.rating,
                'user_comment': rating_request.comment or '',
            }
        else:
            # Legacy: store in user_rating/user_comment columns (auto mode)
            update_data = {'user_rating': rating_request.rating}
            if rating_request.comment is not None:
                update_data['user_comment'] = rating_request.comment

        supabase.table('simulations').update(update_data).eq('id', job_id).execute()

        logger.info(f"Rating {rating_request.rating} submitted for job {job_id} stage={stage} by user {user_id}")
        return {"status": "success", "message": "Rating submitted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in submit_rating: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# --- 7. Soft-delete and restore endpoints ---

@app.delete("/api/v1/simulations/{job_id}")
async def soft_delete_simulation(job_id: str, user_id: str = Depends(verify_jwt)):
    """
    Soft-delete a simulation by setting deleted_at timestamp.
    Cannot delete a running job. Requires ownership.
    """
    try:
        response = supabase.table('simulations').select('id, user_id, status').eq('id', job_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail=f"Simulation {job_id} not found")

        job = response.data[0]
        if job['user_id'] != user_id:
            raise HTTPException(status_code=403, detail="Permission denied")
        if job['status'] == 'running':
            raise HTTPException(status_code=409, detail="Cannot delete a running simulation")

        supabase.table('simulations').update(
            {'deleted_at': datetime.now(timezone.utc).isoformat()}
        ).eq('id', job_id).execute()

        # Invalidate storage cache so quota check reflects the deletion
        _storage_cache.pop(user_id, None)

        logger.info(f"Soft-deleted simulation {job_id} by user {user_id}")
        return {"status": "success", "message": f"Simulation {job_id} deleted"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in soft_delete_simulation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/simulations/{job_id}/cancel")
@limiter.limit("10/minute")
async def cancel_simulation(request: Request, job_id: str, user_id: str = Depends(verify_jwt)):
    """
    Cancel a queued or running simulation.
    Sets status to 'cancelled'. Worker will detect this and kill the subprocess.
    """
    try:
        response = supabase.table('simulations').select('id, user_id, status').eq('id', job_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail=f"Simulation {job_id} not found")

        job = response.data[0]
        if job['user_id'] != user_id:
            raise HTTPException(status_code=403, detail="Permission denied")
        if job['status'] not in ('queued', 'running', 'checkpoint'):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot cancel a simulation with status '{job['status']}'. Only queued, running, or checkpoint simulations can be cancelled."
            )

        supabase.table('simulations').update(
            {'status': 'cancelled'}
        ).eq('id', job_id).execute()

        logger.info(f"Simulation {job_id} cancelled by user {user_id}")
        return {"status": "success", "message": f"Simulation {job_id} cancelled"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in cancel_simulation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# --- 8. Stage confirm/reject (controlled pipeline) ---

@app.post("/api/v1/simulations/{job_id}/stage/confirm")
@limiter.limit("10/minute")
async def confirm_stage(request: Request, job_id: str, user_id: str = Depends(verify_jwt)):
    """
    User confirms current pipeline stage, allowing the pipeline to advance.
    Only used by controlled pipeline mode. Re-queues the job so Worker picks it up.
    Accepts optional comment to record user feedback for the stage.
    """
    try:
        body = await request.json() if request.headers.get('content-type', '').startswith('application/json') else {}
    except Exception:
        body = {}
    comment = (body.get('comment') or '').strip()

    try:
        response = supabase.table('simulations').select(
            'id, user_id, status, pipeline_stage, pipeline_state'
        ).eq('id', job_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail=f"Simulation {job_id} not found")

        job = response.data[0]
        if job['user_id'] != user_id:
            raise HTTPException(status_code=403, detail="Permission denied")
        if job['status'] != 'checkpoint':
            raise HTTPException(
                status_code=409,
                detail=f"Cannot confirm: simulation status is '{job['status']}', expected 'checkpoint'."
            )

        pipeline_stage = job.get('pipeline_stage')
        pipeline_state = job.get('pipeline_state') or {}

        # Append stage feedback (accumulative, never overwritten)
        stage_feedback = pipeline_state.setdefault('stage_feedback', [])
        stage_feedback.append({
            'stage': pipeline_stage,
            'action': 'confirm',
            'comment': comment,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

        # Keep pipeline_stage as-is so Worker knows which stage was confirmed
        # and routes to the next one
        supabase.table('simulations').update({
            'status': 'queued',
            'pipeline_state': pipeline_state,
        }).eq('id', job_id).execute()

        logger.info(f"Stage confirmed for job {job_id} (stage={pipeline_stage}, comment={'yes' if comment else 'none'})")
        return {"status": "success", "message": f"Stage '{pipeline_stage}' confirmed. Pipeline will continue."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in confirm_stage: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/simulations/{job_id}/stage/reject")
@limiter.limit("10/minute")
async def reject_stage(request: Request, job_id: str, user_id: str = Depends(verify_jwt)):
    """
    User rejects current pipeline stage. Marks the simulation as failed.
    Used by controlled pipeline mode. Preserves result_data and stage feedback.
    """
    try:
        body = await request.json() if request.headers.get('content-type', '').startswith('application/json') else {}
    except Exception:
        body = {}
    comment = (body.get('comment') or '').strip()

    try:
        response = supabase.table('simulations').select(
            'id, user_id, status, pipeline_mode, pipeline_stage, pipeline_state, result_data'
        ).eq('id', job_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail=f"Simulation {job_id} not found")

        job = response.data[0]
        if job['user_id'] != user_id:
            raise HTTPException(status_code=403, detail="Permission denied")
        if job['status'] != 'checkpoint':
            raise HTTPException(
                status_code=409,
                detail=f"Cannot reject: simulation status is '{job['status']}', expected 'checkpoint'."
            )

        pipeline_stage = job.get('pipeline_stage')
        pipeline_state = job.get('pipeline_state') or {}

        # Append stage feedback (accumulative, never overwritten)
        stage_feedback = pipeline_state.setdefault('stage_feedback', [])
        stage_feedback.append({
            'stage': pipeline_stage,
            'action': 'reject',
            'comment': comment,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

        existing_result = job.get('result_data') or {}
        existing_result['rejected_stage'] = pipeline_stage
        # Persist all stage feedback into result_data (pipeline_state may be cleared)
        existing_result['stage_feedback'] = stage_feedback

        supabase.table('simulations').update({
            'status': 'failed',
            'pipeline_state': pipeline_state,
            'result_data': existing_result,
        }).eq('id', job_id).execute()

        logger.info(f"Stage '{pipeline_stage}' rejected for job {job_id} (comment={'yes' if comment else 'none'}). Marked as failed.")
        return {"status": "success", "message": f"Stage '{pipeline_stage}' rejected. Simulation marked as failed."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in reject_stage: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# --- 9. Restore endpoint ---

@app.post("/api/v1/simulations/{job_id}/restore")
async def restore_simulation(job_id: str, user_id: str = Depends(verify_jwt)):
    """
    Restore a soft-deleted simulation by clearing deleted_at.
    """
    try:
        response = supabase.table('simulations').select('id, user_id, deleted_at').eq('id', job_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail=f"Simulation {job_id} not found")

        job = response.data[0]
        if job['user_id'] != user_id:
            raise HTTPException(status_code=403, detail="Permission denied")
        if not job.get('deleted_at'):
            raise HTTPException(status_code=400, detail="Simulation is not deleted")

        supabase.table('simulations').update(
            {'deleted_at': None}
        ).eq('id', job_id).execute()

        # Invalidate storage cache (restored task re-counts toward quota)
        _storage_cache.pop(user_id, None)

        logger.info(f"Restored simulation {job_id} by user {user_id}")
        return {"status": "success", "message": f"Simulation {job_id} restored"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in restore_simulation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# --- 10. Storage usage endpoint ---

def _format_bytes(size_bytes: int) -> str:
    """Format bytes into human-readable string."""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}" if i > 0 else f"{int(size)} B"


def _get_storage_dir_size(prefix: str) -> int:
    """
    Recursively sum file sizes under a Supabase Storage prefix.
    Used as fallback when upload_stats.total_bytes is not available.
    """
    total = 0
    try:
        listed = supabase.storage.from_('simulation_results').list(prefix)
        if not listed:
            return 0
        for item in listed:
            item_path = f"{prefix}/{item['name']}"
            if item.get('id') is None:
                # Directory — recurse
                total += _get_storage_dir_size(item_path)
            else:
                # File — metadata includes size in bytes
                meta = item.get('metadata') or {}
                total += meta.get('size', 0)
    except Exception:
        pass
    return total


# In-memory cache: { user_id: { "result": {...}, "expires": timestamp } }
_storage_cache = {}
_STORAGE_CACHE_TTL = 300  # 5 minutes


def _get_user_storage_bytes(user_id: str) -> int:
    """
    Return total storage bytes for a user. Uses cache when available.
    Called by both the storage endpoint and the quota check in create_simulation_task.
    """
    cached = _storage_cache.get(user_id)
    if cached and time.time() < cached["expires"]:
        return cached["result"]["total_bytes"]

    try:
        response = (
            supabase.table('simulations')
            .select('id, result_data')
            .eq('user_id', user_id)
            .is_('deleted_at', 'null')
            .execute()
        )
        total = 0
        for row in response.data:
            rd = row.get('result_data') or {}
            stats = rd.get('upload_stats') or {}
            recorded = stats.get('total_bytes')
            if recorded is not None and recorded > 0:
                total += recorded
            else:
                storage_base = rd.get('storage_base_path')
                if storage_base:
                    total += _get_storage_dir_size(storage_base)
        return total
    except Exception as e:
        logger.error(f"Error computing storage for quota check: {e}")
        return 0  # fail open: allow submission if storage check fails


@app.get("/api/v1/user/storage")
@limiter.limit("10/minute")
async def get_user_storage(request: Request, user_id: str = Depends(verify_jwt)):
    """
    Return storage usage summary for the authenticated user.
    Uses pre-recorded total_bytes when available, falls back to
    querying Supabase Storage metadata for historical tasks.
    Results cached for 5 minutes to avoid flooding Supabase Storage API.
    """
    # Check cache first (skip if user just deleted data)
    cached = _storage_cache.get(user_id)
    if cached and time.time() < cached["expires"]:
        return cached["result"]

    try:
        response = (
            supabase.table('simulations')
            .select('id, status, result_data, created_at')
            .eq('user_id', user_id)
            .is_('deleted_at', 'null')
            .execute()
        )

        total_bytes = 0
        task_count = len(response.data)
        breakdown = {}
        per_task = {}  # task_id -> bytes

        for row in response.data:
            status = row.get('status', 'unknown')
            breakdown[status] = breakdown.get(status, 0) + 1
            task_bytes = 0

            rd = row.get('result_data') or {}
            stats = rd.get('upload_stats') or {}
            recorded = stats.get('total_bytes')

            if recorded is not None and recorded > 0:
                # New tasks: use pre-recorded value (fast)
                task_bytes = recorded
            else:
                # Historical tasks: query Storage metadata (fallback)
                storage_base = rd.get('storage_base_path')
                if storage_base:
                    task_bytes = _get_storage_dir_size(storage_base)

            total_bytes += task_bytes
            if task_bytes > 0:
                per_task[str(row['id'])] = task_bytes

        result = {
            "total_bytes": total_bytes,
            "total_display": _format_bytes(total_bytes),
            "task_count": task_count,
            "breakdown": breakdown,
            "per_task": per_task,
        }

        # Cache the result
        _storage_cache[user_id] = {"result": result, "expires": time.time() + _STORAGE_CACHE_TTL}
        return result

    except Exception as e:
        logger.error(f"Error in get_user_storage: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# --- 11. User profile creation (fallback when RLS blocks frontend insert) ---

class ProfileRequest(BaseModel):
    display_name: str
    organization: Optional[str] = None

@app.post("/api/v1/users/me/profile")
@limiter.limit("5/minute")
async def create_profile(request: Request, profile: ProfileRequest, user_id: str = Depends(verify_jwt)):
    """
    Create user profile via service_role (bypasses RLS).
    Called by frontend as fallback when direct Supabase insert fails.
    """
    try:
        # Check if profile already exists
        existing = supabase.table('user_profiles').select('id').eq('id', user_id).execute()
        if existing.data:
            return {"status": "exists", "message": "Profile already exists"}

        supabase.table('user_profiles').insert({
            'id': user_id,
            'display_name': profile.display_name,
            'organization': profile.organization,
            'privacy_accepted_at': datetime.now(timezone.utc).isoformat(),
        }).execute()

        logger.info(f"Created profile for user {user_id}")
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error creating profile: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# --- 12. Account deletion (GDPR "right to erasure") ---

@app.delete("/api/v1/users/me")
@limiter.limit("3/minute")
async def delete_account(request: Request, user_id: str = Depends(verify_jwt)):
    """
    Permanently delete the authenticated user's account and all associated data.
    Deletion order: simulations (hard delete) → Storage files → user_profiles → Auth user.
    """
    logger.info(f"Account deletion requested by user {user_id}")

    errors = []

    # 1. Get all user simulations (including soft-deleted)
    try:
        sims = supabase.table('simulations').select(
            'id, result_data'
        ).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"Failed to query simulations for deletion: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to query user data: {e}")

    # 2. Delete Storage files for each simulation
    for row in (sims.data or []):
        rd = row.get('result_data') or {}
        storage_base = rd.get('storage_base_path')
        if storage_base:
            try:
                # List and delete all files under the storage prefix
                files = supabase.storage.from_('simulation_results').list(storage_base)
                if files:
                    paths = [f"{storage_base}/{f['name']}" for f in files if f.get('id') is not None]
                    if paths:
                        supabase.storage.from_('simulation_results').remove(paths)
            except Exception as e:
                errors.append(f"Storage cleanup for sim {row['id']}: {e}")
                logger.warning(f"Storage cleanup error for sim {row['id']}: {e}")

    # 3. Hard-delete all simulations from DB
    try:
        supabase.table('simulations').delete().eq('user_id', user_id).execute()
        logger.info(f"Deleted all simulations for user {user_id}")
    except Exception as e:
        errors.append(f"Simulations delete: {e}")
        logger.error(f"Failed to delete simulations: {e}", exc_info=True)

    # 4. Delete user profile
    try:
        supabase.table('user_profiles').delete().eq('id', user_id).execute()
        logger.info(f"Deleted user_profiles for user {user_id}")
    except Exception as e:
        errors.append(f"Profile delete: {e}")
        logger.warning(f"Failed to delete user_profiles: {e}")

    # 5. Delete Auth user (uses service_role admin API)
    try:
        supabase.auth.admin.delete_user(user_id)
        logger.info(f"Deleted Auth user {user_id}")
    except Exception as e:
        errors.append(f"Auth delete: {e}")
        logger.error(f"Failed to delete Auth user: {e}", exc_info=True)

    # Clear storage cache
    _storage_cache.pop(user_id, None)

    if errors:
        logger.warning(f"Account deletion completed with errors for user {user_id}: {errors}")
        return {
            "status": "partial",
            "message": "Account deleted with some cleanup errors. Please contact support if issues persist.",
            "errors": errors,
        }

    logger.info(f"Account fully deleted for user {user_id}")
    return {"status": "success", "message": "Account and all associated data have been permanently deleted."}


# --- 13. Admin status endpoint (monitoring) ---

@app.api_route("/api/v1/admin/status", methods=["GET", "HEAD"])
def admin_status():
    """
    Aggregated health check: API server status + Worker health.
    No authentication required — used by UptimeRobot or similar external monitors.
    Returns HTTP 200 if everything is healthy, 503 if Worker is unreachable.
    """
    worker_status = None
    worker_error = None

    try:
        req = urllib.request.Request(WORKER_HEALTH_URL, method='GET')
        with urllib.request.urlopen(req, timeout=3) as resp:
            worker_status = json.loads(resp.read())
    except Exception as e:
        worker_error = str(e)

    all_healthy = worker_status is not None
    return JSONResponse(
        status_code=200 if all_healthy else 503,
        content={
            "api": "ok",
            "worker": worker_status if worker_status else {"status": "unreachable", "error": worker_error},
            "all_healthy": all_healthy,
        },
    )
