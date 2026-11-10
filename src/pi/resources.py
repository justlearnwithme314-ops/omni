"""Settings, project trust, context files, skills and prompt templates (Pi's settings-manager, trust-manager,
resource-loader, skills.ts, prompt-templates.ts)."""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONFIG_DIR = ".pi"
CONTEXT_CANDIDATES = ["AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"]

DEFAULT_SETTINGS: Dict[str, Any] = {
    "defaultThinkingLevel": "medium",
    "steeringMode": "one-at-a-time",
    "followUpMode": "one-at-a-time",
    "doubleEscapeAction": "tree",
    "defaultProjectTrust": "ask",
    "theme": "omni",
    "quietStartup": False,
    "tuiMode": "fullscreen",
    "hideThinkingBlock": False,
    "enableSkillCommands": True,
    "autocompleteMaxVisible": 5,
    "compaction": {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 20000},
    "retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 2000, "maxAgentDelayMs": 60000},
    "packages": [], "extensions": [], "skills": [], "prompts": [], "themes": [],
}

GLOBAL_ONLY = {"defaultProjectTrust", "httpProxy", "selfUpdateCommand", "cacheWarming"}


def agent_dir() -> Path:
    """Pi's agent directory. OMNI_PI_AGENT_DIR > PI_CODING_AGENT_DIR > ~/.pi/agent (shared with a real Pi install)."""
    return Path(os.environ.get("OMNI_PI_AGENT_DIR") or os.environ.get("PI_CODING_AGENT_DIR") or (Path.home() / ".pi" / "agent")).expanduser()


def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _read_json(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


class Settings:
    """Layered: defaults < <agent-dir>/settings.json < .pi/settings.json (only when trusted)."""

    def __init__(self, agent: Path, cwd: str, trusted: bool):
        self.agent = Path(agent)
        self.cwd = cwd
        self.trusted = trusted
        self.reload()

    def reload(self) -> None:
        self.global_data = _read_json(self.agent / "settings.json")
        self.project_data = _read_json(Path(self.cwd) / CONFIG_DIR / "settings.json") if self.trusted else {}
        proj = {k: v for k, v in self.project_data.items() if k not in GLOBAL_ONLY}
        self.data = _deep_merge(_deep_merge(DEFAULT_SETTINGS, self.global_data), proj)

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set_global(self, key: str, value: Any) -> None:
        data = _read_json(self.agent / "settings.json")
        cur = data
        parts = key.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
        self.agent.mkdir(parents=True, exist_ok=True)
        (self.agent / "settings.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.reload()


class TrustStore:
    """Project trust decisions, persisted in <agent-dir>/trust.json ({path: true|false})."""

    def __init__(self, agent: Path):
        self.path = Path(agent) / "trust.json"

    def _load(self) -> Dict[str, bool]:
        return _read_json(self.path)  # type: ignore[return-value]

    def decision(self, cwd: str) -> Optional[bool]:
        data = self._load()
        cur = os.path.abspath(cwd)
        while True:
            if cur in data:
                return bool(data[cur])
            parent = os.path.dirname(cur)
            if parent == cur:
                return None
            cur = parent

    def save(self, cwd: str, trusted: bool) -> None:
        data = self._load()
        data[os.path.abspath(cwd)] = trusted
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def project_has_resources(cwd: str) -> bool:
    return (Path(cwd) / CONFIG_DIR).is_dir()


# ------------------------------------------------------------------------------------------- frontmatter
def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if text.startswith("﻿"):
        text = text[1:]
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
        for line in raw.splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                k, v = line.split(":", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data, body


# ------------------------------------------------------------------------------------------- context
def load_context_file_from_dir(d: Path) -> Optional[Dict[str, str]]:
    for name in CONTEXT_CANDIDATES:
        p = d / name
        if p.is_file():
            try:
                txt = p.read_text(encoding="utf-8")
                return {"path": str(p), "content": txt[1:] if txt.startswith("﻿") else txt}
            except OSError:
                continue
    return None


def load_context_files(cwd: str, agent: Path) -> List[Dict[str, str]]:
    files: List[Dict[str, str]] = []
    seen = set()
    g = load_context_file_from_dir(Path(agent))
    if g:
        files.append(g)
        seen.add(g["path"])
    ancestors: List[Dict[str, str]] = []
    cur = Path(os.path.abspath(cwd))
    while True:
        f = load_context_file_from_dir(cur)
        if f and f["path"] not in seen:
            ancestors.insert(0, f)
            seen.add(f["path"])
        if cur.parent == cur:
            break
        cur = cur.parent
    return files + ancestors


def load_system_md(cwd: str, agent: Path, trusted: bool, name: str) -> Optional[str]:
    if trusted:
        p = Path(cwd) / CONFIG_DIR / name
        if p.is_file():
            return p.read_text(encoding="utf-8")
    p = Path(agent) / name
    return p.read_text(encoding="utf-8") if p.is_file() else None


# ------------------------------------------------------------------------------------------------ skills
@dataclass
class Skill:
    name: str
    description: str
    file_path: str
    base_dir: str
    source: str
    disable_model_invocation: bool = False
    frontmatter: Dict[str, Any] = field(default_factory=dict)


_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _git_root(start: Path) -> Optional[Path]:
    cur = start
    while True:
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def skill_roots(cwd: str, agent: Path, trusted: bool, extra: List[str]) -> List[Tuple[Path, str]]:
    roots: List[Tuple[Path, str]] = [(Path(agent) / "skills", "user"), (Path.home() / ".agents" / "skills", "user")]
    if trusted:
        roots.append((Path(cwd) / CONFIG_DIR / "skills", "project"))
        stop = _git_root(Path(cwd))
        cur = Path(os.path.abspath(cwd))
        while True:
            roots.append((cur / ".agents" / "skills", "project"))
            if (stop and cur == stop) or cur.parent == cur:
                break
            cur = cur.parent
    for e in extra:
        roots.append((Path(os.path.expanduser(e)), "path"))
    return roots


def load_skill_file(p: Path, source: str, diagnostics: List[str]) -> Optional[Skill]:
    try:
        fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
    except Exception as e:
        diagnostics.append(f"skill {p}: {e}")
        return None
    name = str(fm.get("name") or (p.parent.name if p.name == "SKILL.md" else p.stem))
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        diagnostics.append(f"skill {p}: description is required")
        return None
    if not _NAME_RE.match(name) or len(name) > 64:
        diagnostics.append(f"skill {p}: name '{name}' should use lowercase letters, numbers and hyphens")
    if len(desc) > 1024:
        diagnostics.append(f"skill {p}: description exceeds 1024 characters")
    return Skill(name=name, description=desc.strip(), file_path=str(p), base_dir=str(p.parent), source=source,
                 disable_model_invocation=bool(fm.get("disable-model-invocation")), frontmatter=fm)


def discover_skills(cwd: str, agent: Path, trusted: bool, extra: List[str], diagnostics: List[str],
                    shadowed: Optional[List[Skill]] = None) -> List[Skill]:
    """`shadowed` (optional) collects the skills that lost a name collision, so /commands can offer them by source."""
    skills: Dict[str, Skill] = {}

    def add(s: Optional[Skill]) -> None:
        if not s:
            return
        if s.name in skills:
            if skills[s.name].file_path != s.file_path:
                diagnostics.append(f"skill name collision '{s.name}': keeping {skills[s.name].file_path}")
                if shadowed is not None:
                    shadowed.append(s)
            return
        skills[s.name] = s

    for root, source in skill_roots(cwd, agent, trusted, extra):
        if root.is_file():
            add(load_skill_file(root, source, diagnostics))
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git")]
            if "SKILL.md" in filenames:
                add(load_skill_file(Path(dirpath) / "SKILL.md", source, diagnostics))
                dirnames[:] = []  # a skill's own subfolders are its resources
            elif Path(dirpath) == root:
                for fn in filenames:  # standalone markdown skills at the root
                    if fn.endswith(".md") and fn.lower() not in ("readme.md",):
                        add(load_skill_file(Path(dirpath) / fn, source, diagnostics))
    return list(skills.values())


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")


def format_skills_for_prompt(skills: List[Skill], reader: str = "read") -> str:
    visible = [s for s in skills if not s.disable_model_invocation]
    if not visible:
        return ""
    lines = ["\n\nThe following skills provide specialized instructions for specific tasks.",
             "Use the read tool to load a skill's file when the task matches its description." if reader == "read" else
             "Use bash to load a skill's file when the task matches its description." if reader == "bash" else
             "Load a skill's file when the task matches its description.",
             "Resolve relative paths against the skill's directory (parent of SKILL.md). Use absolute paths in tool calls.",
             "", "<available_skills>"]
    for s in visible:
        lines += ["  <skill>", f"    <name>{_xml(s.name)}</name>", f"    <description>{_xml(s.description)}</description>",
                  f"    <location>{_xml(s.file_path)}</location>", "  </skill>"]
    lines.append("</available_skills>")
    return "\n".join(lines)


def expand_skill_command(text: str, skills: List[Skill]) -> str:
    if not text.startswith("/skill:"):
        return text
    sp = text.find(" ")
    name = text[7:] if sp == -1 else text[7:sp]
    args = "" if sp == -1 else text[sp + 1:].strip()
    skill = next((s for s in skills if s.name == name), None)
    if not skill:
        return text
    _, body = parse_frontmatter(Path(skill.file_path).read_text(encoding="utf-8"))
    block = (f'<skill name="{skill.name}" location="{skill.file_path}">\nReferences are relative to {skill.base_dir}.\n\n'
             f"{body.strip()}\n</skill>")
    return f"{block}\n\n{args}" if args else block


# -------------------------------------------------------------------------------------- prompt templates
@dataclass
class PromptTemplate:
    name: str
    description: str
    content: str
    file_path: str
    argument_hint: Optional[str] = None
    source: str = "user"


def discover_templates(cwd: str, agent: Path, trusted: bool, extra: List[str],
                       shadowed: Optional[List[PromptTemplate]] = None) -> List[PromptTemplate]:
    """Later sources override earlier ones (project > user, like Pi); `shadowed` collects the overridden ones."""
    dirs: List[Tuple[Path, str]] = [(Path(agent) / "prompts", "user")]
    if trusted:
        dirs.append((Path(cwd) / CONFIG_DIR / "prompts", "project"))
    for e in extra:
        dirs.append((Path(os.path.expanduser(e)), "path"))
    out: Dict[str, PromptTemplate] = {}
    for d, source in dirs:
        files = [d] if d.is_file() else sorted(d.glob("*.md")) if d.is_dir() else []
        for p in files:
            try:
                fm, body = parse_frontmatter(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            desc = fm.get("description") or next((l.strip() for l in body.splitlines() if l.strip()), "")
            if shadowed is not None and p.stem in out and out[p.stem].file_path != str(p):
                shadowed.append(out[p.stem])
            out[p.stem] = PromptTemplate(p.stem, str(desc)[:200], body, str(p), fm.get("argument-hint"), source)
    return list(out.values())


def split_args(s: str) -> List[str]:
    """Shell-like: quotes only open at the start of an argument; unmatched quotes stay literal."""
    args, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        if s[i] in "\"'":
            q = s[i]
            j, buf, closed = i + 1, "", False
            while j < n:
                if s[j] == "\\" and j + 1 < n and s[j + 1] == q:
                    buf += q
                    j += 2
                    continue
                if s[j] == q:
                    closed = True
                    j += 1
                    break
                buf += s[j]
                j += 1
            if closed and (j >= n or s[j].isspace()):
                args.append(buf)
                i = j
                continue
        j = i
        while j < n and not s[j].isspace():
            j += 1
        args.append(s[i:j])
        i = j
    return args


def expand_template(t: PromptTemplate, arg_text: str) -> str:
    args = split_args(arg_text)
    content = t.content

    def rep_brace(m: re.Match) -> str:
        inner = m.group(1)
        mm = re.match(r"^@:(\d+)(?::(\d+))?$", inner)
        if mm:
            start = int(mm.group(1)) - 1
            ln = int(mm.group(2)) if mm.group(2) else None
            return " ".join(args[start:start + ln] if ln is not None else args[start:])
        mm = re.match(r"^(\d+|@):-(.*)$", inner, re.S)
        if mm:
            key, default = mm.group(1), mm.group(2)
            if key == "@":
                return " ".join(args) if args else default
            idx = int(key) - 1
            return args[idx] if idx < len(args) and args[idx] != "" else default
        return m.group(0)

    content = re.sub(r"\$\{([^}]*)\}", rep_brace, content)
    content = content.replace("$ARGUMENTS", " ".join(args)).replace("$@", " ".join(args))
    content = re.sub(r"\$(\d+)", lambda m: args[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(args) else "", content)
    return content


# ----------------------------------------------------------------------------------- extension discovery
def _entries_for_dir(d: Path) -> List[Path]:
    pkg = d / "package.json"
    if pkg.is_file():
        data = _read_json(pkg)
        exts = (data.get("pi") or data.get("omp") or {}).get("extensions")
        if isinstance(exts, str):
            exts = [exts]
        if isinstance(exts, list) and exts:
            out: List[Path] = []
            for e in exts:
                if not isinstance(e, str):
                    continue
                if any(ch in e for ch in "*?["):  # glob entries
                    out.extend(sorted(p.resolve() for p in d.glob(e) if p.is_file() and p.suffix in (".ts", ".js", ".mjs")
                                      and not p.name.endswith(".d.ts")))
                    continue
                p = (d / e).resolve()
                if p.is_dir():  # a directory entry: its index, or every extension file inside it (Pi's loader does this)
                    idx = next((p / n for n in ("index.ts", "index.js", "index.mjs") if (p / n).is_file()), None)
                    out.extend([idx] if idx else discover_extension_dir(p))
                elif p.exists():
                    out.append(p)
                else:
                    for ext in (".ts", ".js", ".mjs"):
                        if p.with_suffix(ext).exists():
                            out.append(p.with_suffix(ext))
                            break
            return out
    for nm in ("index.ts", "index.js", "index.mjs"):
        if (d / nm).is_file():
            return [d / nm]
    return []


def discover_extension_dir(d: Path) -> List[Path]:
    """`dir/*.ts|*.js`, `dir/*/index.ts|js`, `dir/*/package.json` with a `pi.extensions` field - one level deep."""
    out: List[Path] = []
    if not d.is_dir():
        return out
    for child in sorted(d.iterdir(), key=lambda p: p.name.lower()):
        if child.is_file() and child.suffix in (".ts", ".js", ".mjs") and not child.name.endswith(".d.ts"):
            out.append(child)
        elif child.is_dir() and not child.name.startswith("."):
            out.extend(_entries_for_dir(child))
    return out


def discover_extensions(cwd: str, agent: Path, trusted: bool, settings: Settings, extra: List[str],
                        omni_plugin_dir: Optional[Path]) -> List[Path]:
    paths: List[Path] = []
    if trusted:
        paths += discover_extension_dir(Path(cwd) / CONFIG_DIR / "extensions")
    paths += discover_extension_dir(Path(agent) / "extensions")
    if omni_plugin_dir is not None:
        paths += discover_extension_dir(omni_plugin_dir)
    for e in list(settings.get("extensions") or []) + list(extra):
        p = Path(os.path.expanduser(e))
        if not p.is_absolute():
            p = Path(cwd) / p
        if p.is_dir():
            paths += _entries_for_dir(p) or discover_extension_dir(p)
        elif p.exists():
            paths.append(p)
    seen, uniq = set(), []
    for p in paths:
        k = os.path.normcase(str(p.resolve()))
        if k not in seen:
            seen.add(k)
            uniq.append(p.resolve())
    return uniq
