"""Pi packages (docs/packages.md): `install / remove / update / list` for npm, git and local sources, settings
`packages` entries with resource filters, and package resource discovery (extensions, skills, prompts, themes).

Layout (same as Pi):
    npm  -> <root>/npm/node_modules/<name>         root = <agent-dir> (personal) or <cwd>/.pi (project, -l)
    git  -> <root>/git/<host>/<path>
    -e   -> <agent-dir>/tmp/extensions/...         temporary, not written to settings
    local paths are used in place.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RESOURCE_TYPES = ("extensions", "skills", "prompts", "themes")
EXT_SUFFIXES = (".ts", ".tsx", ".js", ".mjs", ".jsx")


@dataclass
class Source:
    kind: str                 # npm | git | local
    raw: str
    name: str = ""            # npm package name
    spec: str = ""            # npm spec (name@version)
    pinned: bool = False
    url: str = ""             # git clone url
    host: str = ""
    repo_path: str = ""
    ref: Optional[str] = None
    path: str = ""            # local absolute path

    @property
    def identity(self) -> str:
        if self.kind == "npm":
            return f"npm:{self.name}"
        if self.kind == "git":
            return f"git:{self.host}/{self.repo_path}".lower()
        return "local:" + os.path.normcase(os.path.abspath(self.path))


def parse_source(raw: str, base_dir: Optional[str] = None) -> Source:
    s = raw.strip()
    if s.startswith("npm:"):
        spec = s[4:]
        if spec.startswith("@"):
            name, _, ver = spec[1:].partition("@")
            name = "@" + name
        else:
            name, _, ver = spec.partition("@")
        return Source("npm", raw, name=name, spec=spec, pinned=bool(re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].*)?", ver or "")))
    if s.startswith("git:") or re.match(r"^(https?|ssh)://", s) or s.startswith("git@"):
        body = s[4:] if s.startswith("git:") else s
        ref = None
        m = re.match(r"^(.*?)(?:@([^@/]+))?$", body) if not body.startswith("git@") else re.match(r"^(git@[^:]+:[^@]+?)(?:@([^@/]+))?$", body)
        if m:
            body, ref = m.group(1), m.group(2)
        if body.startswith("git@"):
            host, _, path = body[4:].partition(":")
            url = body
        else:
            b = re.sub(r"^(https?|ssh)://", "", body)
            host, _, path = b.partition("/")
            url = body if re.match(r"^(https?|ssh)://", body) else f"https://{b}"
        path = path[:-4] if path.endswith(".git") else path
        if not url.endswith(".git") and url.startswith("https://"):
            url = url + ".git"
        return Source("git", raw, url=url, host=host, repo_path=path.strip("/"), ref=ref)
    p = os.path.expanduser(s)
    if not os.path.isabs(p):
        p = os.path.join(base_dir or os.getcwd(), p)
    return Source("local", raw, path=os.path.abspath(p))


def install_root(agent_dir: Path, cwd: str, local: bool, temporary: bool = False) -> Path:
    if temporary:
        return Path(agent_dir) / "tmp" / "extensions"
    return (Path(cwd) / ".pi") if local else Path(agent_dir)


def package_dir(src: Source, root: Path) -> Path:
    if src.kind == "npm":
        return root / "npm" / "node_modules" / Path(*src.name.split("/"))
    if src.kind == "git":
        return root / "git" / src.host / Path(*src.repo_path.split("/"))
    return Path(src.path)


class PackageError(Exception):
    pass


def _run(cmd: List[str], cwd: Optional[Path] = None) -> str:
    exe = shutil.which(cmd[0])
    if not exe:
        raise PackageError(f"{cmd[0]} was not found on PATH")
    r = subprocess.run([exe] + cmd[1:], cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if r.returncode != 0:
        raise PackageError(f"{' '.join(cmd)} failed:\n{(r.stderr or r.stdout).strip()[-2000:]}")
    return r.stdout


def _npm_deps(d: Path) -> None:
    pkg = d / "package.json"
    if pkg.is_file():
        data = json.loads(pkg.read_text(encoding="utf-8") or "{}")
        if data.get("dependencies"):
            _run(["npm", "install", "--omit=dev", "--legacy-peer-deps", "--no-audit", "--no-fund"], cwd=d)


def install_source(src: Source, root: Path) -> Path:
    d = package_dir(src, root)
    if src.kind == "local":
        if not d.exists():
            raise PackageError(f"local package not found: {d}")
        return d
    if src.kind == "npm":
        npm_root = root / "npm"
        npm_root.mkdir(parents=True, exist_ok=True)
        if not (npm_root / "package.json").exists():
            (npm_root / "package.json").write_text(json.dumps({"name": "pi-packages", "private": True}, indent=2), encoding="utf-8")
        _run(["npm", "install", src.spec, "--legacy-peer-deps", "--no-audit", "--no-fund", "--omit=dev"], cwd=npm_root)
        return d
    # git
    if (d / ".git").exists():
        _run(["git", "fetch", "--tags", "--force", "origin"], cwd=d)
        target = src.ref or _default_branch(d)
        try:
            _run(["git", "rev-parse", "--verify", f"origin/{target}"], cwd=d)
            _run(["git", "reset", "--hard", f"origin/{target}"], cwd=d)
        except PackageError:
            _run(["git", "checkout", "--force", target], cwd=d)
    else:
        d.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", src.url, str(d)])
        if src.ref:
            _run(["git", "checkout", "--force", src.ref], cwd=d)
    _npm_deps(d)
    return d


def _default_branch(d: Path) -> str:
    try:
        out = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=d).strip()
        return out.rsplit("/", 1)[-1]
    except PackageError:
        return "main"


# ------------------------------------------------------------------------------------------------ settings
def _settings_path(agent_dir: Path, cwd: str, local: bool) -> Path:
    return (Path(cwd) / ".pi" / "settings.json") if local else (Path(agent_dir) / "settings.json")


def _read(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _entry_source(e: Any) -> Optional[str]:
    return e if isinstance(e, str) else (e.get("source") if isinstance(e, dict) else None)


def add_to_settings(agent_dir: Path, cwd: str, src: Source, local: bool) -> Path:
    p = _settings_path(agent_dir, cwd, local)
    data = _read(p)
    pkgs = data.setdefault("packages", [])
    base = str(p.parent)
    raw = src.raw
    if src.kind == "local":  # store relative to the settings file when it lives below it
        try:
            raw = os.path.relpath(src.path, base) if not os.path.isabs(src.raw) else src.path
            if not raw.startswith("."):
                raw = "./" + raw
        except ValueError:
            raw = src.path
    for i, e in enumerate(pkgs):
        es = _entry_source(e)
        if es and parse_source(es, base).identity == src.identity:
            if isinstance(e, dict):
                pkgs[i] = dict(e, source=raw)
            else:
                pkgs[i] = raw
            break
    else:
        pkgs.append(raw)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def remove_from_settings(agent_dir: Path, cwd: str, raw: str, local: bool) -> bool:
    p = _settings_path(agent_dir, cwd, local)
    data = _read(p)
    base = str(p.parent)
    ident = parse_source(raw, os.getcwd()).identity
    before = list(data.get("packages") or [])
    after = [e for e in before if not (_entry_source(e) and parse_source(_entry_source(e), base).identity == ident)]
    if len(after) == len(before):
        return False
    data["packages"] = after
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


@dataclass
class Configured:
    entry: Any
    source: Source
    scope: str            # user | project | temporary
    root: Path
    filters: Dict[str, Any] = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return package_dir(self.source, self.root)


def configured_packages(agent_dir: Path, cwd: str, trusted: bool) -> List[Configured]:
    out: Dict[str, Configured] = {}
    scopes = [("user", Path(agent_dir) / "settings.json", Path(agent_dir))]
    if trusted:
        scopes.append(("project", Path(cwd) / ".pi" / "settings.json", Path(cwd) / ".pi"))
    for scope, path, root in scopes:
        for e in _read(path).get("packages") or []:
            raw = _entry_source(e)
            if not raw:
                continue
            src = parse_source(raw, str(path.parent))
            filters = {k: e[k] for k in RESOURCE_TYPES if isinstance(e, dict) and k in e}
            prev = out.get(src.identity)
            if prev and scope == "project" and isinstance(e, dict) and e.get("autoload") is False:
                prev.filters.update(filters)  # a filtering delta over the personal entry
                continue
            out[src.identity] = Configured(e, src, scope, root, filters)
    return list(out.values())


# ----------------------------------------------------------------------------------------- resources
def _expand_patterns(base: Path, patterns: List[str], kind: str) -> List[Path]:
    inc: List[Path] = []
    exc: List[str] = []
    for pat in patterns:
        if not isinstance(pat, str):
            continue
        if pat.startswith("!"):
            exc.append(pat[1:].lstrip("./"))
            continue
        if any(ch in pat for ch in "*?["):
            inc.extend(sorted(p for p in base.glob(pat.lstrip("./")) if _accept(p, kind)))
        else:
            p = (base / pat).resolve()
            if p.exists():
                inc.extend(_walk(p, kind) if p.is_dir() else [p])
    res = []
    for p in inc:
        rel = _rel(p, base)
        if not any(fnmatch.fnmatch(rel, e) for e in exc) and p not in res:
            res.append(p)
    return res


def _rel(p: Path, base: Path) -> str:
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _accept(p: Path, kind: str) -> bool:
    if kind == "extensions":
        return p.is_file() and p.suffix in EXT_SUFFIXES and not p.name.endswith(".d.ts")
    if kind == "skills":
        return (p.is_dir() and (p / "SKILL.md").is_file()) or (p.is_file() and p.suffix == ".md")
    if kind == "prompts":
        return p.is_file() and p.suffix == ".md"
    if kind == "themes":
        return p.is_file() and p.suffix == ".json"
    return False


def _walk(d: Path, kind: str) -> List[Path]:
    """Conventional discovery inside a resource directory."""
    if kind == "extensions":
        from .resources import discover_extension_dir, _entries_for_dir
        return _entries_for_dir(d) or discover_extension_dir(d)
    if kind == "skills":
        if (d / "SKILL.md").is_file():
            return [d / "SKILL.md"]
        out = []
        for dp, dn, fn in os.walk(d):
            dn[:] = [x for x in dn if x not in ("node_modules", ".git")]
            if "SKILL.md" in fn:
                out.append(Path(dp) / "SKILL.md")
                dn[:] = []
        return out
    suffix = ".md" if kind == "prompts" else ".json"
    return sorted(p for p in d.glob(f"*{suffix}") if p.is_file())


def package_resources(d: Path, filters: Optional[Dict[str, Any]] = None) -> Dict[str, List[Path]]:
    filters = filters or {}
    out: Dict[str, List[Path]] = {k: [] for k in RESOURCE_TYPES}
    if d.is_file():  # a file path loads one extension
        out["extensions"] = [d]
        return out
    manifest = (_read(d / "package.json").get("pi") or _read(d / "package.json").get("omp") or {}) if (d / "package.json").exists() else {}
    for kind in RESOURCE_TYPES:
        decl = manifest.get(kind)
        if isinstance(decl, str):
            decl = [decl]
        if isinstance(decl, list):
            found = _expand_patterns(d, decl, kind)
        else:
            conv = d / kind
            found = _walk(conv, kind) if conv.is_dir() else []
            if kind == "extensions" and not found and not manifest and not any((d / k).is_dir() for k in RESOURCE_TYPES):
                found = [p for p in (d / n for n in ("index.ts", "index.js", "index.mjs")) if p.is_file()][:1]
        f = filters.get(kind)
        if f is not None:
            found = _apply_filter(d, found, f)
        out[kind] = [p.resolve() for p in found]
    if out["skills"]:
        out["skills"] = [p if p.name == "SKILL.md" or p.suffix == ".md" else p / "SKILL.md" for p in out["skills"]]
    return out


def _apply_filter(base: Path, found: List[Path], flt: List[str]) -> List[Path]:
    if flt == []:
        return []
    rels = {p: _rel(p, base) for p in found}
    pos = [x for x in flt if isinstance(x, str) and not x.startswith(("!", "+", "-"))]
    keep = [p for p in found if not pos or any(fnmatch.fnmatch(r, x.lstrip("./")) for x in pos for r in [rels[p]])]
    for x in flt:
        if not isinstance(x, str):
            continue
        if x.startswith("!"):
            keep = [p for p in keep if not fnmatch.fnmatch(rels[p], x[1:].lstrip("./"))]
        elif x.startswith("-"):
            keep = [p for p in keep if rels[p] != x[1:].lstrip("./")]
        elif x.startswith("+"):
            p = next((q for q in found if rels[q] == x[1:].lstrip("./")), None)
            if p and p not in keep:
                keep.append(p)
    return keep


def collect(agent_dir: Path, cwd: str, trusted: bool, diagnostics: List[str], auto_install: bool = True,
            temporary: Optional[List[str]] = None) -> Dict[str, List[Path]]:
    """All resources of configured (and -e temporary) packages; missing installs are attempted once and skipped on failure."""
    out: Dict[str, List[Path]] = {k: [] for k in RESOURCE_TYPES}
    items = configured_packages(agent_dir, cwd, trusted)
    for raw in temporary or []:
        src = parse_source(raw, cwd)
        items.append(Configured(raw, src, "temporary", install_root(agent_dir, cwd, False, temporary=True)))
    for c in items:
        d = c.dir
        try:
            if not d.exists() or (c.scope == "temporary" and c.source.kind != "local" and not c.source.pinned):
                if not auto_install and c.scope != "temporary":
                    diagnostics.append(f"package {c.source.raw} is not installed (run: omni install {c.source.raw})")
                    continue
                d = install_source(c.source, c.root)
        except PackageError as e:
            if d.exists() and c.scope == "temporary":
                diagnostics.append(f"package {c.source.raw}: update check failed, using the cached copy ({e})")
            else:
                diagnostics.append(f"package {c.source.raw} failed to install; its resources are skipped: {e}")
                continue
        res = package_resources(d, c.filters)
        for k in RESOURCE_TYPES:
            out[k].extend(p for p in res[k] if p not in out[k])
    return out


def is_package_spec(s: str) -> bool:
    return s.startswith(("npm:", "git:", "git@")) or bool(re.match(r"^(https?|ssh)://", s))


# ------------------------------------------------------------------------------------------------ CLI
def cli(argv: List[str], agent_dir: Path, cwd: str) -> int:
    from . import safe_stdio
    safe_stdio()
    cmd = argv[0]
    rest = argv[1:]
    local = any(a in ("-l", "--local") for a in rest)
    args = [a for a in rest if a not in ("-l", "--local")]
    if cmd == "install":
        if not args:
            print("usage: omni install <npm:pkg[@ver] | git:host/path[@ref] | ./path> [-l]")
            return 2
        rc = 0
        for raw in args:
            src = parse_source(raw, cwd)
            try:
                d = install_source(src, install_root(agent_dir, cwd, local))
                path = add_to_settings(agent_dir, cwd, src, local)
                res = package_resources(d)
                summary = ", ".join(f"{len(v)} {k}" for k, v in res.items() if v) or "no resources found"
                print(f"✓ installed {raw} ({summary}) → {path}")
            except PackageError as e:
                print(f"✗ {raw}: {e}")
                rc = 1
        return rc
    if cmd in ("remove", "uninstall"):
        rc = 0
        for raw in args:
            src = parse_source(raw, cwd)
            removed = remove_from_settings(agent_dir, cwd, raw, local)
            d = package_dir(src, install_root(agent_dir, cwd, local))
            if src.kind == "npm" and (install_root(agent_dir, cwd, local) / "npm" / "package.json").exists():
                try:
                    _run(["npm", "uninstall", src.name, "--no-audit", "--no-fund"], cwd=install_root(agent_dir, cwd, local) / "npm")
                except PackageError:
                    pass
            elif src.kind == "git" and d.exists():
                shutil.rmtree(d, ignore_errors=True)
            print(f"{'✓ removed' if removed else '· not configured:'} {raw}")
            rc |= 0 if removed else 1
        return rc
    if cmd == "list":
        items = configured_packages(agent_dir, cwd, True)
        if not items:
            print("No packages configured. Install one with: omni install npm:<package>")
            return 0
        for c in items:
            d = c.dir
            state = "installed" if d.exists() else "missing"
            res = package_resources(d, c.filters) if d.exists() else {k: [] for k in RESOURCE_TYPES}
            summary = ", ".join(f"{len(v)} {k}" for k, v in res.items() if v)
            print(f"  {c.source.raw:<48} {c.scope:<8} {state:<10} {summary}")
            print(f"      {d}")
        return 0
    if cmd == "update":
        only_ext = "--extensions" in args
        targets = [a for a in args if not a.startswith("--")]
        rc = 0
        for c in configured_packages(agent_dir, cwd, True):
            if targets and not any(parse_source(t, cwd).identity == c.source.identity for t in targets):
                continue
            if c.source.kind == "local":
                continue
            if c.source.kind == "npm" and c.source.pinned and c.dir.exists() and not only_ext:
                print(f"· {c.source.raw} is pinned")
                continue
            try:
                install_source(c.source, c.root)
                print(f"✓ updated {c.source.raw}")
            except PackageError as e:
                print(f"✗ {c.source.raw}: {e}")
                rc = 1
        return rc
    print(f"unknown package command {cmd}")
    return 2
