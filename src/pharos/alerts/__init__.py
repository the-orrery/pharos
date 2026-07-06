"""Stateful alert manager: turn per-run HealthEvents into queryable tickets.

A pharos `run` produces a fresh HealthEvent per check every invocation. Pushing
a notification on every DEGRADED/DOWN event is noisy (same failure re-paged each
run) and stateless (no "what is currently broken?" view for an agent to triage).

This subsystem treats an alert as a *ticket* keyed by check id, with a lifecycle
(firing → acked → resolved). The reconciler (`manager.reconcile`) diffs the
current run against open tickets and emits only the *transitions* worth acting
on: NEW (first failure → page), RESOLVED (recovery → all-clear), and ONGOING
(still failing / acked → silent). The store (`store`) persists tickets in the
same local-SQLite/WAL pattern as telemetry so concurrent CLI processes are safe.
"""

from __future__ import annotations

from pharos.alerts.manager import Transition, reconcile

__all__ = ["Transition", "reconcile"]
