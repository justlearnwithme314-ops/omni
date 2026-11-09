"""
Shadow git worktrees (Oh My Pi ``session-worktree`` / Pi isolation).

Fork the current checkout into a linked git worktree on a throw-away branch, carry the
uncommitted tracked changes along, run commands/tests there, and merge back only if the
tests pass. Everything is a real ``git worktree``; if the directory is not a git repo the
operations raise :class:`WorktreeError` (nothing is simulated).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from ..paths import user_data_dir
from ..paths import workspace_root

logger = logging.getLogger(__name__)

Command = Union[str, Sequence[str]]


class WorktreeError(RuntimeError):
    pass


@dataclass
class ShadowWorktree:
    id: str
    path: str
    branch: str
    base_commit: str
    created_at: float
    status: str = "active"  # active | merged | discarded


@dataclass
class VerifyOutcome:
    passed: bool
    merged: bool
    exit_code: int
    output: str
    message: str


def _argv(command: Command) -> List[str]:
    return shlex.split(command, posix=True) if isinstance(command, str) else list(command)


class ShadowWorktreeManager:
    def __init__(self, repo_dir: Optional[str] = None, base_dir: Optional[str] = None):
        self.repo_dir = os.path.abspath(repo_dir or workspace_root())
        # Outside the repo so `git status` of the main checkout stays clean.
        self.base_dir = base_dir or str(user_data_dir() / "worktrees")
        os.makedirs(self.base_dir, exist_ok=True)
        self.active_worktrees: Dict[str, ShadowWorktree] = {}

    # ---- git helpers -------------------------------------------------------
    def _git(self, args: List[str], cwd: Optional[str] = None, check: bool = True, input_bytes: Optional[bytes] = None):
        proc = subprocess.run(["git", *args], cwd=cwd or self.repo_dir, capture_output=True, input=input_bytes)
        if check and proc.returncode != 0:
            raise WorktreeError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode(errors='replace').strip()}")
        return proc

    def _require_repo(self) -> None:
        if not shutil.which("git"):
            raise WorktreeError("git is not installed")
        probe = self._git(["rev-parse", "--is-inside-work-tree"], check=False)
        if probe.returncode != 0:
            raise WorktreeError(f"{self.repo_dir} is not a git repository")

    # ---- lifecycle ---------------------------------------------------------
    def create_worktree(self, name_suffix: Optional[str] = None) -> Tuple[bool, str, ShadowWorktree]:
        self._require_repo()
        wt_id = f"wt_{name_suffix or secrets.token_hex(4)}"
        branch = f"omni/{wt_id}"
        path = os.path.join(self.base_dir, f"{os.path.basename(self.repo_dir)}-{wt_id}")
        base = self._git(["rev-parse", "HEAD"]).stdout.decode().strip()
        self._git(["worktree", "add", "-b", branch, path, "HEAD"])

        carried = ""
        patch = self._git(["diff", "HEAD", "--binary"]).stdout
        if patch.strip():
            applied = self._git(["apply", "--whitespace=nowarn", "-"], cwd=path, check=False, input_bytes=patch)
            carried = " (uncommitted tracked changes carried over)" if applied.returncode == 0 else \
                f" (WARNING: could not carry uncommitted changes: {applied.stderr.decode(errors='replace').strip()})"

        wt = ShadowWorktree(id=wt_id, path=path, branch=branch, base_commit=base, created_at=time.time())
        self.active_worktrees[wt_id] = wt
        return True, f"Created worktree '{wt_id}' on branch '{branch}' at {path}{carried}", wt

    def _get(self, worktree_id: str) -> ShadowWorktree:
        wt = self.active_worktrees.get(worktree_id)
        if not wt:
            raise WorktreeError(f"Worktree '{worktree_id}' not found")
        return wt

    def execute_in_worktree(self, worktree_id: str, command: Command, timeout: int = 300) -> Tuple[int, str, str]:
        wt = self._get(worktree_id)
        try:
            proc = subprocess.run(_argv(command), cwd=wt.path, capture_output=True, text=True, timeout=timeout)
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"
        except FileNotFoundError as e:
            return 127, "", str(e)

    def commit_worktree(self, worktree_id: str, message: str = "omni: shadow worktree changes") -> bool:
        wt = self._get(worktree_id)
        self._git(["add", "-A"], cwd=wt.path)
        if not self._git(["status", "--porcelain"], cwd=wt.path).stdout.strip():
            return False
        self._git(["-c", "user.name=omni", "-c", "user.email=omni@localhost", "commit", "-m", message], cwd=wt.path)
        return True

    def merge_worktree(self, worktree_id: str) -> Tuple[bool, str]:
        """Fast-forward the main checkout to the worktree branch (never creates conflicts silently)."""
        wt = self._get(worktree_id)
        self.commit_worktree(worktree_id)
        merged = self._git(["merge", "--ff-only", wt.branch], check=False)
        if merged.returncode != 0:
            return False, ("Fast-forward merge refused (main moved or has conflicting local changes): "
                           + merged.stderr.decode(errors="replace").strip())
        wt.status = "merged"
        self.discard_worktree(worktree_id)
        return True, f"Merged '{wt.branch}' into the current branch"

    def discard_worktree(self, worktree_id: str) -> Tuple[bool, str]:
        wt = self._get(worktree_id)
        self._git(["worktree", "remove", "--force", wt.path], check=False)
        self._git(["branch", "-D", wt.branch], check=False)
        shutil.rmtree(wt.path, ignore_errors=True)
        del self.active_worktrees[worktree_id]
        return True, f"Removed worktree '{worktree_id}'"

    def verify_and_merge(self, worktree_id: str, test_command: Command) -> VerifyOutcome:
        """Run tests in the worktree; merge if they pass, discard if they fail."""
        code, out, err = self.execute_in_worktree(worktree_id, test_command)
        output = (out + err).strip()
        if code != 0:
            self.discard_worktree(worktree_id)
            return VerifyOutcome(False, False, code, output, "Tests failed: worktree discarded, main untouched")
        ok, msg = self.merge_worktree(worktree_id)
        return VerifyOutcome(True, ok, code, output, msg)

    def list_worktrees(self) -> List[Dict[str, Any]]:
        return [
            {"id": w.id, "branch": w.branch, "path": w.path, "status": w.status}
            for w in self.active_worktrees.values()
        ]
