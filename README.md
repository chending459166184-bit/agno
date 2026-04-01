# Agno 企业主智能体本地 PoC

这个项目现在采用下面这条链路：

`FastAPI / Gateway -> Agno Orchestrator -> LiteLLM Proxy -> 各模型提供方`

当前目标不是把 Agno 绑死到某一家模型，而是把“主智能体编排”和“模型接入”拆开：

- Agno 只负责主智能体和子智能体协作。
- 所有模型统一经过 LiteLLM Proxy。
- Agno 业务逻辑里只看到 alias，不直接看到厂商模型名。
- Codex/ChatGPT 订阅登录、OpenAI API key、MiniMax、GLM/Z.AI 都归到统一模型网关里。

## 当前 alias

- `coder-premium`: ChatGPT Subscription / Codex，经由本地 `codex app-server` 适配层
- `coder-api`: OpenAI API key
- `minimax-general`: MiniMax
- `glm-coder`: Z.AI / GLM coding endpoint

## 目录结构

- `app/main.py`: FastAPI 入口、Gateway、`/debug` 页面
- `app/runtime.py`: Agno Orchestrator + 子智能体装配
- `app/agent_configs.py`: 系统智能体 catalog、用户/项目 override、effective config 合并
- `app/trace_view.py`: trace summary 归一化和时间线视图
- `app/model_gateway/`: LiteLLM 路由、注册表、健康检查、Agno 模型工厂
- `app/adapters/codex_subscription_adapter.py`: `coder-premium` 的 OpenAI-compatible 本地适配层
- `app/adapters/codex_app_server_client.py`: 与 `codex app-server` 通信的 JSON-RPC 客户端
- `app/mcp/user_workspace_server.py`: 当前用户工作区的 MCP server
- `app/workspace_mcp.py`: direct MCP probe helper，供 `/gateway/debug/workspace-mcp/*` 复用
- `configs/litellm_proxy.yaml`: LiteLLM alias 配置
- `configs/model_router.yaml`: `task_type -> alias` 路由配置
- `scripts/run_local.sh`: 本地启动 Gateway + adapter + LiteLLM Proxy
- `scripts/run_litellm_proxy.sh`: 单独启动 LiteLLM Proxy
- `scripts/warmup_chatgpt_subscription.py`: 预热 `coder-premium`
- `scripts/smoke_test_models.py`: alias 冒烟
- `scripts/smoke_test_orchestrator.py`: Gateway -> Agno -> LiteLLM 端到端验证

## 路由设计

默认 `task_type` 路由定义在 [configs/model_router.yaml](/Users/chending/Downloads/agno-test-project/configs/model_router.yaml)：

- `orchestrate`
- `knowledge`
- `workspace`
- `testing`
- `execution`
- `general`

Agno 不再写死厂商模型名，只会按 `task_type` 取 alias。以后新增模型，优先改：

1. [configs/litellm_proxy.yaml](/Users/chending/Downloads/agno-test-project/configs/litellm_proxy.yaml)
2. [configs/model_router.yaml](/Users/chending/Downloads/agno-test-project/configs/model_router.yaml)

当前还保留了一层“主智能体主导 + 可配置兜底”的混合路由：

- 主体仍由 `Enterprise Orchestrator` 自主决定是否调用内部成员或 `External Agent Broker`
- 外部兜底规则来自 [configs/agent_discovery.yaml](/Users/chending/Downloads/agno-test-project/configs/agent_discovery.yaml) 的 `prefetch.rules`
- 运行开关来自 `.env`:
  - `EXTERNAL_PREFETCH_ENABLED`
  - `EXTERNAL_PREFETCH_MODE=off|hint|prefetch`

另外有一层高优先级的安全保护：

- 命中“当前目录 / 我的文件 / 工作区 / 列文件 / 读文件 / 写文件”这类文件系统请求时，会优先进入 `workspace guard`
- `workspace guard` 只允许通过 Workspace MCP 访问当前 `workspace_root`
- 如果 Workspace MCP 没有真实成功执行，就会安全失败，不会退化成列工程根目录
- `coder-premium` 默认 `cwd` 也已经收缩到 `CODEX_SAFE_CWD_ROOT` 下的隔离 sandbox，而不是工程根目录
- 命中“运行代码 / 执行命令 / 测试脚本 / 验证输出”这类高风险请求时，会优先进入 `execution guard`
- `execution guard` 只允许通过 `Execution Agent -> Execution Sandbox` 执行，不会在普通 chat 环境里偷跑代码

## 执行沙箱

这一轮新增了独立执行链路：

`/gateway/exec/run -> Execution Manager -> Sandbox Runner -> artifacts / audit / trace`

核心目录：

- `app/execution/manager.py`: job 生命周期、审计、产物和 writeback 编排
- `app/execution/runner.py`: `docker | process` 两种执行模式
- `app/execution/workspace_stage.py`: 把当前用户 workspace 拷贝到 job staging 目录
- `app/execution/policy.py`: 超时、内存、CPU、网络、writeback 等策略
- `app/execution/artifacts.py`: 产物检测和读取
- `app/execution/schemas.py`: 执行请求、job、result、artifact 数据结构

安全边界：

- 主智能体不直接执行代码
- 执行只看到 `data/exec_jobs/<job_id>/workspace`
- 看不到工程根目录、`.env`、`.git`、`app/`、`configs/`、其他用户 workspace
- 默认禁网
- 默认不写回用户 workspace
- 每次执行都会挂到同一个 `trace_id`

### Docker 与 process fallback

默认配置是：

- `EXEC_SANDBOX_MODE=docker`

如果本机没有可用 Docker，当前实现会自动回退到 `process` 模式，保证本地 PoC 可跑，但要明确这时的风险边界更弱：

- `process` 模式仍然使用独立 job 工作目录
- 仍然做超时、内存和输出截断
- Python 任务会通过 `sitecustomize` 禁网
- 但它不如 Docker 的 `--network none / cap-drop / read-only rootfs` 那么强

如果你是做更严肃的本地验收，优先装好 Docker 再跑。

## 本地启动

### 1. 准备环境

```bash
cd /Users/chending/Downloads/agno-test-project
cp .env.example .env
```

按需填写 `.env`：

- `OPENAI_API_KEY` / `OPENAI_CODER_MODEL`
- `MINIMAX_API_BASE` / `MINIMAX_API_KEY` / `MINIMAX_MODEL_ID`
- `ZAI_API_BASE` / `ZAI_API_KEY` / `ZAI_MODEL_ID`

如果你想走 `coder-premium`，确保本机先有 Codex 登录态：

```bash
codex login status
```

### 2. 预热 ChatGPT Subscription / Codex

如果本机已经登录：

```bash
python scripts/warmup_chatgpt_subscription.py
```

如果还没登录，触发设备流：

```bash
python scripts/warmup_chatgpt_subscription.py --device-auth
```

### 3. 启动整套本地 PoC

```bash
bash scripts/run_local.sh
```

这个脚本会做三件事：

- 起 `coder-premium` adapter
- 起 LiteLLM Proxy
- 起当前 FastAPI / Agno Gateway

启动后访问：

- 调试页: [http://localhost:7777/debug](http://localhost:7777/debug)
- Swagger: [http://localhost:7777/docs](http://localhost:7777/docs)
- AgentOS 团队接口: [http://localhost:7777/teams](http://localhost:7777/teams)

## 运行验证

### 验证 1: 模型网关状态

```bash
curl http://127.0.0.1:7777/gateway/runtime-status
```

新的 `live` 定义是：

- LiteLLM Proxy 可达
- 且至少一个 alias 完成真实调用

不再是“`OPENAI_API_KEY` 是否存在”。

### 验证 2: alias 冒烟

```bash
python scripts/smoke_test_models.py
```

如果你要求所有已配置 alias 都通过：

```bash
python scripts/smoke_test_models.py --require-all
```

### 验证 3: Orchestrator 端到端

```bash
python scripts/smoke_test_orchestrator.py
```

默认它会：

- 调 `/gateway/runtime-status`
- 获取 `alice` 的本地 token
- 调 `/gateway/chat`
- 验证返回 `mode=agno`

### 验证 4: Workspace MCP 读写

```bash
TOKEN=$(curl -s http://127.0.0.1:7777/gateway/dev-token/alice | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:7777/gateway/debug/workspace-mcp/write \
  -d '{"project_id":"alpha","path":"notes/mcp-demo.txt","content":"hello from mcp","overwrite":true}'
```

写完以后再读：

```bash
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:7777/gateway/debug/workspace-mcp/read \
  -d '{"project_id":"alpha","path":"notes/mcp-demo.txt"}'
```

### 验证 5: Agent 配置页 / Trace 面板

- 打开 [http://localhost:7777/debug](http://localhost:7777/debug)
- 切到 `Agents` tab，修改某个系统智能体的 `enabled / priority / allow_auto_route / preferred_model_alias`
- 切到 `Chat` tab 再发请求
- 切到 `Trace` tab 查看这次请求的 `selected_agents / member_outputs / mcp_tool_call / a2a_*` 时间线

### 验证 6: Execution Sandbox

```bash
TOKEN=$(curl -s http://127.0.0.1:7777/gateway/dev-token/charlie | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:7777/gateway/exec/run \
  -d '{
    "project_id":"alpha",
    "language":"python",
    "command":"python inline_task.py",
    "files":[{"path":"inline_task.py","content":"print(\"hello sandbox\")"}],
    "timeout_seconds":10,
    "writeback":false
  }'
```

预期：

- `job.status=success`
- `stdout` 里包含 `hello sandbox`
- trace summary 里能看到 `sandbox_job_created -> sandbox_stage_prepared -> sandbox_started -> sandbox_completed`
- `/debug` 的 `Execution` tab 能直接查看同一个 job 的状态、日志和产物

### 验证 7: 文件目录权限边界

```bash
TOKEN=$(curl -s http://127.0.0.1:7777/gateway/dev-token/charlie | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:7777/gateway/chat \
  -d '{"message":"当前我目录下有哪些文件","project_id":"alpha"}'
```

预期：

- 只出现 `charlie` 的 workspace 文件，例如 `notes/alpha-analysis.md`
- 不会出现 `.env`、`.git/`、`app/`、`configs/`、`tests/`
- `mode` 为 `workspace_guard`
- `selected_agents` 包含 `Workspace Agent`
- trace summary 里有 `mcp_tool_call -> workspace_list_files`

## UI / API

- `GET /gateway/dev-token/{user_id}`: 生成本地演示 token
- `GET /gateway/dev-users`: 获取当前 demo 用户矩阵和可访问项目
- `GET /gateway/codex-status`: 查看本机是否存在可桥接的 Codex 登录态
- `POST /gateway/codex-login`: 把本机 `~/.codex/auth.json` 桥接为平台内部 JWT
- `GET /gateway/runtime-status`: 查看 LiteLLM Proxy 健康、alias 探测结果、默认路由
- `GET /gateway/me`: 查看当前 token 对应用户
- `GET /gateway/workspace`: 当前用户空间 direct view
- `GET /gateway/workspace/file`: direct debug 读文件
- `POST /gateway/workspace/file`: direct debug 写文件
- `GET /gateway/knowledge?q=...`: 当前用户 / 项目作用域下的知识搜索
- `POST /gateway/debug/workspace-mcp/list|read|write`: 通过真实 Workspace MCP server 做 deterministic probe
- `POST /gateway/exec/run`: 提交 sandbox 执行任务
- `GET /gateway/exec/{job_id}`: 查看执行任务状态
- `GET /gateway/exec/{job_id}/logs`: 查看 stdout/stderr
- `GET /gateway/exec/{job_id}/artifacts`: 查看产物列表
- `GET /gateway/exec/{job_id}/artifacts/{path}`: 读取单个产物
- `GET /gateway/external-agents`: 查看动态发现的 external agents
- `POST /gateway/external-agents/refresh`: 刷新 external catalog
- `POST /gateway/external-agents/invoke`: 直接走 External Agent Broker + A2A
- `GET /gateway/agent-catalog`: 系统内置 agent 模板
- `GET /gateway/agent-configs`: 当前用户的 user/project override
- `GET /gateway/agent-configs/effective`: 当前用户 + 当前项目的 effective agents
- `PUT /gateway/agent-configs/{agent_key}`: 保存当前项目 override
- `DELETE /gateway/agent-configs/{agent_key}`: 删除当前项目 override，恢复默认
- `POST /gateway/chat`: 调用主智能体
- `GET /gateway/audit/{trace_id}`: 查看请求链路审计
- `GET /gateway/trace/{trace_id}/summary`: 适合前端渲染的 trace summary

`POST /gateway/chat` 返回里现在会包含：

- `mode`
- `selected_agents`
- `prefetch_info`
- `effective_agents`
- `model_routes`
- `knowledge_hits`
- `member_outputs`
- `trace_summary`

## 多用户隔离

- JWT 中包含 `tenant_id / user_id / role / project_ids`
- Gateway 校验当前 `project_id` 是否属于当前用户可访问范围
- 知识检索只命中当前项目和当前用户个人作用域
- MCP 工作区始终绑定到当前用户根目录
- debug 页面内置了 `alice(alpha,beta) / bob(beta) / charlie(alpha)` 的对照矩阵，便于验证“同用户跨项目”和“同项目跨用户”
- 审计链路会写入 `trace_id / request_id / session_id / tenant_id / user_id`

## Codex 登录态桥接

`/gateway/codex-login` 现在仍然保留，但它只负责“平台登录身份桥接”，不再被当作 Agno 的直接模型凭证。

这条链路的职责是：

- 读取 `~/.codex/auth.json`
- 解析本机 Codex 用户身份
- 为当前平台签发内部 JWT

真正的 `coder-premium` 模型能力，走的是：

- `codex app-server`
- `app/adapters/codex_subscription_adapter.py`
- `LiteLLM Proxy`

## 测试

```bash
source .venv/bin/activate
pytest -q
```
