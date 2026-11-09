"""
Paperclip Managed Employee Adapter (hermes-paperclip-adapter in Hermes Agent).
Plugs Omni into the Paperclip autonomous company organization hierarchy.
Allows the agent to operate as a managed AI employee with assigned tickets,
org hierarchy, status heartbeats, manager reviews, and budget limits.
"""

from dataclasses import dataclass, field
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
from ..paths import user_data_dir

logger = logging.getLogger(__name__)


@dataclass
class PaperclipTicket:
    id: str
    title: str
    description: str
    priority: str = "high"  # "low", "medium", "high", "critical"
    status: str = "todo"  # "todo", "in_progress", "submitted", "done"
    assigned_to: str = "emp_omni_01"
    created_at: float = field(default_factory=time.time)


@dataclass
class EmployeeProfile:
    company_name: str = "Paperclip Global Corp"
    employee_id: str = "emp_omni_01"
    role: str = "Autonomous Principal Systems Engineer"
    manager_id: str = "manager_director_01"
    monthly_budget_usd: float = 150.00
    spent_usd: float = 3.42
    status: str = "active"


class PaperclipAdapter:
    def __init__(self, config_file: Optional[str] = None):
        self.config_file = config_file or str(user_data_dir() / "paperclip_employee.json")
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        self.profile = EmployeeProfile()
        self.tickets: Dict[str, PaperclipTicket] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                prof_data = data.get("profile", {})
                self.profile = EmployeeProfile(**prof_data)
                for t in data.get("tickets", []):
                    ticket = PaperclipTicket(**t)
                    self.tickets[ticket.id] = ticket
            except Exception as e:
                logger.error(f"Failed to load paperclip config: {e}")

        if not self.tickets:
            # Seed default tasks
            t1 = PaperclipTicket(
                id="TICK-101",
                title="Implement Universal Plugin Hot-Reloading",
                description="Refactor adapter engine to discover plugins with zero restarts.",
                priority="high",
                status="in_progress",
            )
            t2 = PaperclipTicket(
                id="TICK-102",
                title="Optimize Token Prefix Cache Alignment",
                description="Align system prompts on 1024-byte boundaries for Antigravity cache hits.",
                priority="medium",
                status="todo",
            )
            self.tickets[t1.id] = t1
            self.tickets[t2.id] = t2
            self._save()

    def _save(self):
        try:
            data = {
                "profile": {
                    "company_name": self.profile.company_name,
                    "employee_id": self.profile.employee_id,
                    "role": self.profile.role,
                    "manager_id": self.profile.manager_id,
                    "monthly_budget_usd": self.profile.monthly_budget_usd,
                    "spent_usd": self.profile.spent_usd,
                    "status": self.profile.status,
                },
                "tickets": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "description": t.description,
                        "priority": t.priority,
                        "status": t.status,
                        "assigned_to": t.assigned_to,
                        "created_at": t.created_at,
                    }
                    for t in self.tickets.values()
                ],
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save paperclip config: {e}")

    def heartbeat(self) -> Dict[str, Any]:
        """Sends agent heartbeat check-in to Paperclip company manager."""
        return {
            "status": "online",
            "employee_id": self.profile.employee_id,
            "role": self.profile.role,
            "active_tickets": len([t for t in self.tickets.values() if t.status == "in_progress"]),
            "budget_remaining_usd": round(self.profile.monthly_budget_usd - self.profile.spent_usd, 2),
            "timestamp": time.time(),
        }

    def list_tickets(self) -> List[PaperclipTicket]:
        return list(self.tickets.values())

    def submit_deliverable(self, ticket_id: str, summary: str, patch_diff: str = "") -> Dict[str, Any]:
        """Submits completed work for manager approval."""
        ticket = self.tickets.get(ticket_id)
        if not ticket:
            return {"success": False, "error": f"Ticket '{ticket_id}' not found"}

        ticket.status = "submitted"
        self._save()
        return {
            "success": True,
            "ticket_id": ticket_id,
            "title": ticket.title,
            "status": "submitted_for_review",
            "manager": self.profile.manager_id,
            "summary": summary,
            "patch_bytes": len(patch_diff.encode("utf-8")),
        }
