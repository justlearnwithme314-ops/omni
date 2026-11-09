"""
Comprehensive Deep Feature Verification Test Suite for Omni
Tests Standout Features from Pi, Hermes, and OpenClaw, Native Custom Omni Plugins,
and Granular Feature Toggles / Mode Presets.
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _isolation  # noqa: F401  (must come before any src import)
import asyncio
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kernel.config import HarnessConfig
from src.kernel.events import EventBus
from src.kernel.features import FeatureRegistry, CANONICAL_FEATURES, PROFILE_PRESETS
from src.kernel.state import SessionState, Message
from src.kernel.engine import KernelEngine
from src.providers.keyring import Keyring
from src.providers.router import ProviderRouter
from src.adapters.registry import MasterToolRegistry
from src.adapters.omni import OmniAdapter, omni_tool
from src.features.internal_urls import InternalURLResolver
from src.features.subagents import SubagentDelegator
from src.features.speculation import SpeculativeExecutor
from src.features.prefix_cache import PromptPrefixGuardian
from src.features.auxiliary import AuxiliaryLLMClient
from src.features.permissions import PermissionGate
from src.features.persistence import SessionStore


class TestOmniDeepFeatures(unittest.TestCase):
    def setUp(self):
        self.config = HarnessConfig()
        self.event_bus = EventBus()
        self.test_dir = os.path.join(os.path.dirname(__file__), "test_scratch")
        os.makedirs(self.test_dir, exist_ok=True)
        self.test_db = os.path.join(self.test_dir, "test_session_store.sqlite")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            try:
                shutil.rmtree(self.test_dir)
            except Exception:
                pass

    def test_01_feature_registry_and_lean_profile(self):
        """Verify feature registry toggles and switching to the lightweight Pi-style variant."""
        registry = FeatureRegistry(config_data={})
        self.assertTrue(registry.is_enabled("context_compaction"))

        # Switch to lightweight Pi-style lean mode
        self.assertTrue(registry.apply_profile("lean"))
        self.assertFalse(registry.is_enabled("openclaw_skills"), "Lean mode must disable heavy openclaw skills")
        self.assertFalse(registry.is_enabled("pi_extensions"), "Lean mode must disable heavy pi extensions")
        self.assertFalse(registry.is_enabled("self_evolution"), "Lean mode must disable self-evolution")
        self.assertTrue(registry.is_enabled("context_compaction"), "Lean mode preserves fast context compaction")

        # Switch back to full autonomous mode
        self.assertTrue(registry.apply_profile("autonomous"))
        self.assertTrue(registry.is_enabled("openclaw_skills"))
        self.assertTrue(registry.is_enabled("subagents"))
        self.assertTrue(registry.is_enabled("permission_gates"))

        # Individual toggle
        toggled = registry.toggle("speculative_execution")
        self.assertIsNotNone(toggled)
        self.assertEqual(registry.is_enabled("speculative_execution"), toggled)

    def test_02_pi_internal_urls_resolver(self):
        """Verify Pi internal URL resolution (skill://, file://, agent://)."""
        resolver = InternalURLResolver()
        self.assertTrue(resolver.is_internal_url("skill://calculator"))
        self.assertTrue(resolver.is_internal_url("file:///test/path"))
        self.assertFalse(resolver.is_internal_url("https://example.com"))

        # Resolve existing calculator skill
        resolved_skill = resolver.resolve_url("skill://calculator")
        self.assertIsNotNone(resolved_skill)
        self.assertIn("calculator", resolved_skill.lower())

    def test_03_pi_subagent_delegation(self):
        """Verify Pi subagent task delegation for planner, scout, and reviewer."""
        router = ProviderRouter(keyring=Keyring(), config=self.config)
        tool_reg = MasterToolRegistry(config=self.config, event_bus=self.event_bus)
        engine = KernelEngine(
            config=self.config,
            event_bus=self.event_bus,
            provider_router=router,
            tool_registry=tool_reg,
        )

        delegator = SubagentDelegator(engine=engine, tool_registry=tool_reg)
        parent_state = SessionState()

        loop = asyncio.new_event_loop()
        try:
            report = loop.run_until_complete(
                delegator.spawn_task(
                    parent_state=parent_state,
                    specialist="scout",
                    instruction="Find all .py files in src/",
                )
            )
            self.assertEqual(report["specialist"], "scout")
            self.assertIn("subagents", parent_state.scratchpad)
        finally:
            loop.close()

    def test_04_pi_speculative_execution(self):
        """Verify Pi speculative tool call detection during token generation."""
        executor = SpeculativeExecutor()
        stream_token = '<tool_call>{"name": "calculator", "arguments": {"expression":'
        spec_info = executor.check_speculation(stream_token)
        self.assertIsNotNone(spec_info)
        self.assertEqual(spec_info.get("tool_name"), "calculator")

    def test_05_hermes_prefix_cache_guardian(self):
        """Verify Hermes sacred prompt prefix caching and cache-preserving injection."""
        guardian = PromptPrefixGuardian()
        session_id = "test_prefix_sess"
        sys_prompt = "You are Omni, a helpful AI assistant."
        tools = [{"type": "function", "function": {"name": "test_tool"}}]

        # Record initial prefix
        guardian.record_initial_prefix(session_id, sys_prompt, tools)

        # Validate unchanged turn
        is_valid, warning = guardian.validate_turn_prefix(session_id, sys_prompt, tools)
        self.assertTrue(is_valid)
        self.assertIsNone(warning)

        # Mutate system prompt mid-session -> must flag violation
        mutated_sys_prompt = sys_prompt + " Extra injected rule."
        is_valid_mutated, warning_mutated = guardian.validate_turn_prefix(session_id, mutated_sys_prompt, tools)
        self.assertFalse(is_valid_mutated)
        self.assertIsNotNone(warning_mutated)
        self.assertIn("KV Prefix Cache", warning_mutated)

        # Cache-preserving injection
        injection_msg = guardian.format_mid_session_injection("Injected skill content", source="skill")
        self.assertEqual(injection_msg["role"], "user")
        self.assertIn("Injected skill content", injection_msg["content"])

    def test_06_hermes_auxiliary_llm_client(self):
        """Verify Hermes auxiliary client for session titling and judging."""
        router = ProviderRouter(keyring=Keyring(), config=self.config)
        aux_client = AuxiliaryLLMClient(provider_router=router)

        loop = asyncio.new_event_loop()
        try:
            messages = [{"role": "user", "content": "How do I build a binary search tree in Python?"}]
            title = loop.run_until_complete(aux_client.generate_session_title(messages))
            self.assertIsNotNone(title)
            self.assertTrue(len(title) > 0)

            # Score mock trajectory
            score_data = loop.run_until_complete(aux_client.score_trajectory({"user_input": "hi", "assistant_response": "Hello! How can I help you today?", "error": None}))
            self.assertTrue(score_data.get("success", False) or score_data.get("passed", False))
            self.assertEqual(score_data["score"], 1.0)
        finally:
            loop.close()

    def test_07_openclaw_permission_gate(self):
        """Verify OpenClaw dangerous command interception and white/blacklisting."""
        gate = PermissionGate()

        # Safe command
        allowed, reason = gate.check_permission("execute_bash", {"command": "echo hello"})
        self.assertTrue(allowed)

        # Destructive command: rm -rf
        allowed_rm, reason_rm = gate.check_permission("execute_bash", {"command": "rm -rf /var/data"})
        self.assertFalse(allowed_rm)
        self.assertIn("destructive", reason_rm.lower())

        # Destructive SQL: DROP TABLE
        allowed_sql, reason_sql = gate.check_permission("execute_bash", {"command": "DROP TABLE users;"})
        self.assertFalse(allowed_sql)

        # Allow always
        gate.allow_always("rm -rf /tmp/scratch")
        allowed_whitelisted, _ = gate.check_permission("execute_bash", {"command": "rm -rf /tmp/scratch"})
        self.assertTrue(allowed_whitelisted)

    def test_08_openclaw_session_persistence_and_resume(self):
        """Verify OpenClaw durable SQLite session persistence, listing, and resumption."""
        store = SessionStore(db_path=self.test_db)
        state = SessionState(session_id="resume_test_123")
        state.add_message("user", "Hello Omni!")
        state.add_message("assistant", "Hello! How can I help you today?")
        state.scratchpad["test_key"] = "test_val"

        saved_id = store.save_session(state, title="Test Resumption Session", model="gemini-2.5-flash", mode="autonomous")
        self.assertEqual(saved_id, "resume_test_123")

        # List sessions
        sessions = store.list_sessions()
        self.assertTrue(len(sessions) >= 1)
        self.assertEqual(sessions[0]["session_id"], "resume_test_123")
        self.assertEqual(sessions[0]["title"], "Test Resumption Session")

        # Load session
        loaded_state = store.load_session("resume_test_123")
        self.assertIsNotNone(loaded_state)
        self.assertEqual(len(loaded_state.messages), 2)
        self.assertEqual(loaded_state.messages[0].content, "Hello Omni!")
        self.assertEqual(loaded_state.scratchpad.get("test_key"), "test_val")

        # Delete session
        self.assertTrue(store.delete_session("resume_test_123"))
        self.assertIsNone(store.load_session("resume_test_123"))

    def test_09_native_omni_custom_plugins(self):
        """Verify native Omni plugin loading and @omni_tool decorator."""
        # Test @omni_tool decorated function
        @omni_tool(name="unit_test_tool", description="A test tool")
        def sample_fn(x: int, msg: str = "ok") -> str:
            return f"Processed {x}: {msg}"

        self.assertTrue(getattr(sample_fn, "_is_omni_tool", False))
        self.assertEqual(sample_fn._omni_tool_name, "unit_test_tool")
        self.assertIn("x", sample_fn._omni_tool_params["properties"])

        # Discover native custom tools from plugins/omni/
        adapter = OmniAdapter()
        res = adapter.discover()
        discovered_names = [t.name for t in res]
        self.assertIn("system_diagnostic", discovered_names, "Native sample_custom_tool must be discovered")


if __name__ == "__main__":
    unittest.main(verbosity=2)
