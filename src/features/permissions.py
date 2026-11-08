"""Omni Permission Gate Module (OpenClaw Standard).

Intercepts potentially destructive operations and tool executions,
enforcing workspace containment, protection of sensitive files,
and prevention of destructive shell commands with whitelist/blacklist memory.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Pattern, Set, Tuple, Union

logger = logging.getLogger("omni.features.permissions")

# Destructive shell command patterns
DESTRUCTIVE_SHELL_PATTERNS: List[Tuple[Pattern[str], str]] = [
    (
        re.compile(
            r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*[rf]|--recursive\s+--force|--force\s+--recursive)\b",
            re.IGNORECASE,
        ),
        "Destructive recursive force deletion ('rm -rf')",
    ),
    (
        re.compile(
            r"\b(DROP\s+DATABASE|DROP\s+SCHEMA|DROP\s+TABLE|TRUNCATE\s+TABLE)\b",
            re.IGNORECASE,
        ),
        "Destructive SQL drop/truncate operation",
    ),
    (
        re.compile(
            r"\b(mkfs(\.[a-zA-Z0-9]+)?|fdisk|parted|gdisk|sfdisk|format\s+[a-zA-Z]:)\b",
            re.IGNORECASE,
        ),
        "Destructive filesystem format/partition operation ('mkfs')",
    ),
    (
        re.compile(r"\bdd\s+.*if=", re.IGNORECASE),
        "Raw block-level copy/overwrite ('dd')",
    ),
    (
        re.compile(r"\bshred\s+", re.IGNORECASE),
        "File shredding operation ('shred')",
    ),
    (
        re.compile(
            r"\b(del\s+/[a-zA-Z/]*[sqf]|rmdir\s+/[a-zA-Z/]*[sq]|Remove-Item\b.*(-Recurse|-Force))",
            re.IGNORECASE,
        ),
        "Destructive Windows force file/directory deletion",
    ),
    (
        re.compile(r"\b(shutdown|reboot|poweroff|init\s+0)\b", re.IGNORECASE),
        "System shutdown or reboot command",
    ),
    (
        re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:", re.IGNORECASE),
        "Fork bomb command detected",
    ),
]

# Sensitive file read patterns
SENSITIVE_FILE_PATTERNS: List[Tuple[Pattern[str], str]] = [
    (
        re.compile(r"(/etc/(shadow|passwd|sudoers|master\.passwd))", re.IGNORECASE),
        "System credentials file access (/etc/shadow, /etc/passwd)",
    ),
    (
        re.compile(
            r"(\.ssh/(id_rsa|id_ed25519|id_ecdsa|id_dsa|authorized_keys|known_hosts)|id_rsa|id_ed25519)",
            re.IGNORECASE,
        ),
        "SSH private key or credential file",
    ),
    (
        re.compile(r"(\.aws/(credentials|config)|\.azure/|\.gcloud/)", re.IGNORECASE),
        "Cloud provider credentials file",
    ),
    (
        re.compile(r"(\.env($|\.|\/))", re.IGNORECASE),
        "Environment secrets file (.env)",
    ),
    (
        re.compile(r"(\.git/config)", re.IGNORECASE),
        "Git repository configuration file",
    ),
    (
        re.compile(
            r"(credentials\.json|client_secret\.json|service_account\.json)",
            re.IGNORECASE,
        ),
        "Service credentials file",
    ),
    (
        re.compile(r"(\.pem|\.key|\.pfx|\.pkcs12)$", re.IGNORECASE),
        "Cryptographic private key file",
    ),
    (
        re.compile(
            r"([C-Z]:[\\/](Windows[\\/]System32[\\/]config[\\/](SAM|SYSTEM|SECURITY|SOFTWARE)))",
            re.IGNORECASE,
        ),
        "Windows security registry hive file",
    ),
    (
        re.compile(r"(\.(bash_history|zsh_history|history))", re.IGNORECASE),
        "Command-line history file",
    ),
]

# Sensitive directories for write protection
SYSTEM_DIRECTORIES: List[Pattern[str]] = [
    re.compile(r"^(/etc|/bin|/sbin|/usr|/boot|/sys|/proc|/dev|/var/log)($|[/\\])", re.IGNORECASE),
    re.compile(r"^([C-Z]:[\\/](Windows|Program Files|Program Files \(x86\)))($|[/\\])", re.IGNORECASE),
]


class PermissionGate:
    """Intercepts destructive tool calls before execution according to OpenClaw rules.

    Maintains whitelist and blacklist memory patterns and validates:
    1. Destructive shell commands (rm -rf, DROP DATABASE, mkfs, format, dd).
    2. Overwrite / modification outside workspace boundary.
    3. Unauthorized reads of sensitive credentials and system files.
    """

    def __init__(
        self,
        workspace_dir: Optional[Union[str, Path]] = None,
        allowed_patterns: Optional[Set[str]] = None,
        denied_patterns: Optional[Set[str]] = None,
    ) -> None:
        if workspace_dir is not None:
            self.workspace_dir = Path(workspace_dir).resolve()
        else:
            self.workspace_dir = Path.cwd().resolve()

        self._allowed_patterns: Set[str] = set(allowed_patterns or [])
        self._denied_patterns: Set[str] = set(denied_patterns or [])

    def allow_always(self, command_pattern: str) -> None:
        """Add command pattern to permanent whitelist memory."""
        pattern_str = command_pattern.strip()
        self._allowed_patterns.add(pattern_str)
        self._denied_patterns.discard(pattern_str)

    def deny_always(self, command_pattern: str) -> None:
        """Add command pattern to permanent blacklist memory."""
        pattern_str = command_pattern.strip()
        self._denied_patterns.add(pattern_str)
        self._allowed_patterns.discard(pattern_str)

    def get_whitelist(self) -> List[str]:
        """Return list of allowed command patterns."""
        return sorted(self._allowed_patterns)

    def get_blacklist(self) -> List[str]:
        """Return list of denied command patterns."""
        return sorted(self._denied_patterns)

    def _matches_pattern(self, pattern: str, text: str) -> bool:
        """Check if target text matches pattern via exact match, substring, or regex."""
        if not pattern or not text:
            return False
        if pattern == text or pattern.lower() in text.lower():
            return True
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            pass
        return False

    def _extract_commands(self, args: Dict[str, Any]) -> List[str]:
        """Extract shell commands from arguments dictionary."""
        candidates = []
        for key in ("command", "cmd", "code", "script", "query", "sql"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
        return candidates

    def _extract_paths(self, args: Dict[str, Any]) -> List[str]:
        """Extract filesystem paths from arguments dictionary."""
        candidates = []
        for key in ("path", "file_path", "file", "filename", "dest", "destination", "target"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
        return candidates

    def _is_outside_workspace(self, path_str: str) -> Tuple[bool, Optional[Path]]:
        """Check whether path resolves outside workspace directory."""
        try:
            target_path = Path(path_str).expanduser()
            if not target_path.is_absolute():
                target_path = (self.workspace_dir / target_path).resolve()
            else:
                target_path = target_path.resolve()

            try:
                target_path.relative_to(self.workspace_dir)
                is_outside = False
            except ValueError:
                is_outside = True

            return is_outside, target_path
        except Exception:
            return True, None

    def check_permission(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """Check whether tool execution is permitted.

        Args:
            tool_name: Name of tool being invoked.
            args: Arguments provided to the tool.

        Returns:
            tuple[bool, str]: (allowed, prompt_or_reason)
        """
        tool = (tool_name or "").strip().lower()
        args = args or {}

        # 1. Gather text representations
        cmds = self._extract_commands(args)
        paths = self._extract_paths(args)

        all_snippets = list(cmds) + list(paths)
        for v in args.values():
            if isinstance(v, str) and v.strip() and v not in all_snippets:
                all_snippets.append(v.strip())

        # 2. Check deny_always patterns
        for pattern in self._denied_patterns:
            if self._matches_pattern(pattern, tool) or any(
                self._matches_pattern(pattern, s) for s in all_snippets
            ):
                return False, f"Blocked: Matches deny_always pattern '{pattern}'"

        # 3. Check allow_always patterns
        for pattern in self._allowed_patterns:
            if self._matches_pattern(pattern, tool) or any(
                self._matches_pattern(pattern, s) for s in all_snippets
            ):
                return True, f"Allowed: Matches allow_always pattern '{pattern}'"

        # 4. Check destructive shell commands (rm -rf, DROP DATABASE, mkfs, etc.)
        is_shell = tool in (
            "bash",
            "shell",
            "sh",
            "exec",
            "cmd",
            "terminal",
            "run_command",
            "execute",
            "powershell",
            "zsh",
            "execute_bash",
            "sql",
            "sqlite",
            "query",
        )
        texts_to_check = cmds if is_shell else all_snippets

        for snippet in texts_to_check:
            for pattern, reason in DESTRUCTIVE_SHELL_PATTERNS:
                m = pattern.search(snippet)
                if m:
                    return False, f"Blocked: {reason} detected ('{m.group(0).strip()}')"

        # 5. Check sensitive file reads
        is_read = tool in (
            "read",
            "read_file",
            "view",
            "cat",
            "open",
            "read_text",
            "get_file",
            "load",
        )
        if is_read:
            for p_str in paths:
                for pattern, reason in SENSITIVE_FILE_PATTERNS:
                    if pattern.search(p_str):
                        return False, f"Blocked: Sensitive file read access detected ('{p_str}') - {reason}"

        # Inspect shell commands for sensitive file read attempts
        if is_shell:
            for snippet in cmds:
                for pattern, reason in SENSITIVE_FILE_PATTERNS:
                    m = pattern.search(snippet)
                    if m:
                        return False, f"Blocked: Sensitive file read access detected in command ('{m.group(0)}') - {reason}"

        # 6. Check overwrite outside workspace
        is_write = tool in (
            "write",
            "write_file",
            "edit",
            "edit_file",
            "patch",
            "create_file",
            "overwrite_file",
            "save_file",
            "delete_file",
            "remove_file",
            "unlink",
            "delete",
        )
        if is_write:
            for p_str in paths:
                # Check dangerous system roots
                for sys_dir in SYSTEM_DIRECTORIES:
                    if sys_dir.search(p_str):
                        return False, f"Blocked: Attempt to modify system directory ('{p_str}')"

                # Check containment within workspace
                is_outside, resolved_path = self._is_outside_workspace(p_str)
                if is_outside:
                    path_display = str(resolved_path) if resolved_path else p_str
                    return (
                        False,
                        f"Blocked: File modification outside workspace boundary ('{path_display}')",
                    )

        # In shell commands, check for redirection write targeting system files
        if is_shell:
            for snippet in cmds:
                redir_matches = re.findall(r"(?:>{1,2})\s*([^\s;|<>&]+)", snippet)
                for redir_path in redir_matches:
                    for sys_dir in SYSTEM_DIRECTORIES:
                        if sys_dir.search(redir_path):
                            return (
                                False,
                                f"Blocked: Shell redirection targeting system directory ('{redir_path}')",
                            )

        return True, "Operation allowed"

    def intercept(self, tool_name: str, args: dict) -> None:
        """Enforce permission check and raise PermissionError if blocked."""
        allowed, reason = self.check_permission(tool_name, args)
        if not allowed:
            raise PermissionError(f"[PermissionGate] {reason}")

    def attach(self, event_bus: Any) -> None:
        """Attach gate listener to EventBus 'before_tool_exec' hook."""
        async def _hook_before_tool_exec(
            session_id: str,
            tool_name: str,
            args: dict,
            context: dict,
        ) -> None:
            self.intercept(tool_name, args)

        if hasattr(event_bus, "subscribe"):
            event_bus.subscribe("before_tool_exec", _hook_before_tool_exec)
