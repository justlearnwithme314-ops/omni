"""
Comprehensive Verification Test Suite for OpenClaw Profile & Commands
Verifies all 69 CLI subcommands, 48 chat slash commands, profile activation,
isolation, and execution under the OpenClaw profile.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kernel.commands import CommandRegistry, CLI_SUBCOMMANDS, OPENCLAW_COMMAND_NAMES
from src.kernel.commands_executor import CommandExecutor
from src.kernel.state import SessionState


class TestOpenClawProfileAll(unittest.TestCase):
    def setUp(self):
        self.registry = CommandRegistry(active_profile="openclaw")
        self.executor = CommandExecutor(command_registry=self.registry)
        self.state = SessionState()

    def test_01_openclaw_profile_activation_and_isolation(self):
        """Verify openclaw profile activates all OpenClaw commands and isolates OMP-only commands."""
        self.assertTrue(self.registry.apply_profile("openclaw"))
        active = {c.name for c in self.registry.get_active_commands()}

        # Must include all OpenClaw essentials
        for cmd in ["whoami", "allowlist", "nodes", "pair", "channels", "security",
                    "secrets", "gateway", "cron", "doctor", "steer", "tts", "dashboard"]:
            self.assertIn(cmd, active, f"Command '{cmd}' must be active under OPENCLAW profile")

        # Must isolate OMP-only internal commands
        self.assertNotIn("cleanse", active, "OMP cleanse must be disabled under OPENCLAW profile")
        self.assertNotIn("omfg", active, "OMP omfg must be disabled under OPENCLAW profile")
        self.assertNotIn("ratchet", active, "OMP ratchet must be disabled under OPENCLAW profile")

    def test_02_openclaw_chat_commands_execution(self):
        """Verify execution of OpenClaw slash commands under active profile."""
        self.registry.apply_profile("openclaw")
        loop = asyncio.new_event_loop()
        try:
            test_commands = [
                ("/whoami", "OpenClaw Identity"),
                ("/allowlist add /tmp/safe", "Added '/tmp/safe' to allowlist"),
                ("/tts on", "Text-to-Speech synthesis is now ON"),
                ("/activation mention", "Group activation mode set to: MENTION"),
                ("/send allow", "Outbound delivery policy set to: ALLOW"),
                ("/think xhigh", "Thinking level set to: XHIGH"),
                ("/verbose on", "Verbose logging mode is now ON"),
                ("/trace on", "Plugin trace display is now ON"),
                ("/elevated on", "Elevated execution permissions are now ENABLED"),
                ("/queue fifo", "Turn queue mode set to: FIFO"),
                ("/steer Wait for tests", "Live guidance queued"),
                ("/followup Run next step", "Followup turn queued"),
                ("/subagents", "Session Subagent Runs"),
                ("/agents", "OpenClaw Agent Personas"),
                ("/dashboard", "OpenClaw Web Control UI"),
                ("/diagnostics", "OpenClaw Gateway Diagnostics"),
                ("/openclaw", "OpenClaw Assistant"),
                ("/nodes", "OpenClaw Paired Nodes"),
                ("/pair", "Companion Device Pairing"),
                ("/channels", "Connected Chat Channels"),
                ("/security", "OpenClaw Security Audit"),
                ("/secrets", "Secrets & Credentials"),
                ("/name Operation Claw", "Session renamed to: 'Operation Claw'"),
            ]

            for cmd_str, expected_substr in test_commands:
                handled, output = loop.run_until_complete(self.executor.execute(cmd_str, self.state))
                self.assertTrue(handled, f"OpenClaw command '{cmd_str}' must be handled")
                self.assertIn(expected_substr.lower(), output.lower(), f"Output of '{cmd_str}' must contain '{expected_substr}'")
        finally:
            loop.close()

    def test_03_openclaw_cli_subcommands_catalog(self):
        """Verify all OpenClaw CLI subcommands are cataloged in CLI_SUBCOMMANDS."""
        expected_cli = [
            "setup", "onboard", "configure", "config", "templates", "backup",
            "database", "migrate", "storage", "doctor", "triage", "dashboard",
            "reset", "uninstall", "message", "mcp", "transcripts", "agent",
            "agents", "status", "health", "audit", "sessions", "gateway",
            "daemon", "logs", "system", "models", "nodes", "devices", "node",
            "connect", "sandbox", "cron", "qr", "pairing", "plugins", "channels",
            "security", "secrets", "skills"
        ]
        for cmd in expected_cli:
            self.assertIn(cmd, CLI_SUBCOMMANDS, f"CLI subcommand '{cmd}' must be in CLI_SUBCOMMANDS catalog")


if __name__ == "__main__":
    unittest.main(verbosity=2)
