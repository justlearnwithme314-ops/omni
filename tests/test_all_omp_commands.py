"""
Comprehensive Verification Test Suite for All OMP Commands & Profiles
Verifies all 56 CLI subcommands, 175 slash commands, dynamic profile filtering,
and real command handler executions across every category.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kernel.commands import CommandRegistry, CLI_SUBCOMMANDS, ALL_SLASH_COMMANDS, COMMAND_PROFILES
from src.kernel.commands_executor import CommandExecutor
from src.kernel.state import SessionState


class TestAllOmpCommands(unittest.TestCase):
    def setUp(self):
        self.registry = CommandRegistry(active_profile="full")
        self.executor = CommandExecutor(command_registry=self.registry)
        self.state = SessionState()

    def test_01_cli_subcommands_catalog(self):
        """Verify all 56+ OMP CLI subcommands are registered with descriptions."""
        self.assertTrue(len(CLI_SUBCOMMANDS) >= 56)
        expected_sample = ["acp", "agents", "auth", "bench", "browser-relay", "cleanse", "commit",
                           "config", "diff", "doctor", "find", "git", "grep", "login", "models",
                           "ps", "read", "say", "session", "stats", "undo", "usage", "worktree"]
        for cmd in expected_sample:
            self.assertIn(cmd, CLI_SUBCOMMANDS, f"CLI subcommand '{cmd}' must be in catalog")
            self.assertTrue(len(CLI_SUBCOMMANDS[cmd]) > 0)

    def test_02_slash_commands_catalog(self):
        """Verify 170+ unique slash commands are cataloged across categories."""
        active_cmds = self.registry.get_active_commands()
        self.assertTrue(len(active_cmds) >= 170, f"Expected >= 170 slash commands, got {len(active_cmds)}")

        categories = {c.category for c in active_cmds}
        self.assertIn("core", categories)
        self.assertIn("pi", categories)
        self.assertIn("omp", categories)
        self.assertIn("hermes", categories)
        self.assertIn("openclaw", categories)

    def test_03_profile_filtering_integrity(self):
        """Verify strict profile isolation across pi, omp, hermes, openclaw, and full."""
        # 1. PI Profile: only core and pi
        self.assertTrue(self.registry.apply_profile("pi"))
        pi_cmds = self.registry.get_active_commands()
        self.assertTrue(len(pi_cmds) <= 20)
        self.assertEqual({c.category for c in pi_cmds}, {"core", "pi"})
        self.assertIsNone(self.registry.get_command("compact"))
        self.assertIsNone(self.registry.get_command("curator"))
        self.assertIsNone(self.registry.get_command("gateway"))

        # 2. OMP Profile: core, pi, and omp
        self.assertTrue(self.registry.apply_profile("omp"))
        omp_cmds = self.registry.get_active_commands()
        self.assertTrue(len(omp_cmds) >= 150)
        self.assertEqual({c.category for c in omp_cmds}, {"core", "pi", "omp"})
        self.assertIsNotNone(self.registry.get_command("compact"))
        self.assertIsNone(self.registry.get_command("curator"))
        self.assertIsNone(self.registry.get_command("gateway"))

        # 3. HERMES Profile: core, pi, and hermes
        self.assertTrue(self.registry.apply_profile("hermes"))
        hermes_cmds = self.registry.get_active_commands()
        self.assertEqual({c.category for c in hermes_cmds}, {"core", "pi", "hermes"})
        self.assertIsNotNone(self.registry.get_command("curator"))
        self.assertIsNone(self.registry.get_command("compact"))

        # 4. OPENCLAW Profile: core, pi, and openclaw
        self.assertTrue(self.registry.apply_profile("openclaw"))
        claw_cmds = self.registry.get_active_commands()
        self.assertEqual({c.category for c in claw_cmds}, {"core", "pi", "openclaw"})
        self.assertIsNotNone(self.registry.get_command("gateway"))
        self.assertIsNone(self.registry.get_command("compact"))

        # 5. FULL Profile: all
        self.assertTrue(self.registry.apply_profile("full"))
        full_cmds = self.registry.get_active_commands()
        self.assertTrue(len(full_cmds) >= 170)

    def test_04_representative_command_executions(self):
        """Verify real execution of commands across all categories."""
        self.registry.apply_profile("full")
        loop = asyncio.new_event_loop()
        try:
            sample_tests = [
                ("/help", "Omni Slash Commands"),
                ("/tokens", "Session Tokens:"),
                ("/compact", "SnapCompact executed"),
                ("/cleanse", "cleansed"),
                ("/thinking high", "HIGH"),
                ("/budget", "Output Budget"),
                ("/stats", "Telemetry Analytics"),
                ("/todo append Buy bread", "Added todo"),
                ("/tree", "Session Timeline Tree"),
                ("/memory", "Mnemopi Knowledge Graph"),
                ("/curator", "Knowledge Curator"),
                ("/doctor", "Omni Diagnostic Health Audit"),
                ("/cron", "Autonomous Cron Scheduler"),
                ("/status", "Omni System Status"),
                ("/webhooks", "Omni Webhook Endpoints"),
                ("/snapshot test_snapshot", "created in sqlite"),
                ("/rollback 0", "rolled back"),
            ]

            for cmd_str, expected_substr in sample_tests:
                handled, output = loop.run_until_complete(self.executor.execute(cmd_str, self.state))
                self.assertTrue(handled, f"Command '{cmd_str}' must be handled")
                self.assertIn(expected_substr.lower(), output.lower(), f"Output of '{cmd_str}' must contain '{expected_substr}'")
        finally:
            loop.close()

    def test_05_disabled_command_feedback(self):
        """Verify clear user guidance when invoking a command disabled under the active profile."""
        self.registry.apply_profile("pi")
        loop = asyncio.new_event_loop()
        try:
            handled, output = loop.run_until_complete(self.executor.execute("/compact", self.state))
            self.assertTrue(handled)
            self.assertIn("disabled under profile 'PI'", output)
            self.assertIn("/profile full", output)
        finally:
            loop.close()

    def test_06_individual_command_toggle(self):
        """Verify individual command on/off toggle."""
        self.registry.apply_profile("full")
        self.assertIsNotNone(self.registry.get_command("compact"))

        # Disable
        self.registry.toggle_command("compact", False)
        self.assertIsNone(self.registry.get_command("compact"))

        # Re-enable
        self.registry.toggle_command("compact", True)
        self.assertIsNotNone(self.registry.get_command("compact"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
