"""Task context shared by the graph, the tap route and /state: working conditions and saved duration estimates.

Estimate inputs come from trusted records only: the assigned task (type, quantity, unit, fixture ground condition),
the verified catalog (machine category, and operator skill / machine age when the catalog carries them) and the
weather at the task's scheduled start from the same cache-only weather source as the conditions check.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .api import schemas as s
from .catalog import Catalog
from .conditions import Conditions
from .estimation import EstimateInputs, Frozen, estimate
from .store import Store, utcnow


class Planner:
    def __init__(self, store: Store, conditions: Conditions | None, estimator: Frozen, catalog: Catalog | None,
                 clock: Callable[[], datetime] = utcnow):
        self.store, self.conditions, self.estimator, self.catalog, self.clock = (store, conditions, estimator, catalog,
                                                                                clock)

    def inputs(self, session: s.Session, task: s.AssignedTask) -> EstimateInputs:
        machine = self.catalog.machines.get(task.machine_id) if self.catalog else None
        weather = None
        if self.conditions is not None:
            weather, coverage, _ = self.conditions.weather.lookup(
                self.store.get_site(session.site_id), task.scheduled_start_at, self.clock())
            weather = weather if coverage in ("fresh", "stale") else None
        return EstimateInputs(
            task_type=task.task_type, work_quantity=task.work_quantity, work_unit=task.work_unit,
            ground_condition=task.ground_condition, machine_category=machine.category if machine else None,
            operator_skill=self.catalog.operator_skill.get(session.operator_id) if self.catalog else None,
            machine_age_years=self.catalog.machine_age_years.get(task.machine_id) if self.catalog else None,
            weather=weather)

    def result(self, session: s.Session, task: s.AssignedTask) -> dict[str, Any]:
        return estimate(self.estimator, self.inputs(session, task))

    def estimate_fn(self, session: s.Session) -> Callable[[s.AssignedTask], dict[str, Any]]:
        """Used inside the start transaction: the estimate in force at the start is saved with it."""
        return lambda task: self.result(session, task)

    def enrich(self, session: s.Session, task: s.AssignedTask) -> s.AssignedTask:
        update: dict[str, Any] = {}
        if task.status != "completed":
            if self.conditions is not None:
                update["conditions"] = self.conditions.check(session, task)
            update["estimate"] = self.store.estimate_for_task(task.task_id, self.result(session, task))
        if task.started_at is not None:
            end = task.completed_at or self.clock()  # started_at is wall-clock time, so is the end
            update["elapsed_minutes"] = round(max((end - task.started_at).total_seconds(), 0) / 60, 1)
        return task.model_copy(update=update)
