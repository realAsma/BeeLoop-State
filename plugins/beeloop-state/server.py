"""MCP server for long-term work state.

Tools:
  state_get, state_index_search, state_initialize, state_update

Records are keyed by (cwd, work_name); only search does not require both. Tools
never spawn a model. `--validate` re-checks the store and exits.
"""

import argparse
import filecmp
import json
import shutil
import sys
from pathlib import Path
from typing import Annotated, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    from pydantic import Field

    from config import ConfigError, state_dir
    from core.store import Filters, Store, StoreError
except ImportError as exc:
    # `python3` resolves through whatever PATH the host process was launched
    # with, which need not be the interpreter of your interactive shell. The
    # only symptom the host reports is "server failed to connect", so name the
    # interpreter that is actually short of the dependency.
    print(f"state: {exc}\n"
          f"state: this interpreter is {sys.executable}\n"
          f"state: install the dependencies there with "
          f"`{sys.executable} -m pip install mcp 'jsonschema>=4'`", file=sys.stderr)
    raise SystemExit(2) from exc

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"

# Stated truthfully, because a client that does not know what a tool does has to
# assume the worst.
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                       idempotentHint=True, openWorldHint=False)
# Initialize creates a new record; update replaces supplied strings and lists.
CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                         idempotentHint=False, openWorldHint=False)
UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                         idempotentHint=False, openWorldHint=False)
mcp = FastMCP("state", instructions="""Long-term state for long-running work.

A work item has a searchable index row and a prose work file.

Records are keyed by (cwd, work_name); a name is unique only within its cwd.

Find your work by exact cwd first:
    state_index_search(cwd="<the directory you are working in>", completion="open", limit=0)
Complete state_index_search's index stage before state_get; use state-ask only
if candidates remain ambiguous.

state_get returns the write_token required by state_update; stale tokens are refused.

Write only through these tools. Hand edits bypass schema enforcement.
""")

# Set by main(); the tools are a thin shell over it.
STORE: Store = None  # type: ignore[assignment]


@mcp.tool(annotations=READ)
def state_get(work_name: str, cwd: str) -> dict:
    """The whole record for one work item: work file fields plus the index row.

    Reads are unrestricted and convey no ownership. Returns `updated` as the
    `write_token` for state_update.

    Args:
        work_name: The work item to read.
        cwd: The directory it runs in. Required: it is the other half of the
            key, so the same name in another directory is a different record.
    """
    record = STORE.get(work_name, cwd)
    return {**record, "write_token": record["updated"]}


@mcp.tool(annotations=READ)
def state_index_search(
    since: str | None = None,
    until: str | None = None,
    completion: Literal["open", "done"] | None = None,
    cwd: str | None = None,
    limit: Annotated[int, Field(ge=0)] = 20,
) -> list[dict]:
    """Index rows, newest first. Never opens a work file -- one read.

    Recall has two stages. In the index stage, filter mechanically by time,
    state, and place, then inspect every returned row before opening any record:
    use `short_description` for relevance, `completion` for state, `updated` for
    recency, and (`cwd`, `work_name`) for identity. Select every potentially
    relevant row. In the record stage, call state_get for each selected
    identity.

    Args:
        since: Lower bound on `updated`, inclusive. A duration like '7d'
            (s/m/h/d/w) or a timestamp like '2026-08-21T12:00:00Z'.
        until: Upper bound on `updated`, exclusive, same forms. A window needs
            two ends: "the 14 days before the last 7" is unsayable with since
            alone.
        completion: Filter to open or done work. This is the one-bit fact, not
            `current_status`, which is prose.
        cwd: Exact working-directory filter, not a prefix.
        limit: Maximum rows returned; pass 0 for an exhaustive sweep.
    """
    return STORE.search(Filters(since=since, until=until, completion=completion,
                                cwd=cwd, limit=limit))


@mcp.tool(annotations=CREATE)
def state_initialize(work_name: str, short_description: str, cwd: str) -> dict:
    """File a new work item and return its first `write_token`: STRUCTURE ONLY.

    Call state_update next for content. Fails if the name exists in this cwd.

    Args:
        work_name: Names the work within `cwd`; those two together are the key.
            Lowercase-hyphenated is the convention. Also the filename stem, so
            no path separators and no leading '.', '_' or '-'.
        short_description: One-line summary, at most 120 characters.
        cwd: The work's real directory; half the immutable key.
    """
    row = STORE.initialize(work_name, short_description, cwd)
    return {"work_name": row["work_name"], "cwd": row["cwd"],
            "write_token": row["updated"]}


@mcp.tool(annotations=UPDATE)
def state_update(
    work_name: str,
    cwd: str,
    write_token: str,
    description: str | None = None,
    current_status: str | None = None,
    prior_actions: list[str] | None = None,
    next_steps: list[str] | None = None,
    blockers: list[str] | None = None,
    artifacts: list[dict[str, str]] | None = None,
    final_learnings: str | None = None,
    completion: Literal["open", "done"] | None = None,
    short_description: str | None = None,
) -> dict:
    """All content, in ONE call. Returns the new `updated` and `write_token`.

    Omitted fields remain unchanged; supplied lists replace the whole list.
    Missing or stale tokens, schema violations, unknown fields, and nonexistent
    work items are refused. For a stale token, re-read, merge, and retry.

    There is no delete. A work item becomes completion="done".

    Args:
        work_name: The work item to write.
        cwd: The directory it runs in. Required: it is the other half of the
            key, so the same name in another directory is a different record.
        write_token: The latest token returned by state_get, state_initialize,
            or state_update.
        description: What the work is and what done looks like. Revise only if
            the work changes.
        current_status: The state of the world, not a summary of the session.
        prior_actions: Brief attempts and outcomes, especially important dead
            ends.
        next_steps: Concrete enough for the next person to begin without asking
            questions.
        blockers: Who or what is being waited on.
        artifacts: Needed paths, links, commits, or job IDs, each with a note.
        final_learnings: Lessons for somebody facing the same problem on
            different work, usually written at the end.
        completion: "open" or "done".
        short_description: Replace the index one-liner only when it no longer
            fits the work. At most 120 characters.
    """
    given = {"description": description, "current_status": current_status,
             "prior_actions": prior_actions, "next_steps": next_steps,
             "blockers": blockers, "artifacts": artifacts,
             "final_learnings": final_learnings, "completion": completion,
             "short_description": short_description}
    fields = {key: value for key, value in given.items() if value is not None}
    updated = STORE.update(work_name, cwd, fields, expected=write_token)
    return {"updated": updated, "write_token": updated}


def prepare(states_dir: Path) -> Path:
    """Prepare the shared external store and refresh its shipped schema."""
    states_dir.mkdir(parents=True, exist_ok=True)
    source, target = ASSETS / "schema.json", states_dir / "schema.json"
    if not target.exists() or not filecmp.cmp(source, target, shallow=False):
        shutil.copyfile(source, target)
    index = states_dir / "index.jsonl"
    index.touch()
    _refuse_a_legacy_store(index)
    return states_dir


def _refuse_a_legacy_store(index: Path) -> None:
    """Refuse pre-3.0 `task_name` rows instead of creating a mixed store."""
    for line in index.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # Store() reports a malformed index with the line number.
        if isinstance(row, dict) and "work_name" not in row:
            raise StoreError(
                f"this is a pre-3.0 store, keyed on task_name. Records are keyed on "
                f"(cwd, work_name) now and there is no converter, so move it aside "
                f"first: mv {index.parent} {index.parent}.archive")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="beeloop-state", description=__doc__)
    parser.add_argument("--validate", action="store_true",
                        help="re-check the whole store and exit")
    args = parser.parse_args(argv)

    global STORE
    try:
        states_dir = state_dir()
        STORE = Store(prepare(states_dir))
    except (ConfigError, StoreError, OSError, RuntimeError) as exc:
        print(f"beeloop-state: {exc}", file=sys.stderr)
        return 2
    # stderr, never stdout: stdout is the JSON-RPC channel and a stray line
    # there corrupts the handshake.
    print(f"beeloop-state: store at {STORE.dir}", file=sys.stderr)

    if args.validate:
        if problems := STORE.validate():
            print("\n".join(problems), file=sys.stderr)
            print(f"\n{len(problems)} problem(s)", file=sys.stderr)
            return 1
        print(f"{STORE.dir}: {len(STORE.read_index())} work item(s), no problems")
        return 0

    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
