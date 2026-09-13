"""Persistent configuration for the standalone BeeLoop State store."""

from __future__ import annotations

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
    if data is None:
        return (Path.home() / ".beeloop_states").resolve()
    configured = data.get("state_dir")
    if not isinstance(configured, str) or not configured:
        raise ConfigError(f"{path} must contain a non-empty string `state_dir`")
    resolved = Path(configured)
    if not resolved.is_absolute():
        raise ConfigError(f"{path}: `state_dir` must be an absolute path")
    return resolved.resolve()


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
