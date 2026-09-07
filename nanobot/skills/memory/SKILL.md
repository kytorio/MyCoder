---
name: memory
description: Search conversation history and understand Dream-managed profile and memory files.
---

# Memory

## Structure

- `SOUL.md` — Bot personality and communication style. Managed through `memory_save` and Dream. Do not edit directly.
- `USER.md` — User profile and preferences. Managed through `memory_save` and Dream. Do not edit directly.
- `memory/MEMORY.md` — Long-term facts (project context, important events). Managed through `memory_save` and Dream. Do not edit directly.
- `memory/history.jsonl` — append-only JSONL, not loaded into context. Prefer the
  built-in `grep` tool to search it.

## Search Past Events

Use the `History log` path shown in the system prompt. Always pass it to `grep`;
never substitute a different project-relative `memory/history.jsonl`, which may belong
to the selected project. Each JSONL line contains `cursor`, `timestamp`, and `content`.

- For broad searches, start with `output_mode="count"` or the default
  `files_with_matches` mode before expanding to full content
- Use `output_mode="content"` plus `context_before` / `context_after` when you need the exact matching lines
- Use `fixed_strings=true` for literal timestamps or JSON fragments
- Use `head_limit` / `offset` to page through long histories

Examples (replace `<history-log-path>` with the path from the system prompt):
- `grep(pattern="keyword", path="<history-log-path>", case_insensitive=true)`
- `grep(pattern="2026-04-02 10:00", path="<history-log-path>", fixed_strings=true)`
- `grep(pattern="keyword", path="<history-log-path>", output_mode="count", case_insensitive=true)`
- `grep(pattern="oauth|token", path="<history-log-path>", output_mode="content", case_insensitive=true)`

## Important

- Use `memory_save` only for a durable fact or preference explicitly supported by the current
  user's own request. Pass an exact `source_excerpt`; never save secrets, temporary instructions,
  quoted third-party claims, or inferred facts.
- Only tell the user that a fact was remembered after `memory_save` returns `status=committed`.
  If the tool is unavailable or returns an error, say that it was not confirmed saved.
- **Do not directly edit SOUL.md, USER.md, or MEMORY.md.** Explicit entries are protected from
  ordinary file writes and Dream rewrites.
- To correct an explicit entry, call `memory_save` with the user's new explicit statement and the
  prior `entry_id` as `replaces_entry_id`.
- Users can view Dream's activity with the `/dream-log` command.
