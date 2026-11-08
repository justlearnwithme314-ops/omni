"""
OmniHarness End-to-End Verification Test Suite
Tests microkernel, provider keyring/rotation, drop-in adapters (OpenClaw, Hermes, Pi),
mode transitions, and Hermes self-evolution loops.
"""

import asyncio
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kernel.config import HarnessConfig
from src.kernel.events import EventBus
from src.kernel.state import SessionState, Message
from src.kernel.engine import KernelEngine
from src.providers.keyring import Keyring
from src.providers.client import UnifiedLLMClient
from src.providers.router import ProviderRouter
from src.adapters.registry import MasterToolRegistry
from src.modes.manager import ModeManager
from src.telemetry.tracer import TrajectoryTracer
from src.evolution.optimizer import EvolutionOptimizer


class TestOmniHarness(unittest.TestCase):
    def setUp(self):
        self.config = HarnessConfig()
        self.event_bus = EventBus()
        self.test_db = os.path.join(os.path.dirname(__file__), "test_auth.db")
        self.test_tracer_db = os.path.join(os.path.dirname(__file__), "test_telemetry.sqlite")
        for db in [self.test_db, self.test_tracer_db]:
            if os.path.exists(db):
                try: os.remove(db)
                except Exception: pass
        self.keyring = Keyring(db_path=self.test_db)
        self.tracer = TrajectoryTracer(db_path=self.test_tracer_db)

    def tearDown(self):
        # Cleanup test databases
        for db in [self.test_db, self.test_tracer_db]:
            if os.path.exists(db):
                try:
                    os.remove(db)
                except Exception:
                    pass

    def test_01_feature_toggles_and_config(self):
        """Verify dynamic feature toggles and profile configuration."""
        self.assertTrue(self.config.is_feature_enabled("plugins_openclaw"))
        self.config.set_feature("plugins_openclaw", False)
        self.assertFalse(self.config.is_feature_enabled("plugins_openclaw"))
        self.config.set_feature("plugins_openclaw", True)

        mode_mgr = ModeManager()
        mode_mgr.set_mode("lean")
        mode_mgr.apply_to_config(self.config)
        self.assertEqual(self.config.active_mode, "lean")
        # In lean mode, plugins are disabled by default for minimal Pi-like overhead
        self.assertFalse(self.config.is_feature_enabled("plugins_openclaw"))

        mode_mgr.set_mode("autonomous")
        mode_mgr.apply_to_config(self.config)
        self.assertTrue(self.config.is_feature_enabled("plugins_openclaw"))

    def test_02_keyring_and_cooldown_rotation(self):
        """Verify Pi-style SQLite credential pool and rate-limit cooldown blocking."""
        self.keyring.add_key("anthropic", "sk-ant-test-primary", priority=10)
        self.keyring.add_key("anthropic", "sk-ant-test-fallback", priority=5)

        keys = self.keyring.get_active_unblocked_keys("anthropic")
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0]["api_key"], "sk-ant-test-primary")

        # Simulate HTTP 429 rate limit on primary key -> block for 60 seconds
        self.keyring.block_key("anthropic", keys[0]["key_id"], cooldown_seconds=60, reason="HTTP 429 RateLimit")

        # Key pool should now automatically surface only the unblocked fallback key
        active_keys = self.keyring.get_active_unblocked_keys("anthropic")
        self.assertEqual(len(active_keys), 1)
        self.assertEqual(active_keys[0]["api_key"], "sk-ant-test-fallback")

    def test_03_hermes_xml_tool_call_parser(self):
        """Verify parsing of Hermes XML <tool_call> tokens into canonical JSON tool calls."""
        client = UnifiedLLMClient()
        xml_content = (
            "I will calculate this for you.\n"
            "<tool_call>{\"name\": \"calculator\", \"arguments\": {\"expression\": \"21 * 2\"}}</tool_call>"
        )
        cleaned_text, parsed_tools = client.parse_tool_calls(xml_content)
        self.assertEqual(len(parsed_tools), 1)
        self.assertEqual(parsed_tools[0]["name"], "calculator")
        self.assertEqual(parsed_tools[0]["arguments"], {"expression": "21 * 2"})

    def test_04_openclaw_dropin_skill_execution(self):
        """Verify drop-in OpenClaw skill loading from SKILL.md and subprocess execution."""
        registry = MasterToolRegistry(config=self.config, event_bus=self.event_bus)
        registry.discover_all()

        calc_tool = registry.get_tool("calculator")
        self.assertIsNotNone(calc_tool, "OpenClaw calculator skill should be discovered")
        self.assertEqual(calc_tool.source, "openclaw")

        # Execute calculator skill
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                registry.execute_tool("calculator", {"expression": "12 * 12"})
            )
            data = json.loads(result)
            self.assertEqual(data.get("result"), 144)
        finally:
            loop.close()

    def test_05_hermes_dropin_plugin_execution(self):
        """Verify drop-in Hermes Python plugin loading and direct execution."""
        registry = MasterToolRegistry(config=self.config, event_bus=self.event_bus)
        registry.discover_all()

        crypto_tool = registry.get_tool("sha256_hash")
        self.assertIsNotNone(crypto_tool, "Hermes sha256_hash tool should be discovered")
        self.assertEqual(crypto_tool.source, "hermes")

        test_str = "omni-harness-verification"
        expected_hash = hashlib.sha256(test_str.encode("utf-8")).hexdigest()

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                registry.execute_tool("sha256_hash", {"text": test_str})
            )
            self.assertEqual(result.strip(), expected_hash)
        finally:
            loop.close()

    def test_06_pi_dropin_extension_execution(self):
        """Verify drop-in Pi JS extension execution via Node bridge."""
        registry = MasterToolRegistry(config=self.config, event_bus=self.event_bus)
        registry.discover_all()

        banner_tool = registry.get_tool("format_banner")
        self.assertIsNotNone(banner_tool, "Pi format_banner tool should be discovered")
        self.assertEqual(banner_tool.source, "pi")

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                registry.execute_tool("format_banner", {"title": "TEST"})
            )
            self.assertIn("=== TEST ===", result)
        finally:
            loop.close()

    def test_07_kernel_engine_turn_loop(self):
        """Verify the core microkernel turn loop with tool execution and message state."""
        registry = MasterToolRegistry(config=self.config, event_bus=self.event_bus)
        registry.discover_all()

        router = ProviderRouter(keyring=self.keyring, config=self.config)
        engine = KernelEngine(
            config=self.config,
            event_bus=self.event_bus,
            provider_router=router,
            tool_registry=registry,
            tracer=self.tracer,
        )

        loop = asyncio.new_event_loop()
        try:
            session_state = SessionState()
            # Run a turn using mock provider
            res = loop.run_until_complete(
                engine.run_turn(session_state, "Calculate 5 * 5", max_tool_iterations=2)
            )
            self.assertIsNotNone(res)
            self.assertTrue(len(session_state.messages) >= 2)
        finally:
            loop.close()

    def test_08_hermes_self_evolution_patch(self):
        """Verify telemetry recording and Hermes-style prompt patch generation."""
        # Log a mock failed turn trace
        self.tracer.log_session_start("session_evolve_test", mode="evolve", model="test-model")
        self.tracer.log_turn(
            session_id="session_evolve_test",
            turn_index=1,
            user_input="Run query",
            assistant_response="Error in query",
            error="SyntaxError: JSON schema mismatch",
        )
        self.tracer.log_session_end("session_evolve_test", success=False)

        optimizer = EvolutionOptimizer(tracer=self.tracer, patches_dir=os.path.join(os.path.dirname(__file__), "test_patches"))
        failures = optimizer.analyze_failures()
        self.assertTrue(len(failures) >= 1)
        self.assertEqual(failures[0]["category"], "schema_formatting_error")

        patch_file = optimizer.generate_prompt_patch(
            failure_summary="Schema formatting error",
            rule_instruction="Validate JSON schema before dispatch."
        )
        self.assertTrue(os.path.exists(patch_file))
        active_patches = optimizer.get_active_patches()
        self.assertIn("Validate JSON schema before dispatch.", active_patches)

        # Cleanup test patches
        try:
            os.remove(patch_file)
            os.rmdir(os.path.join(os.path.dirname(__file__), "test_patches"))
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
