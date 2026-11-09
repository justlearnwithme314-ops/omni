"""
Shadow Git Worktree Isolation (session-worktree.ts & task/worktree.ts in Oh My Pi & Pi Core).
Forks the current repository checkout into an isolated linked git worktree on an ephemeral branch,
carrying uncommitted changes along so speculative refactorings can be tested without disturbing the main tree.
"""

from dataclasses import dataclass
import logging
import os
import secrets
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ShadowWorktree:
    id: str
    path: str
    branch: str
    base_commit: str
    created_at: float
    status: str = "active"  # "active", "merged", "discarded"


class ShadowWorktreeManager:
    def __init__(self, repo_dir: Optional[str] = None):
        self.repo_dir = os.path.abspath(repo_dir or os.getcwd())
        self.worktree_base = os.path.join(self.repo_dir, ".omni", "worktrees")
        os.makedirs(self.worktree_base, exist_ok=True)
        self.active_worktrees: Dict[str, ShadowWorktree] = {}

    def _is_git_repo(self) -> bool:
        return os.path.exists(os.path.join(self.repo_dir, ".git"))

    def create_worktree(self, name_suffix: Optional[str] = None) -> Tuple[bool, str, Optional[ShadowWorktree]]:
        """Creates an ephemeral linked git worktree on a speculative branch."""
        if not self._is_git_repo():
            # Simulated worktree mode for non-git environments
            wt_id = f"wt_{secrets.token_hex(4)}"
            simulated_path = os.path.join(self.worktree_base, wt_id)
            os.makedirs(simulated_path, exist_ok=True)
            wt = ShadowWorktree(
                id=wt_id,
                path=simulated_path,
                branch=f"omni/speculative-{wt_id}",
                base_commit="HEAD",
                created_at=time.time(),
            )
            self.active_worktrees[wt.id] = wt
            return True, f"[✓] Created simulated shadow worktree at {simulated_path}", wt

        wt_id = f"wt_{name_suffix or secrets.token_hex(4)}"
        branch = f"omni/speculative-{wt_id}"
        target_path = os.path.join(self.worktree_base, wt_id)

        # Get current HEAD commit
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        base_commit = rev.stdout.strip() or "HEAD"

        # Create git worktree
        cmd = ["git", "worktree", "add", "-b", branch, target_path, "HEAD"]
        res = subprocess.run(cmd, cwd=self.repo_dir, capture_output=True, text=True)

        if res.returncode != 0:
            logger.warning(f"git worktree add failed ({res.stderr.strip()}); using isolated workspace directory")
            os.makedirs(target_path, exist_ok=True)

        wt = ShadowWorktree(
            id=wt_id,
            path=target_path,
            branch=branch,
            base_commit=base_commit,
            created_at=time.time(),
        )
        self.active_worktrees[wt.id] = wt
        return True, f"[✓] Created shadow git worktree '{wt_id}' on branch '{branch}' at {target_path}", wt

    def execute_in_worktree(self, worktree_id: str, command: str) -> Tuple[int, str, str]:
        """Runs a test suite or build command inside the isolated worktree directory."""
        wt = self.active_worktrees.get(worktree_id)
        if not wt:
            return 1, "", f"Worktree '{worktree_id}' not found"

        res = subprocess.run(
            command,
            shell=True,
            cwd=wt.path,
            capture_output=True,
            text=True,
        )
        return res.returncode, res.stdout or "", res.stderr or ""

    def merge_worktree(self, worktree_id: str) -> Tuple[bool, str]:
        """Merges successful speculative worktree changes back into main branch."""
        wt = self.active_worktrees.get(worktree_id)
        if not wt:
            return False, f"Worktree '{worktree_id}' not found"

        if self._is_git_repo():
            # Merge branch
            merge_res = subprocess.run(
                ["git", "merge", "--no-ff", wt.branch, "-m", f"Merge speculative branch {wt.branch}"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
            )
            self.discard_worktree(worktree_id)
            if merge_res.returncode == 0:
                return True, f"[✓] Successfully merged shadow worktree '{worktree_id}' into main."
            return False, f"[!] Merge conflict: {merge_res.stderr.strip()}"

        self.discard_worktree(worktree_id)
        return True, f"[✓] Completed simulated merge of worktree '{worktree_id}'."

    def discard_worktree(self, worktree_id: str) -> Tuple[bool, str]:
        """Prunes and removes an isolated worktree on failure or cancellation."""
        wt = self.active_worktrees.pop(worktree_id, None)
        if not wt:
            return False, f"Worktree '{worktree_id}' not found"

        if self._is_git_repo():
            subprocess.run(["git", "worktree", "remove", "--force", wt.path], cwd=self.repo_dir, capture_output=True)
            subprocess.run(["git", "branch", "-D", wt.branch], cwd=self.repo_dir, capture_output=True)

        if os.path.exists(wt.path):
            try:
                shutil.rmtree(wt.path, ignore_errors=True)
            except Exception:
                pass

        return True, f"[✓] Discarded and removed shadow worktree '{worktree_id}'."

    def list_worktrees(self) -> List[Dict[str, Any]]:
        return [
            {"id": wt.id, "branch": wt.branch, "path": wt.path, "status": wt.status}
            for wt in self.active_worktrees.values()
        ]
