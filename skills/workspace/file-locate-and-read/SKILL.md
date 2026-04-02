---
name: file-locate-and-read
description: When the user asks about a file's content but only provides a filename or incomplete path, first locate candidate files inside the current user's workspace, then read the best match, and only summarize evidence-backed content.
---

Use this skill when the request is about:
- "这个文件讲了什么"
- "帮我看看 xxx.txt / xxx.md / xxx.json 的内容"
- "读取某个文件"
- "总结某个文件"
- "某个文件里写了什么"
and the user may provide:
- an exact relative path
- only a filename
- an incomplete path
- or an ambiguous reference to a file

Your job is not only to call Workspace MCP safely, but to choose the right sequence of tools and avoid false negatives.

# Core principles

1. Stay strictly inside the current user's workspace.
   - Never mention repository root, system paths, or other users' files.
   - Never assume a file exists unless MCP evidence confirms it.

2. Distinguish "path resolution" from "content reading".
   - If the exact relative path is already known and trustworthy, read it directly.
   - If the user only provides a filename or incomplete path, resolve candidates first, then read.
   - Do not treat "one list call returned empty" as proof that the file does not exist somewhere deeper in the user's workspace.

3. Do not confuse "listing" with "searching".
   - `workspace_list_files` lists files under a path anchor.
   - If the user provides only a filename, do not blindly pass that filename as the anchor and stop on empty results.
   - First determine whether the input is:
     - an exact relative path
     - a directory prefix
     - or only a filename / basename query

4. Only summarize file content after a real read.
   - File content questions require `workspace_read_text_file` evidence.
   - A directory listing is not enough evidence to summarize file content.

5. Prefer precise, minimal, evidence-backed answers.
   - If multiple candidates exist, do not guess.
   - If no candidate can be confirmed, say so clearly.
   - If content is truncated, mention that the summary is based on the visible portion only.

# Intent classification

Classify the request into one of these intents:

- read_exact_file
  The user gave an exact relative path that looks actionable, such as:
  - docs/test-focus.txt
  - notes/today.md
  - reports/weekly/summary.txt

- locate_then_read
  The user asks about a file's content but only provides:
  - a filename, e.g. test-focus.txt
  - a basename, e.g. summary.md
  - a likely partial path, e.g. weekly/summary.txt
  - a natural-language reference, e.g. “那个测试重点文件”

- list_directory
  The user asks what files/directories exist.

- ambiguous_workspace_question
  It is related to workspace but you cannot yet tell whether the user wants listing, reading, or writing.

# Action policy by case

## Case A: exact relative path is provided
Examples:
- 读取 docs/test-focus.txt
- reports/daily.md 里写了什么

Action:
1. Confirm the path is a valid relative path inside the current workspace boundary.
2. Call `workspace_read_text_file` directly.
3. Summarize the content based only on the read result.
4. Mention the confirmed relative path.

Do not:
- run unnecessary broad listing first
- convert this into a vague "I couldn't find it" without trying the direct read

## Case B: only filename is provided
Examples:
- test-focus.txt 这文件讲的什么内容
- 帮我看看 summary.md
- config.json 里写了什么

Action:
1. Treat this as `locate_then_read`, not `list_directory`.
2. First inspect the current user's workspace to find candidate files that may match this filename.
3. Prefer the smallest safe sequence that can surface candidate paths.
4. If exactly one confirmed candidate is found:
   - call `workspace_read_text_file` on that candidate
   - then summarize the content
5. If multiple candidates are found:
   - do not guess
   - return a clarification with the candidate relative paths
6. If no candidate is found:
   - state that no matching file could be confirmed in the current user's workspace

Important:
- Do not stop after a single failed anchored list if the user's wording clearly indicates a filename search task.
- A filename-only query requires candidate resolution logic, not just directory listing logic.

## Case C: partial path is provided
Examples:
- weekly/test-focus.txt
- docs/summary.md
- project-a/readme.md

Action:
1. Try to interpret it as a relative path candidate.
2. If direct read fails or the path cannot be confirmed:
   - fall back to candidate resolution inside the user's workspace
   - search for likely matches by suffix / filename
3. If a unique candidate is found, read it.
4. If multiple candidates exist, clarify.

## Case D: user asks "what files are here"
Examples:
- 当前目录下有哪些文件
- 我工作区里有什么
- 列一下 docs 目录

Action:
1. This is a real listing task.
2. Use `workspace_list_files`.
3. Summarize the listing only.
4. Do not fabricate file content summaries.

## Case E: ambiguous reference
Examples:
- 那个重点文件讲了什么
- 帮我看看之前那个文档

Action:
1. Check whether prior evidence in the current task already identified a concrete path.
2. If yes, read that path.
3. If not, ask a clarification question.
4. Do not invent the target file from memory.

# Candidate resolution strategy

When the user provides only a filename or incomplete path, use this resolution strategy:

1. Check whether prior evidence already contains a confirmed `resolved_relative_path`.
   - If yes, reuse it.

2. If not, inspect the workspace to surface candidate files.
   - Prefer candidate-oriented discovery over broad narrative answers.
   - If tool capability is limited, use the available listing tool in a way that maximizes the chance of discovering nested matches.

3. Rank candidates using these signals:
   - exact filename match is strongest
   - suffix path match is stronger than loose similarity
   - files in user-relevant directories are preferred
   - previously referenced directories in current context are preferred
   - do not use content guessing for ranking

4. Resolution outcomes:
   - exactly one strong candidate -> read it
   - multiple plausible candidates -> clarify
   - zero confirmed candidates -> return not_found / not_confirmed

# Required tool evidence by answer type

You may only produce these user-facing answers if the following evidence exists:

- "这个文件讲了什么 / 内容总结"
  Requires: `workspace_read_text_file`

- "这个文件存在 / 不存在 / 未找到"
  Requires: listing or read failure evidence tied to the current workspace

- "当前目录有哪些文件"
  Requires: `workspace_list_files`

If the evidence is insufficient, do not overstate confidence.

# Clarification policy

Ask a clarification only when necessary.

Ask clarification if:
- multiple candidate files match
- the user reference is too ambiguous
- the file exists but reading requires a more specific path selection
- the tool evidence cannot distinguish between several plausible matches

Do not ask clarification if:
- there is already exactly one confirmed candidate
- you can safely read the file directly with a known relative path

Example clarification:
- “我在你当前工作区里发现多个同名候选文件，请确认你想看哪一个：
  - docs/test-focus.txt
  - archive/test-focus.txt”

# Failure handling

## No candidate found
Say:
- you could not confirm a matching file inside the current user's workspace
- therefore you cannot summarize its content
- optionally suggest the user provide a relative path

Do not say:
- the file definitely does not exist everywhere
- the file content is probably about X

## Read failed after candidate resolution
Say:
- the candidate path was identified, but reading failed
- mention whether it looks like not found / not readable / tool failure
- do not fabricate summary content

## Truncated content
If `workspace_read_text_file` returns truncated content:
- say the summary is based on the visible portion only
- avoid claiming full-document certainty

# Output style

When successful:
- first confirm the path
- then summarize the content in plain language
- keep the answer concise and evidence-backed

Example:
- “我已读取你当前工作区中的 `docs/test-focus.txt`。这份文件主要讲了……”
- “根据 `reports/weekly/summary.md` 的内容，这份文档主要说明了……”

When not found:
- “我在你当前工作区内还没有确认到 `test-focus.txt`，因此暂时无法总结它的内容。若你知道相对路径，可以直接发给我，例如 `docs/test-focus.txt`。”

When multiple matches:
- “我找到了多个可能的文件，请确认你想看哪一个：
  - docs/test-focus.txt
  - old/test-focus.txt”

# Guardrails

Never do the following:
- summarize a file you did not read
- treat a filename-only query as a proof of absence after one shallow listing
- confuse current user workspace with repository root
- infer content from the filename alone
- switch to testing advice, coding advice, or general speculation when the user explicitly asked for file content

# Preferred execution patterns

Pattern 1: exact path
- direct read -> summarize

Pattern 2: filename only
- resolve candidates -> if unique then read -> summarize

Pattern 3: multiple matches
- resolve candidates -> clarify

Pattern 4: no confirmed match
- return not_found / ask for relative path

# Examples

## Example 1
User: `docs/test-focus.txt 这文件讲了什么？`
Action:
- read exact path
- summarize

## Example 2
User: `test-focus.txt 这文件讲的什么内容？`
Action:
- locate candidates inside current user's workspace
- if one match -> read and summarize
- if none -> say not confirmed
- if many -> clarify

## Example 3
User: `帮我看看 summary.md`
Action:
- locate_then_read
- do not answer from filename alone

## Example 4
User: `当前目录下有哪些文件？`
Action:
- list_directory
- summarize listing only

## Example 5
User: `那个上次提到的重点文件讲什么`
Action:
- if previous evidence contains a confirmed path, read it
- otherwise clarify