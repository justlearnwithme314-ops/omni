"""
Verification Test Suite for Command Profiles and Command Execution Handlers
Tests profile filtering (pi, omp, hermes, openclaw, full), individual command toggles,
and real command handler executions.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kernel.commands import CommandRegistry, COMMAND_PROFILES
from src.kernel.commands_executor import CommandExecutor
from src.kernel.state import SessionState


class TestCommandProfiles(unittest.TestCase):
    def setUp(self):
        self.registry = CommandRegistry(active_profile="full")
        self.executor = CommandExecutor(command_registry=self.registry)
        self.state = SessionState()

    def test_01_profile_filtering(self):
        """Verify command profile filtering across pi, omp, hermes, openclaw, and full."""
        # 1. PI Profile: only core & pi commands
        self.assertTrue(self.registry.apply_profile("pi"))
        active = {c.name for c in self.registry.get_active_commands()}
        self.assertIn("model", active)
        self.assertIn("undo", active)
        self.assertIn("tools", active)
        self.assertNotIn("compact", active, "OMP compact must be disabled in PI profile")
        self.assertNotIn("curator", active, "Hermes curator must be disabled in PI profile")
        self.assertNotIn("gateway", active, "OpenClaw gateway must be disabled in PI profile")

        # 2. OMP Profile: core, pi, and omp commands
        self.assertTrue(self.registry.apply_profile("omp"))
        active_omp = {c.name for c in self.registry.get_active_commands()}
        self.assertIn("compact", active_omp)
        self.assertIn("diff", active_omp)
        self.assertIn("thinking", active_omp)
        self.assertNotIn("curator", active_omp)
        self.assertNotIn("gateway", active_omp)

        # 3. HERMES Profile: core, pi, and hermes commands
        self.assertTrue(self.registry.apply_profile("hermes"))
        active_hermes = {c.name for c in self.registry.get_active_commands()}
        self.assertIn("curator", active_hermes)
        self.assertIn("learn", active_hermes)
        self.assertIn("memory", active_hermes)
        self.assertNotIn("compact", active_hermes)
        self.assertNotIn("gateway", active_hermes)

        # 4. OPENCLAW Profile: core, pi, and openclaw commands
        self.assertTrue(self.registry.apply_profile("openclaw"))
        active_claw = {c.name for c in self.registry.get_active_commands()}
        self.assertIn("gateway", active_claw)
        self.assertIn("cron", active_claw)
        self.assertIn("doctor", active_claw)
        self.assertNotIn("cleanse", active_claw)
        self.assertNotIn("curator", active_claw)

        # 5. FULL Profile: all commands
        self.assertTrue(self.registry.apply_profile("full"))
        active_full = {c.name for c in self.registry.get_active_commands()}
        self.assertIn("undo", active_full)
        self.assertIn("compact", active_full)
        self.assertIn("curator", active_full)
        self.assertIn("gateway", active_full)

    def test_02_individual_command_toggle(self):
        """Verify toggling individual slash commands on and off."""
        self.registry.apply_profile("full")
        self.assertIsNotNone(self.registry.get_command("compact"))

        # Disable /compact
        self.assertFalse(self.registry.toggle_command("compact", False))
        self.assertIsNone(self.registry.get_command("compact"))

        # Re-enable /compact
        self.assertTrue(self.registry.toggle_command("compact", True))
        self.assertIsNotNone(self.registry.get_command("compact"))

    def test_03_command_handlers_execution(self):
        """Verify real execution of handlers via CommandExecutor."""
        self.registry.apply_profile("full")
        loop = asyncio.new_event_loop()
        try:
            # Test /tokens
            self.state.token_count = 1250
            handled, msg = loop.run_until_complete(self.executor.execute("/tokens", self.state))
            self.assertTrue(handled)
            self.assertIn("1,250", msg)

            # Test /thinking
            handled, msg = loop.run_until_complete(self.executor.execute("/thinking high", self.state))
            self.assertTrue(handled)
            self.assertIn("HIGH", msg)
            self.assertEqual(self.state.scratchpad.get("thinking_budget"), "high")

            # Test /cleanse
            handled, msg = loop.run_until_complete(self.executor.execute("/cleanse", self.state))
            self.assertTrue(handled)
            self.assertEqual(len(self.state.scratchpad), 0)

            # Test /doctor
            handled, msg = loop.run_until_complete(self.executor.execute("/doctor", self.state))
            self.assertTrue(handled)
            self.assertIn("Omni Diagnostic Health Audit", msg)
            self.assertIn("Python:", msg)

            # Test /profile switch via handler
            handled, msg = loop.run_until_complete(self.executor.execute("/profile pi", self.state))
            self.assertTrue(handled)
            self.assertEqual(self.registry.active_profile, "pi")
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
