---
name: external-agent-routing
description: Decide when to escalate from internal agents to the external agent broker, while preserving least-necessary delegation.
---

Use this skill when a task may require knowledge or judgment outside the current internal agents.

Rules:
- Prefer internal agents first: `Knowledge Agent`, `Workspace Agent`, `Test Agent`.
- Only hand off to `External Agent Broker` when the task needs a clearly external specialty such as compliance, security architecture, analytics, industry review, or other partner expertise.
- Before handing off, summarize what internal context has already been gathered.
- Ask the broker for the narrowest useful external help instead of forwarding the whole conversation blindly.
- When the external result comes back, integrate it with internal context and note any trust boundary or validation caveat.

