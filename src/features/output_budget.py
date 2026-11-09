"""
Intelligent Output Budgeting & Clamping (OMP Architecture Standout)
"""

from typing import Tuple


class OutputBudgetEnforcer:
    def __init__(self, max_lines: int = 100, max_chars: int = 8000, head_ratio: float = 0.5):
        self.max_lines = max_lines
        self.max_chars = max_chars
        self.head_ratio = head_ratio

    def clamp(
        self,
        raw_output: str,
        custom_max_lines: int = None,
        custom_max_chars: int = None,
    ) -> Tuple[str, bool]:
        if not raw_output or not isinstance(raw_output, str):
            return str(raw_output or ""), False

        limit_lines = custom_max_lines or self.max_lines
        limit_chars = custom_max_chars or self.max_chars

        lines = raw_output.splitlines()
        total_lines = len(lines)
        total_chars = len(raw_output)

        if total_lines <= limit_lines and total_chars <= limit_chars:
            return raw_output, False

        head_lines_count = max(5, int(limit_lines * self.head_ratio))
        tail_lines_count = max(5, limit_lines - head_lines_count)

        if head_lines_count + tail_lines_count >= total_lines:
            head_chars = int(limit_chars * self.head_ratio)
            tail_chars = limit_chars - head_chars
            omitted = total_chars - limit_chars
            marker = f"\n\n[... {omitted:,} characters truncated by OutputBudget to preserve context ...]\n\n"
            return raw_output[:head_chars] + marker + raw_output[-tail_chars:], True

        head = lines[:head_lines_count]
        tail = lines[-tail_lines_count:]
        omitted_lines = total_lines - (head_lines_count + tail_lines_count)
        omitted_chars = sum(len(l) for l in lines[head_lines_count:-tail_lines_count])

        marker = (
            f"\n\n[... {omitted_lines:,} lines ({omitted_chars:,} characters) "
            f"truncated by OutputBudget to preserve model context ...]\n\n"
        )
        clamped = "\n".join(head) + marker + "\n".join(tail)
        return clamped, True
