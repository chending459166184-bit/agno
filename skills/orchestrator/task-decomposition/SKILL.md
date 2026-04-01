---
name: task-decomposition
description: Break enterprise requests into internal steps first, then decide whether any external specialty is justified.
---

Apply this skill when the orchestrator receives a request that may need multiple agents.

Workflow:
- Identify which parts are project knowledge, user workspace context, testing/verification, and possible external specialty.
- Route project background to `Knowledge Agent`.
- Route user-owned files to `Workspace Agent`.
- Route test strategy and validation to `Test Agent`.
- Use `External Agent Broker` only if the request still needs a specialist view after internal context is gathered.
- In the final answer, distinguish internal findings from external specialist input.

