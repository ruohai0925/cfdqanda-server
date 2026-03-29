# cfdqanda-server

> **许可证：** [PolyForm Strict 1.0.0](https://polyformproject.org/licenses/strict/1.0.0/) — 源码仅供个人及非商业用途查阅和使用，禁止商业使用。

[CFDQandA](https://cfdqanda.com) 的平台服务层 —— 一个基于自然语言的 CFD 仿真自动化平台。

本仓库包含 **API 服务器**（FastAPI）和 **后台 Worker**，连接 React 前端与 [Foam-Agent](https://github.com/YYgroup/Foam-Agent) 仿真引擎。

## 架构

```
浏览器（React）
    │
    ▼
API 服务器（FastAPI，端口 8000）
    │
    ▼
Supabase（PostgreSQL + Storage + Realtime + Auth）
    ▲
    │
Worker（轮询循环）
    │
    ├─ [自动模式] Foam-Agent 子进程（LangGraph → OpenFOAM）
    │
    └─ [受控模式] MCP 客户端 → Foam-Agent MCP 服务器（分步执行 + 用户检查点）
```

服务器**不包含任何仿真逻辑**，只负责：

1. 接收前端的任务请求，写入 Supabase 数据库
2. 轮询 Supabase 中排队的任务，启动 Foam-Agent 执行
3. 任务完成/失败/取消后将结果（单个文件 + ZIP 归档）上传到 Supabase Storage

支持两种执行模式：

- **自动模式（`auto`）**：Foam-Agent 作为子进程一次性运行完整仿真流程（规划 → 生成 → 运行 → 纠错 → 可视化）
- **受控模式（`controlled`）**：通过 MCP 协议分步调用 Foam-Agent（规划 → 文件生成 → 预运行 → 完整运行），支持在任意阶段暂停等待用户确认

## 文件说明

| 文件 | 说明 |
|------|------|
| `api_server.py` | FastAPI 应用 —— 13 个 REST 端点：JWT 鉴权、任务管理、文件浏览、反馈/评价、用户账户（Profile 创建 + 账户删除）、存储用量查询 |
| `worker.py` | 后台 Worker —— 轮询任务队列、启动 Foam-Agent 执行（自动/受控两种模式）、上传结果（单个文件 + ZIP 归档）、stale job 恢复、数据清理（TTL + purge） |
| `mcp_client.py` | MCP 客户端 —— 管理 Foam-Agent MCP 服务器进程，提供异步接口调用 `plan()`、`input_writer()`、`run()`、`review()`、`apply_fixes()` |
| `allrun_validator.py` | Allrun 安全审计 —— 解析 Allrun 脚本，检测危险命令（`rm -rf`、`curl` 等），白名单验证 |
| `token_extractor.py` | Token 用量提取 —— 从仿真日志中解析 LLM API 调用的 token 消耗 |
| `analyze_cases.py` | 仿真任务分析脚本 —— 状态、模型、错误分类、用户统计（`--md`、`--csv`、`--ssh`） |
| `check_storage.py` | 平台资源报告 —— 服务器磁盘/内存/Docker + Supabase 存储用量（`--ssh`、`--quick`、`--detail`） |
| `cleanup_runs.sh` | 手动磁盘清理脚本 —— 按需删除本地 `runs/` 目录（支持 `--all`、`--id`、`--range`、`--before`、`--largest N`、`--dry-run`） |
| `docker_start_workers.sh` | 多 Worker 启动脚本 —— 一键启动 N 个 Worker 容器，每个有独立 WORKER_ID、MCP 端口、日志文件 |
| `docker_submit_test_tasks.sh` | 测试任务批量提交脚本 —— 通过 Supabase Auth 登录后快速提交 N 个测试任务 |
| `.env` | 环境变量（不提交到 git） |
| `.env.example` | `.env` 模板 |

## 前置条件

- **Docker Engine** 已安装（已在 WSL2 和 GCP 上测试通过）
- **Foam-Agent** 仓库已克隆到本地（服务器通过子进程/MCP 调用它）
- **Supabase** 项目已配置（`simulations` 表 + `user_profiles` 表 + `simulation_results` 存储桶 + `claim_next_job` RPC 函数）

## 安装配置

1. 复制 `.env.example` 为 `.env` 并填写所有值：

```bash
cp .env.example .env
```

2. 最重要的变量是 `FOAM_AGENT_DIR` —— 设置为 Foam-Agent 目录的**绝对路径**：

```
FOAM_AGENT_DIR=/home/youruser/path/to/Foam-Agent
```

## 启动

### Docker Compose（推荐）

```bash
# 构建镜像（首次或代码变更后）
docker build -f Dockerfile.api -t cfdqanda-api .
docker build -f Dockerfile.worker -t cfdqanda-worker .

# 启动所有服务
docker compose up -d

# 查看日志
docker compose logs -f api
docker compose logs -f worker

# 检查状态
docker compose ps

# 停止
docker compose down
```

### 多 Worker（并发测试）

```bash
docker compose up -d --scale worker=3
# 或使用辅助脚本：
./docker_start_workers.sh 3
```

### 磁盘清理

```bash
./cleanup_runs.sh --dry-run --largest 10   # 预览最大的 10 个 run
./cleanup_runs.sh --before 100             # 删除 ID < 100 的所有 run
./cleanup_runs.sh --all                    # 删除所有 run
```

## API 接口

所有 POST/PATCH/DELETE 端点均需要 Supabase JWT（`Authorization: Bearer <token>`）。`user_id` 从 JWT 的 `sub` claim 中提取，不再由请求体传入。

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 健康检查 |
| `POST` | `/api/v1/simulations` | 创建新的仿真任务（支持 `pipeline_mode`、`checkpoints`、`llm_config` 等参数） |
| `GET` | `/api/v1/simulations/{job_id}/files` | 获取已完成/失败/取消/检查点任务的文件树 |
| `POST` | `/api/v1/simulations/{job_id}/feedback` | 提交文件级反馈（同时保存到本地和云端） |
| `PATCH` | `/api/v1/simulations/{job_id}/rating` | 提交任务级或阶段级评价（1=成功, 2=部分成功, 3=失败） |
| `DELETE` | `/api/v1/simulations/{job_id}` | 软删除任务（设置 `deleted_at` 时间戳，不可删除运行中的任务） |
| `POST` | `/api/v1/simulations/{job_id}/cancel` | 取消排队中/运行中/检查点中的任务（Worker 检测后终止子进程） |
| `POST` | `/api/v1/simulations/{job_id}/stage/confirm` | 确认流水线检查点，允许流水线继续执行（受控模式） |
| `POST` | `/api/v1/simulations/{job_id}/stage/reject` | 拒绝流水线检查点，将任务标记为失败（受控模式） |
| `POST` | `/api/v1/simulations/{job_id}/restore` | 恢复已软删除的任务（清除 `deleted_at`） |
| `GET` | `/api/v1/user/storage` | 获取当前用户的云端存储用量统计（5 分钟缓存） |
| `POST` | `/api/v1/users/me/profile` | 创建用户 Profile（RLS fallback，5 次/分钟限流） |
| `DELETE` | `/api/v1/users/me` | 删除账户及所有关联数据（GDPR 被遗忘权，3 次/分钟限流） |
| `GET` | `/api/v1/admin/status` | 聚合健康检查：API + Worker 状态（无需认证，用于 UptimeRobot 监控） |

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `FOAM_AGENT_DIR` | 是 | Foam-Agent 仓库的绝对路径 |
| `SUPABASE_URL` | 是 | Supabase 项目 URL |
| `SUPABASE_SERVICE_KEY` | 是 | Supabase service_role key（绕过 RLS） |
| `SUPABASE_JWT_SECRET` | 是 | Supabase JWT 验证密钥（Dashboard → Settings → API → JWT Secret） |
| `OPENAI_API_KEY` | 否 | 默认 OpenAI API key（用于 embedding 和用户未提供 key 时的回退） |
| `WM_PROJECT_DIR` | 否 | OpenFOAM 安装路径（默认 `/opt/openfoam10`） |
| `EXTRA_CORS_ORIGINS` | 否 | 额外的 CORS 来源（逗号分隔） |
| `MIDDLEWARE_DIR` | 否 | cfdqanda-middleware 仓库路径（启用检查点预运行机制） |
| `SIMULATION_TIMEOUT` | 否 | 仿真子进程超时（秒），默认 `3600`（1 小时） |
| `STALE_JOB_THRESHOLD` | 否 | stale job 检测阈值（秒），默认 `7200`（2 小时）。启动时重置超过此阈值仍为 `running` 的任务 |
| `MCP_SERVER_PORT` | 否 | MCP 服务端口，默认 `7860` |
| `WORKER_ID` | 否 | Worker 唯一标识（默认 `worker-{PID}`），多 Worker 部署时用于区分日志 |
| `HEALTH_CHECK_PORT` | 否 | Worker 健康检查 HTTP 端口，默认 `8001`。设为 `0` 禁用 |
| `WORKER_HEALTH_URL` | 否 | Worker 健康检查 URL，供管理状态端点使用。默认 `http://localhost:8001/health`；Docker 中自动设为 `http://worker:8001/health` |
| `USER_STORAGE_LIMIT_MB` | 否 | 每用户最大云端存储（MB），默认 `2048`（2 GB） |
| `USER_DAILY_TASK_LIMIT` | 否 | 每用户每日最大任务提交数，默认 `10`（UTC 零点重置） |

## 工作流程

### 自动模式（默认）

1. 用户在前端提交仿真需求（`pipeline_mode='auto'`）
2. API 服务器向 Supabase `simulations` 表插入一行，状态为 `queued`
3. Worker 每 2 秒轮询一次，通过 `claim_next_job()` RPC 原子性地认领任务（`FOR UPDATE SKIP LOCKED`，防止多 Worker 竞态），将状态更新为 `running`
4. Worker 在 `Foam-Agent/runs/{job_id}/` 下写入 `prompt.txt`，然后以 `cwd=FOAM_AGENT_DIR` 启动 Foam-Agent 子进程
5. 子进程执行期间，Worker 定时轮询数据库检测取消请求和超时
6. 成功时：Worker 运行 Allrun 安全审计、构建文件树、创建 ZIP 归档、上传所有文件到 Supabase Storage，设置状态为 `completed`
7. 失败/取消时：Worker 上传已有文件 + ZIP 归档，设置状态为 `failed` 或 `cancelled` 并记录错误信息
8. 前端通过 Supabase Realtime 实时接收状态更新

### 受控流水线模式（MCP）

当 `pipeline_mode='controlled'` 时，Worker 通过 MCP 协议分步调用 Foam-Agent：

```
plan() → [plan_review 检查点] → input_writer() → [files_review 检查点]
→ pre-run（10 步 + 纠错循环，最多 5 次）→ [pre_run_review 检查点] → full run() → completed
```

- 每个检查点由用户通过前端确认（`stage/confirm`）或拒绝（`stage/reject`）
- 检查点是可选的，通过提交任务时的 `checkpoints` 参数指定启用哪些
- 预运行阶段包含自动纠错循环（最多 5 次）：失败 → MCP `review()` + `apply_fixes()` → 重试
- 完整运行不含纠错循环（配置已在预运行阶段验证通过）

### 数据生命周期

Worker 内置自动清理机制（每小时检查一次）：

| 类型 | 保留天数 | 动作 |
|------|---------|------|
| 失败/取消的任务 | 7 天 | 自动软删除（设置 `deleted_at`） |
| 已完成的任务 | 14 天 | 自动软删除 |
| 已软删除的任务 | 3 天 | 硬删除（清理 Supabase Storage 文件 → 本地 `runs/` 目录 → 数据库行） |

### 用户账户管理

- **Profile 自动创建**：用户首次登录时自动创建 `user_profiles` 记录。三层 fallback：注册时存入 `user_metadata` → 登录后前端直接 INSERT → API 端点用 service_role 绕过 RLS
- **账户删除**（GDPR 被遗忘权）：`DELETE /api/v1/users/me` 级联删除 Storage 文件 → simulations → user_profiles → Auth 用户。UI 按钮暂时隐藏

### BYOK（自带密钥）

用户可以在提交任务时选择自己的 LLM 配置（提供商、模型版本、API key 或 Codex OAuth token）。Worker 将这些配置作为环境变量注入 Foam-Agent 子进程，并在读取后**立即从数据库中删除**敏感令牌，保护用户隐私。

支持的认证方式：
- **API Key**：适用于 `openai`、`anthropic` 等标准提供商
- **Codex OAuth Token**：适用于 `openai-codex` 提供商（写入临时 `auth.json`，运行结束后自动清理）

### 多 Worker 并发

多个 Worker 可以安全并发运行 —— `claim_next_job()` RPC 使用 PostgreSQL `FOR UPDATE SKIP LOCKED` 防止重复领取。每个 Worker 通过 `WORKER_ID` 环境变量标识，日志中包含 Worker 标识便于排查。

### 健康检查与监控

三层防御机制，确保管理员能快速发现并修复故障：

| 层级 | 作用 | 实现 |
|------|------|------|
| Docker 自愈 | 容器挂了自动重启 | `docker-compose.yml` 中配置 `restart: unless-stopped` + `healthcheck` |
| 聚合状态端点 | 一个 URL 查看全部状态 | `GET /api/v1/admin/status` 返回 API + Worker 健康信息 |
| UptimeRobot | 外部告警（邮件） | 每 5 分钟监控聚合端点，异常自动发邮件 |

**Worker 健康检查** —— 每个 Worker 暴露 `GET /health` 端点（默认端口 8001）：

```bash
curl localhost:8001/health
# {"status":"idle","worker_id":"worker-123","jobs_processed":5,"jobs_succeeded":3,"jobs_failed":2,"current_job_id":null,"uptime_seconds":3600,"start_time":"..."}
```

**聚合管理端点** —— 同时检查 API 和 Worker 状态（无需认证）：

```bash
curl localhost:8000/api/v1/admin/status
# 全部正常（HTTP 200）：
# {"api":"ok","worker":{"status":"idle","worker_id":"worker-123",...},"all_healthy":true}

# Worker 挂了（HTTP 503）：
# {"api":"ok","worker":{"status":"unreachable","error":"Connection refused"},"all_healthy":false}
```

**排障流程**（用户反馈任务一直排队不动时）：

1. 浏览器打开 `https://your-domain/api/v1/admin/status`（不需要登录）
2. 看 `all_healthy` 字段：
   - `true` → Worker 活着，问题在别处（检查 Supabase、用户 prompt 等）
   - `false` → Worker 挂了，执行第 3 步
3. SSH 到服务器重启：`docker compose restart worker`

配置 UptimeRobot 后，第 1 步由机器自动完成 —— 发现异常发邮件通知，恢复后也会通知。

## 维护日志

### 2026-03-29

- **自助邀请码领取页面**（`InvitationBoard.jsx`、`App.jsx`、`Auth.jsx`）：`#invite` 路由下的公开页面，展示 20 个邀请码并提供复制按钮。已使用的邀请码显示为灰色删除线。所有邀请码用完后通过 Supabase RPC（`list_invitation_codes_with_replenish`）自动生成新一批。注册表单邀请码输入框旁添加"没有邀请码？点此领取"链接。
- **修复 MCP 服务器忽略 BYOK LLM 配置**（`mcp_client.py`、`worker.py`）：受控流水线模式下，MCP 服务器子进程启动时未传递 `FOAMAGENT_MODEL_PROVIDER`、`FOAMAGENT_MODEL_VERSION` 和 API key 等环境变量，导致始终回退到 `openai-codex` 默认值。BYOK 用户（如 DeepSeek）命中 Codex OAuth 端点并收到 `HTTP 401 token_expired` 错误。修复：`MCPServerManager.start()` 新增 `llm_env` 参数；`_ensure_mcp_server()` 从任务的 `llm_config` 构建 LLM 环境变量，并在不同任务间配置变化时自动重启 MCP 服务器。
- **新增重型仿真 prompt 预检**（`worker.py`）：`_check_prompt()` 新增检测 LES、DES、DPM、FWH/声学、FSI、反应流和 3D VOF 等关键词，命中时注入 `[PLATFORM NOTE]`，鉴于平台资源限制，推荐使用粗网格和 `purgeWrite`，避免超出磁盘/内存限制。
- **Codex token 过期邮件告警**：配置 `msmtp` + Gmail SMTP + 每日 cron（`codex-token-sync.sh check --cron`），token 过期前 2 天发邮件提醒。登录仍需手动浏览器交互（`./codex-token-sync.sh login`）。
- **修正 cron 路径**：旧 cron 指向错误目录（`cfdqanda-server/`），已修正到项目根目录（`cfdqanda/`）。
- **更新 Foam-Agent 微信社区说明**：将 "maintainer" 改为 "volunteer"，添加微信号作为扫码入群的替代方式（[PR #24](https://github.com/csml-rpi/Foam-Agent/pull/24)）。

## 测试

```bash
# 在 API 容器中运行测试
docker compose exec api python -m pytest tests/ -v

# 或本地安装依赖后运行
pip install -r requirements-api.txt
python -m pytest tests/ -v
```
