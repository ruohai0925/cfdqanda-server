import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
# --- 新增这两行 ---
from dotenv import load_dotenv
load_dotenv()  # 自动读取同目录下的 .env 文件
# ------------------
from supabase import create_client, Client
from pydantic import BaseModel
from typing import Optional
import logging
import jwt
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# --- 1. 初始化与配置 ---

# 配置日志记录，方便我们调试
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    allow_methods=["*"],         # 允许所有 HTTP 方法 (GET, POST, etc.)
    allow_headers=["*"],         # 允许所有 HTTP 请求头
)
# ----------------------------------

# --- 2. 定义数据模型 ---

# LLM 配置模型 — 通用设计，不与特定 Agent 耦合
class LLMConfig(BaseModel):
    model_provider: Optional[str] = None   # e.g. "openai", "anthropic", "ollama"
    model_version: Optional[str] = None    # e.g. "gpt-4o", "claude-sonnet-4-5-20250929"
    api_key: Optional[str] = None          # 用户自带的 API key（仅用于本次任务）

# 使用 Pydantic 定义前端发送过来的请求体(body)应该长什么样
# 这可以提供自动的数据验证和生成 API 文档
# Note: user_id is no longer in the body — it comes from JWT (verify_jwt dependency)
class SimulationRequest(BaseModel):
    prompt: str
    llm_config: Optional[LLMConfig] = None  # 用户可选的 LLM 配置

class FeedbackRequest(BaseModel):
    file_path: str  # 文件路径，如 "output/log.blockMesh"
    feedback_content: str  # 反馈内容

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
        # 构建插入数据
        insert_data = {
            'prompt': sim_request.prompt,
            'user_id': user_id,
            'status': 'queued'  # 将初始状态明确设置为 '排队中'
        }
        # 如果用户提供了 LLM 配置，写入 llm_config JSONB 列
        if sim_request.llm_config:
            insert_data['llm_config'] = sim_request.llm_config.model_dump(exclude_none=True)

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

    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)
        # 捕获任何其他异常，并返回一个服务器内部错误
        raise HTTPException(status_code=500, detail=f"An internal server error occurred: {str(e)}")

# --- 4. (可选) 创建一个根端点用于测试 ---

@app.get("/")
def read_root():
    """
    一个简单的"健康检查"端点，用于确认服务器是否正在运行。
    """
    return {"message": "Foam-Agent API Server is running!"}


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

        # 检查任务是否完成
        if job['status'] not in ['completed', 'failed']:
            raise HTTPException(
                status_code=400,
                detail=f"Simulation {job_id} is not completed yet. Current status: {job['status']}"
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
        # 1. 验证任务是否存在
        response = supabase.table('simulations').select('*').eq('id', job_id).execute()
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
        # 写入本地 WSL 文件系统（Foam-Agent/runs/ 下）
        # ==========================================
        try:
            local_file_path = str(resolved_feedback_path)

            # 确保父目录存在 (防止报错)
            os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

            # 写入文件
            with open(local_file_path, "w", encoding="utf-8") as f:
                f.write(fb_request.feedback_content)

            logger.info(f"Feedback saved locally to: {local_file_path}")

        except Exception as local_error:
            # 如果本地写入失败（比如权限问题），记录日志但不中断请求
            logger.error(f"Failed to write local feedback file: {local_error}")

        # ==========================================

        # 5. 上传到 Supabase Storage (保持原有逻辑)
        storage_base_path = f"public/{user_id}/{job_id}"
        storage_feedback_path = f"{storage_base_path}/{feedback_file_path}"

        try:
            supabase.storage.from_("simulation_results").upload(
                path=storage_feedback_path,
                file=fb_request.feedback_content.encode('utf-8'),
                file_options={"content-type": "text/plain", "upsert": "true"}
            )
            logger.info(f"Feedback uploaded to Supabase: {storage_feedback_path}")
        except Exception as storage_error:
            logger.error(f"Storage upload failed: {storage_error}")
            raise HTTPException(status_code=500, detail=f"Storage upload failed: {str(storage_error)}")

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
