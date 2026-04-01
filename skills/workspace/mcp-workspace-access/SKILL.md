---
name: mcp-workspace-access
description: Use Workspace MCP to inspect only the current user's workspace, then return evidence-backed summaries.
---

Use this skill when the request depends on files, folders, drafts, notes, or write operations inside the current user's workspace.

Workflow:
- First confirm the relevant file or directory by listing before reading, unless the exact path is already known.
- Use Workspace MCP for every read or write. Do not guess file existence from memory.
- Stay inside the current user's workspace and never refer to repository root, system paths, or other users' files.
- If the request is ambiguous, prefer listing candidate files before choosing one to read.
- When reporting results, summarize only what MCP returned and mention the confirmed path.
