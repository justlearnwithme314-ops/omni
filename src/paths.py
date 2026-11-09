"""
Single source of truth for filesystem locations.

Two distinct notions that used to be conflated (and hard-coded as ``D:\\Harness\\...``):

* ``PROJECT_ROOT``      - where Omni itself lives: bundled plugins, harness.config.json, credentials DB.
* ``workspace_root()``  - the project the user is working ON: checkpoints, AST search, mental models,
                          sandbox working directory. Defaults to the current directory; override with
                          the ``OMNI_WORKSPACE`` environment variable (the test-suite does).
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def workspace_root() -> Path:
    return Path(os.environ.get("OMNI_WORKSPACE") or os.getcwd()).resolve()


def history_file() -> Path:
    """Prompt history used for ghost suggestions. ``OMNI_HISTORY`` overrides ``~/.omni_history``."""
    return Path(os.environ.get("OMNI_HISTORY") or (Path.home() / ".omni_history"))


def user_data_dir() -> Path:
    """Per-user state (history, browser profile, worktrees). ``OMNI_HOME`` overrides ``~/.omni``."""
    return Path(os.environ.get("OMNI_HOME") or (Path.home() / ".omni"))
