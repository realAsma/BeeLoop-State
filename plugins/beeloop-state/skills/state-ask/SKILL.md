---
name: state-ask
description: Answer a semantic question from stored work state. Use when the user asks what work is blocked, whether something was tried before, what was learned, or another question that requires opening potentially relevant work records rather than listing the index.
---

# state-ask

Spawn one read-only subagent to answer the state question, so the sweep's opened
records stay out of the main thread's context. If subagent spawning is
unavailable, run the same procedure yourself in the main thread and report the
answer directly.

The procedure — the subagent's only instructions, or yours when running it
directly:

1. Never write or spawn another agent. Include open and completed work, and
   respect any requested `cwd` or `since`.
2. Sweep `7d`, `21d–7d`, `49d–21d`, `105d–49d`, then older, always with
   `limit=0`.
3. In each window, complete `state_index_search`'s index stage: inspect every
   returned row and select every potentially relevant one before opening any
   record.
4. Complete the record stage: call `state_get` for every selected row, passing
   its `cwd` as well as its `work_name`, and read the whole record.
5. Stop when answered, except a negative answer requires the complete sweep.
6. Return exactly `{answer: string, sources: list[{work_name, cwd}]}`. `sources`
   lists only the records that support the answer, not every one opened during
   the sweep. Both fields are needed: a bare name is not an identifier, because
   a name is only unique within its directory.
