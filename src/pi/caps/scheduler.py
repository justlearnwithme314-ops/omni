"""`scheduler`: prompts that run on a schedule (Hermes `cronjob`, OpenClaw cron / heartbeats).

A job is a prompt, a schedule and a working directory. When it is due, a fresh headless session runs the prompt
(same model and profile as when the job was created, minus this capability, so jobs cannot schedule jobs) and the
answer is saved to `<agent-dir>/cron/output/<job-id>/<time>.md`.

Schedules:   `every 30m` · `every 2h` · `every 1d`      interval
             `0 9 * * 1-5`                              cron (minute hour day month weekday; lists, ranges, steps)
             `in 20m` · `2026-11-14T09:00`              once

Two special kinds:

    heartbeat    (OpenClaw heartbeats) a recurring check that stays silent unless something needs you. The run reads
                 `HEARTBEAT.md` in the job's folder if there is one, and when the model answers `HEARTBEAT_OK` nothing
                 is saved or delivered. Create with `heartbeat: true`.
    `on <name>`  (Temporal signals) a job that waits, for days if need be, until the signal arrives, then runs with
                 the signal's text added to its prompt. Send one with `omni signal <name> [text]`, the `cronjob`
                 tool (`action: signal`), or `POST /signal` on the gateway's HTTP channel. `on <name> repeat` runs
                 on every signal instead of once.

Who runs them: an open Omni session checks every 30 seconds, and `omni cron run` runs whatever is due and exits -
put that in Windows Task Scheduler or crontab to have jobs fire while no session is open. `omni cron list` shows
the jobs. In a session the model manages jobs with the `cronjob` tool; `/cron` lists them.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

TICK_SECONDS = 30
STALE_RUN_SECONDS = 3600
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_file_lock = threading.Lock()


# ------------------------------------------------------------------------------------------- schedules
def _field(spec: str, lo: int, hi: int) -> set:
    out: set = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, st = part.split("/", 1)
            step = int(st)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        if a < lo or b > hi or a > b or step < 1:
            raise ValueError(f"'{spec}' is outside {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


def parse_schedule(text: str) -> Dict[str, Any]:
    """-> {"kind": "interval", "seconds"} | {"kind": "cron", "fields"} | {"kind": "once", "at"}; raises ValueError."""
    s = (text or "").strip().lower()
    m = re.match(r"^on\s+([a-z0-9][a-z0-9_.-]{0,63})(\s+repeat)?$", s)
    if m:
        return {"kind": "signal", "name": m.group(1), "repeat": bool(m.group(2))}
    m = re.match(r"^(every|in)\s+(\d+)\s*([smhdw])[a-z]*$", s)
    if m:
        seconds = int(m.group(2)) * _UNITS[m.group(3)]
        if seconds < 60:
            raise ValueError("the shortest schedule is one minute")
        return {"kind": "interval", "seconds": seconds} if m.group(1) == "every" else {"kind": "once", "at": time.time() + seconds}
    parts = s.split()
    if len(parts) == 5:
        try:
            dow = _field(parts[4].replace("7", "0"), 0, 6)
            fields = [_field(parts[0], 0, 59), _field(parts[1], 0, 23), _field(parts[2], 1, 31), _field(parts[3], 1, 12), dow]
        except ValueError as e:
            raise ValueError(f"bad cron expression: {e}") from None
        return {"kind": "cron", "fields": [sorted(f) for f in fields], "expr": " ".join(parts)}
    try:
        return {"kind": "once", "at": datetime.fromisoformat(text.strip()).timestamp()}
    except ValueError:
        raise ValueError("schedule must be `every 30m`, `in 2h`, a 5-field cron expression, or an ISO time like 2026-11-14T09:00") from None


def next_run(schedule: Dict[str, Any], after: float) -> Optional[float]:
    kind = schedule["kind"]
    if kind == "interval":
        return after + schedule["seconds"]
    if kind == "once":
        return schedule["at"] if schedule["at"] > after else None
    if kind == "signal":
        return None  # runs when the signal arrives, not at a time
    minute, hour, dom, month, dow = (set(f) for f in schedule["fields"])
    t = datetime.fromtimestamp(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if t.month in month and t.day in dom and (t.isoweekday() % 7) in dow and t.hour in hour and t.minute in minute:
            return t.timestamp()
        if t.month not in month or t.day not in dom or (t.isoweekday() % 7) not in dow:
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
        elif t.hour not in hour:
            t = (t + timedelta(hours=1)).replace(minute=0)
        else:
            t += timedelta(minutes=1)
    return None


# ------------------------------------------------------------------------------------------- signals
HEARTBEAT_OK = "HEARTBEAT_OK"


def send_signal(agent_dir: Path, name: str, payload: str = "") -> int:
    """Queue a signal; jobs waiting `on <name>` run at the next scheduler tick. Returns how many jobs are waiting."""
    name = name.strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{0,63}$", name):
        raise ValueError("signal names use letters, numbers, '.', '_' and '-'")
    d = cron_dir(agent_dir) / "signals"
    d.mkdir(parents=True, exist_ok=True)
    with _file_lock:
        f = d / f"{name}.json"
        try:
            queue = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            queue = []
        queue.append({"at": time.time(), "payload": str(payload)[:4000]})
        f.write_text(json.dumps(queue), encoding="utf-8")
    return sum(1 for j in load_jobs(agent_dir) if j.get("enabled") and _signal_name(j) == name)


def _signal_name(job: Dict[str, Any]) -> Optional[str]:
    try:
        sched = parse_schedule(job.get("schedule", ""))
    except ValueError:
        return None
    return sched["name"] if sched["kind"] == "signal" else None


def _take_signal(agent_dir: Path, name: str) -> Optional[Dict[str, Any]]:
    f = cron_dir(agent_dir) / "signals" / f"{name}.json"
    try:
        queue = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not queue:
        return None
    first = queue.pop(0)
    f.write_text(json.dumps(queue), encoding="utf-8") if queue else f.unlink()
    return first


# ------------------------------------------------------------------------------------------- storage
def cron_dir(agent_dir: Path) -> Path:
    return Path(agent_dir) / "cron"


def load_jobs(agent_dir: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads((cron_dir(agent_dir) / "jobs.json").read_text(encoding="utf-8"))
        return [j for j in data if isinstance(j, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_jobs(agent_dir: Path, jobs: List[Dict[str, Any]]) -> None:
    d = cron_dir(agent_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "jobs.json.tmp"
    tmp.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    os.replace(tmp, d / "jobs.json")


def create_job(agent_dir: Path, name: str, prompt: str, schedule_text: str, cwd: str, profile: str = "",
               model: str = "", now: Optional[float] = None, deliver: str = "", heartbeat: bool = False) -> Dict[str, Any]:
    sched = parse_schedule(schedule_text)
    now = time.time() if now is None else now
    job = {"id": uuid.uuid4().hex[:8], "name": name.strip() or prompt.strip()[:40], "prompt": prompt, "schedule": schedule_text.strip(),
           "cwd": cwd, "profile": profile, "model": model, "enabled": True, "created": now,
           "next_run": next_run(sched, now), "last_run": None, "last_status": None, "runs": 0, "deliver": deliver, "heartbeat": bool(heartbeat)}
    with _file_lock:
        jobs = load_jobs(agent_dir)
        jobs.append(job)
        save_jobs(agent_dir, jobs)
    return job


def _when(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def format_jobs(jobs: List[Dict[str, Any]]) -> str:
    if not jobs:
        return "No scheduled jobs."
    rows = []
    for j in jobs:
        state = "on " if j.get("enabled") else "off"
        rows.append(f"{j['id']}  {state}  {j['schedule']:<16} next {_when(j.get('next_run'))}  last {_when(j.get('last_run'))} "
                    f"{j.get('last_status') or ''}  {j['name']}")
    return "\n".join(rows)


# ------------------------------------------------------------------------------------------- running
def run_job(agent_dir: Path, job: Dict[str, Any]) -> Tuple[str, str]:
    """Run one job in a fresh headless session. Returns (status, text)."""
    from ..agent import AgentSession, SessionOptions
    from ..modes import HeadlessUI
    from ..profiles import load_profile
    cwd = job.get("cwd") or os.getcwd()
    if not os.path.isdir(cwd):
        return "error", f"working directory no longer exists: {cwd}"
    from . import effective_profile
    prof = effective_profile(load_profile(job.get("profile") or None), Path(agent_dir))  # the profile plus your toggles
    caps = [c for c in prof.get("capabilities") or [] if c != "scheduler"]
    opts = SessionOptions(cwd=cwd, model=job.get("model") or None, mode="print", profile=prof["name"], capabilities=caps,
                          agent_dir=str(agent_dir), session_name=f"cron: {job['name']}", approve=True,
                          append_system_prompt="This is a scheduled, unattended run: nobody can answer questions. "
                                               "Do the task and end with a short report of what you did or found.")
    s = AgentSession(opts, ui=HeadlessUI())
    try:
        s.start()
        prompt = job["prompt"]
        if job.get("signal_payload") is not None:
            prompt += f"\n\nThe event you were waiting for has arrived" + (f": {job['signal_payload']}" if job["signal_payload"] else ".")
        if job.get("heartbeat"):
            notes = ""
            try:
                notes = (Path(cwd) / "HEARTBEAT.md").read_text(encoding="utf-8")[:6000]
            except OSError:
                pass
            prompt = ("Heartbeat check." + (f"\n\nStanding instructions (HEARTBEAT.md):\n{notes}" if notes.strip() else "") +
                      f"\n\n{prompt}\n\nIf nothing needs the user's attention right now, reply with exactly {HEARTBEAT_OK} and nothing else.")
        s.prompt(prompt, source="cron")
        msgs = [e["message"] for e in s.sessions.get_branch() if e.get("type") == "message" and e["message"].get("role") == "assistant"]
        last = msgs[-1] if msgs else None
        if last is None:
            return "error", "the model gave no answer"
        text = "".join(b.get("text", "") for b in last.get("content") or [] if b.get("type") == "text").strip()
        if last.get("stopReason") in ("error", "aborted"):
            return "error", last.get("errorMessage") or text or str(last.get("stopReason"))
        return "ok", text
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"
    finally:
        try:
            s.shutdown()
        except Exception:
            pass


def run_due(agent_dir: Path, now: Optional[float] = None, only: Optional[str] = None) -> List[Dict[str, Any]]:
    """Run every due job (or the job `only`, due or not). Safe to call from several processes."""
    now = time.time() if now is None else now
    with _file_lock:
        jobs = load_jobs(agent_dir)
        due = []
        taken: Dict[str, Any] = {}
        for j in jobs:
            running = j.get("running_since")
            if running and now - running < STALE_RUN_SECONDS:
                continue
            if (only and j["id"] == only) or (not only and j.get("enabled") and j.get("next_run") and j["next_run"] <= now):
                j["running_since"] = now
                due.append(dict(j))
            elif not only and j.get("enabled") and _signal_name(j):
                name = _signal_name(j)
                if name not in taken:  # one signal wakes every job waiting for it
                    taken[name] = _take_signal(agent_dir, name)
                sig = taken[name]
                if sig is not None:
                    j["running_since"] = now
                    due.append(dict(j, signal_payload=sig.get("payload", "")))
        if due:
            save_jobs(agent_dir, jobs)
    results = []
    for job in due:
        started = time.time()
        status, text = run_job(agent_dir, job)
        quiet = bool(job.get("heartbeat")) and status == "ok" and HEARTBEAT_OK in text and len(text.strip()) < 200
        if quiet:
            status = "idle"  # a heartbeat with nothing to report leaves no file and sends nothing
        out_dir = cron_dir(agent_dir) / "output" / job["id"]
        out_file = out_dir / (time.strftime("%Y%m%d-%H%M%S", time.localtime(started)) + ".md")
        if not quiet:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file.write_text(f"# {job['name']}\n\n- schedule: {job['schedule']}\n- ran: {_when(started)}\n- status: {status}\n\n{text}\n",
                                encoding="utf-8")
        with _file_lock:
            jobs = load_jobs(agent_dir)
            for j in jobs:
                if j["id"] != job["id"]:
                    continue
                j.pop("running_since", None)
                j.update(last_run=started, last_status=status, runs=int(j.get("runs") or 0) + 1)
                try:
                    j["next_run"] = next_run(parse_schedule(j["schedule"]), max(now, started)) if not only or j.get("enabled") else j.get("next_run")
                except ValueError:
                    j["next_run"] = None
                kind = parse_schedule(j["schedule"])
                if kind["kind"] == "once" or (kind["kind"] == "signal" and not kind.get("repeat") and "signal_payload" in job):
                    j["enabled"], j["next_run"] = False, None
            save_jobs(agent_dir, jobs)
        undelivered = None
        if job.get("deliver") and not quiet:  # send the result to the chat the job belongs to (gateway channels)
            from ..gateway import deliver as send_to_chat
            undelivered = send_to_chat(agent_dir, job["deliver"], f"⏰ {job['name']} ({status})\n\n{text[:3500]}")
        results.append({"id": job["id"], "name": job["name"], "status": status, "text": text, "file": "" if quiet else str(out_file),
                        "delivered": bool(job.get("deliver")) and undelivered is None, "delivery_error": undelivered})
    return results


# ------------------------------------------------------------------------------------------- capability
def _tools(session: Any) -> Dict[str, ToolDef]:
    agent = Path(session.agent_dir)

    def find(job_id: str) -> Dict[str, Any]:
        j = next((j for j in load_jobs(agent) if j["id"] == job_id or j["name"] == job_id), None)
        if j is None:
            raise ToolError(f"no job '{job_id}'. Jobs:\n{format_jobs(load_jobs(agent))}")
        return j

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        action = str(p.get("action") or "list").lower()
        if action == "list":
            return text_result(format_jobs(load_jobs(agent)), {"jobs": len(load_jobs(agent))})
        if action == "create":
            prompt, schedule = str(p.get("prompt") or "").strip(), str(p.get("schedule") or "").strip()
            if not prompt or not schedule:
                raise ToolError("create needs prompt (a self-contained task) and schedule (`every 2h`, `0 9 * * 1-5`, `in 20m`)")
            from ..ai.models import model_key
            try:
                job = create_job(agent, str(p.get("name") or ""), prompt, schedule, session.cwd,
                                 str(session.base_profile.get("name") or ""), model_key(session.model) if session.model else "",
                                 deliver=str(p.get("deliver") or getattr(session, "gateway_target", "") or ""),
                                 heartbeat=bool(p.get("heartbeat")))
            except ValueError as e:
                raise ToolError(str(e)) from None
            return text_result(f"Scheduled job {job['id']} \"{job['name']}\" ({job['schedule']}); next run {_when(job['next_run'])}.",
                               {"id": job["id"], "next_run": job["next_run"]})
        if action == "signal":
            try:
                waiting = send_signal(agent, str(p.get("name") or p.get("id") or ""), str(p.get("payload") or ""))
            except ValueError as e:
                raise ToolError(str(e)) from None
            return text_result(f"Signal sent; {waiting} job(s) are waiting for it.", {"waiting": waiting})
        job = find(str(p.get("id") or p.get("name") or ""))
        if action == "run":
            r = run_due(agent, only=job["id"])
            if not r:
                raise ToolError("the job is already running")
            return text_result(f"Ran {job['name']}: {r[0]['status']}\n\n{r[0]['text'][:4000]}", {"file": r[0]["file"]}, r[0]["status"] != "ok")
        with _file_lock:
            jobs = load_jobs(agent)
            if action == "remove":
                jobs = [j for j in jobs if j["id"] != job["id"]]
                msg = f"Removed job {job['id']} \"{job['name']}\"."
            elif action in ("pause", "resume"):
                for j in jobs:
                    if j["id"] == job["id"]:
                        j["enabled"] = action == "resume"
                        if j["enabled"]:
                            j["next_run"] = next_run(parse_schedule(j["schedule"]), time.time())
                msg = f"Job {job['id']} is now {'on' if action == 'resume' else 'off'}."
            else:
                raise ToolError("action must be create, list, run, pause, resume or remove")
            save_jobs(agent, jobs)
        return text_result(msg, {"id": job["id"]})

    return {"cronjob": ToolDef(
        name="cronjob", label="Schedule",
        description="Schedule a prompt to run later or repeatedly in a fresh unattended session. Actions: create "
                    "(prompt, schedule, name?), list, run (id), pause (id), resume (id), remove (id). Schedules: "
                    "`every 30m`, `every 1d`, cron `0 9 * * 1-5`, `in 20m`, an ISO time, or `on <signal>` to wait for an "
                    "event (send it with action=signal, name, payload). The prompt must be "
                    "self-contained: the scheduled run does not see this conversation.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "list", "run", "pause", "resume", "remove", "signal"]},
            "prompt": {"type": "string"}, "schedule": {"type": "string"}, "name": {"type": "string"}, "id": {"type": "string"},
            "deliver": {"type": "string", "description": "send results to a chat, e.g. telegram:123456 (optional)"},
            "heartbeat": {"type": "boolean", "description": "a recurring check that reports only when something needs attention"},
            "payload": {"type": "string", "description": "text for action=signal"}},
            "required": ["action"]},
        execute=execute, prompt_snippet="Schedule prompts to run later or repeatedly")}


def _start_ticker(session: Any) -> str:
    _unload(session)
    stop = threading.Event()
    session._cron_stop = stop
    agent = Path(session.agent_dir)

    def loop() -> None:
        while not stop.wait(TICK_SECONDS):
            try:
                for r in run_due(agent):
                    session._notify({"type": "notice", "level": "info" if r["status"] == "ok" else "warning",
                                     "message": f"Scheduled job \"{r['name']}\" finished ({r['status']}). Output: {r['file']}"})
            except Exception:
                pass
    if getattr(session.ui, "has_ui", False):  # only a session that stays open is a useful clock
        threading.Thread(target=loop, daemon=True, name="omni-cron").start()
    return ""


def _unload(session: Any) -> None:
    stop = getattr(session, "_cron_stop", None)
    if stop is not None:
        stop.set()
    session._cron_stop = None


CAPABILITIES = [Capability("scheduler", "Scheduled jobs", "Run prompts on a schedule (intervals, cron, one-shot) in fresh "
                           "unattended sessions.", "Hermes cronjob, OpenClaw cron", tools=_tools, prompt=_start_ticker,
                           commands=lambda s: {"cron": ("List scheduled jobs", lambda _a: format_jobs(load_jobs(Path(s.agent_dir))))},
                           unload=_unload)]


def cli(argv: List[str], agent_dir: Path) -> int:
    """`omni cron list | run [id] | remove <id>` without opening a session."""
    sub = argv[0] if argv else "list"
    if sub == "list":
        print(format_jobs(load_jobs(agent_dir)))
        return 0
    if sub == "run":
        results = run_due(agent_dir, only=argv[1] if len(argv) > 1 else None)
        for r in results:
            print(f"{r['name']}: {r['status']} -> {r['file']}")
        if not results:
            print("Nothing is due.")
        return 0 if all(r["status"] == "ok" for r in results) else 1
    if sub == "signal" and len(argv) > 1:
        try:
            n = send_signal(agent_dir, argv[1], " ".join(argv[2:]))
        except ValueError as e:
            print(e)
            return 2
        print(f"Signal '{argv[1]}' sent; {n} job(s) are waiting for it." + ("" if n else " (none yet: it stays queued)"))
        return 0
    if sub == "remove" and len(argv) > 1:
        with _file_lock:
            jobs = load_jobs(agent_dir)
            kept = [j for j in jobs if j["id"] != argv[1]]
            save_jobs(agent_dir, kept)
        print("Removed." if len(kept) < len(jobs) else f"No job {argv[1]}.")
        return 0
    print("usage: omni cron list | run [id] | remove <id>")
    return 2
