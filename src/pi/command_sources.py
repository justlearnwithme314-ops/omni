"""Command sources: which plugin, extension, prompt template or skill a `/command` comes from.

Several sources can provide the same command name (two extensions both register `/review`, a project prompt
template overrides a user one, a package skill collides with a local skill). Every command therefore has a
**source prefix** and a qualified form `/<prefix>:<name>` that always means that exact source:

    pi:      Pi's built-in commands                  /pi:model
    omni:    Omni's additions                        /omni:profile
    <ext>:   an extension (its name, slugified)      /code-tools:review
    prompt:  user prompt templates                   /prompt:review
    project: project prompt templates (.pi/prompts)  /project:review
    skill:   skills (Pi's own `/skill:name`)          /skill:pdf        (a shadowed duplicate: /skill-project:pdf)

A bare `/name` runs the only command with that name. When several sources provide it, the remembered choice
(`commandSources` in settings) is used; otherwise the interactive window asks, and non-interactive modes use
Pi's order (built-in > extension > prompt template > skill). Prefixes can be renamed per source
(`commandPrefixes` in settings), e.g. `{"ext:my-very-long-extension": "mx"}`. `/commands` manages both.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

KIND_ORDER = {"builtin": 0, "omni": 1, "extension": 2, "prompt": 3, "skill": 4}
KIND_LABEL = {"builtin": "Pi built-in", "omni": "Omni", "extension": "extension", "prompt": "prompt template",
              "skill": "skill"}
PREFS_KEY = "commandSources"
PREFIXES_KEY = "commandPrefixes"


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "ext"


@dataclass
class CommandEntry:
    name: str                    # what the user types after `/` without a prefix ("review", "skill:pdf")
    kind: str                    # builtin | omni | extension | prompt | skill
    source_key: str              # stable id of the source, used for prefix overrides ("ext:code-tools", "prompt:project")
    prefix: str                  # current prefix for that source
    origin: str                  # human-readable source ("extension code-tools", "prompt template (project)")
    description: str = ""
    hint: Optional[str] = None
    ref: Any = None              # extension command dict / PromptTemplate / Skill
    default: bool = True         # False for a template or skill that another one of the same kind overrides
    order: int = 0               # registration order inside the kind

    @property
    def qualified(self) -> str:
        if self.kind == "skill":  # skills are typed as `skill:<name>`; the prefix replaces `skill`
            return f"{self.prefix}:{self.name.split(':', 1)[1]}"
        return f"{self.prefix}:{self.name}"

    @property
    def path(self) -> str:
        r = self.ref
        if isinstance(r, dict):
            return str(r.get("source") or "")
        return str(getattr(r, "file_path", "") or "")

    def rank(self) -> Tuple[int, int, int]:
        return (KIND_ORDER.get(self.kind, 9), 0 if self.default else 1, self.order)


@dataclass
class Resolution:
    entry: Optional[CommandEntry]
    args: str
    candidates: List[CommandEntry] = field(default_factory=list)
    explicit: bool = False       # typed with a source prefix
    remembered: bool = False     # picked from `commandSources`

    @property
    def ambiguous(self) -> bool:
        return self.entry is None and len(self.candidates) > 1


class CommandSources:
    """Builds the command list from a session on demand (cheap) and resolves `/name` or `/prefix:name`."""

    def __init__(self, session, include_builtins: bool = True):
        self.s = session
        self.include_builtins = include_builtins

    # ------------------------------------------------------------------------------------------ settings
    def _settings_dict(self, key: str) -> Dict[str, str]:
        v = self.s.settings.get(key) if getattr(self.s, "settings", None) is not None else None
        return dict(v) if isinstance(v, dict) else {}

    def preferences(self) -> Dict[str, str]:
        return self._settings_dict(PREFS_KEY)

    def prefix_overrides(self) -> Dict[str, str]:
        return self._settings_dict(PREFIXES_KEY)

    def remember(self, name: str, qualified: Optional[str]) -> None:
        prefs = self.preferences()
        if qualified:
            prefs[name] = qualified
        else:
            prefs.pop(name, None)
        self.s.settings.set_global(PREFS_KEY, prefs)

    def set_prefix(self, source_key: str, prefix: Optional[str]) -> str:
        """Rename a source's prefix (None/"" restores the default). Returns the prefix now in effect."""
        before = {e.qualified: e for e in self.entries() if e.source_key == source_key}
        if not before:
            raise ValueError(f"unknown source '{source_key}'")
        over = self.prefix_overrides()
        if prefix:
            p = slug(prefix)
            if p in {e.prefix for e in self.entries() if e.source_key != source_key}:
                raise ValueError(f"prefix '{p}' is already used by another source")
            over[source_key] = p
        else:
            over.pop(source_key, None)
        self.s.settings.set_global(PREFIXES_KEY, over)
        after = {(e.kind, e.name): e.qualified for e in self.entries() if e.source_key == source_key}
        prefs = self.preferences()  # remembered choices store qualified names: follow the rename
        changed = False
        for name, q in list(prefs.items()):
            if q in before:
                prefs[name] = after.get((before[q].kind, before[q].name), q)
                changed = True
        if changed:
            self.s.settings.set_global(PREFS_KEY, prefs)
        return next(iter(after.values())).split(":", 1)[0] if after else (prefix or "")

    def reset(self) -> None:
        self.s.settings.set_global(PREFS_KEY, {})

    # ------------------------------------------------------------------------------------------- entries
    def entries(self) -> List[CommandEntry]:
        from .commands import BUILTIN, OMNI_EXTRAS
        out: List[CommandEntry] = []
        if self.include_builtins:
            for i, (n, d, h) in enumerate(BUILTIN):
                out.append(CommandEntry(n, "builtin", "pi", "pi", KIND_LABEL["builtin"], d, h, None, True, i))
            for i, (n, d, h) in enumerate(OMNI_EXTRAS):
                out.append(CommandEntry(n, "omni", "omni", "omni", KIND_LABEL["omni"], d, h, None, True, i))
        ext = getattr(self.s, "ext", None)
        ext_cmds = list(getattr(ext, "command_list", None) or [])
        if not ext_cmds:  # older hosts / stand-ins only expose the name -> command map
            ext_cmds = [dict(c, name=n) for n, c in (getattr(ext, "commands", None) or {}).items()]
        first_by_name: Dict[str, str] = {}
        for i, c in enumerate(ext_cmds):
            ename = c.get("extension") or _ext_name_from_path(c.get("source") or "") or "extension"
            key = f"ext:{ename}"
            first_by_name.setdefault(c["name"], key)
            out.append(CommandEntry(c["name"], "extension", key, slug(ename), f"extension {ename}",
                                    c.get("description") or "extension command", None, c,
                                    first_by_name[c["name"]] == key, i))
        templates = [(t, True) for t in getattr(self.s, "templates", [])] + \
                    [(t, False) for t in getattr(self.s, "shadowed_templates", [])]
        for i, (t, default) in enumerate(templates):
            src = getattr(t, "source", "user") or "user"
            key = f"prompt:{src}" if src != "path" else f"prompt:{_parent_name(t.file_path)}"
            pre = {"user": "prompt", "project": "project"}.get(src) or slug(_parent_name(t.file_path)) + "-prompts"
            out.append(CommandEntry(t.name, "prompt", key, pre, f"prompt template ({src if src != 'path' else _parent_name(t.file_path)})",
                                    t.description, t.argument_hint, t, default, i))
        if self.s.settings.get("enableSkillCommands", True) if getattr(self.s, "settings", None) is not None else True:
            skills = [(k, True) for k in getattr(self.s, "skills", [])] + \
                     [(k, False) for k in getattr(self.s, "shadowed_skills", [])]
            for i, (k, default) in enumerate(skills):
                src = getattr(k, "source", "") or "skills"
                key = "skill" if default else f"skill:{src}"
                pre = "skill" if default else f"skill-{slug(src)}"
                out.append(CommandEntry(f"skill:{k.name}", "skill", key, pre, f"skill ({src})", k.description, None, k,
                                        default, i))
        # de-duplicate prefixes between different sources, then apply user overrides
        seen: Dict[str, str] = {}
        for e in out:
            base, n = e.prefix, 2
            while seen.get(e.prefix, e.source_key) != e.source_key:
                e.prefix = f"{base}-{n}"
                n += 1
            seen[e.prefix] = e.source_key
        over = self.prefix_overrides()
        for e in out:
            if e.source_key in over:
                e.prefix = over[e.source_key]
        return out

    def by_name(self) -> Dict[str, List[CommandEntry]]:
        groups: Dict[str, List[CommandEntry]] = {}
        for e in self.entries():
            groups.setdefault(e.name, []).append(e)
        for g in groups.values():
            g.sort(key=CommandEntry.rank)
        return groups

    def conflicts(self) -> Dict[str, List[CommandEntry]]:
        return {n: g for n, g in self.by_name().items() if len(g) > 1}

    def sources(self) -> List[Tuple[str, str, str, int]]:
        """(source_key, prefix, origin, number of commands) per source, in first-seen order."""
        acc: Dict[str, List[Any]] = {}
        for e in self.entries():
            acc.setdefault(e.source_key, [e.prefix, e.origin, 0])[2] += 1
        return [(k, v[0], v[1], v[2]) for k, v in acc.items()]

    # ---------------------------------------------------------------------------------------- resolution
    def lookup(self, token: str) -> Tuple[List[CommandEntry], bool]:
        """Candidates for the text after `/` (no arguments). Returns (candidates, explicit-prefix?).
        A plain name wins over a qualified form, so Pi's own names (`/skill:pdf`) keep their meaning."""
        entries = self.entries()
        named = [e for e in entries if e.name == token]
        if named:
            return sorted(named, key=CommandEntry.rank), False
        exact = [e for e in entries if e.qualified == token]
        return exact[:1], bool(exact)

    def resolve(self, text: str, choose: Optional[Callable[[str, List[CommandEntry]], Optional[CommandEntry]]] = None,
                kinds: Optional[set] = None) -> Resolution:
        """Resolve `/token args`. `choose(name, candidates)` is asked when the name is ambiguous and no choice is
        remembered; without it Pi's order decides. `kinds` limits which kinds may resolve (e.g. print mode)."""
        body = text.strip()[1:] if text.strip().startswith("/") else text.strip()
        token, _, args = body.partition(" ")
        cands, explicit = self.lookup(token)
        if kinds is not None:
            cands = [c for c in cands if c.kind in kinds]
        if not cands:
            return Resolution(None, args, [], explicit)
        if len(cands) == 1:
            return Resolution(cands[0], args, cands, explicit)
        pref = self.preferences().get(token)
        hit = next((c for c in cands if c.qualified == pref), None)
        if hit is not None:
            return Resolution(hit, args, cands, False, True)
        if choose is not None:
            return Resolution(choose(token, cands), args, cands)
        return Resolution(cands[0], args, cands)

    # ----------------------------------------------------------------------------------------- execution
    def prompt_text(self, entry: CommandEntry, args: str, unique: bool = False) -> str:
        """User-message text for a prompt template or skill entry.

        `unique` (the name has no other source): keep the raw `/name args` so the agent expands it exactly as Pi
        does (extensions' `input` handlers see the raw text). Otherwise expand here, so the chosen source is used."""
        from . import resources as res
        raw = f"/{entry.name}" + (f" {args}" if args else "")
        if unique and entry.default:
            return raw
        if entry.kind == "prompt":
            return res.expand_template(entry.ref, args)
        if entry.kind == "skill":
            return res.expand_skill_command(raw, [entry.ref])
        raise ValueError(entry.kind)

    # ------------------------------------------------------------------------------------- autocomplete
    def completions(self, query: str) -> List[Dict[str, Any]]:
        """Items for the `/` menu: a name provided by several sources appears once per source."""
        q = query.lower()
        groups = self.by_name()
        prefs = self.preferences()
        items: List[Dict[str, Any]] = []
        for name, group in groups.items():
            multi = len(group) > 1
            for e in group:
                hay_name, hay_q = name.lower(), e.qualified.lower()
                if q and not (hay_name.startswith(q) or hay_q.startswith(q) or q in hay_name):
                    continue
                starred = multi and prefs.get(name) == e.qualified
                if multi:
                    label = f"/{name}  [{e.prefix}]"
                    insert = f"/{e.qualified} "
                    desc = f"{'★ ' if starred else ''}{e.description}"
                else:
                    typed_prefix = q.split(":", 1)[0] if ":" in q and hay_q.startswith(q) and not hay_name.startswith(q) else ""
                    label = f"/{e.qualified}" if typed_prefix else f"/{name}"
                    insert = f"/{e.qualified} " if typed_prefix else f"/{name} "
                    desc = e.description
                items.append({"label": label, "insert": insert, "desc": desc, "tag": e.kind,
                              "kind": e.kind, "qualified": e.qualified, "multi": multi,
                              "_sort": (not hay_name.startswith(q) and not hay_q.startswith(q), name, e.rank())})
        items.sort(key=lambda i: i.pop("_sort"))
        return items


def _ext_name_from_path(p: str) -> str:
    if not p:
        return ""
    base = os.path.basename(p.rstrip("/\\"))
    if re.match(r"index\.(ts|js|mjs|tsx)$", base):
        return os.path.basename(os.path.dirname(p.rstrip("/\\")))
    return re.sub(r"\.(ts|js|mjs|tsx)$", "", base)


def _parent_name(p: str) -> str:
    d = os.path.dirname(p or "")
    name = os.path.basename(d)
    if name == "prompts":
        name = os.path.basename(os.path.dirname(d)) or name
    return name or "path"
