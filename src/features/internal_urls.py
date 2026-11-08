"""Omni Internal URL Protocol Resolver.

Resolves internal URI schemes (skill://, artifact://, agent://, file://)
into workspace content, telemetry traces, and runtime agent states.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Set, Union
import urllib.parse
import urllib.request

logger = logging.getLogger("omni.features.internal_urls")


class class_or_instance_method:
    """Descriptor enabling a method to be invoked cleanly on either class or instance."""

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn = fn

    def __get__(self, instance: Any, owner: type) -> Callable[..., Any]:
        if instance is None:
            return lambda *args, **kwargs: self.fn(owner.get_default_instance(), *args, **kwargs)
        return lambda *args, **kwargs: self.fn(instance, *args, **kwargs)


class InternalURLResolver:
    """Resolves internal Omni harness protocols to content, traces, or runtime states.

    Supported URI schemes:
      - skill://<name>     : Reads the SKILL.md content from discovered skills.
      - artifact://<id>    : Retrieves stored run artifacts/traces by ID from telemetry or storage.
      - agent://<id>       : Reads child agent state and scratchpad.
      - file://<path>      : Normalizes and reads local file paths safely.
    """

    SUPPORTED_SCHEMES: Set[str] = {"skill", "artifact", "agent", "file", "xd"}
    _default_instance: Optional["InternalURLResolver"] = None

    def __init__(
        self,
        workspace_root: Optional[Union[str, Path]] = None,
        tracer_db_path: Optional[str] = None,
        allowed_roots: Optional[List[Union[str, Path]]] = None,
        skills: Optional[Dict[str, str]] = None,
        artifacts: Optional[Dict[str, Any]] = None,
        agents: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize InternalURLResolver with configurable storage roots and registries."""
        if workspace_root is not None:
            self.workspace_root = Path(workspace_root).resolve()
        else:
            # Auto-detect omni repo root if possible, fallback to cwd
            here = Path(__file__).resolve()
            candidate = here.parent.parent.parent
            if (candidate / "harness.config.json").exists() or (candidate / "pyproject.toml").exists():
                self.workspace_root = candidate
            else:
                self.workspace_root = Path.cwd().resolve()

        self.tracer_db_path = tracer_db_path or self._find_tracer_db()
        self.allowed_roots = (
            [Path(r).resolve() for r in allowed_roots]
            if allowed_roots is not None
            else None
        )

        self._skills_cache: Dict[str, str] = dict(skills or {})
        self._artifacts_store: Dict[str, Any] = dict(artifacts or {})
        self._agents_store: Dict[str, Any] = dict(agents or {})

    @classmethod
    def get_default_instance(cls) -> "InternalURLResolver":
        """Retrieve or create the shared default resolver instance."""
        if cls._default_instance is None:
            cls._default_instance = cls()
        return cls._default_instance

    def _find_tracer_db(self) -> Optional[str]:
        """Locate existing telemetry SQLite database."""
        candidates = [
            self.workspace_root / "telemetry.sqlite",
            self.workspace_root / "tests" / "test_telemetry.sqlite",
            Path.cwd() / "telemetry.sqlite",
        ]
        for c in candidates:
            if c.is_file():
                return str(c)
        return str(self.workspace_root / "telemetry.sqlite")

    @classmethod
    def is_internal_url(cls, url: str) -> bool:
        """Check whether a URL matches any supported internal scheme.

        Supports invocation on both InternalURLResolver class and instance.
        """
        if not isinstance(url, str):
            return False
        clean = url.strip()
        if not clean:
            return False
        try:
            parsed = urllib.parse.urlparse(clean)
            return parsed.scheme.lower() in cls.SUPPORTED_SCHEMES
        except Exception:
            return False

    @class_or_instance_method
    def resolve_url(self, url: str) -> Optional[str]:
        """Resolve an internal URL to its string content representation.

        Returns string content if successfully resolved, or None if the resource
        could not be found, is inaccessible, or is not an internal URL.
        """
        if not self.is_internal_url(url):
            return None

        try:
            parsed = urllib.parse.urlparse(url.strip())
            scheme = parsed.scheme.lower()

            if scheme == "skill":
                return self._resolve_skill(parsed)
            elif scheme == "artifact":
                return self._resolve_artifact(parsed)
            elif scheme == "agent":
                return self._resolve_agent(parsed)
            elif scheme == "file":
                return self._resolve_file(parsed)
            elif scheme == "xd":
                return self._resolve_skill(parsed) or self._resolve_artifact(parsed)
            return None
        except Exception as e:
            logger.debug(f"InternalURLResolver: Failed to resolve '{url}': {e}")
            return None

    # -------------------------------------------------------------------------
    # Scheme: skill://<name>
    # -------------------------------------------------------------------------

    def register_skill(self, name: str, content_or_path: Union[str, Path]) -> None:
        """Manually register or override a skill's SKILL.md content."""
        if isinstance(content_or_path, Path):
            if content_or_path.is_file():
                self._skills_cache[name] = content_or_path.read_text(encoding="utf-8", errors="replace")
        else:
            self._skills_cache[name] = str(content_or_path)

    def _resolve_skill(self, parsed: urllib.parse.ParseResult) -> Optional[str]:
        """Read SKILL.md content from discovered skills matching identifier."""
        raw_target = (parsed.netloc + parsed.path).strip("/")
        if not raw_target:
            return None

        # Clean trailing SKILL.md if caller explicitly supplied skill://calc/SKILL.md
        if raw_target.lower().endswith("/skill.md"):
            name = raw_target[:-len("/skill.md")].split("/")[-1]
        else:
            name = raw_target.split("/")[-1]

        # 1. In-memory registered cache
        if raw_target in self._skills_cache:
            return self._skills_cache[raw_target]
        if name in self._skills_cache:
            return self._skills_cache[name]

        # 2. Check direct path under workspace root
        direct_candidate = self.workspace_root / raw_target
        if direct_candidate.is_file() and direct_candidate.name.lower() == "skill.md":
            content = direct_candidate.read_text(encoding="utf-8", errors="replace")
            self._skills_cache[name] = content
            return content
        if (direct_candidate / "SKILL.md").is_file():
            content = (direct_candidate / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            self._skills_cache[name] = content
            return content

        # 3. Discover skills in standard directory hierarchy
        scan_roots = [
            self.workspace_root / "skills",
            self.workspace_root / "plugins" / "openclaw",
            self.workspace_root / "plugins" / "pi",
            self.workspace_root / "plugins" / "hermes",
            self.workspace_root / "plugins",
        ]

        # Recursive search for SKILL.md files
        for search_dir in scan_roots:
            if not search_dir.exists():
                continue
            for skill_path in search_dir.rglob("SKILL.md"):
                folder_name = skill_path.parent.name
                # Fast folder name match
                if folder_name.lower() == name.lower():
                    try:
                        content = skill_path.read_text(encoding="utf-8", errors="replace")
                        self._skills_cache[name] = content
                        return content
                    except Exception as err:
                        logger.warning(f"Failed to read skill at {skill_path}: {err}")

                # Check frontmatter name
                try:
                    text_head = skill_path.read_text(encoding="utf-8", errors="replace")[:1024]
                    fm_match = re.search(r"^\s*name:\s*([a-zA-Z0-9_\-\.]+)", text_head, re.MULTILINE | re.IGNORECASE)
                    if fm_match and fm_match.group(1).strip().lower() == name.lower():
                        full_content = skill_path.read_text(encoding="utf-8", errors="replace")
                        self._skills_cache[name] = full_content
                        return full_content
                except Exception:
                    continue

        return None

    # -------------------------------------------------------------------------
    # Scheme: artifact://<id>
    # -------------------------------------------------------------------------

    def register_artifact(self, artifact_id: str, data: Any) -> None:
        """Register an in-memory artifact or execution trace."""
        self._artifacts_store[artifact_id] = data

    def _resolve_artifact(self, parsed: urllib.parse.ParseResult) -> Optional[str]:
        """Retrieve stored run artifacts/traces by ID from DB or storage."""
        artifact_id = (parsed.netloc + parsed.path).strip("/")
        if not artifact_id:
            return None

        # 1. In-memory store
        if artifact_id in self._artifacts_store:
            data = self._artifacts_store[artifact_id]
            if isinstance(data, (dict, list)):
                return json.dumps(data, indent=2, default=str)
            return str(data)

        # 2. SQLite telemetry database lookup
        if self.tracer_db_path and os.path.exists(self.tracer_db_path):
            try:
                with sqlite3.connect(self.tracer_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()

                    # Check turn_traces table by trace_id
                    cursor.execute(
                        """
                        SELECT trace_id, session_id, turn_index, user_input, assistant_response,
                               tool_calls_json, tool_results_json, error, timestamp
                        FROM turn_traces
                        WHERE trace_id = ?
                        """,
                        (artifact_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        trace_dict = dict(row)
                        for col in ("tool_calls_json", "tool_results_json"):
                            if trace_dict.get(col):
                                try:
                                    trace_dict[col] = json.loads(trace_dict[col])
                                except Exception:
                                    pass
                        return json.dumps(trace_dict, indent=2, default=str)

                    # Check turn_traces table by session_id (returns all turn traces for session)
                    cursor.execute(
                        """
                        SELECT trace_id, session_id, turn_index, user_input, assistant_response,
                               tool_calls_json, tool_results_json, error, timestamp
                        FROM turn_traces
                        WHERE session_id = ?
                        ORDER BY turn_index ASC
                        """,
                        (artifact_id,),
                    )
                    rows = cursor.fetchall()
                    if rows:
                        traces = [dict(r) for r in rows]
                        for t in traces:
                            for col in ("tool_calls_json", "tool_results_json"):
                                if t.get(col):
                                    try:
                                        t[col] = json.loads(t[col])
                                    except Exception:
                                        pass
                        return json.dumps(traces, indent=2, default=str)

                    # Check sessions table
                    cursor.execute(
                        """
                        SELECT session_id, mode, model, start_time, end_time, success, total_tokens, cost
                        FROM sessions
                        WHERE session_id = ?
                        """,
                        (artifact_id,),
                    )
                    s_row = cursor.fetchone()
                    if s_row:
                        return json.dumps(dict(s_row), indent=2, default=str)
            except Exception as e:
                logger.debug(f"Error querying telemetry DB for artifact '{artifact_id}': {e}")

        # 3. File system artifact storage directories
        artifact_dirs = [
            self.workspace_root / "artifacts",
            self.workspace_root / "traces",
            self.workspace_root / "telemetry",
        ]
        for adir in artifact_dirs:
            if not adir.exists():
                continue
            for ext in ("", ".json", ".txt", ".md", ".log"):
                candidate = adir / f"{artifact_id}{ext}"
                if candidate.is_file():
                    try:
                        return candidate.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass

        return None

    # -------------------------------------------------------------------------
    # Scheme: agent://<id>
    # -------------------------------------------------------------------------

    def register_agent(self, agent_id: str, state_or_scratchpad: Any) -> None:
        """Register active child agent state or scratchpad."""
        self._agents_store[agent_id] = state_or_scratchpad

    def _resolve_agent(self, parsed: urllib.parse.ParseResult) -> Optional[str]:
        """Read child agent state/scratchpad by ID."""
        agent_id = (parsed.netloc + parsed.path).strip("/")
        if not agent_id:
            return None

        obj = self._agents_store.get(agent_id)

        # Fallback to subagents registry if available
        if obj is None:
            try:
                from .subagents import SubagentDelegator
                obj = SubagentDelegator.get_active_subagent(agent_id)
            except Exception:
                pass

        if obj is None:
            return None

        # If object is a SessionState instance
        if hasattr(obj, "scratchpad") and hasattr(obj, "messages"):
            payload: Dict[str, Any] = {
                "agent_id": agent_id,
                "session_id": getattr(obj, "session_id", agent_id),
                "scratchpad": getattr(obj, "scratchpad", {}),
                "turn_index": getattr(obj, "turn_index", 0),
                "token_count": getattr(obj, "token_count", 0),
                "messages": [
                    m.to_dict() if hasattr(m, "to_dict") else m
                    for m in getattr(obj, "messages", [])
                ],
            }
            return json.dumps(payload, indent=2, default=str)

        # If dictionary
        if isinstance(obj, dict):
            return json.dumps(obj, indent=2, default=str)

        # If string
        if isinstance(obj, str):
            return obj

        return str(obj)

    # -------------------------------------------------------------------------
    # Scheme: file://<path>
    # -------------------------------------------------------------------------

    def _resolve_file(self, parsed: urllib.parse.ParseResult) -> Optional[str]:
        """Normalize and read local file paths safely."""
        raw_netloc = parsed.netloc or ""
        raw_path = parsed.path or ""

        # Windows paths like file://C:/foo or file:///C:/foo
        if re.match(r"^[a-zA-Z]:", raw_netloc):
            combined = raw_netloc + raw_path
        elif raw_netloc and raw_netloc.lower() != "localhost":
            combined = raw_netloc + raw_path
        else:
            combined = raw_path

        # Convert URL encoding and separators to OS pathname
        system_path_str = urllib.request.url2pathname(combined)

        # Normalize target path
        path_obj = Path(system_path_str)

        if not path_obj.is_file():
            # Handle relative path resolution against workspace root
            clean_rel = system_path_str.lstrip("/\\")
            candidate = (self.workspace_root / clean_rel).resolve()
            if candidate.is_file():
                path_obj = candidate
            else:
                path_obj = path_obj.resolve()
        else:
            path_obj = path_obj.resolve()

        # Enforce file existence and regular file check
        if not path_obj.is_file():
            return None

        # Enforce allowed_roots boundary if configured
        if self.allowed_roots is not None:
            allowed = any(path_obj.is_relative_to(r) for r in self.allowed_roots)
            if not allowed:
                logger.warning(
                    f"Access denied: Path '{path_obj}' outside allowed roots {self.allowed_roots}"
                )
                return None

        try:
            return path_obj.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Error reading file '{path_obj}': {e}")
            return None
