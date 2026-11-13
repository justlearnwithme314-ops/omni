"""Usage report from the local traces (Langfuse's dashboards, as one page).

`tracing` writes every run to `<agent-dir>/traces/`. This module adds them up:

    omni usage                    last 7 days: runs, tokens, cost per day; by model; by tool; ratings
    omni usage --days 30
    omni usage --html report.html  the same as a self-contained HTML page
    /usage                        in a session

It reads only local files and sends nothing anywhere.
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_traces(agent_dir: Path, days: float = 7.0, now: Optional[float] = None) -> Dict[str, List[Dict[str, Any]]]:
    now = time.time() if now is None else now
    since = now - days * 86400
    traces, scores = [], []
    d = Path(agent_dir) / "traces"
    for f in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
        try:
            if f.stat().st_mtime < since:
                continue
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") == "score":
                    if o.get("at", 0) >= since:
                        scores.append(o)
                elif isinstance(o.get("observations"), list) and o.get("start", 0) >= since:
                    traces.append(o)
        except OSError:
            continue
    return {"traces": traces, "scores": scores}


def summarize(agent_dir: Path, days: float = 7.0, now: Optional[float] = None) -> Dict[str, Any]:
    data = load_traces(agent_dir, days, now)
    by_day: Dict[str, Dict[str, float]] = {}
    by_model: Dict[str, Dict[str, float]] = {}
    by_tool: Dict[str, Dict[str, float]] = {}
    total = {"runs": 0, "input": 0, "output": 0, "cached": 0, "cost": 0.0, "seconds": 0.0, "errors": 0}
    for tr in data["traces"]:
        day = time.strftime("%Y-%m-%d", time.localtime(tr["start"]))
        dd = by_day.setdefault(day, {"runs": 0, "input": 0, "output": 0, "cost": 0.0})
        dd["runs"] += 1
        total["runs"] += 1
        total["seconds"] += max(0.0, float(tr.get("end", tr["start"])) - float(tr["start"]))
        if tr.get("outcome") not in (None, "completed"):
            total["errors"] += 1
        for o in tr["observations"]:
            dur = max(0.0, float(o.get("end", 0)) - float(o.get("start", 0)))
            if o.get("type") == "generation":
                u = o.get("usage") or {}
                tin, tout = int(u.get("input", 0)) + int(u.get("cacheRead", 0)), int(u.get("output", 0))
                cost = float(o.get("cost") or 0.0)
                m = by_model.setdefault(str(o.get("name") or "?"), {"calls": 0, "input": 0, "output": 0, "cost": 0.0, "seconds": 0.0})
                m["calls"] += 1
                m["input"] += tin
                m["output"] += tout
                m["cost"] += cost
                m["seconds"] += dur
                dd["input"] += tin
                dd["output"] += tout
                dd["cost"] += cost
                total["input"] += tin
                total["output"] += tout
                total["cached"] += int(u.get("cacheRead", 0))
                total["cost"] += cost
            else:
                t = by_tool.setdefault(str(o.get("name") or "?").replace("tool:", ""), {"calls": 0, "errors": 0, "seconds": 0.0})
                t["calls"] += 1
                t["seconds"] += dur
                t["errors"] += 1 if o.get("error") else 0
    values = [float(s.get("value", 0)) for s in data["scores"]]
    return {"days": days, "total": total, "by_day": dict(sorted(by_day.items())),
            "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["cost"] or -kv[1]["calls"])),
            "by_tool": dict(sorted(by_tool.items(), key=lambda kv: -kv[1]["seconds"])),
            "ratings": {"count": len(values), "average": round(sum(values) / len(values), 2) if values else None}}


def format_text(s: Dict[str, Any]) -> str:
    t = s["total"]
    if not t["runs"]:
        return (f"No traced runs in the last {s['days']:g} day(s). Turn on the `tracing` capability "
                "(/capabilities tracing on) and work for a while.")
    rows = [f"Last {s['days']:g} day(s): {t['runs']} runs · {t['input']:,} tokens in ({t['cached']:,} cached) · {t['output']:,} out · "
            f"${t['cost']:.2f} · {t['seconds'] / 60:.1f} min" + (f" · {t['errors']} did not complete" if t["errors"] else ""), ""]
    rows.append("Per day")
    for day, d in s["by_day"].items():
        rows.append(f"  {day}  {int(d['runs']):>4} runs  {int(d['input']):>10,} in  {int(d['output']):>8,} out  ${d['cost']:.2f}")
    rows += ["", "By model"]
    for name, m in list(s["by_model"].items())[:8]:
        rows.append(f"  {name:<40} {int(m['calls']):>4} calls  {int(m['input']):>10,} in  {int(m['output']):>8,} out  ${m['cost']:.2f}  "
                    f"{m['seconds'] / max(1, m['calls']):.1f}s avg")
    if s["by_tool"]:
        rows += ["", "By tool (slowest first)"]
        for name, x in list(s["by_tool"].items())[:10]:
            rows.append(f"  {name:<24} {int(x['calls']):>4} calls  {x['seconds']:>7.1f}s total" + (f"  {int(x['errors'])} errors" if x["errors"] else ""))
    if s["ratings"]["count"]:
        rows += ["", f"Your ratings: {s['ratings']['count']} · average {s['ratings']['average']}"]
    return "\n".join(rows)


def format_html(s: Dict[str, Any]) -> str:
    t = s["total"]
    esc = html.escape

    def table(title: str, head: List[str], body: List[List[Any]]) -> str:
        if not body:
            return ""
        th = "".join(f"<th>{esc(h)}</th>" for h in head)
        trs = "".join("<tr>" + "".join(f"<td>{esc(str(c))}</td>" for c in r) + "</tr>" for r in body)
        return f"<h2>{esc(title)}</h2><table><tr>{th}</tr>{trs}</table>"
    peak = max((d["cost"] for d in s["by_day"].values()), default=0) or 1
    bars = "".join(f"<div class=bar><span>{esc(day[5:])}</span><i style='width:{max(2, int(260 * d['cost'] / peak))}px'></i>"
                   f"<b>${d['cost']:.2f} · {int(d['runs'])} runs</b></div>" for day, d in s["by_day"].items())
    return (
        "<!doctype html><meta charset=utf-8><title>Omni usage</title><style>body{font-family:system-ui;background:#0b0f17;color:#e5e7eb;"
        "max-width:900px;margin:32px auto;padding:0 16px}h1{font-size:22px}h2{font-size:15px;color:#9ca3af;margin-top:28px}"
        "table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:5px 10px;border-bottom:1px solid #1f2937;font-size:13px}"
        ".cards{display:flex;gap:12px;flex-wrap:wrap}.card{background:#111827;border-radius:8px;padding:12px 16px}.card b{display:block;font-size:20px}"
        ".bar{display:flex;align-items:center;gap:8px;font-size:12px;margin:3px 0}.bar span{width:44px;color:#9ca3af}.bar i{display:block;height:12px;"
        "background:#7aa2ff;border-radius:3px}.bar b{font-weight:400;color:#9ca3af}</style>"
        f"<h1>Omni usage — last {s['days']:g} day(s)</h1><div class=cards>"
        f"<div class=card>runs<b>{t['runs']}</b></div><div class=card>cost<b>${t['cost']:.2f}</b></div>"
        f"<div class=card>tokens in<b>{t['input']:,}</b></div><div class=card>tokens out<b>{t['output']:,}</b></div>"
        f"<div class=card>time<b>{t['seconds'] / 60:.1f} min</b></div>"
        + (f"<div class=card>rating<b>{s['ratings']['average']}</b></div>" if s["ratings"]["count"] else "") + "</div>"
        + (f"<h2>Cost per day</h2>{bars}" if bars else "")
        + table("By model", ["model", "calls", "tokens in", "tokens out", "cost", "avg seconds"],
                [[n, int(m["calls"]), f"{int(m['input']):,}", f"{int(m['output']):,}", f"${m['cost']:.2f}", f"{m['seconds'] / max(1, m['calls']):.1f}"]
                 for n, m in s["by_model"].items()])
        + table("By tool", ["tool", "calls", "seconds", "errors"],
                [[n, int(x["calls"]), f"{x['seconds']:.1f}", int(x["errors"])] for n, x in s["by_tool"].items()]))


def cli(argv: List[str], agent_dir: Path) -> int:
    days, out = 7.0, None
    i = 0
    while i < len(argv):
        if argv[i] == "--days" and i + 1 < len(argv):
            try:
                days = float(argv[i + 1])
            except ValueError:
                print("--days needs a number")
                return 2
            i += 2
        elif argv[i] == "--html" and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
        else:
            print("usage: omni usage [--days N] [--html report.html]")
            return 2
    s = summarize(agent_dir, days)
    if out:
        Path(out).write_text(format_html(s), encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(format_text(s))
    return 0
