from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_state_dir(monkeypatch):
    """Legacy environment and host data paths must not steer the tests."""
    for name in (
        "BEEBOT_STATE_DIR",
        "BEELOOP_STATE_DIR",
        "PLUGIN_DATA",
        "CLAUDE_PLUGIN_DATA",
    ):
        monkeypatch.delenv(name, raising=False)
