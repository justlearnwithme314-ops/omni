"""System prompt construction - port of core/system-prompt.ts (sectioned prompt, identical wording)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .resources import Skill, format_skills_for_prompt

_SECTION_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass
class PromptOptions:
    cwd: str
    custom_prompt: Optional[str] = None
    selected_tools: List[str] = field(default_factory=lambda: ["read", "bash", "edit", "write"])
    hidden_tools: List[str] = field(default_factory=list)
    tool_snippets: Dict[str, str] = field(default_factory=dict)
    tool_guidelines: Dict[str, List[str]] = field(default_factory=dict)
    prompt_guidelines: List[str] = field(default_factory=list)
    append_system_prompt: str = ""
    sections: Dict[str, str] = field(default_factory=dict)
    context_files: List[Dict[str, str]] = field(default_factory=list)
    skills: List[Skill] = field(default_factory=list)
    docs_root: Optional[str] = None

    def to_json(self) -> Dict:
        return {"cwd": self.cwd, "customPrompt": self.custom_prompt, "selectedTools": self.selected_tools,
                "hiddenTools": self.hidden_tools, "toolSnippets": self.tool_snippets, "toolGuidelines": self.tool_guidelines,
                "promptGuidelines": self.prompt_guidelines, "appendSystemPrompt": self.append_system_prompt,
                "sections": self.sections, "contextFiles": self.context_files,
                "skills": [{"name": s.name, "description": s.description, "filePath": s.file_path} for s in self.skills]}


def _rules(selected: List[str], tool_guidelines: Dict[str, List[str]], prompt_guidelines: List[str]) -> str:
    rules: List[str] = []
    seen = set()

    def add(r: str) -> None:
        r = r.strip()
        if r and r not in seen:
            seen.add(r)
            rules.append(r)

    has = lambda n: n in selected  # noqa: E731
    if (has("bash") or has("powershell")) and not has("grep") and not has("find") and not has("ls"):
        if has("bash") and has("powershell"):
            add("Use bash or PowerShell for file operations like listing, searching, and finding files")
        elif has("powershell"):
            add("Use PowerShell for file operations like listing, searching, and finding files")
        else:
            add("Use bash for file operations like ls, rg, find")
    for name in selected:
        for r in tool_guidelines.get(name, []):
            add(r)
    for r in prompt_guidelines:
        add(r)
    add("Be concise in your responses")
    add("Show file paths clearly when working with files")
    return "\n".join(f"- {r}" for r in rules)


def build_sections(o: PromptOptions) -> Dict[str, str]:
    for name in o.sections:
        if not _SECTION_RE.match(name) or name == "preamble":
            raise ValueError(f"Invalid system prompt section name: {name}")
    declared = [t for t in o.selected_tools if t not in o.hidden_tools]
    s: Dict[str, str] = {}
    if o.custom_prompt:
        s["preamble"] = o.custom_prompt
    else:
        s["preamble"] = ("You are a coding assistant running inside pi, a coding agent harness. Help users inspect files, run "
                         "commands, and modify code.")
        visible = [t for t in declared if o.tool_snippets.get(t)]
        tools = "\n".join(f"- {t}: {o.tool_snippets[t]}" for t in visible) if visible else "(none)"
        s["tools"] = f"{tools}\n\nAdditional tools may be available."
        s["rules"] = _rules(declared, o.tool_guidelines, o.prompt_guidelines)
        if o.docs_root:
            s["docs"] = (f"Pi documentation (read only when the user asks about the harness itself, its SDK, extensions, themes, "
                         f"skills, or TUI):\n- Main documentation: {o.docs_root}/README.md\n- Additional docs: {o.docs_root}/docs\n"
                         f"- Examples: {o.docs_root}/examples (extensions, custom tools, SDK)\n"
                         f"- When asked about: extensions (docs/extensions.md, examples/extensions/), themes (docs/themes.md), "
                         f"skills (docs/skills.md), prompt templates (docs/prompt-templates.md), TUI components (docs/tui.md), "
                         f"keybindings (docs/keybindings.md), SDK integrations (docs/sdk.md), custom providers "
                         f"(docs/custom-provider.md), adding models (docs/models.md), packages (docs/packages.md)\n"
                         f"- Before implementing harness changes, read the relevant docs and examples in full, following related "
                         f"cross-references.")
    if o.append_system_prompt:
        s["addendum"] = o.append_system_prompt
    if o.context_files:
        s["project_context"] = "\n\n".join(["Project-specific instructions and guidelines:"] + [
            f'<project_instructions path="{f["path"]}">\n{f["content"]}\n</project_instructions>' for f in o.context_files])
    reader = "read" if "read" in declared else "bash" if "bash" in declared else (
        "indirect" if any(t in o.selected_tools for t in ("read", "bash")) else None)
    if reader and o.skills:
        sp = format_skills_for_prompt(o.skills, reader).strip()
        if sp:
            s["skills"] = sp
    s["cwd"] = o.cwd.replace("\\", "/")
    for name, content in o.sections.items():
        if content:
            s[name] = content
    out = {"preamble": s["preamble"]}
    for name, content in s.items():
        if name != "preamble":
            out[name] = f"<{name}>\n{content}\n</{name}>"
    return out


def render(sections: Dict[str, str]) -> str:
    return "\n\n".join(v for v in sections.values() if v)


def build_system_prompt(o: PromptOptions) -> str:
    return render(build_sections(o))
