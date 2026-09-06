---
name: save
description: Write the current work state to the memory store, for a reader who was not here. Use when the user says "/save", "$save", "save this", "save my state", "save where I got to", "checkpoint this", "record what we did", "handing off", "wrapping up", "I'm done for the day", "stepping away", "hand this over", "context is nearly full", or otherwise asks to persist progress on the work at hand so another session can pick it up. Finds the work item this belongs to and merges into it, or initializes a new one.
---

# save

**Assume the conversation is lost.** Persist a self-contained account in its
one work item for a reader who was not present.

## Execution

1. **Find the work item, mechanically first.**

   ```
   state_index_search(cwd="<the directory you are working in>", completion="open", limit=0)
   ```

   Complete `state_index_search`'s index stage. Usually there are only a handful
   of rows and one clearly matches this work.

2. **If multiple candidates remain ambiguous, ask the contents.** Only then,
   invoke `beeloop-state:state-ask` with a one-line description of the work and
   this `cwd`. Take the `{work_name, cwd}` pair from `sources`. If no row matches,
   this is new work — go directly to step 3.

3. **Get the record and write token.** If one matches, read it:

   ```
   state_get(work_name="<the work>", cwd="<the directory you are working in>")
   ```

   If none matches, initialize one. Pick a `work_name` that is lower-case,
   hyphenated, and describes the work rather than the session. It only has to be
   unused in THIS directory, so name it for what it is:

   ```
   state_initialize(work_name="<slug>",
                    cwd="<the directory you are working in>",
                    short_description="<one line, at most 120 characters>")
   ```

   `cwd` is half the key and is set here or never. Both `state_get` and
   `state_initialize` return the `write_token` for the next step; do not re-read
   a record you just initialized.

4. **Write the content, in one call.** Merge with what step 3 returned and
   follow `state_update`'s field contract.

   Persist every context-only assumption, rejected-approach rationale, and fact
   learned from a colleague in the appropriate fields. If it is absent from the
   record, it does not survive.

5. **If the work is finished**, add `completion="done"` and write
   `final_learnings` for somebody facing the same problem on different work,
   rather than the successor served by `prior_actions`.

6. **If the write was refused as stale**, re-run `state_get`, take the new
   `write_token`, re-merge, and retry step 4. Never force or work around it.

7. **If the store reports a limit error**, keep the persisted field concise. If
   omitted detail must survive, write or update the single workspace-relative
   file `agent_art/states/<work-name>.md`, add or keep
   `{"item": "agent_art/states/<work-name>.md", "note": "Detailed notes."}`
   in the complete artifacts list, and retry step 4 with the concise field and
   complete list. One document per work item, not one per save. The server never
   creates or modifies this file. If the work has no usable workspace, keep the
   state concise and ask the user where durable detail should go; do not invent
   a path.

8. Tell the user which work item you saved to, the new `updated`, and a one-line
   summary of what the next person will find.
