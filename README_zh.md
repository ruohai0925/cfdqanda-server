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

### 2026-05-04

- **诊断回国一个月期间数据被静默清空**（Tasks 469–508）：回国后跑首次 weekly review，报告显示 `**该时间段内没有任务。**`，但 id sequence 已经走到 509（历史共 ~508 个任务）。排查：`simulations` 表整张空，但 Supabase Storage 仍保留 8 个用户、45 个任务目录、2262 个文件、665 MB。根因定位到 **worker.py 自己的 TTL purge 循环**——没人看的时候它一直在跑：failed/cancelled 行 7 天软删 + 3 天后硬删；completed 行 14+3。到 ~2026-04-26 历史所有行都被硬删完。Storage 之所以保留，是因为条件式 Storage 清理（`if storage_base_path:`）对老行（`result_data` 早于该字段）相当于 no-op——但**无条件**的 DB 行删除照样跑了，制造了让 purge 隐身一个月的孤儿 Storage 现象。
- **Worker TTL 放宽**（`worker.py`）：`TTL_FAILED_DAYS` 7 → 30，`TTL_COMPLETED_DAYS` 14 → 90，`PURGE_RETENTION_DAYS` 3 → 7。净保留期 failed/cancelled 37 天、completed 97 天——一个月的离开窗口绰绰有余。
- **硬删现在先确认 Storage 清理成功才删 DB 行**（`worker.py:_purge_deleted_simulations`）：`result_data.storage_base_path` 缺失时回退到 `public/{user_id}/{job_id}`（`worker.py` 其它所有上传位置都用这个路径）。`_remove_storage_directory()` 后再 `list()` 一次前缀，发现仍有残留就拒绝删 DB 行；mesh 文件清理也是同一道闸。本地 `runs/` 清理保持 best-effort，不阻塞 DB 删除。孤儿 Storage 场景从此消失。
- **单任务磁盘限额 400 MB → 1024 MB**（`worker.py`、`.env.example`）：Task #325（`cg.liang@nuaa.edu.cn`，21700 单体电池放电热仿真，`laplacianFoam`）跑了 11 轮 Rewrite 已接近收敛，最后栽在 `[Errno 28] No space left on device`——agent 推理没问题，是预算太紧。新默认值在 `.env.example` 里以 `TASK_DISK_LIMIT_MB=1024` 文档化。
- **Worker 启动时 Codex token 健康检查**（`worker.py:_check_platform_codex_token`）：读取 `$CODEX_HOME/auth.json` 把 access_token 当 JWT 解析，根据 `exp` claim 输出 `ERROR` / `WARN（< 24h 过期）` / `INFO` 三档日志。把 2026-04-09 的失败模式（Tasks 453/454/455 同时撞上 `HTTP 401 token_expired`，1 小时后 Task 468 撞上 `HTTP 500`）从"静默烧用户 quota"提到了"启动一眼可见"。修复后重启验证日志：`Platform Codex token: token valid until 2026-05-15`。函数本身用 6 个伪造 JWT 单测过（healthy / expiring_soon / expired / missing / malformed / 非 JWT）。
- **平台侧失败退还每日 quota**（`api_server.py`）：新增 `PLATFORM_REFUND_CATEGORIES = ['auth_error_platform', 'codex_quota_exceeded']` 和 `_count_today_billable(user_id)` helper，用 `.in_()` 过滤 JSONB path `result_data->>error_category`。create-task quota check 和 `GET /user/daily-usage` 都改用 helper，从总数里扣掉退还任务。endpoint 多返了 `total_today` 和 `refunded_platform_failures` 字段供前端透明展示。端到端验证：6 个 probe 任务（3 个 ok / 2 个 platform-fail / 1 个 byok-auth-fail）→ billable = 4；`auth_error_byok` 正确仍计入 quota（用户自己的 key 出问题，不退）。
- **`weekly_review.py` 用户分页 + CI 兼容**：默认 `per_page=50` 只返了 151 个 auth 用户里的前 50 个，导致报告里大半邮箱列空白。改为 `per_page=200` 分页拉。`.env` 加载也改成可选，CI 里用环境变量直接跑。
- **GitHub Actions 自动 weekly review**（`.github/workflows/weekly_review.yml`）：每周一 09:00 UTC 跑 `weekly_review.py`，报告作为 90 天 artifact 上传，并自动开 labeled GitHub issue。`workflow_dispatch` 手动触发已验证（[run 25353753830](https://github.com/ruohai0925/cfdqanda-server/actions/runs/25353753830)），首个 issue (#1) 自动创建成功。本次事故的直接教训：每周自动检查在跑，就不会出现一个月的盲区。
- **`check_storage.py` 用户分页 + Storage 真值扫描**：和 `weekly_review.py` 同款分页 bug——之前显示 50 个用户而非 151。更糟：Storage 总用量从 `simulations.result_data.upload_stats.total_bytes` 算，TTL 清空后这字段没了，脚本报 **0 GB** 但实际 **665 MB / 2262 个文件**。改为直接遍历 `simulation_results` bucket 当真值；DB 行还在时交叉显示 status，没有就显示 `(no DB row)`。Storage > 0 但 DB rows = 0 时额外打一行 warning。
- **运维清理**：清掉本地 805 MB 的孤儿 `runs/{job_id}` 目录（83 个，DB 行早没了——保留 `test-*` 手测目录和 `.gitkeep`）。`docker builder prune -af` 回收 28.96 GB build cache。本地磁盘共回收 ~30 GB。重启了两个 worker replica 让 worker.py 改动生效——启动日志已确认 `Task disk limit set to 1024 MB` 以及 codex token healthcheck 输出。
- **User Guide 页面嵌入英文平台介绍视频**（`cfdqanda-client/src/UserGuide.jsx`、`App.jsx`）：YouTube 教程（`_Fveasp8QHI`）通过 `youtube-nocookie` 域名嵌入指南页顶部，lazy-loaded、16:9 自适应 iframe。Footer 链接从 `User Guide` 改为 `User Guide (incl. video)` / `用户指南（含视频）`，让视频从现有入口可发现，不增加新的顶层 UI。
- **一次性恢复工具**（不放任何 repo，留在 `cfdqanda/` 父目录）：`rebuild_history.py` 遍历 Storage 重建只读任务索引，DB 没了也能离线参考（恢复了 45 个历史任务）。`system_review.py` 分析同样的数据，输出失败模式和按优先级排序的改进建议——本次维护日志的所有改动都来自它的输出。不 commit，一次性诊断工具。

### 2026-04-09

- **诊断 controlled mode 系统性弱于 auto mode 的根因**（Tasks 433/443/458/459/465/466/478）：用户 fuadhhasan@RPI 在 Task 433 留言 `"Case 433 failed using the interactive mode; yet case 434 succeeded using the e2e mode."` 直接引出了 4 个互相耦合的 bug。同一 buoyantFoam prompt 在两种 mode 下生成的 `0/alphat`、`system/fvSchemes`、`system/fvSolution` 不同——controlled mode 持续漏写 `compressible::` namespace 前缀、`div(phi,K)`、`div(phi,Ekp)`、以及 compressible 形式的 `div(((rho*nuEff)*dev2(T(grad(U)))))`。
- **Bug A — `_OF_VERSION_RE` false positive**（`worker.py`）：原正则 `(?:openfoam|of)\s*...(\d+)` 会把 `"temperature of 300K"` 误识为 `OpenFOAM 300`。收紧到 `\bopenfoam[-\s_]*v?(\d{1,2})(?!\d)`，必须出现完整 "openfoam" 词，限定 1-2 位版本号。原 false positive 偶然救了 auto mode（误触发的版本不匹配警告里恰好有 alphat namespace hint），controlled mode 因为根本没调 `_check_prompt`，连这个偶然救命都没有。
- **Bug B — v10 关键 syntax hint 永久写进 `_PLATFORM_NOTE`**（`worker.py`）：把所有 v10 必须的提示（`compressible::alphatJayatillekeWallFunction` 命名空间、`stopAt endTime`、`Gauss upwind`、PIMPLE 的 `rhoFinal`/`pFinal`/`p_rghFinal` 等终代条目、动能/总能投影项 `div(phi,K)`/`div(phi,Ekp)`、compressible 形式的 `div(((rho*nuEff)*dev2(T(grad(U)))))`、以及禁止 `#codeStream` 因 docker root 阻塞 runtime C++ 编译）从藏在版本警告里改为永久存在于 platform note。Note 从 ~1700 字符紧凑到 1223 字符。
- **Bug C — controlled mode 现在调用 `_check_prompt`**（`worker.py:_handle_controlled_pipeline`）：原来的 controlled 路径直接把裸 `job['prompt']` 传给 `client.plan()` / `client.input_writer()` / `client.review()` / `client.apply_fixes()`，**完全跳过了 platform note 增强**。修复：在函数顶部一次性计算 `augmented_prompt = job['prompt'] + [PLATFORM NOTE]`，stash 到 `pipeline_state['augmented_prompt']`（jsonb 持久化以便 checkpoint 恢复时复用），4 个 MCP 调用全部用增强版本。Job 458 验证 `pipeline_state.augmented_prompt` 字段已正确填入完整 v10 hint。
- **Bug D — 已提交并端到端验证上游 [csml-rpi/Foam-Agent#28](https://github.com/csml-rpi/Foam-Agent/pull/28)**：MCP `input_writer` 工具（`fastmcp_server.py`）把 `retrieve_references()` 第 5 个返回值 `similar_case_advice` 用 `_` 丢掉了，导致 MCP 路径生成文件时拿到的上下文比 LangGraph 路径少（auto mode 通过 `state["similar_case_advice"]` 传给 `initial_write()`）。提交了 2 行最小化修复。**本地验证完成**：在本地和云端 Foam-Agent 目录都 checkout PR 分支，跑了两个 MCP 模式任务：(1) **Job 479** —— Task 433 原 `buoyantFoam` prompt（之前 controlled 模式连续 5 次失败），现在 `completed`，0 次 fix 迭代，`system/fvSchemes` 完整正确（`compressible::alphatJayatillekeWallFunction`、`div(phi,K)`、`div(phi,Ekp)`、compressible 形式的 `div(((rho*nuEff)*dev2(T(grad(U)))))`），完整 100 秒物理时间积分，78 个文件 / 3.5 MB 输出；(2) **Job 480** —— 不可压缩 2D lid-driven cavity (`icoFoam`) 回归测试，同样 `completed`（37 个文件 / 282 KB），确认对非热流 case 无回归。PR test plan 4 项已全部勾选，verification comment 已发布（[#issuecomment-4217151169](https://github.com/csml-rpi/Foam-Agent/pull/28#issuecomment-4217151169)）。upstream merge 之后我们 `_PLATFORM_NOTE` 里的 buoyantFoam-specific hint 大部分可以拆掉。
- **Pre-run fix loop 改进**（`worker.py:_mcp_stage_pre_run`）：(1) `PRE_RUN_MAX_FIX_ATTEMPTS` 改为环境变量，默认从 5 提升到 8。(2) 新增早停：如果连续 2 次迭代的错误签名完全相同，直接 bail 为 `pre_run_no_progress`，避免浪费 LLM token。(3) 失败信息区分"次数耗尽"和"无进展放弃"。
- **Checkpoint 超时 30 min → 2 h**（`worker.py`）：`CHECKPOINT_TIMEOUT` 默认值从 1800 改为 7200 秒。Task 444（879329937@qq.com）因为之前 30 分钟超时太激进，用户在 `files_review` 阶段还没看完文件就被自动失败。
- **Auth error 文案区分 BYOK 和平台默认**（`worker.py:_diagnose_error_text`、`tests/test_step049_error_diagnosis.py`）：原文案 `"LLM API authentication failed. Please check your API key."` 对使用平台默认的用户有误导（他们没有 API key 可以"check"）。新增两种类别：`auth_error_byok`（提示用户 key/token 无效或过期）和 `auth_error_platform`（提示平台默认模型暂时不可用，建议用 BYOK）。重构成 `_diagnose_error_text()` 使 controlled mode 的异常处理也能用上。
- **`weekly_review.py` 现在收集全部 5 个反馈层**：之前只读 `simulations.user_rating + user_comment`。修复了 `stage_feedback` 字段位置 bug（实际在 `pipeline_state` 不在 `result_data`），新增 `stage_ratings` jsonb 列、`platform_feedback` 表查询、以及 Storage `*_feedback` 文件扫描。捞到了 Task 433 的关键 user comment——本次整个调研的起点。
- **Codex BYOK token 过期提示**（`AISimulationTab.jsx`）：在 Codex Token 可选输入框下加了警告，说明 Codex OAuth token 每 ~10 天过期、需要重新通过 ChatGPT 客户端登录，建议长期使用选 BYOK 常规 API key。
- **端到端验证 — Job 478**：复用 Task 433 原 prompt（10×5×10 房间 + 600K 热点的浮力热流）以 auto mode 提交。✅ 成功完成（7 MB 输出，114 个文件，100 秒物理时间）。Platform note 注入 + `#codeStream` 警告的组合修复了原始失败。

### 2026-03-29

- **BYOK（自带密钥）验证通过**：首个成功的 BYOK 任务（Task 414，openai/gpt-4o）。确认端到端流程正常：前端提交 → Worker 注入环境变量 → Foam-Agent 执行 → 结果上传。API key 在 Worker 读取后正确从数据库清除。
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
