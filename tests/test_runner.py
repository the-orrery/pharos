"""Tests for the check runner: execution containment + worst-of aggregation."""

from __future__ import annotations

from pharos.checks.runner import aggregate, run_checks
from pharos.notify.base import HealthEvent, Severity, Status


class _StubCheck:
    def __init__(self, id: str, status: Status) -> None:
        self.id = id
        self.source = "stub"
        self._status = status

    def run(self) -> HealthEvent:
        return HealthEvent(
            source=self.source,
            status=self._status,
            severity=Severity.INFO,
            title=f"{self.id} -> {self._status.value}",
        )


class _RaisingCheck:
    id = "boom"
    source = "stub"

    def run(self) -> HealthEvent:
        raise ValueError("kaboom")


class TestRunChecks:
    def test_runs_each_in_order(self) -> None:
        events = run_checks(
            [_StubCheck("a", Status.OK), _StubCheck("b", Status.DEGRADED)]
        )
        assert [e.title for e in events] == ["a -> ok", "b -> degraded"]

    def test_raising_check_becomes_down_event(self) -> None:
        events = run_checks([_RaisingCheck()])
        assert len(events) == 1
        assert events[0].status is Status.DOWN
        assert events[0].severity is Severity.CRITICAL

    def test_one_bad_check_does_not_sink_sweep(self) -> None:
        events = run_checks([_RaisingCheck(), _StubCheck("c", Status.OK)])
        assert len(events) == 2
        assert events[1].status is Status.OK


class TestAggregate:
    def _ev(self, status: Status) -> HealthEvent:
        return HealthEvent(source="s", status=status, severity=Severity.INFO, title="t")

    def test_empty_is_ok(self) -> None:
        assert aggregate([]) is Status.OK

    def test_all_ok(self) -> None:
        assert aggregate([self._ev(Status.OK), self._ev(Status.OK)]) is Status.OK

    def test_degraded_beats_ok(self) -> None:
        assert (
            aggregate([self._ev(Status.OK), self._ev(Status.DEGRADED)])
            is Status.DEGRADED
        )

    def test_down_is_worst(self) -> None:
        assert (
            aggregate(
                [self._ev(Status.DEGRADED), self._ev(Status.DOWN), self._ev(Status.OK)]
            )
            is Status.DOWN
        )

    def test_unknown_between_ok_and_degraded(self) -> None:
        assert (
            aggregate([self._ev(Status.OK), self._ev(Status.UNKNOWN)]) is Status.UNKNOWN
        )
        assert (
            aggregate([self._ev(Status.UNKNOWN), self._ev(Status.DEGRADED)])
            is Status.DEGRADED
        )
