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
- `app/model_gateway/`: LiteLLM 路由、注册表、健康检查、Agno 模型工厂
- `app/adapters/codex_subscription_adapter.py`: `coder-premium` 的 OpenAI-compatible 本地适配层
- `app/adapters/codex_app_server_client.py`: 与 `codex app-server` 通信的 JSON-RPC 客户端
- `app/mcp/user_workspace_server.py`: 当前用户工作区的 MCP server
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
- `general`

Agno 不再写死厂商模型名，只会按 `task_type` 取 alias。以后新增模型，优先改：

1. [configs/litellm_proxy.yaml](/Users/chending/Downloads/agno-test-project/configs/litellm_proxy.yaml)
2. [configs/model_router.yaml](/Users/chending/Downloads/agno-test-project/configs/model_router.yaml)

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

## UI / API

- `GET /gateway/dev-token/{user_id}`: 生成本地演示 token
- `GET /gateway/codex-status`: 查看本机是否存在可桥接的 Codex 登录态
- `POST /gateway/codex-login`: 把本机 `~/.codex/auth.json` 桥接为平台内部 JWT
- `GET /gateway/runtime-status`: 查看 LiteLLM Proxy 健康、alias 探测结果、默认路由
- `GET /gateway/me`: 查看当前 token 对应用户
- `GET /gateway/workspace`: 查看当前用户空间文件
- `GET /gateway/knowledge?q=...`: 当前用户 / 项目作用域下的知识搜索
- `POST /gateway/chat`: 调用主智能体
- `GET /gateway/audit/{trace_id}`: 查看请求链路审计

`POST /gateway/chat` 返回里现在会包含：

- `mode`
- `model_routes`
- `knowledge_hits`
- `member_outputs`

## 多用户隔离

- JWT 中包含 `tenant_id / user_id / role / project_ids`
- Gateway 校验当前 `project_id` 是否属于当前用户可访问范围
- 知识检索只命中当前项目和当前用户个人作用域
- MCP 工作区始终绑定到当前用户根目录
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
