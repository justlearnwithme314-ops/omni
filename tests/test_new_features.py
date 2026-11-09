import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _isolation  # noqa: F401  (must come before any src import)
from pathlib import Path
"""
Verification Test Suite for the 7 Advanced Features:
Output Budgeting, Git Checkpointing & /undo, Skill Dependency Installer,
Mnemopi Memory Graph, Autonomous Cron, Webhooks, and Execution Sandbox.
"""

import asyncio
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.features.output_budget import OutputBudgetEnforcer
from src.features.checkpoints import GitCheckpointManager
from src.features.installer import SkillDependencyInstaller
from src.features.memory_graph import MemoryGraph
from src.features.cron import CronScheduler
from src.features.webhooks import WebhookDispatcher
from src.features.sandbox import ExecutionSandbox


class TestOmniAdvancedFeatures(unittest.TestCase):
    def setUp(self):
        self.scratch_dir = os.path.join(os.path.dirname(__file__), "test_scratch_adv")
        os.makedirs(self.scratch_dir, exist_ok=True)
        self.db_mem = os.path.join(self.scratch_dir, "test_mem.sqlite")
        self.db_cron = os.path.join(self.scratch_dir, "test_cron.sqlite")
        for db in [self.db_mem, self.db_cron]:
            if os.path.exists(db):
                try: os.remove(db)
                except Exception: pass

    def tearDown(self):
        if os.path.exists(self.scratch_dir):
            try:
                shutil.rmtree(self.scratch_dir)
            except Exception:
                pass

    def test_01_output_budget_clamping(self):
        """Verify intelligent head + tail sliding window clamping on large outputs."""
        enforcer = OutputBudgetEnforcer(max_lines=20, max_chars=1000)

        # Output with 100 lines
        huge_output = "\n".join(f"Line {i}: test log statement" for i in range(100))
        clamped, was_truncated = enforcer.clamp(huge_output)

        self.assertTrue(was_truncated)
        self.assertIn("truncated by OutputBudget", clamped)
        self.assertTrue(len(clamped.splitlines()) < 30)
        self.assertTrue(clamped.startswith("Line 0:"))
        self.assertTrue(clamped.endswith("Line 99: test log statement"))

    def test_02_git_checkpoints_and_undo(self):
        """Verify git checkpoint creation and 1-command /undo rollback."""
        chk = GitCheckpointManager(workspace_root=self.scratch_dir)
        test_file = os.path.join(self.scratch_dir, "target.txt")

        # Initial file state
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("Initial clean state\n")
        chk.create_checkpoint(1, "Initial checkpoint")

        # Mutate file in turn 2
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("Modified state by agent tool\n")
        chk.create_checkpoint(2, "Turn 2 mutation")

        with open(test_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "Modified state by agent tool")

        # Undo 1 turn
        success, msg, files = chk.undo(steps=1)
        self.assertTrue(success)
        with open(test_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "Initial clean state")

    def test_03_skill_installer_detection(self):
        """Verify package manager detection and install spec parsing."""
        installer = SkillDependencyInstaller(workspace_root=self.scratch_dir)
        pms = installer.detect_package_managers()
        self.assertIn("pip", pms)
        self.assertIsNotNone(pms["pip"])

        # Test dry-run installation
        specs = {"pip": ["requests"]}
        success, log = installer.install_dependencies(Path(self.scratch_dir), specs, dry_run=True)
        self.assertTrue(success)
        self.assertIn("[Dry-run]", log)

    def test_04_memory_graph_assertions(self):
        """Verify Subject-Predicate-Object assertion storage and semantic recall."""
        mem = MemoryGraph(db_path=self.db_mem)
        mem.add_triple("user", "prefers", "Python", confidence=0.95)
        mem.add_triple("project", "uses", "SQLite", confidence=0.90)

        triples = mem.query_triples(keyword="Python")
        self.assertEqual(len(triples), 1)
        self.assertEqual(triples[0]["subject"], "user")
        self.assertEqual(triples[0]["object"], "Python")

        # Natural statement fact extraction
        facts = mem.extract_and_store_facts("I prefer Dark Mode in the editor")
        self.assertTrue(len(facts) >= 1)

        # Context recall
        context = mem.recall_relevant_context("What does the project use?")
        self.assertIn("Relevant Memory", context)
        self.assertIn("SQLite", context)

    def test_05_cron_scheduler(self):
        """Verify autonomous scheduled recurring task registration and interval calculation."""
        cron = CronScheduler(db_path=self.db_cron)
        task_id = cron.add_task(
            name="Daily GitHub Inspection",
            schedule_expr="daily",
            prompt="Check notifications and summarize",
        )
        self.assertIsNotNone(task_id)

        tasks = cron.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "Daily GitHub Inspection")
        self.assertEqual(tasks[0]["interval_seconds"], 86400.0)

        self.assertTrue(cron.delete_task(task_id))
        self.assertEqual(len(cron.list_tasks()), 0)

    def test_06_webhooks_dispatching(self):
        """Verify Slack challenge handshake and universal webhook routing."""
        dispatcher = WebhookDispatcher(engine=None)

        loop = asyncio.new_event_loop()
        try:
            # Slack challenge verification
            slack_payload = {"type": "url_verification", "challenge": "slack_test_challenge_12345"}
            res = loop.run_until_complete(dispatcher.handle_slack_webhook(slack_payload))
            self.assertEqual(res.get("challenge"), "slack_test_challenge_12345")

            # Missing prompt generic webhook
            err_res = loop.run_until_complete(dispatcher.handle_generic_webhook({}))
            self.assertIn("error", err_res)
        finally:
            loop.close()

    def test_07_execution_sandbox_fallback(self):
        """Verify execution sandbox local fallback and execution interface."""
        sandbox = ExecutionSandbox(workspace_dir=self.scratch_dir, prefer_docker=False)
        code, stdout, stderr = sandbox.execute("echo SANDBOX_ACTIVE")
        self.assertEqual(code, 0)
        self.assertIn("SANDBOX_ACTIVE", stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
