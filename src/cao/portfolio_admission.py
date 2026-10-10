"""Pure advisory planning for a global portfolio WRITE-capacity limit.

A plan cannot grant authority or reserve a slot. The caller must obtain fresh
occupancy evidence and an atomic claim from the sole Work Registry immediately
before launching work. Invalid or unknown evidence fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

READ = "READ"
WRITE = "WRITE"
ADMIT = "ADMIT"
DEFER = "DEFER"
REJECT = "REJECT"
MAX_ITEMS = 100_000


class AdmissionError(ValueError):
    """One or more planning inputs are invalid or cannot be trusted."""

    code = "PORTFOLIO_ADMISSION_INVALID"


@dataclass(frozen=True)
class TaskProposal:
    task_id: str
    repo_id: str
    mode: str


@dataclass(frozen=True)
class ActiveTask:
    task_id: str
    repo_id: str
    mode: str


@dataclass(frozen=True)
class OccupancySnapshot:
    verified: bool
    tasks: tuple[ActiveTask, ...]
    active_write_count: int | None


@dataclass(frozen=True)
class AdmissionDecision:
    task_id: str
    verdict: str
    reason: str


@dataclass(frozen=True)
class AdmissionPlan:
    decisions: tuple[AdmissionDecision, ...]
    active_writes: int
    available_write_slots: int
    remaining_write_slots: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "authority": "ADVISORY",
            "active_writes": self.active_writes,
            "available_write_slots": self.available_write_slots,
            "remaining_write_slots": self.remaining_write_slots,
            "decisions": [
                {"task_id": x.task_id, "verdict": x.verdict, "reason": x.reason}
                for x in self.decisions
            ],
        }


def _identity(value: object) -> bool:
    return (
        type(value) is str and 0 < len(value) <= 200
        and value.isprintable() and not any(ch.isspace() for ch in value)
    )


def _record(item: object, expected: type) -> None:
    if type(item) is not expected:
        raise AdmissionError("unexpected task record type")
    if not _identity(item.task_id) or not _identity(item.repo_id):
        raise AdmissionError("task and repository identities must be canonical")
    if type(item.mode) is not str or item.mode not in (READ, WRITE):
        raise AdmissionError("mode must be READ or WRITE")


def _sequence(value: object) -> tuple:
    if type(value) not in (list, tuple) or len(value) > MAX_ITEMS:
        raise AdmissionError("input must be a bounded list or tuple")
    return tuple(value)


def plan_admission(
    proposals: list[TaskProposal] | tuple[TaskProposal, ...],
    snapshot: OccupancySnapshot,
    *,
    max_writers: int = 2,
) -> AdmissionPlan:
    """Return ordered ADMIT/DEFER/REJECT results from verified occupancy.

    One capacity limit spans every repository. The snapshot is caller-proven;
    no path-conflict, policy or permission decision is made by this planner.
    """
    if type(max_writers) is not int or max_writers < 0:
        raise AdmissionError("max_writers must be nonnegative integer")
    if type(snapshot) is not OccupancySnapshot or snapshot.verified is not True:
        raise AdmissionError("verified occupancy snapshot is required")
    active = _sequence(snapshot.tasks)
    count = snapshot.active_write_count
    if type(count) is not int or count < 0:
        raise AdmissionError("active WRITE count is unknown or invalid")
    ids: set[str] = set()
    observed_writes = 0
    for item in active:
        _record(item, ActiveTask)
        if item.task_id in ids:
            raise AdmissionError("duplicate active task identity")
        ids.add(item.task_id)
        observed_writes += item.mode == WRITE
    if count != observed_writes:
        raise AdmissionError("active WRITE count contradicts identities")

    tasks = _sequence(proposals)
    proposed: set[str] = set()
    for item in tasks:
        _record(item, TaskProposal)
        if item.task_id in proposed:
            raise AdmissionError("duplicate proposed task identity")
        proposed.add(item.task_id)

    available = max(0, max_writers - observed_writes)
    remaining = available
    decisions: list[AdmissionDecision] = []
    for item in tasks:
        if item.task_id in ids:
            verdict, reason = REJECT, "already_active"
        elif item.mode == READ:
            verdict, reason = ADMIT, "read_no_write_slot"
        elif remaining > 0:
            verdict, reason = ADMIT, "write_slot_available"
            remaining -= 1
        else:
            verdict, reason = DEFER, "write_capacity_exhausted"
        decisions.append(AdmissionDecision(item.task_id, verdict, reason))
    return AdmissionPlan(tuple(decisions), observed_writes, available, remaining)
