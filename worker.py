import os
import time
import logging
import subprocess
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from supabase import create_client, Client
from allrun_validator import audit_allrun_scripts

# --- 1. 初始化与配置 ---

# 配置日志，方便我们观察 Worker 的一举一动
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 新增这两行 ---
from dotenv import load_dotenv
load_dotenv()  # 自动读取同目录下的 .env 文件
# ------------------

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

# 从环境变量加载 Supabase 配置
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    logger.error("FATAL: Supabase credentials are not set in the environment variables.")
    raise RuntimeError("Supabase credentials are not set in the environment variables.")

logger.info("Initializing Supabase client for Worker...")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
logger.info("Supabase client initialized successfully.")

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

    # 使用os.walk遍历所有文件和目录
    for root, dirs, files in os.walk(directory_path):
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
        (uploaded_count, failed_count): 成功和失败的文件数量
    """
    uploaded_count = 0
    failed_count = 0

    base_path = Path(local_dir)
    if not base_path.exists():
        logger.error(f"Local directory {local_dir} does not exist")
        return uploaded_count, failed_count

    # 遍历所有文件
    for root, dirs, files in os.walk(local_dir):
        for file in files:
            local_file_path = os.path.join(root, file)

            # 计算相对于local_dir的路径
            rel_file_path = os.path.relpath(local_file_path, local_dir)
            storage_file_path = f"{storage_base_path}/{rel_file_path}".replace('\\', '/')

            try:
                # 读取文件内容
                with open(local_file_path, 'rb') as f:
                    file_content = f.read()

                # 获取文件类型
                _, ext = os.path.splitext(file)
                file_type = ext[1:].lower() if ext else 'unknown'
                content_type = get_content_type(file_type)

                # 上传到Storage
                # 注意：如果文件已存在，需要先删除或使用upsert
                try:
                    # 尝试删除已存在的文件（如果有）
                    supabase_client.storage.from_("simulation_results").remove([storage_file_path])
                except:
                    pass  # 如果文件不存在，忽略错误

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

    logger.info(f"Upload complete: {uploaded_count} files uploaded, {failed_count} files failed")
    return uploaded_count, failed_count


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
            supabase.table('simulations').update(
                {'status': 'queued'}
            ).eq('id', job_id).execute()
            logger.info(f"Stale job {job_id} reset to 'queued' successfully.")

    except Exception as e:
        logger.error(f"Error during stale job recovery: {e}", exc_info=True)


# --- 4. 核心工作逻辑 ---

def find_and_process_job():
    """
    查找一个'queued'状态的任务并处理它。
    如果没有任务，则返回 False。如果处理了任务，则返回 True。

    Uses the claim_next_job() PostgreSQL RPC function which atomically
    selects and locks the next queued job using FOR UPDATE SKIP LOCKED,
    preventing multiple Workers from claiming the same job.
    """
    # Atomically claim the next queued job via RPC.
    # The claim_next_job() function uses FOR UPDATE SKIP LOCKED to ensure
    # only one Worker can claim each job, even under concurrent access.
    response = supabase.rpc('claim_next_job').execute()

    if not response.data:
        return False

    job = response.data[0]
    job_id = job['id']
    logger.info(f"Claimed job {job_id} via claim_next_job() RPC. Processing...")

    # --- 读取用户 LLM 配置，构建子进程环境变量 ---
    llm_config = job.get('llm_config') or {}
    child_env = os.environ.copy()

    # 注入用户自带的 API key（按 provider 设置对应的环境变量）
    user_api_key = llm_config.get('api_key')
    if user_api_key:
        provider = llm_config.get('model_provider', 'openai')
        if provider == 'anthropic':
            child_env['ANTHROPIC_API_KEY'] = user_api_key
        else:
            # openai 和其他 provider 默认使用 OPENAI_API_KEY
            child_env['OPENAI_API_KEY'] = user_api_key
        logger.info(f"Job {job_id}: using user-provided API key for provider={provider}")

        # 立即从数据库中删除 api_key（隐私保护：只保留 provider 和 model 信息）
        try:
            safe_config = {k: v for k, v in llm_config.items() if k != 'api_key'}
            supabase.table('simulations').update(
                {'llm_config': safe_config if safe_config else None}
            ).eq('id', job_id).execute()
            logger.info(f"Job {job_id}: cleared api_key from database")
        except Exception as e:
            logger.warning(f"Job {job_id}: failed to clear api_key from DB: {e}")

    # --- 准备运行目录和文件（在 Foam-Agent/runs/ 下）---
    run_dir = os.path.join(FOAM_AGENT_DIR, "runs", str(job_id))
    os.makedirs(run_dir, exist_ok=True)

    prompt_path = os.path.join(run_dir, "prompt.txt")
    with open(prompt_path, "w") as f:
        f.write(job['prompt'])

    output_path = os.path.join(run_dir, "output")

    # 1. 定义我们希望保存日志的文件路径
    log_path = os.path.join(run_dir, "simulation.log")

    try:
        # Always use python -c inline startup to patch Config defaults before import.
        # This is necessary because Foam-Agent's services/__init__.py creates
        # global_llm_service = LLMService(Config()) at import time, and the default
        # model_provider may be 'openai-codex' which requires Codex OAuth.
        # We always override to 'openai' (using OPENAI_API_KEY) unless the user
        # provides a different provider.
        effective_provider = llm_config.get('model_provider') or 'openai'
        effective_version = llm_config.get('model_version') or 'gpt-4o'
        child_env['FOAM_MODEL_PROVIDER'] = effective_provider
        child_env['FOAM_MODEL_VERSION'] = effective_version
        child_env['FOAM_OUTPUT_DIR'] = os.path.abspath(output_path)
        child_env['FOAM_PROMPT_PATH'] = os.path.abspath(prompt_path)
        logger.info(f"Job {job_id}: using provider={effective_provider}, model={effective_version}")

        command = [
            "python", "-c",
            "import os,sys,inspect; sys.path.insert(0,'src'); "
            "from config import Config; "
            "params=list(inspect.signature(Config.__init__).parameters.keys()); "
            "params.remove('self'); "
            "defaults=list(Config.__init__.__defaults__); "
            "p=os.environ.get('FOAM_MODEL_PROVIDER'); "
            "v=os.environ.get('FOAM_MODEL_VERSION'); "
            "p and defaults.__setitem__(params.index('model_provider'),p); "
            "v and defaults.__setitem__(params.index('model_version'),v); "
            "Config.__init__.__defaults__=tuple(defaults); "
            "from main import main; "
            "c=Config(); c.case_dir=os.environ['FOAM_OUTPUT_DIR']; "
            "main(open(os.environ['FOAM_PROMPT_PATH']).read(),c)"
        ]

        logger.info(f"Executing command for job {job_id}: {' '.join(command)}")
        logger.info(f"Log file for this run will be at: {log_path}")

        # 2. 打开日志文件，准备写入
        with open(log_path, 'w') as log_file:
            # 3. 运行子进程，使用包含用户 LLM 配置的环境变量
            result = subprocess.run(
                command,
                cwd=FOAM_AGENT_DIR,  # 子进程在 Foam-Agent 目录下运行
                env=child_env,    # 注入用户配置的环境变量
                stdout=log_file,  # 将标准输出写入日志文件
                stderr=log_file,  # 将标准错误也写入同一个日志文件
                text=True,
                check=False,
                timeout=SIMULATION_TIMEOUT
            )

        # 4. Post-execution Allrun security audit
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

        # 5. 根据结果更新数据库
        if result.returncode == 0:
            # 命令成功
            logger.info(f"Job {job_id} completed successfully.")

            # Storage中的基础路径
            storage_base_path = f"public/{job['user_id']}/{job_id}"

            # 步骤1: 构建文件树结构（用于前端快速显示）
            logger.info(f"Building file tree for run directory: {run_dir}")
            file_tree = build_file_tree(run_dir)

            # 步骤2: 上传ZIP文件（保留原有功能，作为完整备份）
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

            # 步骤3: 上传整个run_dir目录下的所有文件到Storage
            # 这样前端可以通过Supabase Storage API直接访问文件
            logger.info(f"Uploading all files from {run_dir} to Storage...")
            uploaded_count, failed_count = upload_directory_to_storage(
                run_dir,
                storage_base_path,
                supabase
            )

            if failed_count > 0:
                logger.warning(f"Some files failed to upload: {failed_count} files failed")

            # 步骤4: 构建最终结果数据
            final_result = {
                "log_path_on_server": log_path,
                "output_path_on_server": output_path,
                "zip_storage_path": zip_storage_path,  # ZIP文件路径（保留）
                "storage_base_path": storage_base_path,  # Storage基础路径（用于前端访问）
                "file_tree": file_tree,  # 文件树结构（便于前端快速显示）
                "upload_stats": {  # 上传统计信息
                    "uploaded": uploaded_count,
                    "failed": failed_count
                },
                "allrun_audit": allrun_audit,  # Allrun security audit results
            }

            # 步骤5: 更新数据库
            supabase.table('simulations').update({
                'status': 'completed',
                'result_data': final_result
            }).eq('id', job_id).execute()

            logger.info(f"Job {job_id} completed and all files uploaded successfully. "
                       f"Total files: {uploaded_count}, Failed: {failed_count}")
        else:
            # 命令失败
            logger.error(f"Job {job_id} failed. Check log file for details: {log_path}")
            error_details = {
                "error": f"Foam-Agent script failed with return code {result.returncode}.",
                "log_path_on_server": log_path,
                "allrun_audit": allrun_audit,
            }
            supabase.table('simulations').update({
                'status': 'failed',
                'result_data': error_details
            }).eq('id', job_id).execute()

    except subprocess.TimeoutExpired:
        logger.error(f"Job {job_id} timed out after {SIMULATION_TIMEOUT} seconds.")
        supabase.table('simulations').update({
            'status': 'failed',
            'result_data': {
                'error': f"Simulation timed out after {SIMULATION_TIMEOUT} seconds.",
                'log_path_on_server': log_path,
                'timeout_seconds': SIMULATION_TIMEOUT
            }
        }).eq('id', job_id).execute()

    except Exception as e:
        logger.error(f"A critical error occurred while processing job {job_id}: {e}", exc_info=True)
        supabase.table('simulations').update({
            'status': 'failed',
            'result_data': {'error': f"Worker script encountered an exception: {str(e)}"}
        }).eq('id', job_id).execute()

    return True


# --- 5. 主循环 ---

def main_loop():
    """
    无限循环，不断地寻找并处理任务。
    """
    # Recover any stale jobs from previous Worker crashes
    recover_stale_jobs()

    logger.info("Worker started. Looking for jobs...")
    while True:
        try:
            processed_a_job = find_and_process_job()
            if not processed_a_job:
                # 如果没有任务，就休息一下
                time.sleep(10) # 等待 10 秒
        except Exception as e:
            logger.error(f"An error occurred in the main loop: {e}", exc_info=True)
            time.sleep(30) # 如果主循环出错，等待更长时间再重试


# --- 6. 脚本入口 ---
if __name__ == "__main__":
    main_loop()
