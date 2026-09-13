# BeeLoop State

A global memory layer for Claude Code and Codex. You do a piece of **work** in a
**workspace**; this records where you got to, so you or an agent can resume it.
Both hosts share one store, so work saved in one resumes in the other.

## Interface

Skills `beeloop-state:save`, `:continue`, `:work`, `:state-ask` sit on four MCP
tools: `state_index_search` to find, `state_get` to read, `state_initialize` to
create, `state_update` to write. A record is keyed by `(cwd, work_name)`, so a
name only has to be unique in the directory the work runs in. Writes carry the
latest `write_token` returned by `state_get`, `state_initialize`, or
`state_update`, so two callers cannot overwrite each other.

## Install

Requires `python3 -m pip install mcp 'jsonschema>=4'`.

```sh
/plugin marketplace add /path/to/beeloop-state   # Claude Code; also owner/repo or Git URL
/plugin install beeloop-state@beeloop-state

codex plugin marketplace add /path/to/beeloop-state   # Codex
codex plugin add beeloop-state@beeloop-state
```

Configure the store before starting either plugin host:

```sh
./beeloop-state setup                         # defaults to ~/.beeloop_states
./beeloop-state setup --state-dir /abs/path  # replaces the configured path
```

Setup writes the absolute path to `~/.config/beeloop/state.toml`. When no path
is supplied it preserves an existing configuration, or writes the default when
the file is missing. The server requires this file and does not select a store
through command-line flags or environment variables.

Codex also needs the configured directory in its sandbox
(`~/.codex/config.toml`), then a restart:

```toml
[sandbox_workspace_write]
writable_roots = ["/home/you/.beeloop_states"]
```

<details>
<summary>How do I change the memory location?</summary>

Run `./beeloop-state setup --state-dir PATH`. Relative arguments are resolved
against the current directory before being written to
`~/.config/beeloop/state.toml`. Keep this machine-local file out of shared
checkouts and make sure Codex's `writable_roots` lists the configured directory.

Writes are schema-validated, freshness-checked, `flock`ed, and atomically
renamed.

</details>

## Demo

Monday, you stop mid-flight. The agent picks the record itself and says which:

```text
you   ▸  save this
agent ▸  Saved to qwen35-9b-sft-coding-recovery, updated=2026-03-04T18:20:11Z.
         New record — the nearest, qwen35-9b-nvfp4-ptq, is different work.
```

Friday, a new session with no memory of it:

```text
you   ▸  Status of the Qwen coding regression work? Let's continue.
         ① state_index_search(cwd=".../qwen35-recovery", completion="open", limit=0) → 3 rows
         ② state_get("qwen35-9b-sft-coding-recovery", cwd=".../qwen35-recovery") → the one file
agent ▸  HumanEval recovered to 71.2 from 68.4, still ~3 pts under BF16. Blocked
         on MBPP — you traced it to a chat-template mismatch in the eval harness,
         not the fine-tune. Next was re-running MBPP fixed, then the LR sweep.
```

## Design

See [DESIGN.md](DESIGN.md) for the on-disk layout and record formats.
