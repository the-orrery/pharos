"""Run a list of checks and aggregate their results.

The runner is the only place that *executes* checks; it shields the caller from
a misbehaving check (one that raises despite the protocol contract) by turning
the exception into a DOWN/CRITICAL event rather than aborting the whole sweep.
"""

from __future__ import annotations

import structlog

from pharos.checks.base import Check
from pharos.notify.base import HealthEvent, Severity, Status

logger = structlog.get_logger(__name__)

# Worst-of ordering: OK < DEGRADED < DOWN.  UNKNOWN is treated as DEGRADED-level
# severity for aggregation (something needs a human) but kept distinct on the
# individual event.
_RANK: dict[Status, int] = {
    Status.OK: 0,
    Status.UNKNOWN: 1,
    Status.DEGRADED: 2,
    Status.DOWN: 3,
}


def run_checks(checks: list[Check]) -> list[HealthEvent]:
    """Run every check in order, returning one :class:`HealthEvent` each.

    A check that raises (violating the no-raise contract) is recorded as a
    DOWN/CRITICAL event so one bad check never sinks the rest of the sweep.
    """
    events: list[HealthEvent] = []
    for check in checks:
        try:
            events.append(check.run())
        except Exception as exc:
            # Defensive: contain a misbehaving check so it never aborts the sweep.
            logger.warning("check_raised", check_id=check.id, error=str(exc))
            events.append(
                HealthEvent(
                    source=check.source,
                    status=Status.DOWN,
                    severity=Severity.CRITICAL,
                    title=f"check {check.id} raised an exception",
                    detail=str(exc),
                    evidence={"check_id": check.id, "error": str(exc)},
                )
            )
    return events


def aggregate(events: list[HealthEvent]) -> Status:
    """Return the worst status across *events* (OK < DEGRADED < DOWN).

    An empty list aggregates to OK (nothing wrong).  UNKNOWN ranks between OK
    and DEGRADED so an all-UNKNOWN sweep does not masquerade as healthy.
    """
    worst = Status.OK
    for event in events:
        if _RANK[event.status] > _RANK[worst]:
            worst = event.status
    return worst
