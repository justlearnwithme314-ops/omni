"""OmniHarness Kernel Engine Module.

Coordinates conversation turns, LLM completion, event hooks, and tool executions.
Integrates modular feature toggles:
- permission_gates: dangerous command interception via PermissionGate
- internal_urls: resolution of skill://, artifact://, agent:// via InternalURLResolver
- prefix_caching: byte-stability verification via PromptPrefixGuardian
- auxiliary_client: background side-LLM titling and compaction summarization
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid
import warnings

from .config import HarnessConfig
from .events import EventBus
from .state import Message, SessionState

try:
    from src.features.permissions import PermissionGate
except ImportError:
    PermissionGate = None

try:
    from src.features.internal_urls import InternalURLResolver
except ImportError:
    InternalURLResolver = None

try:
    from src.features.prefix_cache import PromptPrefixGuardian
except ImportError:
    PromptPrefixGuardian = None

try:
    from src.features.auxiliary import AuxiliaryLLMClient
except ImportError:
    AuxiliaryLLMClient = None

try:
    from .features import FeatureRegistry
except ImportError:
    FeatureRegistry = None

logger = logging.getLogger(__name__)


class KernelEngine:
    """Core execution engine coordinating LLM providers, tool registries, and event hooks."""

    def __init__(
        self,
        config: HarnessConfig,
        event_bus: EventBus,
        provider_router: Any = None,
        tool_registry: Any = None,
        tracer: Any = None,
        feature_registry: Any = None,
        permission_gate: Any = None,
        url_resolver: Any = None,
        prompt_prefix_guardian: Any = None,
        auxiliary_client: Any = None,
    ) -> None:
        self.config = config
        self.event_bus = event_bus
        self.provider_router = provider_router
        self.tool_registry = tool_registry
        self.tracer = tracer

        # 1. Feature Registry initialization
        if feature_registry is not None:
            self.feature_registry = feature_registry
        elif hasattr(config, "feature_registry") and config.feature_registry:
            self.feature_registry = config.feature_registry
        elif FeatureRegistry is not None:
            self.feature_registry = FeatureRegistry()
        else:
            self.feature_registry = None

        # 2. Permission Gate component
        if permission_gate is not None:
            self.permission_gate = permission_gate
        elif PermissionGate is not None:
            self.permission_gate = PermissionGate()
        else:
            self.permission_gate = None

        # 3. Internal URL Resolver component
        if url_resolver is not None:
            self.url_resolver = url_resolver
        elif InternalURLResolver is not None:
            self.url_resolver = InternalURLResolver()
        else:
            self.url_resolver = None

        # 4. Prompt Prefix Guardian component
        if prompt_prefix_guardian is not None:
            self.prompt_prefix_guardian = prompt_prefix_guardian
        elif PromptPrefixGuardian is not None:
            self.prompt_prefix_guardian = PromptPrefixGuardian()
        else:
            self.prompt_prefix_guardian = None

        # 5. Auxiliary LLM Client component
        if auxiliary_client is not None:
            self.auxiliary_client = auxiliary_client
        elif AuxiliaryLLMClient is not None:
            self.auxiliary_client = AuxiliaryLLMClient(provider_router=self.provider_router)
        else:
            self.auxiliary_client = None

    def is_feature_enabled(self, feature_id: str) -> bool:
        """Check whether a modular feature toggle is enabled."""
        if self.feature_registry and hasattr(self.feature_registry, "is_enabled"):
            return self.feature_registry.is_enabled(feature_id)
        if hasattr(self.config, "is_feature_enabled"):
            return self.config.is_feature_enabled(feature_id)
        return False

    def _are_tools_enabled(self) -> bool:
        """Check whether any plugin system is active for tool exposure."""
        if self.tool_registry is None:
            return False
        plugin_flags = [
            "plugins_pi",
            "plugins_hermes",
            "plugins_openclaw",
            "plugins_mcp",
            "openclaw_skills",
            "hermes_plugins",
            "pi_extensions",
            "mcp_servers",
        ]
        return any(self.is_feature_enabled(f) for f in plugin_flags)

    async def _get_tool_schemas(self) -> Optional[List[Dict[str, Any]]]:
        """Retrieve OpenAI-compatible tool schemas from tool registry if enabled."""
        if not self._are_tools_enabled() or self.tool_registry is None:
            return None

        schemas = None
        if hasattr(self.tool_registry, "get_openai_schemas"):
            res = self.tool_registry.get_openai_schemas()
            schemas = await res if inspect.isawaitable(res) else res
        elif hasattr(self.tool_registry, "get_schemas"):
            res = self.tool_registry.get_schemas()
            schemas = await res if inspect.isawaitable(res) else res

        return schemas if schemas else None

    def _select_model(self, tier: Optional[str] = None) -> str:
        """Select default model based on configuration and active mode."""
        if tier is None:
            mode = getattr(self.config, "active_mode", "lean")
            tier = "fast" if mode == "lean" else "smart"

        tiers = getattr(self.config, "model_tiers", {})
        models = tiers.get(tier, [])
        if models:
            return models[0]

        smart_models = tiers.get("smart", [])
        if smart_models:
            return smart_models[0]

        return "openai/gpt-4o"

    def _resolve_internal_urls(self, text: str, state: SessionState) -> str:
        """Resolve internal URL schemes (skill://, artifact://, agent://) via InternalURLResolver."""
        resolver = self.url_resolver
        if resolver is None:
            return text

        import re
        pattern = re.compile(
            r"(skill://[^\s<>\"'\)]+|artifact://[^\s<>\"'\)]+|agent://[^\s<>\"'\)]+)",
            re.IGNORECASE,
        )
        matches = list(pattern.finditer(text))
        if not matches:
            return text

        context = {
            "state": state,
            "scratchpad": getattr(state, "scratchpad", {}),
            "session_id": getattr(state, "session_id", ""),
        }

        # If user input is exclusively the URL itself, replace entirely with resolved text
        if len(matches) == 1 and text.strip() == matches[0].group(1).strip():
            resolved = resolver.resolve_url(matches[0].group(1), context=context)
            return resolved if resolved is not None else text

        # Otherwise, resolve each matched URL and append as context
        injections: List[str] = []
        for m in matches:
            url = m.group(1)
            resolved = resolver.resolve_url(url, context=context)
            if resolved is not None:
                injections.append(f"[{url}]:\n{resolved}")

        if injections:
            text = f"{text}\n\n=== Resolved Internal URLs ===\n" + "\n\n".join(injections)

        return text

    async def _call_provider(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        model: str,
    ) -> Any:
        """Call provider router with messages and optional tool definitions."""
        if self.provider_router is None:
            raise RuntimeError("No provider_router configured on KernelEngine")

        complete_fn = getattr(self.provider_router, "complete", None)
        if complete_fn is None:
            complete_fn = getattr(self.provider_router, "chat", None)
        if complete_fn is None:
            raise AttributeError("provider_router must provide 'complete' method")

        call_kwargs: Dict[str, Any] = {
            "messages": messages,
            "model": model,
        }
        if tools:
            call_kwargs["tools"] = tools

        try:
            sig = inspect.signature(complete_fn)
            has_var_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            if has_var_kwargs:
                res = complete_fn(**call_kwargs)
            else:
                filtered = {
                    k: v for k, v in call_kwargs.items() if k in sig.parameters
                }
                res = complete_fn(**filtered)
        except (TypeError, ValueError):
            res = complete_fn(**call_kwargs)

        if inspect.isawaitable(res):
            res = await res
        return res

    def _extract_response(
        self, response: Any
    ) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
        """Normalize response to text content and list of tool call dicts."""
        if response is None:
            return "", None

        if isinstance(response, str):
            return response, None

        content = ""
        tool_calls: Optional[List[Any]] = None

        if isinstance(response, dict):
            if "choices" in response and response["choices"]:
                msg = response["choices"][0].get("message", {})
                content = msg.get("content") or ""
                tool_calls = msg.get("tool_calls")
            else:
                content = response.get("content") or ""
                tool_calls = response.get("tool_calls")
            return content, self._normalize_tool_calls(tool_calls)

        if hasattr(response, "choices") and response.choices:
            choice = response.choices[0]
            msg = getattr(choice, "message", None)
            if msg:
                content = getattr(msg, "content", "") or ""
                tool_calls = getattr(msg, "tool_calls", None)
            else:
                content = getattr(choice, "text", "") or ""
            return content, self._normalize_tool_calls(tool_calls)

        if hasattr(response, "content"):
            content = getattr(response, "content", "") or ""
        elif hasattr(response, "text"):
            content = getattr(response, "text", "") or ""

        if hasattr(response, "tool_calls"):
            tool_calls = getattr(response, "tool_calls", None)

        return content, self._normalize_tool_calls(tool_calls)

    def _normalize_tool_calls(
        self, tool_calls: Any
    ) -> Optional[List[Dict[str, Any]]]:
        """Ensure tool calls are formatted as a list of dictionaries."""
        if not tool_calls:
            return None
        normalized: List[Dict[str, Any]] = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                normalized.append(tc)
            else:
                tc_dict: Dict[str, Any] = {}
                if hasattr(tc, "id"):
                    tc_dict["id"] = tc.id
                if hasattr(tc, "type"):
                    tc_dict["type"] = tc.type
                fn = getattr(tc, "function", None)
                if fn:
                    if isinstance(fn, dict):
                        tc_dict["function"] = fn
                    else:
                        tc_dict["function"] = {
                            "name": getattr(fn, "name", ""),
                            "arguments": getattr(fn, "arguments", "{}"),
                        }
                elif hasattr(tc, "name"):
                    tc_dict["function"] = {
                        "name": getattr(tc, "name", ""),
                        "arguments": getattr(
                            tc, "arguments", getattr(tc, "args", "{}")
                        ),
                    }
                normalized.append(tc_dict)
        return normalized if normalized else None

    def _parse_single_tool_call(
        self, tc: Dict[str, Any]
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Extract tool_call_id, tool_name, and parsed arguments dict."""
        call_id = tc.get("id") or str(uuid.uuid4())
        fn = tc.get("function")
        if isinstance(fn, dict):
            tool_name = fn.get("name", "")
            raw_args = fn.get("arguments", {})
        else:
            tool_name = tc.get("name", "")
            raw_args = tc.get("arguments", tc.get("args", {}))

        if isinstance(raw_args, str):
            try:
                tool_args = json.loads(raw_args) if raw_args.strip() else {}
            except Exception:
                tool_args = {"raw_input": raw_args}
        elif isinstance(raw_args, dict):
            tool_args = raw_args
        else:
            tool_args = {}

        return call_id, tool_name, tool_args

    async def _execute_tool(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Any:
        """Invoke a tool through tool_registry, passing context if accepted."""
        if self.tool_registry is None:
            raise RuntimeError("No tool_registry configured on KernelEngine")

        exec_fn = getattr(self.tool_registry, "execute_tool", None)
        if exec_fn is None:
            exec_fn = getattr(self.tool_registry, "execute", None)
        if exec_fn is None:
            raise AttributeError(
                "tool_registry must provide execute_tool or execute method"
            )

        try:
            sig = inspect.signature(exec_fn)
            accepts_context = "context" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            if accepts_context:
                res = exec_fn(tool_name, tool_args, context=context)
            else:
                res = exec_fn(tool_name, tool_args)
        except (TypeError, ValueError):
            try:
                res = exec_fn(tool_name, tool_args, context=context)
            except TypeError:
                res = exec_fn(tool_name, tool_args)

        if inspect.isawaitable(res):
            res = await res
        return res

    async def run_turn(
        self,
        state: SessionState,
        user_input: str,
        max_tool_iterations: int = 10,
        model: Optional[str] = None,
    ) -> str:
        """Execute a conversation turn with tool loop, event hooks, and feature toggles."""
        # 1. Emits before_turn
        state.turn_index += 1
        await self.event_bus.emit(
            "before_turn",
            session_id=state.session_id,
            turn_index=state.turn_index,
            state=state,
        )

        # 2. Check internal_urls feature toggle
        effective_input = user_input
        if (
            self.feature_registry
            and self.feature_registry.is_enabled("internal_urls")
        ) or self.is_feature_enabled("internal_urls"):
            schemes = ("skill://", "artifact://", "agent://")
            if any(s in effective_input for s in schemes):
                effective_input = self._resolve_internal_urls(effective_input, state)

        # Adds user message to state
        state.add_message(role="user", content=effective_input)

        # 3. Apply context compaction with optional auxiliary client summarization
        if self.is_feature_enabled("context_compaction"):
            custom_summary: Optional[str] = None
            if (
                self.feature_registry
                and self.feature_registry.is_enabled("auxiliary_client")
            ) or self.is_feature_enabled("auxiliary_client"):
                aux = self.auxiliary_client
                if aux is not None:
                    max_turns_threshold = 10
                    conversation = [m for m in state.messages if m.role != "system"]
                    user_turns = sum(1 for m in conversation if m.role == "user")
                    if user_turns > max_turns_threshold:
                        older_msgs = state.get_messages_for_llm()[:-max_turns_threshold * 2]
                        try:
                            custom_summary = await aux.summarize_older_turns(older_msgs)
                        except Exception as aux_err:
                            logger.warning("Auxiliary client summarization error: %s", aux_err)
            state.compact_history(custom_summary=custom_summary)

        final_text: str = ""

        # 4. Turn loop
        for iteration in range(max_tool_iterations):
            # Assemble messages and tool schemas
            messages = state.get_messages_for_llm()
            tools = await self._get_tool_schemas()
            selected_model = model or self._select_model()

            # 5. Check prefix_caching feature toggle
            if (
                self.feature_registry
                and self.feature_registry.is_enabled("prefix_caching")
            ) or self.is_feature_enabled("prefix_caching"):
                guardian = self.prompt_prefix_guardian
                if guardian is not None:
                    sys_prompt = next(
                        (m.content for m in state.messages if m.role == "system"),
                        "",
                    )
                    is_valid, warning_msg = guardian.validate_turn_prefix(
                        session_id=state.session_id,
                        system_prompt=sys_prompt,
                        tools=tools,
                    )
                    if not is_valid and warning_msg:
                        logger.warning("[PrefixCache Guardian] %s", warning_msg)
                        warnings.warn(warning_msg, RuntimeWarning, stacklevel=2)
                        await self.event_bus.emit(
                            "on_error",
                            session_id=state.session_id,
                            error=RuntimeWarning(warning_msg),
                            context={
                                "state": state,
                                "phase": "prefix_caching",
                                "warning": warning_msg,
                            },
                        )

            # Calls event_bus.emit('before_llm', ...)
            await self.event_bus.emit(
                "before_llm",
                session_id=state.session_id,
                messages=messages,
                tools=tools or [],
                model=selected_model,
            )

            # Calls provider_router.complete(...)
            try:
                response = await self._call_provider(
                    messages=messages,
                    tools=tools,
                    model=selected_model,
                )
            except Exception as err:
                await self.event_bus.emit(
                    "on_error",
                    session_id=state.session_id,
                    error=err,
                    context={
                        "state": state,
                        "phase": "llm_complete",
                        "iteration": iteration,
                    },
                )
                raise

            # Calls event_bus.emit('after_llm', ...)
            await self.event_bus.emit(
                "after_llm",
                session_id=state.session_id,
                response=response,
                state=state,
            )

            content, tool_calls = self._extract_response(response)
            final_text = content or ""

            # Adds assistant message to state
            state.add_message(
                role="assistant",
                content=content or "",
                tool_calls=tool_calls,
            )

            # If no tool calls in response, return final answer string
            if not tool_calls:
                return final_text

            # For each tool call:
            for tc in tool_calls:
                call_id, tool_name, tool_args = self._parse_single_tool_call(tc)
                tool_context = {
                    "session_id": state.session_id,
                    "state": state,
                    "call_id": call_id,
                    "iteration": iteration,
                }

                # 6. Check permission_gates feature toggle
                if (
                    self.feature_registry
                    and self.feature_registry.is_enabled("permission_gates")
                ) or self.is_feature_enabled("permission_gates"):
                    gate = self.permission_gate
                    if gate is not None:
                        allowed, reason = gate.check_permission(tool_name, tool_args)
                        if not allowed:
                            logger.warning(
                                "PermissionGate blocked tool '%s': %s",
                                tool_name,
                                reason,
                            )
                            perm_error = PermissionError(
                                f"Permission denied by PermissionGate: {reason}"
                            )
                            await self.event_bus.emit(
                                "on_error",
                                session_id=state.session_id,
                                error=perm_error,
                                context={
                                    "state": state,
                                    "phase": "permission_gate",
                                    "tool_name": tool_name,
                                    "args": tool_args,
                                    "reason": reason,
                                },
                            )
                            tool_result = f"Permission denied by PermissionGate: {reason}"

                            self._render_tool_result(tool_name, tool_result)
                # Emits after_tool_exec
                            await self.event_bus.emit(
                                "after_tool_exec",
                                session_id=state.session_id,
                                tool_name=tool_name,
                                result=tool_result,
                                context=tool_context,
                            )

                            # Append tool result Message to state and proceed to next tool call
                            state.add_message(
                                role="tool",
                                content=str(tool_result),
                                tool_call_id=call_id,
                                name=tool_name,
                            )
                            continue

                self._render_tool_call(tool_name, tool_args)
                # Emits before_tool_exec
                await self.event_bus.emit(
                    "before_tool_exec",
                    session_id=state.session_id,
                    tool_name=tool_name,
                    args=tool_args,
                    context=tool_context,
                )

                # Executes tool via tool_registry.execute_tool(...)
                try:
                    tool_result = await self._execute_tool(
                        tool_name, tool_args, tool_context
                    )
                except Exception as tool_err:
                    await self.event_bus.emit(
                        "on_error",
                        session_id=state.session_id,
                        error=tool_err,
                        context={
                            "state": state,
                            "phase": "tool_exec",
                            "tool_name": tool_name,
                        },
                    )
                    tool_result = f"Error executing tool '{tool_name}': {tool_err}"

                # Emits after_tool_exec
                await self.event_bus.emit(
                    "after_tool_exec",
                    session_id=state.session_id,
                    tool_name=tool_name,
                    result=tool_result,
                    context=tool_context,
                )

                # Appends tool result Message (role='tool') to state
                if isinstance(tool_result, (dict, list)):
                    result_str = json.dumps(tool_result, default=str)
                else:
                    result_str = str(tool_result)

                state.add_message(
                    role="tool",
                    content=result_str,
                    tool_call_id=call_id,
                    name=tool_name,
                )

        return final_text

    def _generate_trajectory(
        self,
        state: SessionState,
        error: Optional[Exception] = None,
    ) -> Dict[str, Any]:
        """Generate trajectory record for telemetry and self-evolution."""
        return {
            "session_id": state.session_id,
            "turn_index": state.turn_index,
            "messages": [m.to_dict() for m in state.messages],
            "scratchpad": dict(state.scratchpad),
            "token_count": state.token_count,
            "success": error is None,
            "error": str(error) if error else None,
        }

    def _get_default_system_prompt(self, model: Optional[str] = None) -> str:
        """Construct default system prompt with explicit model introspection."""
        mode = getattr(self.config, "active_mode", "autonomous")
        active_model = model or getattr(self.config, "default_model", "google-antigravity/gemini-2.5-flash")
        from ..features.context_files import project_context, skills_block
        extra = "".join(f"\n\n{s}" for s in (project_context(), skills_block()) if s)
        return (
            f"You are Omni, a fast, modular, and extensible agentic AI system.\n"
            f"You are currently running on model: '{active_model}' in '{mode}' mode.\n"
            f"When asked who you are or which AI model you are using, introduce yourself as Omni and clearly state that your active underlying model is {active_model}.\n"
            f"Execute user requests accurately using available tools when necessary."
            + extra
        )

    def _render_tool_call(self, tool_name: str, tool_args: Dict[str, Any]):
        try:
            from rich.console import Console
            from rich.panel import Panel
            c = Console(safe_box=False)
            arg_str = json.dumps(tool_args, indent=2) if tool_args else "(no arguments)"
            c.print(Panel(arg_str, title=f"[bold yellow]Tool Call: {tool_name}[/bold yellow]", border_style="yellow", expand=False))
        except Exception:
            print(f"--> [Tool Call: {tool_name}] {tool_args}")

    def _render_tool_result(self, tool_name: str, tool_result: Any):
        try:
            from rich.console import Console
            from rich.panel import Panel
            c = Console(safe_box=False)
            res_str = tool_result if isinstance(tool_result, str) else json.dumps(tool_result, indent=2, default=str)
            if len(res_str) > 600:
                res_str = res_str[:600] + "... [truncated]"
            c.print(Panel(res_str, title=f"[bold green]Result: {tool_name}[/bold green]", border_style="green", expand=False))
        except Exception:
            print(f"<-- [Result: {tool_name}] {str(tool_result)[:140]}")

    async def generate_session_title(self, state: SessionState) -> str:
        """Generate a title for the session using auxiliary client if enabled."""
        if (
            self.feature_registry
            and self.feature_registry.is_enabled("auxiliary_client")
        ) or self.is_feature_enabled("auxiliary_client"):
            aux = self.auxiliary_client
            if aux is not None:
                return await aux.generate_session_title(state.get_messages_for_llm())
        for m in state.messages:
            if m.role == "user" and m.content:
                return m.content[:40]
        return "New Session"

    async def summarize_history(self, state: SessionState) -> str:
        """Summarize conversation history using auxiliary client if enabled."""
        if (
            self.feature_registry
            and self.feature_registry.is_enabled("auxiliary_client")
        ) or self.is_feature_enabled("auxiliary_client"):
            aux = self.auxiliary_client
            if aux is not None:
                return await aux.summarize_older_turns(state.get_messages_for_llm())
        return f"Summary of {len(state.messages)} messages"

    async def run_session(
        self,
        user_input: str,
        state: Optional[SessionState] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> SessionState:
        """Handle complete session lifecycle with trajectory generation and events."""
        if state is None:
            state = SessionState()
            sys_prompt = system_prompt or self._get_default_system_prompt()
            if sys_prompt:
                state.add_message(role="system", content=sys_prompt)

        # 1. Lifecycle start hook
        await self.event_bus.emit(
            "on_session_start",
            session_id=state.session_id,
            state=state,
        )

        session_error: Optional[Exception] = None
        try:
            # 2. Execute turn
            await self.run_turn(state=state, user_input=user_input, model=model)

            # 3. Auxiliary client titling if enabled and not already titled
            if (
                self.feature_registry
                and self.feature_registry.is_enabled("auxiliary_client")
            ) or self.is_feature_enabled("auxiliary_client"):
                aux = self.auxiliary_client
                if aux is not None and "title" not in state.scratchpad:
                    try:
                        title = await aux.generate_session_title(state.get_messages_for_llm())
                        if title:
                            state.scratchpad["title"] = title
                    except Exception as aux_err:
                        logger.warning("Auxiliary client titling error: %s", aux_err)

        except Exception as exc:
            session_error = exc
            state.scratchpad["last_error"] = str(exc)
            await self.event_bus.emit(
                "on_error",
                session_id=state.session_id,
                error=exc,
                context={"state": state, "phase": "run_session"},
            )
        finally:
            # 4. Trajectory generation
            trajectory = self._generate_trajectory(state, error=session_error)

            # 5. Lifecycle end hook
            await self.event_bus.emit(
                "on_session_end",
                session_id=state.session_id,
                trajectory=trajectory,
                state=state,
            )

        return state
