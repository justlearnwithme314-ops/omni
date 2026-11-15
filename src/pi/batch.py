"""`omni batch` and `omni update`.

`omni batch` (Hermes' batch runner) answers many prompts without a window, several at a time:

    omni batch prompts.jsonl [--out results.jsonl] [--parallel 4] [--profile NAME] [--model PATTERN] [--resume]

The input has one JSON object per line: `{"prompt": "...", "id": "optional", "cwd": "optional folder"}`; a plain
line of text is taken as a prompt. Each prompt gets its own fresh session (nothing is shared or saved), and one
result line is written as soon as it finishes: `{"id", "prompt", "answer", "error", "seconds", "cost", "tool_calls"}`.
`--resume` skips ids already present in the output file, so an interrupted run can be continued. The exit code
is 0 when every prompt was answered and 1 when any failed. Cost adds up: every line is a full agent run.

`omni update` updates Omni itself when it was installed from git: it fast-forwards the checkout it runs from and
refuses if you have local changes or the branch has diverged.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def read_items(path: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        item: Any = None
        if line.startswith("{"):
            try:
                item = json.loads(line)
            except ValueError as e:
                raise ValueError(f"line {n}: not valid JSON ({e})") from None
        if not isinstance(item, dict):
            item = {"prompt": line}
        if not str(item.get("prompt") or "").strip():
            raise ValueError(f"line {n}: no `prompt`")
        item.setdefault("id", str(len(items) + 1))
        item["id"] = str(item["id"])
        items.append(item)
    return items


def run_one(item: Dict[str, Any], make_session: Callable[..., Any]) -> Dict[str, Any]:
    from .commands import last_assistant_text
    started = time.time()
    out: Dict[str, Any] = {"id": item["id"], "prompt": item["prompt"], "answer": "", "error": None}
    session = None
    try:
        session = make_session(item.get("cwd"))
        session.prompt(str(item["prompt"]))
        out["answer"] = last_assistant_text(session)
        msgs = [e["message"] for e in session.sessions.entries if e.get("type") == "message"]
        last = next((m for m in reversed(msgs) if m.get("role") == "assistant"), None)
        if last is None:
            out["error"] = "no answer"
        elif last.get("stopReason") in ("error", "aborted"):
            out["error"] = last.get("errorMessage") or last.get("stopReason")
        totals = session.totals()
        out["cost"], out["tool_calls"] = round(float(totals.get("cost") or 0), 6), int(totals.get("toolCalls") or 0)
    except BaseException as e:   # one bad item must not stop the batch (SystemExit from option errors included)
        if isinstance(e, KeyboardInterrupt):
            raise
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if session is not None:
            try:
                session.shutdown()
            except Exception:
                pass
    out["seconds"] = round(time.time() - started, 2)
    return out


def run_batch(items: List[Dict[str, Any]], out_path: Path, make_session: Callable[..., Any], parallel: int = 2,
              resume: bool = False, progress: Optional[Callable[[Dict[str, Any], int, int], None]] = None) -> Dict[str, int]:
    done_ids = set()
    if resume and out_path.is_file():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if not row.get("error"):
                    done_ids.add(str(row.get("id")))
            except ValueError:
                pass
    todo = [i for i in items if i["id"] not in done_ids]
    lock = threading.Lock()
    counts = {"total": len(items), "skipped": len(items) - len(todo), "ok": 0, "failed": 0}
    position = {"next": 0, "finished": 0}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a" if resume else "w", encoding="utf-8") as fh:
        def worker() -> None:
            while True:
                with lock:
                    if position["next"] >= len(todo):
                        return
                    item = todo[position["next"]]
                    position["next"] += 1
                row = run_one(item, make_session)
                with lock:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
                    counts["failed" if row["error"] else "ok"] += 1
                    position["finished"] += 1
                    if progress:
                        progress(row, position["finished"], len(todo))
        threads = [threading.Thread(target=worker, daemon=True, name=f"omni-batch-{n}") for n in range(max(1, min(parallel, 16, len(todo) or 1)))]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(0.5)
    return counts


def cli(argv: List[str], make_session: Callable[..., Any]) -> int:
    """`make_session(profile, model, cwd)` returns a started headless session that is not saved."""
    src, out, parallel, profile, model, resume = None, None, 2, None, None, False
    it = iter(argv)
    for a in it:
        try:
            if a == "--out":
                out = next(it)
            elif a == "--parallel":
                parallel = int(next(it))
            elif a == "--profile":
                profile = next(it)
            elif a == "--model":
                model = next(it)
            elif a == "--resume":
                resume = True
            elif a in ("-h", "--help"):
                print("usage: omni batch FILE.jsonl [--out results.jsonl] [--parallel N] [--profile NAME] [--model PATTERN] [--resume]")
                return 0
            elif a.startswith("-"):
                print(f"omni batch: unknown option {a}")
                return 2
            elif src is None:
                src = a
            else:
                print("omni batch: one input file only")
                return 2
        except (StopIteration, ValueError):
            print(f"omni batch: {a} needs a value")
            return 2
    if src is None:
        print("usage: omni batch FILE.jsonl [--out results.jsonl] [--parallel N] [--profile NAME] [--model PATTERN] [--resume]")
        return 2
    path = Path(src)
    try:
        items = read_items(path)
    except (OSError, ValueError) as e:
        print(f"omni batch: {e}")
        return 2
    if not items:
        print("omni batch: the file has no prompts")
        return 2
    if len({i["id"] for i in items}) != len(items):
        print("omni batch: ids must be unique")
        return 2
    out_path = Path(out) if out else path.with_name(path.stem + ".results.jsonl")

    def show(row: Dict[str, Any], n: int, total: int) -> None:
        mark = "x" if row["error"] else "ok"
        print(f"  [{n}/{total}] {mark:<2} {row['id']}  {row['seconds']}s" + (f"  {row['error']}" if row["error"] else ""), flush=True)

    print(f"  {len(items)} prompt(s), {max(1, parallel)} at a time -> {out_path}")
    try:
        counts = run_batch(items, out_path, lambda cwd=None: make_session(profile, model, cwd), parallel, resume, show)
    except KeyboardInterrupt:
        print("  Interrupted. Finished results are saved; continue with --resume.")
        return 130
    print(f"  done: {counts['ok']} answered, {counts['failed']} failed" + (f", {counts['skipped']} already done" if counts["skipped"] else ""))
    return 1 if counts["failed"] else 0


# ------------------------------------------------------------------------------------------- update
def update(root: Path, run: Callable[..., Any] = subprocess.run) -> int:
    def git(*args: str) -> Any:
        return run(["git", *args], cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    try:
        if git("rev-parse", "--is-inside-work-tree").returncode != 0:
            print(f"  Omni at {root} is not a git checkout; update it the way you installed it.")
            return 1
        dirty = [l for l in git("status", "--porcelain", "--untracked-files=no").stdout.splitlines()
                 if l.strip() and not l.strip().endswith((".pyc", ".sqlite"))]
        if dirty:
            print("  You have local changes in the Omni checkout; commit or stash them first:\n" + "\n".join(f"    {l}" for l in dirty[:10]))
            return 1
        before = git("rev-parse", "--short", "HEAD").stdout.strip()
        fetch = git("fetch", "--quiet")
        if fetch.returncode != 0:
            print(f"  Could not reach the remote: {(fetch.stderr or '').strip()[:300]}")
            return 1
        pull = git("merge", "--ff-only", "@{u}")
        if pull.returncode != 0:
            print(f"  Could not fast-forward (the branch has diverged, or has no upstream): {(pull.stderr or pull.stdout).strip()[:300]}")
            return 1
        after = git("rev-parse", "--short", "HEAD").stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  git failed: {e}")
        return 1
    if before == after:
        print(f"  Omni is up to date ({after}).")
    else:
        log = git("log", "--oneline", f"{before}..{after}").stdout.strip().splitlines()
        print(f"  Updated {before} -> {after} ({len(log)} commit(s)):\n" + "\n".join(f"    {l}" for l in log[:15]))
    return 0
