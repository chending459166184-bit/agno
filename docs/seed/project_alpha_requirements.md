# Alpha 项目需求说明

## 项目目标

Alpha 项目要在企业内部搭建一个主智能体平台本地 PoC。主智能体需要能够调用子智能体、调用 MCP 工具，并对不同用户做空间隔离。

## 第一阶段范围

- 提供统一聊天入口，后端通过 Agent Gateway 注入租户、用户、项目上下文。
- 主智能体至少能协调 Knowledge Agent、Workspace Agent、Test Agent。
- 支持按 tenant_id、project_id、user_id 做知识过滤和工作区隔离。
- 所有请求链路必须带 trace_id、session_id、user_id、tenant_id。

## 验收重点

- Alice 只能看到 alpha 项目资料和 Alice 自己的个人文件。
- Bob 只能看到 beta 项目资料和 Bob 自己的个人文件。
- MCP 文件工具必须能列文件、读文件，并留下审计。
- 输出中要体现知识来源、用户空间上下文和测试建议。
