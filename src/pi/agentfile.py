"""`omni agent`: the agent as one file you can carry to another machine (Letta's `.af` agent file).

What makes an Omni agent *this* agent is spread over a handful of folders: what it remembers, the skills it wrote,
the profiles you built, the jobs it runs. `omni agent export` packs all of that into a single `.omni-agent` file
(a zip you can open with any archive tool); `omni agent import` unpacks it into the agent folder of another install.

    omni agent export [FILE] [--profile NAME] [--only a,b] [--without a,b]
    omni agent import FILE [--replace] [--only a,b] [--dry-run]
    omni agent inspect FILE

Sections (name them with `--only` / `--without`):

    memory        the memory blocks: `<agent-dir>/memories/*.md` (MEMORY.md, USER.md and the blocks a profile adds)
    archive       archival memory passages with their tags, as `archive.jsonl`
    profiles      your custom profiles (`~/.omni/profiles` or `OMNI_PROFILES_DIR`); shipped profiles stay where they are
    skills        the skills in `<agent-dir>/skills` (a skill over 2 MB is left out and named in the manifest)
    capabilities  your switches: `<agent-dir>/omni-capabilities.json`
    cron          scheduled jobs (`<agent-dir>/cron/jobs.json`), without their past output
    keybindings   `<agent-dir>/keybindings.json`
    board         task board cards and their comments, for every project

Import adds by default (`merge`): what is already there stays, what is missing arrives. Memory entries and archive
passages that already exist are not added twice; a profile or skill with the same name is kept and reported.
`--replace` makes each section in the file look like the file instead; sections that are not in the file are never
touched. `--dry-run` prints what would change and writes nothing.

Never in the file: credentials and pairing data (`auth.json`, `secrets.txt`, `nodes.json`, `gateway.json`,
`mcp.json`, anything named like `*token*`, `*.key` or `.env`), session transcripts, and the output of past cron
runs. The manifest inside the file lists what was left out and why. Log in again on the new machine.

Two things are tied to the machine and travel as they are: the project folder of each board card, and the working
folder of each scheduled job. A job whose folder does not exist where it is imported arrives paused.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

FORMAT_VERSION = 1
MARKER = "omni-agent-file"
EXTENSION = ".omni-agent"
SECTIONS = ("memory", "archive", "profiles", "skills", "capabilities", "cron", "keybindings", "board")

MAX_TOTAL_BYTES = 200 * 1024 * 1024   # uncompressed size of a file that import accepts
MAX_MEMBERS = 20000
MAX_SKILL_BYTES = 2 * 1024 * 1024
MAX_SMALL_FILE = 4 * 1024 * 1024      # one memory block, profile or JSON document

SECRET_NAMES = {"auth.json", "secrets.txt", "nodes.json", "gateway.json", "mcp.json", ".env"}
SECRET_PATTERNS = ("*token*", "*.key", ".env.*", "*.pem", "*secret*", "*credential*")
EXCLUDED = [
    "credentials and pairing data: auth.json, secrets.txt, nodes.json, gateway.json, mcp.json",
    "any file or folder named like *token*, *.key, .env, .env.*, *.pem, *secret*, *credential*",
    "session transcripts (sessions/)",
    "cron run output and queued signals (cron/output, cron/signals)",
    "settings.json, trust.json, models.json and other machine settings",
    "shipped profiles and skills that live outside <agent-dir>/skills",
]
_SKIP_DIRS = {"node_modules", ".git", "__pycache__"}
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
_MEMORY_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.md$")
_PROFILE_FILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\.json$")
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AgentFileError(ValueError):
    """The file is not a usable agent file, or the request does not make sense."""


# ------------------------------------------------------------------------------------------- helpers
def is_secret(name: str) -> bool:
    """True for file or folder names that must never travel in an agent file."""
    n = name.lower()
    return n in SECRET_NAMES or any(fnmatch.fnmatchcase(n, p) for p in SECRET_PATTERNS)


def _bad_member(name: str) -> Optional[str]:
    """Why a zip member name is unsafe (None = fine). Member names are forward-slash relative paths."""
    if not name or "\x00" in name:
        return "empty name"
    if name.startswith(("/", "\\")):
        return "absolute path"
    if "\\" in name:
        return "backslash in path"
    if ":" in name:
        return "drive letter or stream in path"
    parts = name.rstrip("/").split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return "path leaves its folder ('..')" if part == ".." else "empty path component"
        if part != part.strip() or part.endswith("."):
            return "path component ends in a space or dot"
        if part.split(".")[0].lower() in _RESERVED:
            return "reserved device name"
    return None


def _inside(base: Path, rel: str) -> Path:
    """`base/rel`, refusing anything that resolves outside `base`."""
    root = Path(base).resolve()
    target = (root / rel).resolve()
    if target == root or root not in target.parents:
        raise AgentFileError(f"'{rel}' would be written outside {root}")
    return target


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _stand_in(agent_dir: Path, cwd: Optional[str] = None, profile: Optional[Dict[str, Any]] = None) -> Any:
    """The storage modules take a session; they only read these attributes."""
    return SimpleNamespace(agent_dir=str(agent_dir), cwd=cwd or os.getcwd(), profile=profile or {}, trusted=False)


def _sections(wanted: Optional[Iterable[str]], default: Iterable[str] = SECTIONS) -> List[str]:
    if wanted is None:
        return [s for s in SECTIONS if s in set(default)]
    if isinstance(wanted, str):
        wanted = [w for w in wanted.split(",")]
    names = [str(w).strip().lower() for w in wanted if str(w).strip()]
    unknown = [n for n in names if n not in SECTIONS]
    if unknown:
        raise AgentFileError(f"unknown section(s): {', '.join(unknown)}. Sections: {', '.join(SECTIONS)}")
    return [s for s in SECTIONS if s in names]


def _entries(text: str) -> List[str]:
    return [e.strip() for e in text.split("§") if e.strip()]


# ------------------------------------------------------------------------------------------- export
def _export_memory(sess: Any, notes: List[Dict[str, str]]) -> Tuple[List[Tuple[str, bytes]], Dict[str, Any]]:
    from .caps import memory as mem
    d = mem.memory_dir(sess)
    known = {spec["file"]: label for label, spec in mem.blocks(sess).items()}
    members, files, total = [], [], 0
    for p in sorted(d.glob("*.md")) if d.is_dir() else []:
        if not p.is_file() or p.is_symlink():
            continue
        if is_secret(p.name):
            notes.append({"item": f"memory/{p.name}", "reason": "name looks like a secret"})
            continue
        if not _MEMORY_FILE.match(p.name) or p.stat().st_size > MAX_SMALL_FILE:
            notes.append({"item": f"memory/{p.name}", "reason": "not a memory block file (name or size)"})
            continue
        try:
            text = p.read_text(encoding="utf-8")  # universal newlines: the file carries LF on every platform
        except (OSError, UnicodeDecodeError):
            notes.append({"item": f"memory/{p.name}", "reason": "not readable as UTF-8 text"})
            continue
        n = len(_entries(text))
        members.append((f"memory/{p.name}", text.encode("utf-8")))
        files.append({"file": p.name, "block": known.get(p.name, ""), "entries": n})
        total += n
    return members, {"count": len(files), "entries": total, "files": files}


def _archive_rows(sess: Any) -> List[Dict[str, Any]]:
    from .caps import archive as arc
    if not arc.db_path(sess).exists():
        return []
    c = arc.connect(sess)
    try:
        rows = c.execute("SELECT id, created, tags, source, content FROM passages ORDER BY id").fetchall()
    finally:
        c.close()
    return [{"id": r[0], "created": r[1], "tags": (r[2] or "").split(), "source": r[3] or "", "content": r[4] or ""} for r in rows]


def _export_archive(sess: Any, notes: List[Dict[str, str]]) -> Tuple[List[Tuple[str, bytes]], Dict[str, Any]]:
    rows = _archive_rows(sess)
    lines = [json.dumps({k: r[k] for k in ("created", "tags", "source", "content")}, ensure_ascii=False) for r in rows]
    return [("archive.jsonl", ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"))], {"count": len(rows)}


def _export_profiles(sess: Any, notes: List[Dict[str, str]]) -> Tuple[List[Tuple[str, bytes]], Dict[str, Any]]:
    from . import profiles as prof
    d = prof.user_profiles_dir()
    members, names = [], []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        item = f"profiles/{p.name}"
        if not p.is_file() or p.is_symlink():
            continue
        if is_secret(p.name):
            notes.append({"item": item, "reason": "name looks like a secret"})
        elif not _PROFILE_FILE.match(p.name.lower()) or p.stat().st_size > MAX_SMALL_FILE:
            notes.append({"item": item, "reason": "not a valid profile file name, or too large"})
        elif not isinstance(_read_json_file(p), dict):
            notes.append({"item": item, "reason": "not a JSON object"})
        else:
            members.append((f"profiles/{p.name.lower()}", p.read_bytes()))
            names.append(p.stem.lower())
    return members, {"count": len(names), "names": names}


def _export_skills(sess: Any, notes: List[Dict[str, str]]) -> Tuple[List[Tuple[str, bytes]], Dict[str, Any]]:
    root = Path(sess.agent_dir) / "skills"
    members: List[Tuple[str, bytes]] = []
    names: List[str] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()) if root.is_dir() else []:
        item = f"skills/{child.name}"
        if child.is_symlink():
            notes.append({"item": item, "reason": "symbolic link"})
            continue
        if is_secret(child.name):
            notes.append({"item": item, "reason": "name looks like a secret"})
            continue
        if _bad_member(item) or not _SKILL_NAME.match(child.name):
            notes.append({"item": item, "reason": "name cannot be stored portably"})
            continue
        if child.is_file():
            if child.suffix.lower() != ".md" or child.name.lower() == "readme.md":
                continue
            if child.stat().st_size > MAX_SKILL_BYTES:
                notes.append({"item": item, "reason": f"larger than {MAX_SKILL_BYTES // (1024 * 1024)} MB"})
                continue
            members.append((item, child.read_bytes()))
            names.append(child.name)
            continue
        files: List[Tuple[str, Path]] = []
        total, too_big, has_skill = 0, False, False
        for dirpath, dirnames, filenames in os.walk(child):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not is_secret(d))
            for fn in sorted(filenames):
                fp = Path(dirpath) / fn
                rel = "skills/" + fp.relative_to(root).as_posix()
                if fp.is_symlink() or not fp.is_file():
                    continue
                if is_secret(fn):
                    notes.append({"item": rel, "reason": "name looks like a secret"})
                    continue
                if _bad_member(rel):
                    notes.append({"item": rel, "reason": "name cannot be stored portably"})
                    continue
                total += fp.stat().st_size
                if total > MAX_SKILL_BYTES:
                    too_big = True
                    break
                files.append((rel, fp))
                has_skill = has_skill or fn == "SKILL.md"
            if too_big:
                break
        if too_big:
            notes.append({"item": item, "reason": f"larger than {MAX_SKILL_BYTES // (1024 * 1024)} MB"})
        elif not has_skill:
            notes.append({"item": item, "reason": "no SKILL.md"})
        else:
            members += [(rel, fp.read_bytes()) for rel, fp in files]
            names.append(child.name)
    return members, {"count": len(names), "names": names}


def _export_capabilities(sess: Any, notes: List[Dict[str, str]]) -> Tuple[List[Tuple[str, bytes]], Dict[str, Any]]:
    from .caps import OVERLAY_FILE
    p = Path(sess.agent_dir) / OVERLAY_FILE
    if not p.is_file():
        return [], {"count": 0}
    data = _read_json_file(p)
    if not isinstance(data, dict):
        notes.append({"item": "capabilities.json", "reason": f"{OVERLAY_FILE} is not a JSON object"})
        return [], {"count": 0}
    return [("capabilities.json", _json_bytes(data))], {"count": len(data)}


def _export_cron(sess: Any, notes: List[Dict[str, str]]) -> Tuple[List[Tuple[str, bytes]], Dict[str, Any]]:
    from .caps import scheduler as sched
    jobs = [{k: v for k, v in j.items() if k not in ("running_since", "signal_payload")} for j in sched.load_jobs(Path(sess.agent_dir))]
    return [("cron.json", _json_bytes({"jobs": jobs}))], {"count": len(jobs), "names": [str(j.get("name") or j.get("id")) for j in jobs]}


def _export_keybindings(sess: Any, notes: List[Dict[str, str]]) -> Tuple[List[Tuple[str, bytes]], Dict[str, Any]]:
    p = Path(sess.agent_dir) / "keybindings.json"
    if not p.is_file():
        return [], {"count": 0}
    data = _read_json_file(p)
    if not isinstance(data, dict):
        notes.append({"item": "keybindings.json", "reason": "not a JSON object"})
        return [], {"count": 0}
    return [("keybindings.json", p.read_bytes())], {"count": len(data)}


def _board_rows(sess: Any, project: Optional[str] = None) -> List[Dict[str, Any]]:
    from .caps import board
    if not (Path(sess.agent_dir) / "board.sqlite").exists():
        return []
    c = board._db(sess)
    try:
        sql, args = "SELECT id, project, title, body, status, reason, created, updated FROM cards", []
        if project is not None:
            sql, args = sql + " WHERE project = ?", [project]
        cards = [dict(zip(("id", "project", "title", "body", "status", "reason", "created", "updated"), r))
                 for r in c.execute(sql + " ORDER BY id", args).fetchall()]
        notes: Dict[int, List[Dict[str, Any]]] = {}
        for card, at, text in c.execute("SELECT card, at, text FROM comments ORDER BY id").fetchall():
            notes.setdefault(card, []).append({"at": at, "text": text})
    finally:
        c.close()
    for card in cards:
        card["comments"] = notes.get(card["id"], [])
    return cards


def _export_board(sess: Any, notes: List[Dict[str, str]], project: Optional[str] = None) -> Tuple[List[Tuple[str, bytes]], Dict[str, Any]]:
    cards = _board_rows(sess, project)
    info = {"count": len(cards), "comments": sum(len(c["comments"]) for c in cards), "project": project}
    return [("board.json", _json_bytes({"project": project, "cards": cards}))], info


def export_agent(agent_dir: Any, out_path: Any, profile: Any = None,
                 include: Iterable[str] = SECTIONS, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Write the agent to `out_path` (a zip; `.omni-agent` is added when the name has another or no extension).

    `profile` (a name or a loaded profile) only decides where memory is read from: profiles can move the memory
    folder and add blocks. `cwd` limits the board section to the cards of that project; without it every card goes.
    Returns {"path", "bytes", "sections": {name: info}, "skipped": [{"item", "reason"}], "excluded": [...]}.
    """
    agent = Path(agent_dir)
    wanted = _sections(include)
    if not wanted:
        raise AgentFileError("nothing to export: no sections selected")
    prof: Dict[str, Any] = {}
    prof_name = None
    if isinstance(profile, dict):
        prof, prof_name = profile, profile.get("name")
    elif profile:
        from . import profiles as profiles_mod
        prof = profiles_mod.load_profile(str(profile))
        prof_name = prof.get("name")
    sess = _stand_in(agent, cwd, prof)
    notes: List[Dict[str, str]] = []
    collectors = {"memory": _export_memory, "archive": _export_archive, "profiles": _export_profiles,
                  "skills": _export_skills, "capabilities": _export_capabilities, "cron": _export_cron,
                  "keybindings": _export_keybindings}
    members: List[Tuple[str, bytes]] = []
    info: Dict[str, Any] = {}
    for name in wanted:
        got, info[name] = _export_board(sess, notes, cwd) if name == "board" else collectors[name](sess, notes)
        members += got
    now = time.time()
    manifest = {"omni_agent_file": True, "marker": MARKER, "format": FORMAT_VERSION,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "created_ts": now,
                "platform": sys.platform, "profile": prof_name, "included": wanted, "sections": info,
                "excluded": EXCLUDED, "skipped": notes,
                "notes": ["board cards keep their project folder and cron jobs their working folder; both are paths "
                          "from the machine that wrote this file"]}
    out = Path(out_path)
    if out.is_dir():
        out = out / default_name(now)
    if out.suffix.lower() != EXTENSION:
        out = out.with_name(out.name + EXTENSION)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", _json_bytes(manifest))
            for name, data in members:
                if is_secret(name.rsplit("/", 1)[-1]):  # belt and braces: nothing secret-named is ever written
                    continue
                z.writestr(name, data)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"path": str(out), "bytes": out.stat().st_size, "sections": info, "skipped": notes, "excluded": EXCLUDED}


def default_name(now: Optional[float] = None) -> str:
    return "agent-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + EXTENSION


# ------------------------------------------------------------------------------------------- reading a file
class _Reader:
    """An opened, validated agent file. Every read counts against the size cap, whatever the zip header claims."""

    def __init__(self, path: Any):
        self.path = Path(path)
        if not self.path.is_file():
            raise AgentFileError(f"no such file: {self.path}")
        try:
            self.zip = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as e:
            raise AgentFileError(f"{self.path.name} is not an agent file ({e})") from None
        try:
            self._validate()
        except Exception:
            self.zip.close()
            raise

    def _validate(self) -> None:
        infos = self.zip.infolist()
        if len(infos) > MAX_MEMBERS:
            raise AgentFileError(f"too many entries ({len(infos)}; the limit is {MAX_MEMBERS})")
        self.total = sum(i.file_size for i in infos)
        if self.total > MAX_TOTAL_BYTES:
            raise AgentFileError(f"too large unpacked ({self.total} bytes; the limit is {MAX_TOTAL_BYTES})")
        for i in infos:
            why = _bad_member(i.filename)
            if why:
                raise AgentFileError(f"unsafe entry '{i.filename}': {why}. Nothing was imported.")
        self.sizes = {i.filename: i.file_size for i in infos if not i.filename.endswith("/")}
        self.names = sorted(self.sizes)
        self.budget = MAX_TOTAL_BYTES
        if "manifest.json" not in self.sizes:
            raise AgentFileError("not an agent file: manifest.json is missing")
        try:
            manifest = json.loads(self.read("manifest.json", MAX_SMALL_FILE).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise AgentFileError("not an agent file: manifest.json is not valid JSON") from None
        if not isinstance(manifest, dict) or manifest.get("omni_agent_file") is not True:
            raise AgentFileError("not an agent file: the manifest has no Omni marker")
        if manifest.get("format") != FORMAT_VERSION or isinstance(manifest.get("format"), bool):
            raise AgentFileError(f"format version {manifest.get('format')!r} is not supported (this Omni reads version {FORMAT_VERSION})")
        self.manifest = manifest
        inc = manifest.get("included")
        self.included = [s for s in SECTIONS if s in inc] if isinstance(inc, list) else list(SECTIONS)

    def read(self, name: str, limit: int = MAX_TOTAL_BYTES) -> bytes:
        limit = min(limit, self.budget)
        with self.zip.open(name) as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise AgentFileError(f"'{name}' is larger than allowed")
        self.budget -= len(data)
        return data

    def under(self, prefix: str) -> List[str]:
        return [n for n in self.names if n.startswith(prefix)]

    def close(self) -> None:
        self.zip.close()


def inspect_agent(path: Any) -> Dict[str, Any]:
    """The manifest plus what each section holds, read straight from the zip (nothing is unpacked to disk)."""
    r = _Reader(path)
    try:
        listing: Dict[str, List[str]] = {}
        for s in r.included:
            if s == "memory":
                listing[s] = [f"{n[7:]} ({r.sizes[n]} bytes)" for n in r.under("memory/")]
            elif s == "archive":
                n = sum(1 for line in r.read("archive.jsonl").splitlines() if line.strip()) if "archive.jsonl" in r.sizes else 0
                listing[s] = [f"{n} passage(s)"]
            elif s == "profiles":
                listing[s] = [n[9:-5] for n in r.under("profiles/") if n.endswith(".json")]
            elif s == "skills":
                counts: Dict[str, int] = {}
                for n in r.under("skills/"):
                    top = n.split("/")[1]
                    counts[top] = counts.get(top, 0) + 1
                listing[s] = [f"{k} ({v} file{'s' if v != 1 else ''})" for k, v in sorted(counts.items())]
            elif s == "cron":
                jobs = _member_json(r, "cron.json", {}).get("jobs") or []
                listing[s] = [f"{j.get('name') or j.get('id')} ({j.get('schedule')})" for j in jobs if isinstance(j, dict)]
            elif s == "board":
                cards = _member_json(r, "board.json", {}).get("cards") or []
                listing[s] = [f"#{c.get('id')} [{c.get('status')}] {c.get('title')}" for c in cards if isinstance(c, dict)]
            else:  # capabilities, keybindings
                listing[s] = sorted(str(k) for k in _member_json(r, f"{s}.json", {}))
        return {"path": str(r.path), "manifest": r.manifest, "sections": listing, "members": len(r.names), "bytes": r.total}
    finally:
        r.close()


def _member_json(r: _Reader, name: str, default: Any) -> Any:
    if name not in r.sizes:
        return default
    try:
        data = json.loads(r.read(name).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return default
    return data if isinstance(data, type(default)) else default


# ------------------------------------------------------------------------------------------- import
class _Import:
    def __init__(self, agent: Path, reader: _Reader, replace: bool, dry: bool):
        self.agent, self.r, self.replace, self.dry = agent, reader, replace, dry
        self.skipped: List[Dict[str, str]] = []
        self.notes: List[str] = []
        self.sess = _stand_in(agent)

    def skip(self, item: str, reason: str) -> None:
        self.skipped.append({"item": item, "reason": reason})

    @staticmethod
    def counts() -> Dict[str, int]:
        return {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}

    # memory: one file per block, entries separated by a § line
    def memory(self) -> Dict[str, int]:
        from .caps import memory as mem
        n = self.counts()
        d = self.agent / "memories"
        limits = {mem.FILES[k]: mem.LIMITS[k] for k in mem.FILES}
        for name in self.r.under("memory/"):
            fn = name[len("memory/"):]
            if not _MEMORY_FILE.match(fn) or is_secret(fn):
                self.skip(name, "not a memory block file name")
                continue
            _inside(d, fn)
            try:
                text = self.r.read(name, MAX_SMALL_FILE).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                new = list(dict.fromkeys(_entries(text)))
            except UnicodeDecodeError:
                self.skip(name, "not UTF-8 text")
                continue
            old = mem.read_entries(d, "", fn)
            if self.replace:
                final = new
                n["removed"] += len([e for e in old if e not in new])
            else:
                final = old + [e for e in new if e not in old]
            n["added"] += len([e for e in final if e not in old])
            n["unchanged"] += len([e for e in new if e in old])
            if final != old and not self.dry:
                mem.write_entries(d, "", final, fn)
            if fn in limits and mem.used(final) > limits[fn]:
                self.notes.append(f"{fn} holds {mem.used(final)} characters, over its {limits[fn]} limit: the agent has to trim it before it can add more")
        return n

    # archive: passages, the same text is never stored twice
    def archive(self) -> Dict[str, int]:
        from .caps import archive as arc
        n = self.counts()
        if "archive.jsonl" not in self.r.sizes:
            return n
        new: Dict[str, Dict[str, Any]] = {}
        for i, line in enumerate(self.r.read("archive.jsonl").decode("utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            if not isinstance(row, dict) or not isinstance(row.get("content"), str) or not row["content"].strip():
                self.skip(f"archive.jsonl line {i}", "not a passage")
                continue
            if row["content"] in new:
                n["unchanged"] += 1
                continue
            new[row["content"]] = row
        def tags_of(row: Dict[str, Any]) -> List[str]:
            t = row.get("tags")
            return t.split() if isinstance(t, str) else [str(x) for x in t] if isinstance(t, list) else []

        rows = _archive_rows(self.sess)
        existing = {r["content"] for r in rows}
        todo = [row for content, row in new.items() if content not in existing]
        n["added"] = len(todo)
        n["unchanged"] += len(new) - len(todo)
        n["removed"] = len([c for c in existing if c not in new]) if self.replace else 0
        if self.dry:
            return n
        if self.replace:  # rebuild in the file's order, with the file's tags and dates
            def shape(r: Dict[str, Any]) -> Tuple[Any, ...]:
                return (r["content"], tags_of(r), str(r.get("source") or ""), r.get("created"))
            if [shape(r) for r in rows] == [shape(r) for r in new.values()]:
                return n
            if rows:
                c = arc.connect(self.sess)
                try:
                    c.execute("DELETE FROM passages")
                    c.commit()
                finally:
                    c.close()
            todo = list(new.values())
        if not todo:
            return n
        stamps = []
        for row in todo:
            rid = arc.insert(self.sess, row["content"], tags_of(row), source=str(row.get("source") or ""))
            if rid > 0 and isinstance(row.get("created"), (int, float)) and not isinstance(row.get("created"), bool):
                stamps.append((float(row["created"]), rid))
        c = arc.connect(self.sess)
        try:
            c.executemany("UPDATE passages SET created = ? WHERE id = ?", stamps)  # keep the original dates
            c.commit()
        finally:
            c.close()
        return n

    def profiles(self) -> Dict[str, int]:
        from . import profiles as prof
        n = self.counts()
        d = prof.user_profiles_dir()
        for name in self.r.under("profiles/"):
            fn = name[len("profiles/"):]
            stem = fn[:-5]
            if not _PROFILE_FILE.match(fn) or is_secret(fn):
                self.skip(name, "not a profile file name")
                continue
            if stem in prof.ALIASES or stem in prof.BUILTIN or (prof.PROJECT_ROOT / "profiles" / fn).is_file():
                self.skip(name, "has the name of a built-in profile")
                continue
            data = self.r.read(name, MAX_SMALL_FILE)
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
            if not isinstance(parsed, dict):
                self.skip(name, "not a JSON object")
                continue
            target = _inside(d, fn)
            if target.exists():
                if _read_json_file(target) == parsed:
                    n["unchanged"] += 1
                    continue
                if not self.replace:
                    self.skip(name, "a profile with this name exists (kept; use --replace to overwrite)")
                    continue
                n["updated"] += 1
            else:
                n["added"] += 1
            if not self.dry:
                _write(target, data)
        return n

    def skills(self) -> Dict[str, int]:
        n = self.counts()
        root = self.agent / "skills"
        groups: Dict[str, List[str]] = {}
        for name in self.r.under("skills/"):
            groups.setdefault(name.split("/")[1], []).append(name)
        for top, names in sorted(groups.items()):
            item = f"skills/{top}"
            single = names == [item]
            if not _SKILL_NAME.match(top) or is_secret(top):
                self.skip(item, "not a usable skill name")
                continue
            if single and not top.lower().endswith(".md"):
                self.skip(item, "not a skill (no SKILL.md)")
                continue
            if not single and not any(x.rsplit("/", 1)[-1] == "SKILL.md" for x in names):
                self.skip(item, "not a skill (no SKILL.md)")
                continue
            if sum(self.r.sizes[x] for x in names) > MAX_SKILL_BYTES:
                self.skip(item, f"larger than {MAX_SKILL_BYTES // (1024 * 1024)} MB")
                continue
            dest = _inside(root, top)
            files: List[Tuple[Path, str]] = []
            for x in names:
                if any(is_secret(part) for part in x.split("/")[1:]):
                    self.skip(x, "name looks like a secret")
                    continue
                files.append((_inside(root, x[len("skills/"):]), x))
            if dest.exists():
                if not self.replace:
                    self.skip(item, "a skill with this name exists (kept; use --replace to overwrite)")
                    continue
                n["updated"] += 1
            else:
                n["added"] += 1
            if self.dry:
                continue
            if dest.is_dir():
                shutil.rmtree(dest)
            elif dest.exists():
                dest.unlink()
            for target, x in files:
                _write(target, self.r.read(x, MAX_SKILL_BYTES))
        return n

    def _json_object(self, member: str, target: Path) -> Dict[str, int]:
        """A flat JSON object file: merge adds the keys that are missing, replace writes the file's version."""
        n = self.counts()
        if member not in self.r.sizes:
            return n
        try:
            new = json.loads(self.r.read(member, MAX_SMALL_FILE).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            new = None
        if not isinstance(new, dict):
            self.skip(member, "not a JSON object")
            return n
        old = _read_json_file(target)
        old = old if isinstance(old, dict) else {}
        if self.replace:
            final = new
            n["removed"] = len([k for k in old if k not in new])
            n["updated"] = len([k for k in new if k in old and old[k] != new[k]])
        else:
            final = dict(old)
            final.update({k: v for k, v in new.items() if k not in old})
            for k in new:
                if k in old and old[k] != new[k]:
                    self.skip(f"{member}: {k}", "already set differently here (kept)")
        n["added"] = len([k for k in new if k not in old])
        n["unchanged"] = len([k for k in new if k in old and old[k] == new[k]])
        if final != old and not self.dry:
            _write(_inside(self.agent, target.name), _json_bytes(final))
        return n

    def capabilities(self) -> Dict[str, int]:
        from .caps import OVERLAY_FILE
        return self._json_object("capabilities.json", self.agent / OVERLAY_FILE)

    def keybindings(self) -> Dict[str, int]:
        return self._json_object("keybindings.json", self.agent / "keybindings.json")

    def cron(self) -> Dict[str, int]:
        from .caps import scheduler as sched
        n = self.counts()
        if "cron.json" not in self.r.sizes:
            return n
        now = time.time()
        incoming: List[Dict[str, Any]] = []
        for j in _member_json(self.r, "cron.json", {}).get("jobs") or []:
            label = f"cron job {j.get('name') or j.get('id')}" if isinstance(j, dict) else "cron job"
            if not isinstance(j, dict) or not all(isinstance(j.get(k), str) and j.get(k) for k in ("id", "prompt", "schedule")):
                self.skip(label, "not a job (id, prompt and schedule are required)")
                continue
            try:
                schedule = sched.parse_schedule(j["schedule"])
            except ValueError as e:
                self.skip(label, f"schedule not understood: {e}")
                continue
            j = {k: v for k, v in j.items() if k not in ("running_since", "signal_payload")}
            j.setdefault("name", j["prompt"].strip()[:40])
            if j.get("enabled"):
                if j.get("cwd") and not os.path.isdir(str(j["cwd"])):
                    j["enabled"], j["next_run"] = False, None
                    self.notes.append(f"{label} arrives paused: its working folder {j['cwd']} does not exist here")
                elif schedule["kind"] == "once":
                    at = j.get("next_run")
                    if not (isinstance(at, (int, float)) and at > now):
                        j["enabled"], j["next_run"] = False, None  # its moment has passed
                else:
                    j["next_run"] = sched.next_run(schedule, now)
            incoming.append(j)

        def same(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
            return all(a.get(k) == b.get(k) for k in ("name", "prompt", "schedule", "cwd"))

        with sched._file_lock:
            old = sched.load_jobs(self.agent)
            if self.replace:
                final = incoming
                n["removed"] = len([o for o in old if not any(o.get("id") == j["id"] for j in incoming)])
                n["updated"] = len([j for j in incoming if any(o.get("id") == j["id"] for o in old)])
                n["added"] = len(incoming) - n["updated"]
            else:
                final = list(old)
                for j in incoming:
                    if any(o.get("id") == j["id"] or same(o, j) for o in final):
                        n["unchanged"] += 1
                    else:
                        final.append(j)
                        n["added"] += 1
            if not self.dry and (n["added"] or n["removed"] or n["updated"]):
                sched.save_jobs(self.agent, final)
        return n

    def board(self) -> Dict[str, int]:
        from .caps import board
        n = self.counts()
        if "board.json" not in self.r.sizes:
            return n
        doc = _member_json(self.r, "board.json", {})
        scope = doc.get("project") if isinstance(doc.get("project"), str) else None
        cards = []
        for c in doc.get("cards") or []:
            if not isinstance(c, dict) or not isinstance(c.get("title"), str) or not c["title"].strip():
                self.skip("board card", "not a card (no title)")
                continue
            cards.append(c)

        def key(c: Dict[str, Any]) -> Tuple[Any, ...]:
            return (c.get("project") or "", c.get("title"), c.get("body") or "", c.get("created"))

        old = _board_rows(self.sess, scope if self.replace else None)
        have = {key(c): c for c in old}
        want = {key(c): c for c in cards}
        n["added"] = len([k for k in want if k not in have])
        n["unchanged"] = len([k for k in want if k in have])
        n["removed"] = len([k for k in have if k not in want]) if self.replace else 0
        if self.dry or not (want or (self.replace and old)):
            return n
        self.agent.mkdir(parents=True, exist_ok=True)
        db = board._db(self.sess)
        try:
            if self.replace:
                ids = [(c["id"],) for c in old]
                db.executemany("DELETE FROM comments WHERE card = ?", ids)
                db.executemany("DELETE FROM cards WHERE id = ?", ids)
            now = time.time()
            for k, c in want.items():
                notes = [x for x in c.get("comments") or [] if isinstance(x, dict) and isinstance(x.get("text"), str)]
                if not self.replace and k in have:  # the card is here already: only bring comments it lacks
                    cid = have[k]["id"]
                    seen = {(x["at"], x["text"]) for x in have[k]["comments"]}
                    notes = [x for x in notes if (x.get("at"), x["text"]) not in seen]
                else:
                    status = c.get("status") if c.get("status") in board.STATUSES else "todo"
                    cid = c.get("id") if self.replace and isinstance(c.get("id"), int) and not isinstance(c.get("id"), bool) else None
                    if cid is not None and db.execute("SELECT 1 FROM cards WHERE id = ?", (cid,)).fetchone():
                        cid = None  # the number is taken by a card of another project
                    cur = db.execute("INSERT INTO cards (id, project, title, body, status, reason, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                     (cid, str(c.get("project") or ""), c["title"][:200], str(c.get("body") or ""), status,
                                      str(c.get("reason") or ""), c.get("created") or now, c.get("updated") or now))
                    cid = cur.lastrowid
                db.executemany("INSERT INTO comments (card, at, text) VALUES (?, ?, ?)",
                               [(cid, x.get("at") or now, x["text"][:2000]) for x in notes])
            db.commit()
        finally:
            db.close()
        return n


def import_agent(agent_dir: Any, path: Any, mode: str = "merge", sections: Optional[Iterable[str]] = None,
                 dry_run: bool = False) -> Dict[str, Any]:
    """Bring an agent file into `agent_dir`.

    `mode="merge"` adds what is missing and keeps what is there; `mode="replace"` makes each included section match
    the file. `sections` limits the import (default: every section in the file). `dry_run` changes nothing.
    Returns {"path", "mode", "dry_run", "sections": {name: {added, updated, unchanged, removed}},
    "skipped": [{"item", "reason"}], "notes": [...], "manifest": {...}}. Raises AgentFileError when the file is
    refused as a whole (not an agent file, unknown format version, unsafe paths, too large).
    """
    if mode not in ("merge", "replace"):
        raise AgentFileError("mode must be 'merge' or 'replace'")
    wanted = _sections(sections) if sections is not None else None
    r = _Reader(path)
    try:
        imp = _Import(Path(agent_dir), r, mode == "replace", bool(dry_run))
        result: Dict[str, Dict[str, int]] = {}
        for s in (wanted if wanted is not None else r.included):
            if s not in r.included:
                imp.skip(s, "this section is not in the file")
                continue
            result[s] = getattr(imp, s)()
        known = ("manifest.json", "archive.jsonl", "capabilities.json", "cron.json", "keybindings.json", "board.json")
        for name in r.names:
            if name not in known and not name.startswith(("memory/", "profiles/", "skills/")):
                imp.skip(name, "not part of any section")
        return {"path": str(r.path), "mode": mode, "dry_run": bool(dry_run), "sections": result,
                "skipped": imp.skipped, "notes": imp.notes, "manifest": r.manifest}
    finally:
        r.close()


# ------------------------------------------------------------------------------------------- command line
USAGE = ("usage: omni agent export [FILE] [--profile NAME] [--only a,b] [--without a,b]\n"
         "       omni agent import FILE [--replace] [--only a,b] [--dry-run]\n"
         "       omni agent inspect FILE\n"
         f"sections: {', '.join(SECTIONS)}")


def _say(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, errors="replace").decode(enc))


def _size(n: int) -> str:
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"


def _parse(args: List[str], valued: Tuple[str, ...], flags: Tuple[str, ...]) -> Tuple[List[str], Dict[str, Any]]:
    """-> (positionals, options); raises AgentFileError on anything unexpected."""
    pos: List[str] = []
    opts: Dict[str, Any] = {}
    i = 0
    while i < len(args):
        a = args[i]
        key, eq, val = a.partition("=")
        if key in valued:
            if not eq:
                i += 1
                if i >= len(args):
                    raise AgentFileError(f"{key} needs a value")
                val = args[i]
            opts[key] = val
        elif a in flags:
            opts[a] = True
        elif a.startswith("-") and a != "-":
            raise AgentFileError(f"unknown option {a}")
        else:
            pos.append(a)
        i += 1
    return pos, opts


def cli(argv: List[str], agent_dir: Any) -> int:
    """`omni agent export | import | inspect` without opening a session. 0 = done, 1 = failed, 2 = usage."""
    argv = list(argv or [])
    sub = argv[0] if argv else ""
    if sub in ("-h", "--help", "help"):
        _say(USAGE)
        return 0
    try:
        if sub == "export":
            pos, opts = _parse(argv[1:], ("--profile", "--only", "--without"), ())
            if len(pos) > 1:
                raise AgentFileError("export takes one FILE")
            include = _sections(opts["--only"]) if "--only" in opts else list(SECTIONS)
            if "--without" in opts:
                drop = _sections(opts["--without"])
                include = [s for s in include if s not in drop]
            if not include:
                raise AgentFileError("no sections left to export")
        elif sub == "import":
            pos, opts = _parse(argv[1:], ("--only",), ("--replace", "--dry-run"))
            if len(pos) != 1:
                raise AgentFileError("import needs one FILE")
            only = _sections(opts["--only"]) if "--only" in opts else None
            if only is not None and not only:
                raise AgentFileError("--only names no section")
        elif sub == "inspect":
            pos, opts = _parse(argv[1:], (), ())
            if len(pos) != 1:
                raise AgentFileError("inspect needs one FILE")
        else:
            raise AgentFileError(f"unknown command '{sub}'" if sub else "what should I do?")
    except AgentFileError as e:
        _say(f"{e}\n{USAGE}")
        return 2
    try:
        if sub == "export":
            profile = opts.get("--profile")
            if profile is None:
                from . import profiles as profiles_mod
                profile = profiles_mod.active_profile_name()
            s = export_agent(agent_dir, pos[0] if pos else default_name(), profile=profile, include=include)
            _say(f"Wrote {s['path']} ({_size(s['bytes'])})")
            for name, info in s["sections"].items():
                extra = "".join(f", {info[k]} {k}" for k in ("entries", "comments") if info.get(k))
                _say(f"  {name:<13} {info.get('count', 0)}{extra}")
            for x in s["skipped"]:
                _say(f"  left out: {x['item']} ({x['reason']})")
            _say("Credentials, session transcripts and cron output are never included.")
        elif sub == "import":
            s = import_agent(agent_dir, pos[0], mode="replace" if opts.get("--replace") else "merge", sections=only,
                             dry_run=bool(opts.get("--dry-run")))
            _say(("Dry run, nothing written. " if s["dry_run"] else "") + f"Import of {s['path']} ({s['mode']}):")
            for name, n in s["sections"].items():
                _say(f"  {name:<13} " + ", ".join(f"{v} {k}" for k, v in n.items() if v or k == "added"))
            for x in s["skipped"]:
                _say(f"  skipped: {x['item']} ({x['reason']})")
            for note in s["notes"]:
                _say(f"  note: {note}")
        else:
            s = inspect_agent(pos[0])
            m = s["manifest"]
            _say(f"{s['path']}: Omni agent file, format {m.get('format')}, created {m.get('created')}, "
                 f"{s['members']} entries, {_size(s['bytes'])} unpacked")
            for name, items in s["sections"].items():
                _say(f"  {name:<13} {len(items) if name != 'archive' else ''}".rstrip())
                for it in items[:20]:
                    _say(f"      {it}")
                if len(items) > 20:
                    _say(f"      ... and {len(items) - 20} more")
            for x in m.get("skipped") or []:
                if isinstance(x, dict):
                    _say(f"  left out at export: {x.get('item')} ({x.get('reason')})")
            _say("Never included: credentials, session transcripts, cron output.")
        return 0
    except (AgentFileError, OSError, zipfile.BadZipFile) as e:
        _say(f"omni agent {sub}: {e}")
        return 1
