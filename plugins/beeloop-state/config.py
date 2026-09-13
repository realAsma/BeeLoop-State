"""Persistent configuration for the standalone BeeLoop State store."""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    pass


def config_path() -> Path:
    try:
        return Path.home() / ".config" / "beeloop" / "state.toml"
    except RuntimeError as exc:
        raise ConfigError(
            f"cannot determine the BeeLoop config directory: {exc}"
        ) from exc


def state_dir() -> Path:
    path = config_path()
    data = _read(path)
    configured = data.get("state_dir")
    if not isinstance(configured, str) or not configured:
        raise ConfigError(f"{path} must contain a non-empty string `state_dir`")
    resolved = Path(configured)
    if not resolved.is_absolute():
        raise ConfigError(f"{path}: `state_dir` must be an absolute path")
    return resolved.resolve()


def selected_state_dir(given: Path | None) -> tuple[Path, bool]:
    """Return the setup target and whether setup must write its config."""
    path = config_path()
    if given is not None:
        return given.expanduser().resolve(), True
    if path.exists():
        return state_dir(), False
    return (Path.home() / ".beeloop_states").resolve(), True


def write_state_dir(directory: Path) -> None:
    _write(config_path(), f'state_dir = {_toml_string(str(directory))}\n')


def _read(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            "BeeLoop State is not configured; run `beeloop-state setup`"
        ) from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    try:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
