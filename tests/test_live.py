"""Exercise the server as a host does: a subprocess speaking stdio JSON-RPC."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "plugins" / "beeloop-state" / "server.py"
# Half of the key, so every tool call but the search carries it.
HERE = "/work/here"


class ToolFailed(Exception):
    pass


class Session:
    """One server subprocess, driven over stdio."""

    def __init__(self, states: Path, *args: str):
        self.home = states.parent / "home"
        env = {**os.environ, "HOME": str(self.home)}
        setup = subprocess.run(
            [sys.executable, str(SERVER), "setup", "--state-dir", str(states)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert setup.returncode == 0, setup.stderr
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env)
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._id = 0
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, payload: dict) -> None:
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def _read_stdout(self) -> None:
        for line in self.process.stdout:
            self._stdout.put(line)
        self._stdout.put(None)

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        deadline = time.monotonic() + 10
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = self._stdout.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                self._abort("server died")
            message = json.loads(line)
            if message.get("id") == self._id:
                return message
        self._abort(f"server did not answer {method!r} within 10 seconds")

    def _abort(self, reason: str) -> None:
        self.process.kill()
        self.process.wait(timeout=5)
        stderr = self.process.stderr.read()
        raise AssertionError(f"{reason}: {stderr[-2000:]}")

    def tools(self) -> dict[str, dict]:
        return {t["name"]: t for t in self.request("tools/list")["result"]["tools"]}

    def call(self, name: str, **arguments):
        message = self.request("tools/call", {"name": name, "arguments": arguments})
        result = message.get("result", {})
        blocks = [block.get("text", "") for block in result.get("content", [])]
        if result.get("isError") or "error" in message:
            raise ToolFailed("".join(blocks) or json.dumps(message.get("error")))
        # A list return arrives as one text block per item, and as
        # structuredContent under "result". Prefer the structured form.
        if "structuredContent" in result:
            structured = result["structuredContent"]
            return structured.get("result", structured)
        return json.loads("".join(blocks))

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=10)


@pytest.fixture
def states(tmp_path: Path) -> Path:
    # Deliberately NOT pre-populated: the server seeds schema.json and the index
    # itself, and that is a thing worth exercising on every run.
    return tmp_path / "states"


@pytest.fixture
def sessions(states: Path):
    live: list[Session] = []

    def open_session(*args: str) -> Session:
        live.append(Session(states, *args))
        return live[-1]

    yield open_session
    for session in live:
        session.close()


# ------------------------------------------------------------------ startup


def test_the_server_seeds_its_own_data_directory(sessions, states: Path):
    # The data directory starts empty on a fresh install.
    sessions()
    assert sorted(path.name for path in states.iterdir()) == ["index.jsonl", "schema.json"]


# ------------------------------------------------------------ the data directory


def _validate(*args: str, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess:
    """Run the server CLI with an isolated environment."""
    return subprocess.run([sys.executable, str(SERVER), *args, "--validate"],
                          capture_output=True, text=True, timeout=60,
                          cwd=None if cwd is None else str(cwd), env={**os.environ, **env})


def _setup(*args: str, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SERVER), "setup", *args],
                          capture_output=True, text=True, timeout=60,
                          cwd=None if cwd is None else str(cwd), env={**os.environ, **env})


def test_setup_writes_the_default_under_home(tmp_path: Path):
    done = _setup(HOME=str(tmp_path))
    assert done.returncode == 0
    wanted = (tmp_path / ".beeloop_states").resolve()
    assert str(wanted) in done.stdout
    assert (wanted / "schema.json").is_file()
    assert f'state_dir = "{wanted}"' in (
        tmp_path / ".config" / "beeloop" / "state.toml"
    ).read_text("utf-8")


def test_explicit_state_dir_overwrites_configuration(tmp_path: Path):
    wanted = tmp_path / "elsewhere"
    assert _setup(HOME=str(tmp_path)).returncode == 0
    done = _setup("--state-dir", str(wanted), HOME=str(tmp_path))
    assert done.returncode == 0
    assert str(wanted.resolve()) in done.stdout
    assert str(wanted.resolve()) in (
        tmp_path / ".config" / "beeloop" / "state.toml"
    ).read_text("utf-8")


def test_omitted_state_dir_preserves_configuration(tmp_path: Path):
    wanted = tmp_path / "configured"
    assert _setup("--state-dir", str(wanted), HOME=str(tmp_path)).returncode == 0
    done = _setup(HOME=str(tmp_path))
    assert done.returncode == 0
    assert str(wanted.resolve()) in done.stdout


def test_relative_setup_path_is_stored_as_absolute(tmp_path: Path):
    done = _setup("--state-dir", "relative", cwd=tmp_path, HOME=str(tmp_path / "home"))
    assert done.returncode == 0
    assert str((tmp_path / "relative").resolve()) in done.stdout


def test_startup_requires_valid_configuration(tmp_path: Path):
    done = _validate(HOME=str(tmp_path), BEEBOT_STATE_DIR=str(tmp_path / "ignored"))
    assert done.returncode == 2
    assert "beeloop-state setup" in done.stderr

    config = tmp_path / ".config" / "beeloop" / "state.toml"
    config.parent.mkdir(parents=True)
    config.write_text("state_dir = [broken", encoding="utf-8")
    done = _validate(HOME=str(tmp_path))
    assert done.returncode == 2
    assert str(config) in done.stderr

    for body in ("other = 1\n", 'state_dir = "relative"\n'):
        config.write_text(body, encoding="utf-8")
        done = _validate(HOME=str(tmp_path))
        assert done.returncode == 2
        assert "state_dir" in done.stderr


def test_the_resolved_directory_is_announced_on_stderr(states: Path):
    # stdout is the JSON-RPC channel; a stray line there breaks the handshake.
    home = states.parent / "home"
    assert _setup("--state-dir", str(states), HOME=str(home)).returncode == 0
    done = _validate(HOME=str(home))
    assert f"beeloop-state: store at {states.resolve()}" in done.stderr


def test_a_pre_3_0_store_is_refused_rather_than_appended_to(states: Path):
    # 3.0 lands at the same path the old store used, so without this the server
    # would append work_name rows to a task_name index and mix the two schemas.
    states.mkdir(parents=True)
    (states / "index.jsonl").write_text(json.dumps({
        "task_name": "old", "task_state_path": "b/old.json", "cwd": "/w",
        "short_description": "d", "updated": "2026-01-01T00:00:00Z",
        "completion": "open"}) + "\n")
    done = _setup("--state-dir", str(states), HOME=str(states.parent / "home"))
    assert done.returncode == 2
    assert "pre-3.0 store" in done.stderr and str(states) in done.stderr


# ------------------------------------------------------------------- server


def test_work_can_be_created_filled_and_found(sessions):
    session = sessions()
    initialized = session.call("state_initialize", work_name="build-store", cwd=HERE,
                               short_description="Build the memory store")
    session.call("state_update", work_name="build-store", cwd=HERE,
                 write_token=initialized["write_token"],
                 current_status="Core and server done.",
                 prior_actions=["Tried deriving the bucket at read time; it has to stay stored."],
                 artifacts=[{"item": "server.py", "note": "the four tools"}])

    assert [r["work_name"] for r in
            session.call("state_index_search", cwd=HERE, completion="open", limit=0)] \
        == ["build-store"]

    record = session.call("state_get", work_name="build-store", cwd=HERE)
    assert record["current_status"] == "Core and server done."
    assert record["short_description"] == "Build the memory store"
    assert "work_state_path" not in record  # filing never leaves the store


def test_an_omitted_field_is_left_alone(sessions):
    session = sessions()
    initialized = session.call("state_initialize", work_name="t", cwd=HERE, short_description="d")
    first = session.call("state_update", work_name="t", cwd=HERE,
                         write_token=initialized["write_token"],
                         current_status="first", blockers=["waiting"])
    session.call("state_update", work_name="t", cwd=HERE, write_token=first["write_token"],
                 current_status="second")
    record = session.call("state_get", work_name="t", cwd=HERE)
    assert record["current_status"] == "second" and record["blockers"] == ["waiting"]


def test_registrations_expose_what_they_should(sessions):
    tools = sessions().tools()
    assert set(tools) == {
        "state_get", "state_index_search", "state_initialize", "state_update"
    }
    assert "state_semantic_search" not in tools


def test_schemas_come_from_the_signatures(sessions):
    # Nothing here is hand-written: the types come from the annotations and the
    # prose from the docstring, so a parameter cannot be documented as one thing
    # and validated as another.
    tools = sessions().tools()
    update = tools["state_update"]["inputSchema"]["properties"]
    assert {"type": "array", "items": {"type": "string"}} in update["prior_actions"]["anyOf"]
    assert {"type": "string", "enum": ["open", "done"]} in update["completion"]["anyOf"]
    assert update["write_token"]["type"] == "string"
    assert "write_token" in tools["state_update"]["inputSchema"]["required"]
    # cwd is half the key, so it is required wherever one record is addressed --
    # and optional on the search, where it is only a filter.
    for name in ("state_get", "state_update", "state_initialize"):
        assert "cwd" in tools[name]["inputSchema"]["required"]
    search = tools["state_index_search"]["inputSchema"]
    assert "cwd" not in search.get("required", [])
    assert search["properties"]["limit"]["default"] == 20
    assert search["properties"]["limit"]["minimum"] == 0


def test_tools_declare_annotations(sessions):
    tools = sessions().tools()
    assert tools["state_get"]["annotations"]["readOnlyHint"] is True
    assert tools["state_update"]["annotations"]["destructiveHint"] is True
    assert tools["state_initialize"]["annotations"]["destructiveHint"] is False


def test_a_violation_reaches_the_client_as_a_readable_error(sessions):
    with pytest.raises(ToolFailed, match=r"short_description.*147 > 120"):
        sessions().call("state_initialize", work_name="t", cwd=HERE, short_description="x" * 147)


def test_an_unknown_field_is_rejected_not_dropped(sessions):
    session = sessions()
    initialized = session.call("state_initialize", work_name="t", cwd=HERE, short_description="d")
    with pytest.raises(ToolFailed):
        session.call("state_update", work_name="t", cwd=HERE,
                     write_token=initialized["write_token"], currrent_status="typo")


# ---------------------------------------------------------------- freshness


def test_a_missing_write_token_is_refused(sessions):
    sessions().call("state_initialize", work_name="t", cwd=HERE, short_description="d")
    with pytest.raises(ToolFailed, match="write_token"):
        sessions().call("state_update", work_name="t", cwd=HERE, current_status="blind")


def test_a_mismatched_write_token_is_refused(sessions):
    session = sessions()
    session.call("state_initialize", work_name="t", cwd=HERE, short_description="d")
    with pytest.raises(ToolFailed, match="changed since you read it"):
        session.call("state_update", work_name="t", cwd=HERE, write_token="not-the-token",
                     current_status="blind")


def test_a_lost_update_is_refused_for_two_callers_on_one_session(sessions):
    session = sessions()
    session.call("state_initialize", work_name="t", cwd=HERE, short_description="d")

    mine = session.call("state_get", work_name="t", cwd=HERE)["write_token"]
    theirs = session.call("state_get", work_name="t", cwd=HERE)["write_token"]
    session.call("state_update", work_name="t", cwd=HERE, write_token=theirs,
                 current_status="theirs")

    with pytest.raises(ToolFailed, match="changed since you read it"):
        session.call("state_update", work_name="t", cwd=HERE, write_token=mine,
                     current_status="mine")
    # The refusal is the useful part: re-read and the retry goes through.
    fresh = session.call("state_get", work_name="t", cwd=HERE)["write_token"]
    assert session.call("state_update", work_name="t", cwd=HERE, write_token=fresh,
                        current_status="mine")["updated"]


def test_initialize_returns_the_first_token_and_update_returns_the_next(sessions):
    session = sessions()
    initialized = session.call("state_initialize", work_name="t", cwd=HERE, short_description="d")
    updated = session.call("state_update", work_name="t", cwd=HERE,
                           write_token=initialized["write_token"],
                           current_status="no state_get needed")
    assert updated["updated"] == updated["write_token"]
    assert updated["write_token"] != initialized["write_token"]


# ----------------------------------------------------------------- validate


def test_validate_runs_as_a_flag_not_a_tool(states: Path):
    home = states.parent / "home"
    assert _setup("--state-dir", str(states), HOME=str(home)).returncode == 0
    done = _validate(HOME=str(home))
    assert done.returncode == 0 and "no problems" in done.stdout
