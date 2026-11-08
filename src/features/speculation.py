"""Omni Speculative Tool Execution & Early Parameter Validation.

Inspects streaming LLM completion tokens in flight, detects candidate tool invocations,
pre-validates parameter schemas, and locks execution dependencies before generation completes.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger("omni.features.speculation")


class class_or_instance_method:
    """Descriptor enabling a method to be invoked cleanly on either class or instance."""

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn = fn

    def __get__(self, instance: Any, owner: type) -> Callable[..., Any]:
        if instance is None:
            return lambda *args, **kwargs: self.fn(owner.get_default_instance(), *args, **kwargs)
        return lambda *args, **kwargs: self.fn(instance, *args, **kwargs)


class SpeculativeExecutor:
    """Speculative Tool Executor (Pi Architecture Standout).

    Monitors partial token streams from the LLM, extracts emerging tool calls,
    validates partial parameter schemas, and acquires dependency locks early
    to minimize execution latency.
    """

    _default_instance: Optional["SpeculativeExecutor"] = None

    def __init__(self, tool_registry: Any = None) -> None:
        """Initialize SpeculativeExecutor with optional tool registry."""
        self.tool_registry = tool_registry
        self._dependency_locks: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def get_default_instance(cls) -> "SpeculativeExecutor":
        """Retrieve or create the shared default SpeculativeExecutor instance."""
        if cls._default_instance is None:
            cls._default_instance = cls()
        return cls._default_instance

    # -------------------------------------------------------------------------
    # Dependency Locking
    # -------------------------------------------------------------------------

    def lock_dependencies(
        self,
        tool_name: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Lock execution dependencies for a speculatively detected tool call.

        Locks the tool execution mutex and any specific resource identifiers
        (e.g., file paths, network endpoints, agent IDs) found in arguments.
        """
        params = parameters or {}
        locked_keys: List[str] = [f"tool:{tool_name}"]

        # Resource-level dependency locks based on parameter semantics
        for key in ("path", "file", "filename", "filepath", "target", "cwd"):
            if key in params and isinstance(params[key], str) and params[key].strip():
                normalized = params[key].replace("\\", "/").strip()
                locked_keys.append(f"file:{normalized}")

        for key in ("url", "uri", "endpoint"):
            if key in params and isinstance(params[key], str) and params[key].strip():
                locked_keys.append(f"net:{params[key].strip()}")

        for key in ("session_id", "agent_id", "task_id"):
            if key in params and isinstance(params[key], str) and params[key].strip():
                locked_keys.append(f"agent:{params[key].strip()}")

        # Register lock records
        now = time.time()
        for lk in locked_keys:
            if lk not in self._dependency_locks:
                self._dependency_locks[lk] = {
                    "key": lk,
                    "tool_name": tool_name,
                    "locked_at": now,
                    "status": "locked",
                }

        return locked_keys

    def release_dependencies(self, lock_keys: List[str]) -> None:
        """Release specific dependency locks."""
        for k in lock_keys:
            self._dependency_locks.pop(k, None)

    def release_all_locks(self) -> None:
        """Release all currently active dependency locks."""
        self._dependency_locks.clear()

    def is_locked(self, lock_key: str) -> bool:
        """Check whether a specific dependency key is currently locked."""
        return lock_key in self._dependency_locks

    def get_locked_dependencies(self) -> Dict[str, Dict[str, Any]]:
        """Return all active dependency locks."""
        return dict(self._dependency_locks)

    # -------------------------------------------------------------------------
    # Schema Pre-Validation
    # -------------------------------------------------------------------------

    def prevalidate_schema(
        self,
        schema: Optional[Dict[str, Any]],
        parameters: Dict[str, Any],
    ) -> Tuple[bool, str, List[str], List[str]]:
        """Pre-validate partial parameters against a tool schema.

        Returns:
            (is_valid, validation_status, missing_required, type_errors)
            validation_status: 'valid' | 'partial' | 'invalid'
        """
        if not schema or not isinstance(schema, dict):
            return True, "valid", [], []

        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # Check required fields
        missing_required = [f for f in required if f not in parameters]

        # Check parameter types for fields present so far
        type_errors: List[str] = []
        for param_name, val in parameters.items():
            if param_name in properties:
                expected_type = properties[param_name].get("type")
                if expected_type == "string" and not isinstance(val, str):
                    type_errors.append(f"'{param_name}' expected string, got {type(val).__name__}")
                elif expected_type == "integer" and not (isinstance(val, int) and not isinstance(val, bool)):
                    type_errors.append(f"'{param_name}' expected integer, got {type(val).__name__}")
                elif expected_type == "number" and not isinstance(val, (int, float)):
                    type_errors.append(f"'{param_name}' expected number, got {type(val).__name__}")
                elif expected_type == "boolean" and not isinstance(val, bool):
                    type_errors.append(f"'{param_name}' expected boolean, got {type(val).__name__}")
                elif expected_type == "array" and not isinstance(val, list):
                    type_errors.append(f"'{param_name}' expected array, got {type(val).__name__}")
                elif expected_type == "object" and not isinstance(val, dict):
                    type_errors.append(f"'{param_name}' expected object, got {type(val).__name__}")

        if type_errors:
            return False, "invalid", missing_required, type_errors
        elif missing_required:
            return False, "partial", missing_required, type_errors
        else:
            return True, "valid", missing_required, type_errors

    # -------------------------------------------------------------------------
    # Stream Inspection & Argument Recovery
    # -------------------------------------------------------------------------

    def _recover_partial_json(self, raw_json_str: str) -> Dict[str, Any]:
        """Heuristically recover and parse incomplete streaming JSON dictionaries."""
        text = raw_json_str.strip()
        if not text:
            return {}

        # 1. Direct parse attempt
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        # 2. Repair unterminated quotes and braces
        repaired = text
        # Count unescaped double quotes
        unescaped_quotes = 0
        for i, ch in enumerate(repaired):
            if ch == '"' and (i == 0 or repaired[i - 1] != "\\"):
                unescaped_quotes += 1

        if unescaped_quotes % 2 != 0:
            repaired += '"'

        open_curly = repaired.count("{") - repaired.count("}")
        open_square = repaired.count("[") - repaired.count("]")

        repaired += "]" * max(0, open_square)
        repaired += "}" * max(0, open_curly)

        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        # 3. Regex key-value extraction fallback
        recovered: Dict[str, Any] = {}
        for k, v in re.findall(r'"([a-zA-Z0-9_]+)"\s*:\s*"([^"]*)"?', text):
            recovered[k] = v
        for k, v in re.findall(r'"([a-zA-Z0-9_]+)"\s*:\s*(-?\d+(?:\.\d+)?)', text):
            recovered[k] = float(v) if "." in v else int(v)
        for k, v in re.findall(r'"([a-zA-Z0-9_]+)"\s*:\s*(true|false)', text, re.IGNORECASE):
            recovered[k] = v.lower() == "true"

        return recovered

    def _extract_tool_invocation(
        self,
        partial_text: str,
    ) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        """Extract tool call name, syntax format, and partial arguments from streaming text.

        Returns (tool_name, syntax_format, partial_parameters) or None.
        """
        if not partial_text:
            return None

        # Format 1: XML tool_call tag with JSON body (<tool_call>\n{"name": "...", "arguments": ...})
        m_xml = re.search(
            r"<(?:tool_call|function_call)>\s*(\{[\s\S]*)$",
            partial_text,
            re.IGNORECASE,
        )
        if m_xml:
            json_body = m_xml.group(1)
            name_match = re.search(
                r'"name"\s*:\s*"([a-zA-Z0-9_\-\.]+)"',
                json_body,
                re.IGNORECASE,
            )
            if name_match:
                tool_name = name_match.group(1).strip()
                # Extract arguments portion
                args_match = re.search(
                    r'"(?:arguments|parameters|input)"\s*:\s*(\{[\s\S]*)$',
                    json_body,
                    re.IGNORECASE,
                )
                args = self._recover_partial_json(args_match.group(1)) if args_match else {}
                return tool_name, "xml_block", args

        # Format 2: XML invoke tag (<invoke name="calculator"> or <tool name="...">)
        m_invoke = re.search(
            r'<(?:invoke|tool|action)\s+name=["\']([a-zA-Z0-9_\-\.]+)["\'](?:\s*>)?([\s\S]*)$',
            partial_text,
            re.IGNORECASE,
        )
        if m_invoke:
            tool_name = m_invoke.group(1).strip()
            rest = m_invoke.group(2) or ""
            args = self._recover_partial_json(rest) if "{" in rest else {}
            return tool_name, "xml_tag", args

        # Format 3: JSON function calling / OpenAI schema stream
        m_json_name = re.search(
            r'"(?:name|tool|action|function_name)"\s*:\s*"([a-zA-Z0-9_\-\.]+)"',
            partial_text,
            re.IGNORECASE,
        )
        if m_json_name:
            tool_name = m_json_name.group(1).strip()
            args_match = re.search(
                r'"(?:arguments|parameters|input|args)"\s*:\s*(\{[\s\S]*)$',
                partial_text,
                re.IGNORECASE,
            )
            args = self._recover_partial_json(args_match.group(1)) if args_match else {}
            return tool_name, "json", args

        # Format 4: ReAct style (Action: tool_name \n Action Input: {...})
        m_action = re.search(
            r"(?:Action|Tool)\s*:\s*([a-zA-Z0-9_\-\.]+)(?:[\r\n]+(?:Action\s+Input|Args|Parameters)\s*:\s*([\s\S]*))?$",
            partial_text,
            re.IGNORECASE,
        )
        if m_action:
            tool_name = m_action.group(1).strip()
            arg_str = m_action.group(2) or ""
            args = self._recover_partial_json(arg_str) if "{" in arg_str else {}
            return tool_name, "action", args

        # Format 5: Direct function call syntax: tool_name(arg="val", ...)
        m_fn = re.search(
            r"\b([a-zA-Z0-9_]{2,})\s*\(([\s\S]*?)(?:\)|$)",
            partial_text,
        )
        if m_fn:
            tool_name = m_fn.group(1).strip()
            args_body = m_fn.group(2) or ""
            args = {}
            for k, v in re.findall(r'([a-zA-Z0-9_]+)\s*=\s*["\']([^"\']*)["\']', args_body):
                args[k] = v
            return tool_name, "function_call", args

        return None

    @staticmethod
    def _extract_schema(tool: Any) -> Dict[str, Any]:
        """Safely extract parameter schema from tool object or dictionary."""
        if tool is None:
            return {}
        if isinstance(tool, dict):
            return (
                tool.get("parameters")
                or tool.get("schema")
                or (tool.get("function", {}).get("parameters") if isinstance(tool.get("function"), dict) else None)
                or {}
            )
        return getattr(tool, "parameters", None) or getattr(tool, "schema", None) or {}

    def _lookup_tool_definition(
        self,
        tool_name: str,
        tool_registry: Any,
    ) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
        """Look up tool instance and parameter schema from registry."""
        registry = tool_registry if tool_registry is not None else self.tool_registry
        if registry is None:
            return None, None

        # 1. MasterToolRegistry style: registry.get_tool(name)
        if hasattr(registry, "get_tool"):
            tool = registry.get_tool(tool_name)
            if tool is not None:
                return tool, self._extract_schema(tool)

        # 2. Dictionary style: registry[tool_name]
        if isinstance(registry, dict):
            item = registry.get(tool_name)
            if item is not None:
                return item, self._extract_schema(item)

        # 3. Object with .tools dict
        if hasattr(registry, "tools") and isinstance(registry.tools, dict):
            item = registry.tools.get(tool_name)
            if item is not None:
                return item, self._extract_schema(item)

        return None, None

    # -------------------------------------------------------------------------
    # Main Speculation Check Entry Point
    # -------------------------------------------------------------------------

    @class_or_instance_method
    def check_speculation(
        self,
        partial_text: str,
        tool_registry: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """Inspect partial LLM stream tokens for tool call names.

        Pre-validates tool parameter schemas and locks execution dependencies.

        Args:
            partial_text: The accumulating partial completion text from the LLM stream.
            tool_registry: Optional tool registry to lookup schemas and validate parameters.

        Returns:
            Speculation report dict if a tool call was detected, or None.
        """
        if not partial_text or not isinstance(partial_text, str):
            return None

        # 1. Inspects partial LLM stream tokens for tool call names
        extracted = self._extract_tool_invocation(partial_text)
        if not extracted:
            return None

        tool_name, syntax_format, partial_params = extracted

        # Lookup tool definition and parameter schema from registry
        tool_instance, param_schema = self._lookup_tool_definition(tool_name, tool_registry)

        # 2. Pre-validates tool parameter schemas
        is_valid, val_status, missing_req, type_errors = self.prevalidate_schema(
            param_schema, partial_params
        )

        # 3. Locks execution dependencies
        locked_keys = self.lock_dependencies(tool_name, partial_params)

        # Determine overall execution readiness
        status = "ready" if (tool_instance is not None and is_valid) else (
            "partial" if val_status == "partial" else ("invalid" if type_errors else "detected")
        )

        result: Dict[str, Any] = {
            "tool_name": tool_name,
            "speculative": True,
            "status": status,
            "format": syntax_format,
            "parameters": partial_params,
            "schema_valid": is_valid,
            "validation_status": val_status,
            "missing_required": missing_req,
            "type_errors": type_errors,
            "locks": locked_keys,
            "tool": tool_instance,
            "confidence": 1.0 if syntax_format in ("xml_block", "json") else 0.85,
        }

        logger.debug(
            f"SpeculativeExecutor: Tool '{tool_name}' detected ({syntax_format}), "
            f"status={status}, locks={locked_keys}"
        )
        return result
