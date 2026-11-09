"""
Time-Traveling Stream Rules (TTSR - OMP Architecture Standout)
Monitors streaming tokens in flight. If the model begins generating malformed commands,
forbidden calls, or known hallucinated flags, TTSR rolls back the generation window
and injects an immediate corrective constraint before the turn completes.
"""

from dataclasses import dataclass, field
import re
from typing import List, Optional, Tuple


@dataclass
class StreamRule:
    name: str
    pattern: re.Pattern
    corrective_constraint: str
    action: str = "rollback_and_constrain"  # "abort" | "rollback_and_constrain"


DEFAULT_RULES = [
    StreamRule(
        name="no_unquoted_semicolons_in_bash",
        pattern=re.compile(r'execute_bash\s*\([^)]*;\s*rm\b', re.IGNORECASE),
        corrective_constraint="Do not chain raw destructive bash commands with unquoted semicolons. Run operations in isolated discrete steps.",
    ),
    StreamRule(
        name="hallucinated_git_flags",
        pattern=re.compile(r'git\s+(?:checkout|switch)\s+--destroy\b', re.IGNORECASE),
        corrective_constraint="The flag '--destroy' is invalid for git. Use standard git commands without hallucinated options.",
    ),
    StreamRule(
        name="invalid_pip_syntax",
        pattern=re.compile(r'pip\s+get\s+', re.IGNORECASE),
        corrective_constraint="Use 'pip install' instead of 'pip get'.",
    ),
]


class TTSRInterceptor:
    """Active stream rule monitor and time-travel rollback manager."""

    def __init__(self, rules: Optional[List[StreamRule]] = None):
        self.rules = rules or list(DEFAULT_RULES)

    def add_rule(self, name: str, regex_pattern: str, corrective_constraint: str):
        self.rules.append(StreamRule(
            name=name,
            pattern=re.compile(regex_pattern, re.IGNORECASE),
            corrective_constraint=corrective_constraint,
        ))

    def evaluate_stream(self, partial_text: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Evaluates partial stream tokens against active rules.
        Returns (violation_detected: bool, rule_name: str, corrective_prompt: str).
        """
        if not partial_text:
            return False, None, None

        for rule in self.rules:
            if rule.pattern.search(partial_text):
                return True, rule.name, rule.corrective_constraint

        return False, None, None
