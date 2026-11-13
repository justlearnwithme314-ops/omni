"""`self-evolve`: the `/evolve` command (see `src/pi/evolve.py`). Proposals only; nothing is applied without you."""
from __future__ import annotations

from typing import Any

from . import Capability


def _commands(session: Any):
    from .. import evolve
    return {"evolve": ("Learn from past sessions: propose skills and memory, then apply the ones you approve",
                       lambda args: evolve.command(session, args))}


CAPABILITIES = [Capability("self-evolve", "Self-evolution", "/evolve studies past sessions and proposes new or corrected "
                           "skills and memory entries for you to approve.", "Hermes self-evolution", commands=_commands)]
