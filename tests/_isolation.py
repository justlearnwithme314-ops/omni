"""
Test isolation. Import this module FIRST in every test module (before any ``src`` import).

It points every location the harness writes to at a throw-away directory, so running the
suite can never touch the real repository, ``~/.omni*`` history, configuration or the
credentials database:

    OMNI_HOME       per-user state (benchmarks, paired nodes, worktrees, browser profile)
    OMNI_WORKSPACE  a scratch git repo (checkpoints / /snapshot / /undo commit HERE)
    OMNI_CONFIG     a copy of harness.config.json with every plugin system enabled
    OMNI_AUTH_DB    scratch credentials database
    OMNI_HISTORY    scratch prompt-history file
"""

import atexit
import json
import os
import shutil
import subprocess
import tempfile

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_installed = False
TMP = ""


def install() -> str:
    global _installed, TMP
    if _installed:
        return TMP
    TMP = tempfile.mkdtemp(prefix="omni-test-")
    home = os.path.join(TMP, "home")
    workspace = os.path.join(TMP, "workspace")
    os.makedirs(os.path.join(home, ".omni"))
    os.makedirs(workspace)

    os.environ.update({
        "OMNI_HOME": os.path.join(home, ".omni"),
        "OMNI_WORKSPACE": workspace,
        "OMNI_CONFIG": os.path.join(TMP, "harness.config.json"),
        "OMNI_AUTH_DB": os.path.join(TMP, "agent_auth.db"),
        "OMNI_HISTORY": os.path.join(home, ".omni_history"),
        "GIT_AUTHOR_NAME": "omni-test", "GIT_AUTHOR_EMAIL": "test@localhost",
        "GIT_COMMITTER_NAME": "omni-test", "GIT_COMMITTER_EMAIL": "test@localhost",
    })

    # Scratch workspace = real git repo with one commit (so checkpoint/undo have something to work with).
    git = lambda *a: subprocess.run(["git", *a], cwd=workspace, capture_output=True)
    git("init", "-q", "-b", "main")
    with open(os.path.join(workspace, "README.md"), "w", encoding="utf-8") as f:
        f.write("scratch workspace\n")
    git("add", ".")
    git("commit", "-qm", "init")

    # Scratch config: start from the project's file but force the full plugin set on.
    cfg = {}
    src = os.path.join(_ROOT, "harness.config.json")
    if os.path.exists(src):
        with open(src, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg["active_mode"] = "autonomous"
    feats = cfg.setdefault("features", {})
    for k in ("plugins_pi", "plugins_hermes", "plugins_openclaw", "plugins_mcp",
              "pi_extensions", "hermes_plugins", "openclaw_skills", "mcp_servers"):
        feats[k] = True
    with open(os.environ["OMNI_CONFIG"], "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    atexit.register(shutil.rmtree, TMP, True)
    _installed = True
    return TMP


install()
