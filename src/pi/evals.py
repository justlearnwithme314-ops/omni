"""Evaluations: run the same prompts under different profiles or models and compare (Langfuse datasets / evals).

A dataset is a JSONL file, one case per line:

    {"id": "sum", "prompt": "What is 17 * 23? Reply with the number only.", "expect": "391"}
    {"id": "readme", "prompt": "Which license does this repo use?", "expect": {"contains": ["MIT"], "not_contains": ["GPL"]}}
    {"id": "date", "prompt": "Print today's ISO date.", "expect": {"regex": "\\\\d{4}-\\\\d{2}-\\\\d{2}"}}

`expect` may be a string (must appear in the answer, case-insensitive), or an object with `contains`,
`not_contains` (lists), `regex`, and `tools` (tool names that must have been called). A case with no `expect`
always passes and is there for its cost and timing.

    omni eval cases.jsonl                                   the active profile
    omni eval cases.jsonl --profiles core,coder,full        compare profiles
    omni eval cases.jsonl --models anthropic/claude-haiku-5-5,anthropic/claude-sonnet-5-5

Each case runs in a fresh headless session in the current folder (no session file, project trusted). The report
shows pass rate, tokens, cost and time per variant; the full results are saved to `<agent-dir>/evals/`.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def load_dataset(path: str) -> List[Dict[str, Any]]:
    cases = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                c = json.loads(line)
            except ValueError as e:
                raise ValueError(f"{path}:{n}: not valid JSON ({e})") from None
            if not isinstance(c, dict) or not str(c.get("prompt") or "").strip():
                raise ValueError(f"{path}:{n}: each case needs a `prompt`")
            c.setdefault("id", f"case-{n}")
            cases.append(c)
    if not cases:
        raise ValueError(f"{path}: no cases")
    return cases


def check(expect: Any, answer: str, tools_used: List[str]) -> Tuple[bool, str]:
    """(passed, why not)."""
    if expect is None:
        return True, ""
    if isinstance(expect, str):
        expect = {"contains": [expect]}
    if not isinstance(expect, dict):
        return False, "expect must be a string or an object"
    low = answer.lower()
    for s in expect.get("contains") or []:
        if str(s).lower() not in low:
            return False, f"missing '{s}'"
    for s in expect.get("not_contains") or []:
        if str(s).lower() in low:
            return False, f"contains '{s}'"
    if expect.get("regex"):
        try:
            if not re.search(str(expect["regex"]), answer, re.S):
                return False, f"does not match /{expect['regex']}/"
        except re.error as e:
            return False, f"bad regex: {e}"
    for t in expect.get("tools") or []:
        if t not in tools_used:
            return False, f"did not call {t}"
    return True, ""


def run_case(agent_dir: Path, case: Dict[str, Any], profile: Optional[str], model: Optional[str], cwd: str) -> Dict[str, Any]:
    from .agent import AgentSession, SessionOptions
    from .modes import HeadlessUI
    started = time.time()
    out: Dict[str, Any] = {"id": case["id"], "passed": False, "why": "", "answer": "", "seconds": 0.0, "input": 0, "output": 0,
                           "cost": 0.0, "tools": [], "turns": 0}
    s = None
    try:
        s = AgentSession(SessionOptions(cwd=cwd, mode="print", profile=profile, model=model, no_session=True, approve=True,
                                        agent_dir=str(agent_dir)), ui=HeadlessUI())
        s.start()
        s.prompt(str(case["prompt"]), source="eval")
        msgs = [e["message"] for e in s.sessions.get_branch() if e.get("type") == "message"]
        assistants = [m for m in msgs if m.get("role") == "assistant"]
        for m in assistants:
            u = m.get("usage") or {}
            out["input"] += (u.get("input") or 0) + (u.get("cacheRead") or 0)
            out["output"] += u.get("output") or 0
            out["cost"] += float((u.get("cost") or {}).get("total") or 0.0)
            out["tools"] += [b.get("name") for b in m.get("content") or [] if b.get("type") == "toolCall"]
        out["turns"] = len(assistants)
        last = assistants[-1] if assistants else None
        if last is None:
            out["why"] = "no answer"
        elif last.get("stopReason") in ("error", "aborted"):
            out["why"] = f"{last.get('stopReason')}: {last.get('errorMessage') or ''}".strip()
        else:
            out["answer"] = "".join(b.get("text", "") for b in last.get("content") or [] if b.get("type") == "text").strip()
            out["passed"], out["why"] = check(case.get("expect"), out["answer"], out["tools"])
    except Exception as e:
        out["why"] = f"{type(e).__name__}: {e}"
    finally:
        if s is not None:
            try:
                s.shutdown()
            except Exception:
                pass
    out["seconds"] = round(time.time() - started, 2)
    return out


def run(agent_dir: Path, dataset: str, profiles: Optional[List[str]] = None, models: Optional[List[str]] = None,
        cwd: Optional[str] = None, on_case: Any = None) -> Dict[str, Any]:
    cases = load_dataset(dataset)
    cwd = cwd or os.getcwd()
    variants = [(p, m) for p in (profiles or [None]) for m in (models or [None])]
    report: Dict[str, Any] = {"dataset": os.path.abspath(dataset), "started": time.time(), "cases": len(cases), "variants": []}
    for profile, model in variants:
        label = " · ".join(x for x in (profile, model) if x) or "active profile"
        results = []
        for case in cases:
            r = run_case(agent_dir, case, profile, model, cwd)
            results.append(r)
            if on_case:
                on_case(label, r)
        report["variants"].append({
            "label": label, "profile": profile, "model": model, "results": results,
            "passed": sum(1 for r in results if r["passed"]), "input": sum(r["input"] for r in results),
            "output": sum(r["output"] for r in results), "cost": round(sum(r["cost"] for r in results), 6),
            "seconds": round(sum(r["seconds"] for r in results), 2)})
    d = Path(agent_dir) / "evals"
    d.mkdir(parents=True, exist_ok=True)
    report["file"] = str(d / (time.strftime("%Y%m%d-%H%M%S") + ".json"))
    Path(report["file"]).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def format_report(report: Dict[str, Any]) -> str:
    n = report["cases"]
    width = max(14, max(len(v["label"]) for v in report["variants"]))
    rows = [f"{'variant':<{width}}  passed   tokens in/out      cost    time", "-" * (width + 46)]
    for v in report["variants"]:
        rows.append(f"{v['label']:<{width}}  {v['passed']:>2}/{n:<3}  {v['input']:>8}/{v['output']:<7} ${v['cost']:<7.4f} {v['seconds']:>6.1f}s")
    for v in report["variants"]:
        failed = [r for r in v["results"] if not r["passed"]]
        if failed:
            rows.append(f"\n{v['label']} — failed:")
            rows += [f"  {r['id']}: {r['why']}" + (f"  (answer: {r['answer'][:80]!r})" if r["answer"] else "") for r in failed]
    rows.append(f"\nFull results: {report['file']}")
    return "\n".join(rows)


def cli(argv: List[str], agent_dir: Path) -> int:
    if not argv or argv[0].startswith("-"):
        print("usage: omni eval <cases.jsonl> [--profiles a,b] [--models x,y]")
        return 2
    opts = {"--profiles": "", "--models": ""}
    rest = argv[1:]
    for i in range(0, len(rest) - 1, 2):
        if rest[i] not in opts:
            print(f"unknown option {rest[i]}")
            return 2
        opts[rest[i]] = rest[i + 1]
    split = lambda v: [x.strip() for x in v.split(",") if x.strip()] or None  # noqa: E731
    try:
        report = run(agent_dir, argv[0], split(opts["--profiles"]), split(opts["--models"]),
                     on_case=lambda label, r: print(f"  {'✓' if r['passed'] else '✗'} {label} · {r['id']} ({r['seconds']}s)", flush=True))
    except (OSError, ValueError) as e:
        print(e)
        return 2
    print()
    print(format_report(report))
    return 0 if all(v["passed"] == report["cases"] for v in report["variants"]) else 1
