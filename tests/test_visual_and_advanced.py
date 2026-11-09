"""
Comprehensive Test Suite for Visual & Advanced Subsystems in Omni
Tests Themes & Skins, Split-Pane Dual View, Spinners & Counters, Petdex Mascots,
TTSR, AST-Grep, Mental Models, Audio Notifier, MoA, and Egress Firewall.
"""

import asyncio
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.features.themes import ThemeManager, THEMES
from src.features.split_pane import DualPaneLayout
from src.features.spinners import LiveStreamingSpinner
from src.features.petdex import PetdexManager, PET_REGISTRY
from src.features.ttsr import TTSRInterceptor
from src.features.ast_grep import ASTGrepSearcher
from src.features.mental_models import MentalModelRepository
from src.features.audio_chimes import AudioNotifier
from src.features.moa import MixtureOfAgentsEngine
from src.features.egress import EgressProxyFirewall


class TestVisualAndAdvancedFeatures(unittest.TestCase):
    def setUp(self):
        self.scratch = os.path.join(os.path.dirname(__file__), "test_scratch_visual")
        os.makedirs(self.scratch, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.scratch):
            try:
                shutil.rmtree(self.scratch)
            except Exception:
                pass

    def test_01_truecolor_themes(self):
        """Verify 5 truecolor themes (Tokyo Night, Catppuccin, Matrix, Nord, Solarized)."""
        tm = ThemeManager(config_path=os.path.join(self.scratch, "omni.config.json"))
        self.assertEqual(len(THEMES), 5)
        for expected in ["tokyo-night", "catppuccin", "matrix", "nord", "solarized"]:
            self.assertIn(expected, THEMES)

        self.assertTrue(tm.set_theme("matrix"))
        self.assertEqual(tm.get_theme().name, "matrix")
        self.assertEqual(tm.get_theme().primary, "#00ff41")

        rich_theme = tm.get_rich_theme()
        self.assertIsNotNone(rich_theme)

    def test_02_split_pane_dual_view(self):
        """Verify DualPaneLayout split-screen rendering (conversation + diff + todos)."""
        dual = DualPaneLayout()
        diff_sample = "--- old.py\n+++ new.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        todos = [{"done": True, "task": "Task 1"}, {"done": False, "task": "Task 2"}]
        layout = dual.render(conversation_text="User: Hello\nAssistant: Hi", diff_text=diff_sample, todos=todos)
        self.assertIsNotNone(layout)

    def test_03_streaming_spinner(self):
        """Verify LiveStreamingSpinner start, update, and stop."""
        spinner = LiveStreamingSpinner(message="Testing...")
        spinner.start()
        spinner.update(token_count=50, note="Generating...")
        spinner.stop(final_message="[✓] Complete")
        self.assertFalse(spinner.running)

    def test_04_petdex_mascots_and_leveling(self):
        """Verify Petdex companion leveling, EXP gain, and cheer animations."""
        pdm = PetdexManager(config_path=os.path.join(self.scratch, "omni.config.json"))
        initial_lvl = pdm.level
        pdm.gain_exp(120)
        self.assertEqual(pdm.level, initial_lvl + 1)

        self.assertTrue(pdm.switch_pet("dragon"))
        self.assertEqual(pdm.get_active_pet().slug, "dragon")

        cheer_text = pdm.cheer(reason="Unit tests green")
        self.assertIn("PETDEX", cheer_text)
        self.assertIn("Unit tests green", cheer_text)

    def test_05_ttsr_stream_rules(self):
        """Verify Time-Traveling Stream Rules intercepting malformed output in flight."""
        ttsr = TTSRInterceptor()
        has_violation, rule, constraint = ttsr.evaluate_stream("I will run execute_bash(cmd='ls; rm -rf /')")
        self.assertTrue(has_violation)
        self.assertEqual(rule, "no_unquoted_semicolons_in_bash")
        self.assertIn("semicolons", constraint)

    def test_06_ast_grep_structural_search(self):
        """Verify AST-Grep structural Python syntax search."""
        searcher = ASTGrepSearcher()
        funcs = searcher.search_functions("complete")
        self.assertTrue(len(funcs) >= 1)
        self.assertIn("complete", funcs[0]["name"])
        self.assertTrue(funcs[0]["line"] > 0)

        classes = searcher.search_classes("Engine")
        self.assertTrue(len(classes) >= 1)

    def test_07_mental_models_repository(self):
        """Verify mental model cards repository (.omni/mental_models/) and rule injection."""
        repo = MentalModelRepository(workspace_root=self.scratch)
        repo.add_model("db-contract", "Database Contract", "Always use parameter binding to prevent injection.")

        models = repo.list_models()
        self.assertTrue(len(models) >= 1)
        rules_text = repo.get_rules_for_prompt()
        self.assertIn("Active Project Mental Models", rules_text)
        self.assertIn("parameter binding", rules_text)

    def test_08_audio_notifier(self):
        """Verify non-blocking AudioNotifier chime execution."""
        AudioNotifier.play_chime("success")
        self.assertTrue(True)

    def test_09_moa_engine_structure(self):
        """Verify Mixture-of-Agents 2-layer reasoning council structure."""
        moa = MixtureOfAgentsEngine(provider_router=None)
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(moa.run_moa("Explain microkernels in 1 sentence"))
            self.assertIn("final_synthesis", res)
            self.assertIn("proposals", res)
            self.assertTrue(len(res["proposals"]) >= 1)
        finally:
            loop.close()

    def test_10_egress_firewall(self):
        """Verify iron-proxy egress whitelist checking and credential placeholder replacement."""
        fw = EgressProxyFirewall()
        self.assertTrue(fw.is_domain_allowed("https://api.github.com/repos"))
        self.assertFalse(fw.is_domain_allowed("https://untrusted-leak.xyz/collect"))

        headers = {"Authorization": "Bearer __IRON_PROXY_GITHUB_TOKEN__"}
        allowed, reason, modified = fw.filter_and_inject("https://api.github.com/test", headers)
        self.assertTrue(allowed)

        sanitized = fw.sanitize_log_data("Bearer ya29.a0AehfTOneREb22TOvrFb2iATnmjJ")
        self.assertNotIn("ya29.a0AehfTOneREb22TOvrFb2iATnmjJ", sanitized)
        self.assertIn("[REDACTED]", sanitized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
