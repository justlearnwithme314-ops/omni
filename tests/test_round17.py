"""Context trimming, model failover and the consensus tool. Offline."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import resilience  # noqa: E402


class Round17(CapCase):
    def profile(self, caps, **extra):
        (self.profiles / "r17.json").write_text(json.dumps({"name": "r17", "extends": "lean", "capabilities": caps, **extra}))
        return self.session(profile="r17")

    @staticmethod
    def second_model(s, provider="faux2", mid="f2"):
        s.registry.register_provider(provider, {"api": "faux", "baseUrl": "http://x.test", "apiKey": "k", "models": [{"id": mid}]})


def convo(turns, size=5000):
    msgs = [{"role": "user", "content": "go"}]
    for i in range(turns):
        msgs.append({"role": "assistant", "content": [{"type": "toolCall", "id": f"c{i}", "name": "bash", "arguments": {}}]})
        msgs.append({"role": "toolResult", "toolCallId": f"c{i}", "content": [{"type": "text", "text": f"<{i}>" + "x" * size + f"</{i}>"},
                                                                             {"type": "image", "data": "abc"}]})
    return msgs


class TestContextTrim(Round17):
    def test_only_old_long_results_are_shortened(self):
        msgs = convo(14)
        out = resilience.trim(msgs, keep_turns=8, max_chars=1500, step=4)
        self.assertIsNot(out, msgs)
        texts = [m["content"][0]["text"] for m in out if m["role"] == "toolResult"]
        self.assertEqual([len(t) < 2000 for t in texts], [True] * 4 + [False] * 10)      # 14 - 8 = 6 -> a step of 4
        self.assertTrue(texts[0].startswith("<0>xxx") and texts[0].endswith("</0>"))
        self.assertIn("characters of this older result were trimmed", texts[0])
        self.assertEqual(out[2]["content"][1], {"type": "image", "data": "abc"})         # images are left alone
        self.assertEqual(len(msgs[2]["content"][0]["text"]), 5000 + 7)                  # the input is not modified

    def test_boundary_moves_in_steps_and_short_results_stay(self):
        cut = lambda n: sum(1 for m in resilience.trim(convo(n), 8, 1500, 4) if m["role"] == "toolResult" and len(m["content"][0]["text"]) < 2000)  # noqa: E731
        self.assertEqual([cut(n) for n in (8, 11, 12, 15, 16)], [0, 0, 4, 4, 8])
        small = convo(20, size=100)
        self.assertIs(resilience.trim(small, 8, 1500, 4), small)

    def test_requests_are_trimmed_but_the_session_file_is_not(self):
        s = self.profile(["context-trim"], context_trim={"keep_turns": 2, "step": 1, "max_chars": 300})
        (Path(s.cwd) / "big.txt").write_text("line of text\n" * 400, encoding="utf-8")
        Faux.queue = [{"tool": "read", "args": {"path": "big.txt"}}] * 4 + [{"text": "done"}]
        s.prompt("read it four times")
        sent = [m for m in Faux.requests[-1]["messages"] if m.get("role") == "toolResult"]
        self.assertEqual(len(sent), 4)
        self.assertIn("were trimmed", sent[0]["content"][0]["text"])
        self.assertNotIn("were trimmed", sent[-1]["content"][0]["text"])
        stored = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
        self.assertTrue(all("were trimmed" not in m["content"][0]["text"] for m in stored))


class TestFailover(Round17):
    def test_falls_back_when_the_model_fails_for_good(self):
        s = self.profile([], fallbackModels=["nope/none", "faux2/f2"])
        self.second_model(s)
        s.settings.set_global("retry", {"enabled": False}) if hasattr(s.settings, "set_global") else None
        Faux.queue = [{"error": "model overloaded", "status": 400}, {"text": "answered by the backup"}]
        s.prompt("hello")
        last = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "assistant"][-1]
        self.assertEqual(last["content"][0]["text"], "answered by the backup")
        self.assertEqual((last.get("provider"), last.get("model")), ("faux2", "f2"))
        self.assertEqual(s.model["id"], "faux-1")                                        # the chosen model is unchanged

    def test_no_fallbacks_means_the_error_is_reported(self):
        s = self.profile([])
        Faux.queue = [{"error": "bad request", "status": 400}]
        s.prompt("hello")
        last = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "assistant"][-1]
        self.assertEqual(last["stopReason"], "error")
        s2 = self.profile([], fallbackModels=["faux2/f2"])
        self.second_model(s2)
        Faux.queue = [{"error": "bad request", "status": 400}, {"error": "also down", "status": 400}]
        s2.prompt("hello")                                                               # every fallback failed too
        last = [e["message"] for e in s2.sessions.entries if e["type"] == "message" and e["message"]["role"] == "assistant"][-1]
        self.assertEqual((last["stopReason"], last["errorMessage"]), ("error", "also down"))


class TestConsensus(Round17):
    def test_asks_each_model_and_returns_answers_side_by_side(self):
        s = self.profile(["consensus"], consensus={"models": ["faux/faux-1", "faux2/f2", "ghost/none", "faux/faux-1"]})
        self.second_model(s)
        Faux.queue = [{"text": "Answer A: use a mutex."}, {"text": "Answer A: use a mutex."}]
        r = self.call(s, "consensus", {"question": "How should two threads share this counter?"})
        text = self.text(r)
        self.assertIn("## faux/faux-1", text)
        self.assertIn("## faux2/f2", text)
        self.assertEqual(text.count("Answer A: use a mutex."), 2)
        self.assertIn("2 listed model(s) were skipped", text)
        self.assertEqual((r["details"]["asked"], r["details"]["answered"]), (2, 2))
        self.assertEqual(Faux.requests[-1]["messages"][0]["content"], "How should two threads share this counter?")
        self.assertEqual(Faux.requests[-1]["tools"], [])

    def test_errors(self):
        s = self.profile(["consensus"])
        self.assertIn("question is required", self.text(self.call(s, "consensus", {})))
        self.assertIn("No models to ask", self.text(self.call(s, "consensus", {"question": "q"})))
        self.assertIn("None of these models", self.text(self.call(s, "consensus", {"question": "q", "models": ["ghost/none"]})))
        Faux.queue = [{"error": "down", "status": 400}]
        r = self.call(s, "consensus", {"question": "q", "models": ["faux/faux-1"]})
        self.assertTrue(r.get("isError"))
        self.assertIn("No model answered", self.text(r))


if __name__ == "__main__":
    unittest.main()
