# cfdqanda-server

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
    ▼
Foam-Agent 子进程（LangGraph → OpenFOAM）
```

服务器**不包含任何仿真逻辑**，只负责：

1. 接收前端的任务请求，写入 Supabase 数据库
2. 轮询 Supabase 中排队的任务，启动 Foam-Agent 子进程执行
3. 任务完成后将结果上传到 Supabase Storage

## 文件说明

| 文件 | 说明 |
|------|------|
| `api_server.py` | FastAPI 应用 —— 任务创建、文件树查询、反馈提交等接口 |
| `worker.py` | 后台 Worker —— 轮询任务队列、启动 Foam-Agent 子进程、上传结果 |
| `app.py` | 简单的健康检查端点（MCP 传输用） |
| `.env` | 环境变量（不提交到 git） |
| `.env.example` | `.env` 模板 |

## 前置条件

- **Foam-Agent** 仓库已克隆到本地（服务器通过子进程调用它）
- **Conda 环境** 已创建：
  - `foam-api` —— 运行 API 服务器（`fastapi`, `uvicorn`, `supabase`, `python-dotenv`）
  - `FoamAgent` —— 运行 Worker（继承 Foam-Agent 的完整依赖）
- **Supabase** 项目已配置（`simulations` 表 + `simulation_results` 存储桶）
- **OpenFOAM v10** 已安装（Foam-Agent 需要）

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

### API 服务器

```bash
cd cfdqanda-server
conda activate foam-api
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

### Worker

```bash
cd cfdqanda-server
conda activate FoamAgent
python -u worker.py
```

### 后台模式（生产环境）

```bash
nohup uvicorn api_server:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &
nohup python -u worker.py > worker.log 2>&1 &
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 健康检查 |
| `POST` | `/api/v1/simulations` | 创建新的仿真任务 |
| `GET` | `/api/v1/simulations/{job_id}/files` | 获取已完成任务的文件树 |
| `POST` | `/api/v1/simulations/{job_id}/feedback` | 提交文件反馈 |

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `FOAM_AGENT_DIR` | 是 | Foam-Agent 仓库的绝对路径 |
| `SUPABASE_URL` | 是 | Supabase 项目 URL |
| `SUPABASE_SERVICE_KEY` | 是 | Supabase service_role key（绕过 RLS） |
| `OPENAI_API_KEY` | 否 | 默认 OpenAI API key（用户未提供时使用） |
| `WM_PROJECT_DIR` | 否 | OpenFOAM 安装路径（默认 `/opt/openfoam10`） |
| `EXTRA_CORS_ORIGINS` | 否 | 额外的 CORS 来源（逗号分隔） |

## 工作流程

1. 用户在前端提交仿真需求
2. API 服务器向 Supabase `simulations` 表插入一行，状态为 `queued`
3. Worker 每 10 秒轮询一次，拾取任务，将状态更新为 `running`
4. Worker 在 `Foam-Agent/runs/{job_id}/` 下写入 `prompt.txt`，然后以 `cwd=FOAM_AGENT_DIR` 启动 Foam-Agent 子进程
5. 成功时：Worker 构建文件树、创建 ZIP 归档、上传所有文件到 Supabase Storage，设置状态为 `completed`
6. 失败时：Worker 设置状态为 `failed` 并记录错误信息
7. 前端通过 Supabase Realtime 实时接收状态更新

### BYOK（自带密钥）

用户可以在提交任务时选择自己的 LLM 配置（提供商、模型版本、API key）。Worker 将这些配置作为环境变量注入 Foam-Agent 子进程，并在读取后**立即从数据库中删除** API key，保护用户隐私。
