"""
Git Checkpointing & Instant /undo Engine (OpenClaw & Aider Architecture Standout)
"""

from pathlib import Path
import subprocess
from typing import List, Optional, Tuple
from ..paths import workspace_root as _workspace_root


class GitCheckpointManager:
    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = Path(workspace_root or _workspace_root()).resolve()
        self.checkpoints: List[str] = []
        self._ensure_git()

    def _ensure_git(self) -> bool:
        git_dir = self.workspace_root / ".git"
        if not git_dir.exists():
            try:
                subprocess.run(["git", "init"], cwd=self.workspace_root, capture_output=True, check=True)
                return True
            except Exception:
                return False
        return True

    def create_checkpoint(self, turn_index: int, description: str = "") -> Optional[str]:
        if not self._ensure_git():
            return None

        tag_name = f"omni_chk_turn_{turn_index}"
        try:
            subprocess.run(["git", "add", "-A"], cwd=self.workspace_root, capture_output=True, check=True)
            status_res = subprocess.run(["git", "status", "--porcelain"], cwd=self.workspace_root, capture_output=True, text=True)
            if status_res.stdout.strip():
                msg = f"checkpoint(turn_{turn_index}): {description or 'Turn snapshot'}"
                subprocess.run(["git", "commit", "-m", msg, "--no-verify"], cwd=self.workspace_root, capture_output=True)
                subprocess.run(["git", "tag", "-f", tag_name], cwd=self.workspace_root, capture_output=True)
                self.checkpoints.append(tag_name)
                return tag_name
        except Exception:
            pass
        return None

    def undo(self, steps: int = 1) -> Tuple[bool, str, List[str]]:
        if not self._ensure_git():
            return False, "Workspace is not a git repository.", []

        try:
            diff_res = subprocess.run(
                ["git", "diff", "--name-only", f"HEAD~{steps}", "HEAD"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
            )
            affected = [f.strip() for f in diff_res.stdout.splitlines() if f.strip()]

            reset_res = subprocess.run(
                ["git", "reset", "--hard", f"HEAD~{steps}"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
            )

            if reset_res.returncode == 0:
                count = len(affected)
                msg = f"Successfully reverted last {steps} turn(s). {count} file(s) restored."
                return True, msg, affected
            else:
                return False, f"Git undo failed: {reset_res.stderr.strip()}", []
        except Exception as e:
            return False, f"Undo error: {e}", []

    def get_diff(self, against: str = "HEAD~1") -> str:
        if not self._ensure_git():
            return ""
        try:
            res = subprocess.run(
                ["git", "diff", against, "--color=always"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
            )
            return res.stdout
        except Exception:
            return ""
