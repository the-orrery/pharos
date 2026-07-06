"""Reconcile a run's HealthEvents against open alert tickets.

`reconcile` is the heart of the stateful alert manager: given the (check_id,
event) pairs from one `run`, it diffs them against the store's open tickets and
returns the *transitions* — what changed since last run. The CLI then notifies
only on NEW and RESOLVED; ONGOING (still failing, or acked) is silent. This is
what makes alerts deduped tickets rather than per-run spam:

  * failing + no open ticket    → INSERT firing ticket      → NEW       (notify)
  * failing + open ticket       → bump last_seen/occurrences → ONGOING   (silent)
  * open ticket, check now OK    → mark resolved             → RESOLVED  (notify)

"failing" = status in {DEGRADED, DOWN}. Only a check that RAN this run and is now
OK recovers its ticket — absence (partial run / removed config) and UNKNOWN do NOT
auto-resolve (absence/uncertainty != recovery; resolve those manually). An acked
ticket that keeps failing stays acked and ONGOING — silence falls out of the
model, no separate mute flag needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pharos.alerts import store
from pharos.notify.base import HealthEvent, Severity, Status

# The statuses that constitute a "failing" check worth a ticket.
_FAILING = (Status.DEGRADED, Status.DOWN)


@dataclass(frozen=True, slots=True)
class Transition:
    """What changed for one alert key across this run.

    kind:
      * NEW      — a check started failing; `event` is the firing HealthEvent
                   (notify it: this is the page).
      * ONGOING  — a check is still failing (or acked); `event` is the latest
                   firing HealthEvent. NOT notified — dedupe/silence lives here.
      * RESOLVED — an open ticket's check recovered/was removed; `event` is a
                   synthesized OK/INFO recovery HealthEvent (notify it: all-clear).

    `event` is always something the CLI could hand straight to `notify()`.
    """

    kind: str  # 'NEW' | 'ONGOING' | 'RESOLVED'
    key: str
    event: HealthEvent


# Transition kinds the CLI should actually notify on. ONGOING is deliberately
# absent — that is the whole point of stateful tickets.
NOTIFY_KINDS = frozenset({"NEW", "RESOLVED"})


def _recovery_event(key: str, alert: dict) -> HealthEvent:
    """Synthesize the OK/INFO HealthEvent for a resolved ticket's all-clear
    notification. Carries the recovered key/source so the recovery message is
    correlatable with the original alert."""
    source = alert.get("source") or key
    return HealthEvent(
        source=source,
        status=Status.OK,
        severity=Severity.INFO,
        title=f"{source} recovered",
        detail=f"alert {key!r} resolved: check is no longer failing",
        evidence={"key": key, "resolved": True},
    )


def reconcile(
    keyed_events: list[tuple[str, HealthEvent]],
    *,
    now: float,
    path: Path | None = None,
) -> list[Transition]:
    """Diff this run's (check_id, event) pairs against open tickets.

    Returns the transitions in a stable order: NEW/ONGOING for this run's checks
    (input order), then RESOLVED for any open ticket whose key did not fail this
    run. The store is mutated as a side effect (insert/touch/resolve).
    """
    transitions: list[Transition] = []

    # Snapshot of open tickets before we mutate, so recoveries are computed
    # against the pre-run state (not against rows we just inserted).
    open_before = store.open_keys(path=path)
    failing_keys: set[str] = set()
    ok_keys: set[str] = set()

    for key, event in keyed_events:
        if event.status is Status.OK:
            ok_keys.add(key)  # ran this run and healthy → candidate recovery below
            continue
        if event.status not in _FAILING:
            continue  # UNKNOWN: neither a ticket nor a confirmed recovery
        failing_keys.add(key)
        severity = event.severity.value

        if key in open_before:
            # Still failing → keep the ticket, refresh fields, no re-notify.
            store.touch_ongoing(
                key=key,
                severity=severity,
                title=event.title,
                detail=event.detail,
                evidence=dict(event.evidence),
                now=now,
                path=path,
            )
            transitions.append(Transition(kind="ONGOING", key=key, event=event))
        else:
            # First failure → open a firing ticket and page.
            store.upsert_firing(
                key=key,
                source=event.source,
                severity=severity,
                title=event.title,
                detail=event.detail,
                evidence=dict(event.evidence),
                now=now,
                path=path,
            )
            transitions.append(Transition(kind="NEW", key=key, event=event))

    # Recoveries: open tickets whose check RAN this run and is now OK. Absence
    # (partial run / removed config) and UNKNOWN are NOT recovery — don't all-clear
    # what we didn't confirm healthy; those linger until OK or manual resolve.
    for key in sorted(open_before & ok_keys):
        alert = store.get(key, path=path)
        if alert is None:  # raced away; nothing to resolve
            continue
        if store.mark_resolved(key, now=now, path=path):
            transitions.append(
                Transition(kind="RESOLVED", key=key, event=_recovery_event(key, alert))
            )

    return transitions


__all__ = ["NOTIFY_KINDS", "Transition", "reconcile"]
