"""Agent files: `omni agent export | import | inspect` (src/pi/agentfile.py). Offline, temp directories only."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.pi import agentfile, profiles  # noqa: E402
from src.pi.caps import OVERLAY_FILE  # noqa: E402
from src.pi.caps import archive as arc  # noqa: E402
from src.pi.caps import board, memory as mem, scheduler as sched  # noqa: E402

SECRET = "SUPERSECRET-do-not-leak"
PROFILE = "af-test-mine"
JOB_FIELDS = ("id", "name", "prompt", "schedule", "cwd", "profile", "model", "enabled", "created", "deliver", "heartbeat")


def tree(root: Path) -> dict:
    """relative path -> bytes for every file under root."""
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()} if root.is_dir() else {}


def run_cli(argv, agent):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = agentfile.cli(argv, agent)
    return code, out.getvalue()


class AgentFileCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.src, self.dst = self.tmp / "src-agent", self.tmp / "dst-agent"
        self.src_profiles, self.dst_profiles = self.tmp / "src-profiles", self.tmp / "dst-profiles"
        self.project = self.tmp / "project"
        for d in (self.src, self.dst, self.src_profiles, self.dst_profiles, self.project):
            d.mkdir()
        self._env = os.environ.get("OMNI_PROFILES_DIR")
        self.addCleanup(self._restore_env)
        self.use_profiles(self.src_profiles)
        self.out = self.tmp / "me.omni-agent"

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("OMNI_PROFILES_DIR", None)
        else:
            os.environ["OMNI_PROFILES_DIR"] = self._env

    def use_profiles(self, d: Path) -> None:
        os.environ["OMNI_PROFILES_DIR"] = str(d)

    def sess(self, agent: Path):
        """What the storage modules read from a session, and nothing else."""
        return SimpleNamespace(agent_dir=str(agent), cwd=str(self.project), profile={})

    # ---------------------------------------------------------------------------------- fixtures
    def populate(self, agent: Path) -> None:
        s = self.sess(agent)
        tool = mem._tools(s)["memory"]
        tool.execute("1", {"action": "add", "target": "memory", "content": "The build runs with `python -m unittest`."})
        tool.execute("2", {"action": "add", "target": "memory", "content": "Two lines\nin one entry."})
        tool.execute("3", {"action": "add", "target": "user", "content": "Prefers short answers."})
        mem.write_entries(mem.memory_dir(s), "", ["I am the careful one."], "PERSONA.md")

        arc.insert(s, "Decision: we ship on Fridays because the cache is cold.", ["decision", "release"], source=str(self.project))
        arc.insert(s, "The staging database lives in Frankfurt.", ["infra"], source=str(self.project))

        b = board._tools(s)["board"]
        b.execute("1", {"action": "add", "title": "Write the exporter", "body": "one file"})
        b.execute("2", {"action": "add", "title": "Ship it"})
        b.execute("3", {"action": "comment", "id": "1", "text": "half done"})
        b.execute("4", {"action": "move", "id": "2", "status": "blocked", "reason": "waiting for review"})

        sched.create_job(agent, "nightly", "Summarise yesterday's commits.", "every 2h", str(self.project), profile="core")
        profiles.save_profile(PROFILE, "core", ["memory", "board"], description="Mine.")

        skill = agent / "skills" / "deploy-staging"
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: deploy-staging\ndescription: \"Deploy to staging\"\n---\n\nRun the script.\n", encoding="utf-8")
        (skill / "scripts" / "run.sh").write_text("echo deploy\n", encoding="utf-8")
        (skill / ".env").write_text(SECRET, encoding="utf-8")
        (skill / "api_token.txt").write_text(SECRET, encoding="utf-8")
        big = agent / "skills" / "huge-skill"
        big.mkdir()
        (big / "SKILL.md").write_text("---\nname: huge-skill\ndescription: \"Big\"\n---\n\nx\n", encoding="utf-8")
        (big / "blob.bin").write_bytes(b"\0" * (agentfile.MAX_SKILL_BYTES + 1))

        (agent / OVERLAY_FILE).write_text(json.dumps({"memory": True, "mcp": False}), encoding="utf-8")
        (agent / "keybindings.json").write_text(json.dumps({"app.model.select": "ctrl+k"}), encoding="utf-8")

        for name in ("auth.json", "secrets.txt", "nodes.json", "gateway.json", "mcp.json", "my.key", "refresh_token"):
            (agent / name).write_text(SECRET, encoding="utf-8")
        (agent / "sessions").mkdir()
        (agent / "sessions" / "one.jsonl").write_text(SECRET, encoding="utf-8")
        (agent / "cron" / "output" / "x").mkdir(parents=True)
        (agent / "cron" / "output" / "x" / "run.md").write_text(SECRET, encoding="utf-8")
        (agent / "memories" / "TOKENS.md").write_text(SECRET, encoding="utf-8")

    def export(self, **kw):
        self.use_profiles(self.src_profiles)
        return agentfile.export_agent(self.src, self.out, **kw)

    def imp(self, path=None, **kw):
        self.use_profiles(self.dst_profiles)
        return agentfile.import_agent(self.dst, path or self.out, **kw)

    def passages(self, agent: Path):
        return [{k: r[k] for k in ("created", "tags", "source", "content")} for r in agentfile._archive_rows(self.sess(agent))]

    def cards(self, agent: Path):
        return [{k: v for k, v in c.items() if k != "id"} for c in agentfile._board_rows(self.sess(agent))]

    def jobs(self, agent: Path):
        return [{k: j.get(k) for k in JOB_FIELDS} for j in sched.load_jobs(agent)]

    def craft(self, members: dict, manifest=None, name="crafted.omni-agent") -> Path:
        p = self.tmp / name
        with zipfile.ZipFile(p, "w") as z:
            if manifest is not False:
                z.writestr("manifest.json", json.dumps(manifest or {"omni_agent_file": True, "format": 1, "included": list(agentfile.SECTIONS)}))
            for k, v in members.items():
                z.writestr(zipfile.ZipInfo(k), v)
        return p

    # ---------------------------------------------------------------------------------- round trip
    def test_roundtrip_into_empty_agent(self):
        self.populate(self.src)
        summary = self.export()
        self.assertEqual(summary["path"], str(self.out))
        self.assertTrue(zipfile.is_zipfile(self.out))
        self.assertEqual(summary["sections"]["memory"]["count"], 3)
        self.assertEqual(summary["sections"]["memory"]["entries"], 4)
        self.assertEqual(summary["sections"]["archive"]["count"], 2)
        self.assertEqual(summary["sections"]["profiles"]["names"], [PROFILE])
        self.assertEqual(summary["sections"]["skills"]["names"], ["deploy-staging"])
        self.assertEqual(summary["sections"]["board"], {"count": 2, "comments": 1, "project": None})
        self.assertEqual(summary["sections"]["cron"]["count"], 1)
        self.assertIn({"item": "skills/huge-skill", "reason": "larger than 2 MB"}, summary["skipped"])

        info = agentfile.inspect_agent(self.out)
        m = info["manifest"]
        self.assertEqual((m["format"], m["omni_agent_file"], m["included"]), (1, True, list(agentfile.SECTIONS)))
        self.assertIn("created", m)
        self.assertTrue(any("auth.json" in x for x in m["excluded"]))
        self.assertTrue(any("session transcripts" in x for x in m["excluded"]))
        self.assertIn({"item": "skills/huge-skill", "reason": "larger than 2 MB"}, m["skipped"])
        self.assertEqual(set(info["sections"]), set(agentfile.SECTIONS))
        self.assertEqual(info["sections"]["profiles"], [PROFILE])
        self.assertEqual(info["sections"]["archive"], ["2 passage(s)"])
        self.assertEqual(len(info["sections"]["board"]), 2)
        self.assertEqual(sorted(info["sections"]["capabilities"]), ["mcp", "memory"])
        self.assertEqual(tree(self.dst), {})  # inspecting unpacks nothing

        result = self.imp()
        self.assertEqual(result["mode"], "merge")
        self.assertEqual(result["skipped"], [])
        self.assertEqual({k: v["added"] for k, v in result["sections"].items()},
                         {"memory": 4, "archive": 2, "profiles": 1, "skills": 1, "capabilities": 2, "cron": 1, "keybindings": 1, "board": 2})

        for fn in ("MEMORY.md", "USER.md", "PERSONA.md"):
            self.assertEqual((self.dst / "memories" / fn).read_bytes(), (self.src / "memories" / fn).read_bytes(), fn)
        self.assertEqual(mem.read_entries(self.dst / "memories", "memory"),
                         ["The build runs with `python -m unittest`.", "Two lines\nin one entry."])
        self.assertFalse((self.dst / "memories" / "TOKENS.md").exists())

        self.assertEqual(self.passages(self.dst), self.passages(self.src))
        self.assertEqual(arc.count(self.sess(self.dst)), 2)
        hits = arc.search(self.sess(self.dst), "Frankfurt")  # the imported archive is searchable, not just present
        self.assertEqual([h["tags"] for h in hits], ["infra"])

        self.assertEqual(tree(self.dst_profiles), tree(self.src_profiles))
        self.assertEqual(profiles.load_profile(PROFILE)["capabilities"], ["memory", "board"])

        want = {k: v for k, v in tree(self.src / "skills").items() if k.startswith("deploy-staging/") and "token" not in k and ".env" not in k}
        self.assertEqual(sorted(want), ["deploy-staging/SKILL.md", "deploy-staging/scripts/run.sh"])
        self.assertEqual(tree(self.dst / "skills"), want)

        self.assertEqual(json.loads((self.dst / OVERLAY_FILE).read_text(encoding="utf-8")), {"memory": True, "mcp": False})
        self.assertEqual(json.loads((self.dst / "keybindings.json").read_text(encoding="utf-8")), {"app.model.select": "ctrl+k"})

        self.assertEqual(self.jobs(self.dst), self.jobs(self.src))
        self.assertTrue(sched.load_jobs(self.dst)[0]["next_run"])
        self.assertFalse((self.dst / "cron" / "output").exists())

        self.assertEqual(self.cards(self.dst), self.cards(self.src))
        shown = board.cards(self.sess(self.dst))
        self.assertEqual([(c["title"], c["status"], c["reason"]) for c in shown],
                         [("Ship it", "blocked", "waiting for review"), ("Write the exporter", "todo", "")])

        for name in ("auth.json", "secrets.txt", "nodes.json", "gateway.json", "mcp.json", "sessions", "my.key", "refresh_token"):
            self.assertFalse((self.dst / name).exists(), name)

    def test_secrets_never_in_the_zip(self):
        self.populate(self.src)
        self.export()
        with zipfile.ZipFile(self.out) as z:
            names = z.namelist()
            blob = b"".join(z.read(n) for n in names)
        self.assertNotIn(SECRET.encode(), blob)
        for n in names:
            base = n.rsplit("/", 1)[-1].lower()
            self.assertNotIn(base, ("auth.json", "secrets.txt", "nodes.json", "gateway.json", "mcp.json", ".env"), n)
            self.assertNotIn("token", base, n)
            self.assertFalse(base.endswith(".key"), n)
            self.assertFalse(n.startswith("sessions"), n)
            self.assertFalse(n.endswith((".sqlite", ".jsonl")) and n != "archive.jsonl", n)
        skipped = {x["item"] for x in agentfile.inspect_agent(self.out)["manifest"]["skipped"]}
        self.assertLessEqual({"memory/TOKENS.md", "skills/deploy-staging/.env", "skills/deploy-staging/api_token.txt"}, skipped)

    def test_export_sections_extension_and_board_scope(self):
        self.populate(self.src)
        other = SimpleNamespace(agent_dir=str(self.src), cwd=str(self.tmp / "elsewhere"), profile={})
        board._tools(other)["board"].execute("1", {"action": "add", "title": "Other project's card"})
        s = agentfile.export_agent(self.src, self.tmp / "partial", include=("board", "memory"), cwd=str(self.project))
        self.assertTrue(s["path"].endswith("partial.omni-agent"))
        info = agentfile.inspect_agent(s["path"])
        self.assertEqual(info["manifest"]["included"], ["memory", "board"])
        self.assertEqual(len(info["sections"]["board"]), 2)  # only this project's cards
        with zipfile.ZipFile(s["path"]) as z:
            self.assertFalse([n for n in z.namelist() if n.startswith(("skills/", "profiles/")) or n in ("archive.jsonl", "cron.json")])
        self.assertEqual(len(agentfile.inspect_agent(self.export()["path"])["sections"]["board"]), 3)
        with self.assertRaises(agentfile.AgentFileError):
            agentfile.export_agent(self.src, self.out, include=("memory", "passwords"))
        r = self.imp(s["path"], sections=("archive", "memory"))
        self.assertEqual(list(r["sections"]), ["memory"])
        self.assertIn({"item": "archive", "reason": "this section is not in the file"}, r["skipped"])

    def test_profile_moves_the_memory_folder(self):
        elsewhere = self.tmp / "custom-memory"
        prof = {"name": "custom", "memory": {"dir": str(elsewhere), "blocks": {"project": 500}}}
        mem.write_entries(elsewhere, "", ["Lives in a monorepo."], "PROJECT.md")
        s = self.export(profile=prof)
        self.assertEqual(s["sections"]["memory"]["files"], [{"file": "PROJECT.md", "block": "project", "entries": 1}])
        self.imp()
        self.assertEqual(mem.read_entries(self.dst / "memories", "", "PROJECT.md"), ["Lives in a monorepo."])

    # ---------------------------------------------------------------------------------- merge / replace / dry run
    def populate_target(self) -> None:
        """A second agent that overlaps with the source and has things of its own."""
        s = self.sess(self.dst)
        mem.write_entries(self.dst / "memories", "memory", ["The build runs with `python -m unittest`.", "Only here."])
        arc.insert(s, "The staging database lives in Frankfurt.", ["infra"])
        arc.insert(s, "A passage only the target has.", [])
        self.use_profiles(self.dst_profiles)
        profiles.save_profile(PROFILE, "core", ["todo"], description="The target's own.")
        profiles.save_profile("af-test-other", "core", [])
        skill = self.dst / "skills" / "deploy-staging"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("target version", encoding="utf-8")
        (skill / "old.txt").write_text("stale", encoding="utf-8")
        (self.dst / OVERLAY_FILE).write_text(json.dumps({"memory": False, "browser": True}), encoding="utf-8")
        (self.dst / "keybindings.json").write_text(json.dumps({"app.exit": "ctrl+q"}), encoding="utf-8")
        board._tools(s)["board"].execute("1", {"action": "add", "title": "Target card"})
        sched.create_job(self.dst, "weekly", "Clean up.", "every 1w", str(self.project))

    def test_merge_keeps_existing_and_dedupes(self):
        self.populate(self.src)
        self.export()
        self.populate_target()
        r = self.imp()
        n = r["sections"]
        self.assertEqual(mem.read_entries(self.dst / "memories", "memory"),
                         ["The build runs with `python -m unittest`.", "Only here.", "Two lines\nin one entry."])
        self.assertEqual((n["memory"]["added"], n["memory"]["unchanged"]), (3, 1))
        self.assertEqual((n["archive"]["added"], n["archive"]["unchanged"], n["archive"]["removed"]), (1, 1, 0))
        self.assertEqual(sorted(p["content"] for p in self.passages(self.dst)),
                         sorted(["The staging database lives in Frankfurt.", "A passage only the target has.",
                                 "Decision: we ship on Fridays because the cache is cold."]))
        self.assertEqual(json.loads((self.dst_profiles / f"{PROFILE}.json").read_text(encoding="utf-8"))["capabilities"], ["todo"])
        self.assertEqual((self.dst / "skills" / "deploy-staging" / "SKILL.md").read_text(encoding="utf-8"), "target version")
        reasons = {x["item"]: x["reason"] for x in r["skipped"]}
        self.assertIn("exists", reasons[f"profiles/{PROFILE}.json"])
        self.assertIn("exists", reasons["skills/deploy-staging"])
        self.assertIn("kept", reasons["capabilities.json: memory"])
        self.assertEqual(json.loads((self.dst / OVERLAY_FILE).read_text(encoding="utf-8")), {"memory": False, "browser": True, "mcp": False})
        self.assertEqual(json.loads((self.dst / "keybindings.json").read_text(encoding="utf-8")), {"app.exit": "ctrl+q", "app.model.select": "ctrl+k"})
        self.assertEqual(sorted(c["title"] for c in self.cards(self.dst)), ["Ship it", "Target card", "Write the exporter"])
        self.assertEqual(sorted(j["name"] for j in sched.load_jobs(self.dst)), ["nightly", "weekly"])

        before = (tree(self.dst / "memories").get("MEMORY.md"), self.passages(self.dst), self.cards(self.dst), self.jobs(self.dst))
        again = self.imp()  # a second import of the same file adds nothing
        self.assertEqual({k: (v["added"], v["updated"], v["removed"]) for k, v in again["sections"].items()},
                         {k: (0, 0, 0) for k in agentfile.SECTIONS})
        self.assertEqual(before, (tree(self.dst / "memories").get("MEMORY.md"), self.passages(self.dst), self.cards(self.dst), self.jobs(self.dst)))

    def test_merge_brings_missing_comments_and_dedupes_within_the_file(self):
        self.populate(self.src)
        self.export()
        self.imp()
        board._tools(self.sess(self.src))["board"].execute("1", {"action": "comment", "id": "1", "text": "second note"})
        arc.insert(self.sess(self.src), "A new passage.", [])
        self.export()
        r = self.imp()
        self.assertEqual((r["sections"]["board"]["added"], r["sections"]["archive"]["added"]), (0, 1))
        first = [c for c in self.cards(self.dst) if c["title"] == "Write the exporter"][0]
        self.assertEqual([x["text"] for x in first["comments"]], ["half done", "second note"])
        twice = self.craft({"memory/MEMORY.md": "same\n§\nsame\n§\nother", "archive.jsonl": '{"content": "dup"}\n{"content": "dup"}\nnot json\n'})
        empty = self.tmp / "third-agent"
        r = agentfile.import_agent(empty, twice, sections=("memory", "archive"))
        self.assertEqual(mem.read_entries(empty / "memories", "memory"), ["same", "other"])
        self.assertEqual(arc.count(SimpleNamespace(agent_dir=str(empty))), 1)
        self.assertIn({"item": "archive.jsonl line 3", "reason": "not a passage"}, r["skipped"])

    def test_replace_overwrites_only_the_included_sections(self):
        self.populate(self.src)
        self.export()
        self.populate_target()
        untouched = (self.passages(self.dst), self.cards(self.dst), tree(self.dst / "skills"))
        r = self.imp(mode="replace", sections=("memory",))
        self.assertEqual(list(r["sections"]), ["memory"])
        self.assertEqual(r["sections"]["memory"]["removed"], 1)
        self.assertEqual((self.dst / "memories" / "MEMORY.md").read_bytes(), (self.src / "memories" / "MEMORY.md").read_bytes())
        self.assertEqual(untouched, (self.passages(self.dst), self.cards(self.dst), tree(self.dst / "skills")))

        r = self.imp(mode="replace")
        n = r["sections"]
        self.assertEqual(self.passages(self.dst), self.passages(self.src))
        self.assertEqual((n["archive"]["added"], n["archive"]["removed"], n["archive"]["unchanged"]), (1, 1, 1))
        self.assertEqual(json.loads((self.dst_profiles / f"{PROFILE}.json").read_text(encoding="utf-8"))["capabilities"], ["memory", "board"])
        self.assertTrue((self.dst_profiles / "af-test-other.json").exists())  # not in the file, so not touched
        self.assertEqual(n["profiles"]["updated"], 1)
        self.assertEqual(sorted(tree(self.dst / "skills")), ["deploy-staging/SKILL.md", "deploy-staging/scripts/run.sh"])
        self.assertEqual(n["skills"]["updated"], 1)
        self.assertEqual(json.loads((self.dst / OVERLAY_FILE).read_text(encoding="utf-8")), {"memory": True, "mcp": False})
        self.assertEqual(json.loads((self.dst / "keybindings.json").read_text(encoding="utf-8")), {"app.model.select": "ctrl+k"})
        self.assertEqual(self.jobs(self.dst), self.jobs(self.src))
        self.assertEqual(agentfile._board_rows(self.sess(self.dst)), agentfile._board_rows(self.sess(self.src)))  # ids too
        self.assertEqual(n["board"]["removed"], 1)

    def test_dry_run_changes_nothing(self):
        self.populate(self.src)
        self.export()
        for mode in ("merge", "replace"):
            r = self.imp(mode=mode, dry_run=True)
            self.assertTrue(r["dry_run"])
            self.assertEqual(r["sections"]["archive"]["added"], 2)
            self.assertEqual(r["sections"]["skills"]["added"], 1)
            self.assertEqual((tree(self.dst), tree(self.dst_profiles)), ({}, {}))
        self.populate_target()
        before = (tree(self.dst), tree(self.dst_profiles))
        r = self.imp(mode="replace", dry_run=True)
        self.assertEqual((r["sections"]["memory"]["removed"], r["sections"]["archive"]["removed"], r["sections"]["board"]["removed"]), (1, 1, 1))
        self.assertEqual((r["sections"]["profiles"]["updated"], r["sections"]["skills"]["updated"]), (1, 1))
        self.assertEqual(before, (tree(self.dst), tree(self.dst_profiles)))
        r = self.imp(dry_run=True)
        self.assertEqual(r["sections"]["memory"]["added"], 3)
        self.assertEqual(before, (tree(self.dst), tree(self.dst_profiles)))

    def test_job_without_its_folder_arrives_paused(self):
        sched.create_job(self.src, "gone", "Check the deploy.", "every 30m", str(self.tmp / "no-such-folder"))
        sched.create_job(self.src, "past", "Once.", "2020-01-01T09:00", str(self.project))
        self.export(include=("cron",))
        r = self.imp()
        jobs = {j["name"]: j for j in sched.load_jobs(self.dst)}
        self.assertEqual((jobs["gone"]["enabled"], jobs["gone"]["next_run"]), (False, None))
        self.assertEqual(jobs["past"]["enabled"], False)
        self.assertTrue(any("paused" in note for note in r["notes"]))

    # ---------------------------------------------------------------------------------- refused files
    def test_zip_slip_and_absolute_paths_are_rejected(self):
        for i, bad in enumerate(("memory/../../evil.md", "../evil.md", "/evil.md", "C:/evil.md", "skills/x/../../../evil.md",
                                 "memory\\..\\evil.md", "skills/x/NUL", "skills//SKILL.md")):
            p = self.craft({"memory/MEMORY.md": "fine", bad: "boom"}, name=f"bad{i}.omni-agent")
            for call in (lambda: self.imp(p), lambda: self.imp(p, dry_run=True), lambda: agentfile.inspect_agent(p)):
                with self.assertRaises(agentfile.AgentFileError, msg=bad):
                    call()
            self.assertEqual(tree(self.dst), {}, bad)  # the good member is not imported either
        self.assertEqual([p.name for p in self.tmp.rglob("evil.md")], [])
        with self.assertRaises(agentfile.AgentFileError):
            agentfile._inside(self.dst, "../outside.md")

    def test_unknown_and_secret_members_are_skipped(self):
        p = self.craft({"skills/ok/SKILL.md": "---\nname: ok\ndescription: d\n---\n", "skills/ok/.env": SECRET, "skills/ok/auth.json": SECRET,
                        "skills/no-skill/readme.txt": "x", "auth.json": SECRET, "memory/sub/DEEP.md": "x", "memory/token.md": "x",
                        "profiles/core.json": "{}", "profiles/Bad Name.json": "{}", "profiles/af-test-list.json": "[]",
                        "cron.json": json.dumps({"jobs": [{"id": "a1", "prompt": "p", "schedule": "whenever"}, {"name": "no id"}]}),
                        "board.json": json.dumps({"cards": [{"body": "no title"}]}), "capabilities.json": "[1]"})
        r = self.imp(p)
        self.assertEqual(sorted(tree(self.dst)), ["skills/ok/SKILL.md"])
        self.assertEqual(tree(self.dst_profiles), {})
        reasons = {x["item"]: x["reason"] for x in r["skipped"]}
        for item in ("skills/ok/.env", "skills/ok/auth.json", "skills/no-skill", "auth.json", "memory/sub/DEEP.md", "memory/token.md",
                     "profiles/core.json", "profiles/Bad Name.json", "profiles/af-test-list.json", "cron job a1", "board card", "capabilities.json"):
            self.assertIn(item, reasons)
        self.assertIn("built-in", reasons["profiles/core.json"])

    def test_wrong_format_version_and_non_agent_files_are_rejected(self):
        cases = {"v2": {"omni_agent_file": True, "format": 2}, "v0": {"omni_agent_file": True, "format": "1"},
                 "nomark": {"format": 1}, "bool": {"omni_agent_file": True, "format": True}}
        for name, manifest in cases.items():
            p = self.craft({"memory/MEMORY.md": "x"}, manifest=manifest, name=f"{name}.omni-agent")
            with self.assertRaises(agentfile.AgentFileError, msg=name):
                self.imp(p)
        with self.assertRaisesRegex(agentfile.AgentFileError, "format version 2"):
            self.imp(self.tmp / "v2.omni-agent")
        with self.assertRaisesRegex(agentfile.AgentFileError, "manifest.json is missing"):
            self.imp(self.craft({"memory/MEMORY.md": "x"}, manifest=False, name="none.omni-agent"))
        text = self.tmp / "text.omni-agent"
        text.write_text("not a zip", encoding="utf-8")
        for p in (text, self.tmp / "missing.omni-agent"):
            with self.assertRaises(agentfile.AgentFileError):
                self.imp(p)
        with self.assertRaises(agentfile.AgentFileError):
            self.imp(self.craft({}, name="ok.omni-agent"), mode="overwrite")
        with self.assertRaises(agentfile.AgentFileError):
            self.imp(self.tmp / "ok.omni-agent", sections=("nope",))
        self.assertEqual(tree(self.dst), {})

    def test_size_caps(self):
        p = self.craft({"memory/MEMORY.md": "x" * 5000})
        with mock.patch.object(agentfile, "MAX_TOTAL_BYTES", 1000):
            with self.assertRaisesRegex(agentfile.AgentFileError, "too large"):
                self.imp(p)
            with self.assertRaisesRegex(agentfile.AgentFileError, "too large"):
                agentfile.inspect_agent(p)
        many = self.craft({f"memory/M{i}.md": "x" for i in range(10)}, name="many.omni-agent")
        with mock.patch.object(agentfile, "MAX_MEMBERS", 5):
            with self.assertRaisesRegex(agentfile.AgentFileError, "too many"):
                self.imp(many)
        self.assertEqual(tree(self.dst), {})
        self.assertEqual((agentfile.MAX_TOTAL_BYTES, agentfile.MAX_MEMBERS, agentfile.MAX_SKILL_BYTES), (200 * 1024 * 1024, 20000, 2 * 1024 * 1024))
        fat = self.craft({"skills/fat/SKILL.md": "x", "skills/fat/blob": b"\0" * 5000}, name="fat.omni-agent")
        with mock.patch.object(agentfile, "MAX_SKILL_BYTES", 1000):
            r = self.imp(fat)
        self.assertEqual(r["sections"]["skills"]["added"], 0)
        self.assertFalse((self.dst / "skills").exists())
        self.assertEqual(self.imp(many)["sections"]["memory"]["added"], 10)  # fine without the lowered caps

    # ---------------------------------------------------------------------------------- command line
    def test_cli(self):
        self.populate(self.src)
        out = self.tmp / "cli.omni-agent"
        code, text = run_cli(["export", str(out), "--profile", "core", "--without", "board,cron"], self.src)
        self.assertEqual(code, 0, text)
        self.assertIn("Wrote", text)
        self.assertIn("huge-skill", text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(agentfile.inspect_agent(out)["manifest"]["included"], ["memory", "archive", "profiles", "skills", "capabilities", "keybindings"])
        code, text = run_cli(["export", str(self.tmp / "only"), "--only=memory,archive", "--profile", "core"], self.src)
        self.assertEqual(code, 0, text)
        self.assertEqual(agentfile.inspect_agent(self.tmp / "only.omni-agent")["manifest"]["included"], ["memory", "archive"])

        code, text = run_cli(["inspect", str(out)], self.src)
        self.assertEqual(code, 0, text)
        self.assertIn("format 1", text)
        self.assertIn("deploy-staging", text)

        self.use_profiles(self.dst_profiles)
        code, text = run_cli(["import", str(out), "--dry-run"], self.dst)
        self.assertEqual(code, 0, text)
        self.assertIn("Dry run", text)
        self.assertEqual((tree(self.dst), tree(self.dst_profiles)), ({}, {}))
        code, text = run_cli(["import", str(out), "--only", "memory,skills"], self.dst)
        self.assertEqual(code, 0, text)
        self.assertEqual(sorted(p.name for p in self.dst.iterdir()), ["memories", "skills"])
        mem.write_entries(self.dst / "memories", "user", ["Changed on the target."])
        code, text = run_cli(["import", "--replace", str(out)], self.dst)
        self.assertEqual(code, 0, text)
        self.assertIn("(replace)", text)
        self.assertEqual(mem.read_entries(self.dst / "memories", "user"), ["Prefers short answers."])
        self.assertEqual(self.passages(self.dst), self.passages(self.src))
        self.assertTrue((self.dst_profiles / f"{PROFILE}.json").exists())

    def test_cli_default_file_name(self):
        mem.write_entries(self.src / "memories", "memory", ["one"])
        old = os.getcwd()
        os.chdir(self.project)
        try:
            code, text = run_cli(["export", "--profile", "core", "--only", "memory"], self.src)
        finally:
            os.chdir(old)
        self.assertEqual(code, 0, text)
        made = list(self.project.glob("agent-*.omni-agent"))
        self.assertEqual(len(made), 1)
        self.assertEqual(agentfile.inspect_agent(made[0])["sections"]["memory"], ["MEMORY.md (3 bytes)"])

    def test_cli_usage_and_failures(self):
        self.craft({}, name="ok.omni-agent")
        ok = str(self.tmp / "ok.omni-agent")
        for argv in ([], ["bogus"], ["import"], ["inspect"], ["inspect", "a", "b"], ["import", ok, "extra"], ["export", "a", "b"],
                     ["export", "--only", "nope"], ["export", "--without", "memory,nope"], ["export", "--frobnicate"],
                     ["export", "--profile"], ["import", ok, "--only", "nope"], ["import", ok, "--profile", "core"],
                     ["export", "--only", "memory", "--without", "memory"], ["inspect", ok, "--dry-run"]):
            code, text = run_cli(argv, self.src)
            self.assertEqual(code, 2, argv)
            self.assertIn("usage: omni agent export", text, argv)
        self.assertEqual(run_cli(["help"], self.src)[0], 0)
        junk = self.tmp / "junk.omni-agent"
        junk.write_text("nope", encoding="utf-8")
        v9 = str(self.craft({}, manifest={"omni_agent_file": True, "format": 9}, name="v9.omni-agent"))
        slip = str(self.craft({"../evil.md": "x"}, name="slip.omni-agent"))
        for argv in (["import", str(self.tmp / "missing.omni-agent")], ["inspect", str(junk)], ["import", str(junk)], ["import", v9],
                     ["inspect", v9], ["import", slip]):
            code, text = run_cli(argv, self.dst)
            self.assertEqual(code, 1, argv)
            self.assertIn("omni agent", text)
        self.assertEqual(tree(self.dst), {})
        self.assertEqual(tree(self.src), {})


if __name__ == "__main__":
    unittest.main()
