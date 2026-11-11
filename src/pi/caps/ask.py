"""`ask`: let the model put a question to the user mid-task (Oh My Pi `ask`, Hermes `clarify`)."""
from __future__ import annotations

from typing import Any, Dict

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

OTHER = "Other (type an answer)"


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        question = str(p.get("question") or "").strip()
        if not question:
            raise ToolError("question is required")
        ui = session.ui
        if ui is None or not getattr(ui, "has_ui", False):
            return text_result("No interactive user is available. Choose the most reasonable option yourself, state "
                               "the assumption you made, and continue.", {"answered": False})
        options = [str(o) for o in p.get("options") or [] if str(o).strip()][:6]
        answer = None
        if options:
            answer = ui.ext_select(title=question, options=options + [OTHER])
            if answer == OTHER:
                answer = ui.ext_input(title=question, placeholder="your answer")
        else:
            answer = ui.ext_input(title=question, placeholder="your answer")
        if answer is None or not str(answer).strip():
            return text_result("The user dismissed the question without answering. Proceed with your best judgment "
                               "or stop and explain what you need.", {"answered": False})
        return text_result(f"User answered: {answer}", {"answered": True, "answer": answer})

    return {"ask": ToolDef(
        name="ask", label="Ask",
        description="Ask the user one question when a decision is genuinely theirs (ambiguous requirement, "
                    "destructive choice). Give 2-6 short `options` when the choices are known.",
        parameters={"type": "object", "properties": {
            "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
            "required": ["question"]},
        execute=execute, prompt_snippet="Ask the user a clarifying question")}


CAPABILITIES = [Capability("ask", "Ask the user", "The model can ask a clarifying question with selectable options.",
                           "Oh My Pi ask, Hermes clarify", tools=_tools)]
