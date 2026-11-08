"""
Hermes-Style Self-Evolution & Prompt/Skill Mutator
Analyzes execution trajectories from SQLite and mutates system prompts or synthesizes skills.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional
from ..telemetry.tracer import TrajectoryTracer


class EvolutionOptimizer:
    def __init__(
        self,
        tracer: TrajectoryTracer,
        patches_dir: str = "prompts/patches",
        skills_dir: str = "skills",
    ):
        self.tracer = tracer
        self.patches_dir = patches_dir
        self.skills_dir = skills_dir
        os.makedirs(self.patches_dir, exist_ok=True)
        os.makedirs(self.skills_dir, exist_ok=True)

    def analyze_failures(self, limit: int = 20) -> List[Dict[str, Any]]:
        failures = self.tracer.get_failed_trajectories(limit=limit)
        categorized = []
        for f in failures:
            error = f.get("error") or ""
            tool_results = f.get("tool_results_json") or "[]"
            analysis = {
                "session_id": f.get("session_id"),
                "turn": f.get("turn_index"),
                "category": "unknown",
                "detail": error,
            }
            if "not found" in error.lower() or "missing" in error.lower():
                analysis["category"] = "tool_discovery_failure"
            elif "syntax" in error.lower() or "json" in error.lower():
                analysis["category"] = "schema_formatting_error"
            elif "permission" in error.lower() or "denied" in error.lower():
                analysis["category"] = "permission_boundary_error"
            elif "rate limit" in error.lower() or "429" in error.lower():
                analysis["category"] = "provider_exhaustion"
            elif "error" in tool_results.lower():
                analysis["category"] = "tool_execution_failure"
            categorized.append(analysis)
        return categorized

    def generate_prompt_patch(
        self, failure_summary: str, rule_instruction: str
    ) -> str:
        timestamp = int(time.time())
        patch_file = os.path.join(self.patches_dir, f"patch_{timestamp}.md")
        content = (
            f"<!-- Auto-evolved patch {timestamp} -->\n"
            f"### Evolution Patch: {failure_summary}\n"
            f"- RULE: {rule_instruction}\n"
            f"- Applied At: {time.ctime(timestamp)}\n"
        )
        with open(patch_file, "w", encoding="utf-8") as f:
            f.write(content)
        return patch_file

    def get_active_patches(self) -> str:
        if not os.path.exists(self.patches_dir):
            return ""
        patches = []
        for file in sorted(os.listdir(self.patches_dir)):
            if file.endswith(".md"):
                with open(
                    os.path.join(self.patches_dir, file), "r", encoding="utf-8"
                ) as f:
                    patches.append(f.read().strip())
        return "\n\n".join(patches)

    def synthesize_skill(
        self, name: str, description: str, script_code: str
    ) -> str:
        skill_dir = os.path.join(self.skills_dir, name)
        os.makedirs(skill_dir, exist_ok=True)
        skill_md = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"metadata:\n"
            f"  openclaw:\n"
            f"    requires:\n"
            f"      bins: []\n"
            f"      env: []\n"
            f"      os: [\"win32\", \"linux\", \"darwin\"]\n"
            f"---\n\n"
            f"# {name} Skill\n"
            f"{description}\n\n"
            f"Execute: python {{baseDir}}/run.py\n"
        )
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(skill_md)
        with open(os.path.join(skill_dir, "run.py"), "w", encoding="utf-8") as f:
            f.write(script_code)
        return skill_dir
