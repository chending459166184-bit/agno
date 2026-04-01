---
name: routed-retrieval
description: Gather project and user-scoped knowledge before answering, and avoid unsupported claims when retrieval returns nothing.
---

Use this skill when the request asks about project background, requirements, runbooks, baselines, internal docs, or prior notes.

Rules:
- Search only within the current tenant, current project, and current user scope.
- Treat retrieval output as the evidence source. Do not invent missing documents, sections, or conclusions.
- If retrieval is empty, say that no matching knowledge was found in the allowed scope.
- When hits exist, keep the answer grounded in the returned titles, scope markers, and snippets.
- Prefer short evidence summaries that the orchestrator can integrate with other agents.
