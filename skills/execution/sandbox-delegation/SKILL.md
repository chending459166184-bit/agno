---
name: sandbox-delegation
description: Delegate code execution and verification to the sandbox tool and avoid pretending a script was run when it was not.
---

Use this skill when the request requires running Python, pytest, scripts, or controlled commands.

Rules:
- Use `execute_in_sandbox` for every real execution or verification step.
- Never present guessed execution output as if it were observed.
- Keep commands minimal and explain what was executed and why.
- Prefer Python entrypoints, inline Python code, or `pytest` over arbitrary shell usage.
- If execution fails, report the sandbox failure clearly instead of filling the gap with speculation.
