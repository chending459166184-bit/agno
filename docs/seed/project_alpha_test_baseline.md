# Alpha 项目测试基线

## 必测能力

- 登录后上下文注入是否正确。
- 同一 session 连续提问是否可继续上下文。
- 不同用户是否会话隔离、知识隔离、文件隔离。
- MCP 文件读取是否只落在当前用户工作区。
- 审计表里是否能还原完整请求链路。

## 建议测试用例

1. Alice 提问 Alpha 项目需求，系统应命中 Alpha 文档，不应命中 Beta 文档。
2. Alice 让主智能体读取个人笔记，系统应只访问 Alice 工作区文件。
3. Bob 提问 Beta 运维问题，系统应只命中 Beta runbook。
4. 检查 audit_logs，确认 gateway_request、mcp_tool_call、gateway_response 都存在。
