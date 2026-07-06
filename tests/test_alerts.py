"""Tests for the stateful alert manager: store CRUD + reconcile lifecycle.

Every test points the store at a temp db via the `path=` kwarg (and a couple
exercise the PHAROS_ALERTS_DB env override) so nothing touches the real
~/.local/share/pharos/alerts.db.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pharos.alerts import store
from pharos.alerts.manager import NOTIFY_KINDS, Transition, reconcile
from pharos.notify.base import HealthEvent, Severity, Status


def _ev(
    source: str,
    status: Status,
    *,
    severity: Severity = Severity.WARNING,
    title: str = "t",
    detail: str = "",
    evidence: dict | None = None,
) -> HealthEvent:
    return HealthEvent(
        source=source,
        status=status,
        severity=severity,
        title=title,
        detail=detail,
        evidence=evidence or {},
    )


# ---------------------------------------------------------------------------
# store CRUD
# ---------------------------------------------------------------------------


class TestStore:
    def test_upsert_and_get(self, tmp_path: Path) -> None:
        db = tmp_path / "a.db"
        store.upsert_firing(
            key="k1",
            source="svc",
            severity="critical",
            title="down",
            detail="boom",
            evidence={"runbook": "fix-it", "x": 1},
            now=100.0,
            path=db,
        )
        a = store.get("k1", path=db)
        assert a is not None
        assert a["status"] == store.FIRING
        assert a["source"] == "svc"
        assert a["severity"] == "critical"
        assert a["first_seen"] == 100.0
        assert a["last_seen"] == 100.0
        assert a["occurrences"] == 1
        assert a["resolved_at"] is None
        assert a["evidence"] == {"runbook": "fix-it", "x": 1}  # json round-trip

    def test_get_missing_is_none(self, tmp_path: Path) -> None:
        assert store.get("nope", path=tmp_path / "a.db") is None

    def test_touch_bumps_occurrences_keeps_first_seen(self, tmp_path: Path) -> None:
        db = tmp_path / "a.db"
        store.upsert_firing(
            key="k",
            source="s",
            severity="warning",
            title="t",
            detail="",
            evidence={},
            now=100.0,
            path=db,
        )
        store.touch_ongoing(
            key="k",
            severity="critical",
            title="t2",
            detail="d2",
            evidence={"n": 2},
            now=200.0,
            path=db,
        )
        a = store.get("k", path=db)
        assert a is not None
        assert a["first_seen"] == 100.0  # unchanged
        assert a["last_seen"] == 200.0  # bumped
        assert a["occurrences"] == 2
        assert a["severity"] == "critical"  # refreshed
        assert a["title"] == "t2"

    def test_list_active_excludes_resolved(self, tmp_path: Path) -> None:
        db = tmp_path / "a.db"
        for k in ("k1", "k2"):
            store.upsert_firing(
                key=k,
                source="s",
                severity="warning",
                title="t",
                detail="",
                evidence={},
                now=1.0,
                path=db,
            )
        assert store.resolve("k1", path=db) is True
        active = store.list_active(path=db)
        assert {a["key"] for a in active} == {"k2"}
        assert len(store.list_all(path=db)) == 2  # resolved still in list_all

    def test_ack_moves_firing_to_acked(self, tmp_path: Path) -> None:
        db = tmp_path / "a.db"
        store.upsert_firing(
            key="k",
            source="s",
            severity="warning",
            title="t",
            detail="",
            evidence={},
            now=1.0,
            path=db,
        )
        assert store.ack("k", "investigating", path=db) is True
        a = store.get("k", path=db)
        assert a is not None
        assert a["status"] == store.ACKED
        assert a["ack_note"] == "investigating"
        assert store.get_open("k", path=db) is not None  # acked is still open

    def test_ack_unknown_key_returns_false(self, tmp_path: Path) -> None:
        assert store.ack("nope", path=tmp_path / "a.db") is False

    def test_resolve_unknown_key_returns_false(self, tmp_path: Path) -> None:
        assert store.resolve("nope", path=tmp_path / "a.db") is False

    def test_env_override_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "env.db"
        monkeypatch.setenv("PHAROS_ALERTS_DB", str(db))
        assert store.db_path() == db
        store.upsert_firing(
            key="k",
            source="s",
            severity="warning",
            title="t",
            detail="",
            evidence={},
            now=1.0,
        )  # no path= → uses env override
        assert db.exists()
        assert store.get("k") is not None


# ---------------------------------------------------------------------------
# reconcile lifecycle
# ---------------------------------------------------------------------------


class TestReconcile:
    def test_new_on_first_failure(self, tmp_path: Path) -> None:
        db = tmp_path / "a.db"
        trs = reconcile(
            [("c1", _ev("svc", Status.DOWN, severity=Severity.CRITICAL))],
            now=10.0,
            path=db,
        )
        assert [t.kind for t in trs] == ["NEW"]
        assert trs[0].key == "c1"
        assert trs[0].event.status is Status.DOWN  # firing event flows through
        a = store.get("c1", path=db)
        assert a is not None
        assert a["status"] == store.FIRING
        assert a["occurrences"] == 1

    def test_ongoing_on_repeat_increments_and_keeps_status(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "a.db"
        reconcile([("c1", _ev("svc", Status.DEGRADED))], now=10.0, path=db)
        trs = reconcile([("c1", _ev("svc", Status.DEGRADED))], now=20.0, path=db)
        assert [t.kind for t in trs] == ["ONGOING"]
        a = store.get("c1", path=db)
        assert a is not None
        assert a["status"] == store.FIRING  # status unchanged
        assert a["occurrences"] == 2  # incremented
        assert a["last_seen"] == 20.0

    def test_resolved_when_failing_check_recovers(self, tmp_path: Path) -> None:
        db = tmp_path / "a.db"
        reconcile([("c1", _ev("svc", Status.DOWN))], now=10.0, path=db)
        # next run: same check now OK → auto-resolve
        trs = reconcile([("c1", _ev("svc", Status.OK))], now=20.0, path=db)
        assert [t.kind for t in trs] == ["RESOLVED"]
        assert trs[0].event.status is Status.OK  # synthesized recovery event
        assert trs[0].event.severity is Severity.INFO
        a = store.get("c1", path=db)
        assert a is not None
        assert a["status"] == store.RESOLVED
        assert a["resolved_at"] == 20.0
        assert store.list_active(path=db) == []

    def test_absent_check_does_not_auto_resolve(self, tmp_path: Path) -> None:
        """Absence != recovery: a check not run this time (partial run / removed
        config) must NOT auto-resolve its open ticket — else `pharos run --config
        subset` would silently clear real alerts. It lingers until OK / manual."""
        db = tmp_path / "a.db"
        reconcile([("c1", _ev("svc", Status.DOWN))], now=10.0, path=db)  # NEW
        trs = reconcile([], now=20.0, path=db)  # c1 not run this time
        assert trs == []
        a = store.get("c1", path=db)
        assert a is not None and a["status"] == store.FIRING

    def test_unknown_does_not_resolve_open_ticket(self, tmp_path: Path) -> None:
        """DOWN then UNKNOWN (can't determine) must NOT all-clear — uncertainty
        != recovery."""
        db = tmp_path / "a.db"
        reconcile([("c1", _ev("svc", Status.DOWN))], now=10.0, path=db)
        trs = reconcile([("c1", _ev("svc", Status.UNKNOWN))], now=20.0, path=db)
        assert trs == []
        a = store.get("c1", path=db)
        assert a is not None and a["status"] == store.FIRING

    def test_unknown_status_is_not_failing(self, tmp_path: Path) -> None:
        db = tmp_path / "a.db"
        trs = reconcile([("c1", _ev("svc", Status.UNKNOWN))], now=10.0, path=db)
        assert trs == []  # UNKNOWN does not open a ticket
        assert store.get("c1", path=db) is None

    def test_acked_repeat_stays_ongoing_no_renotify(self, tmp_path: Path) -> None:
        """The dedupe/silence guarantee: once acked, a still-failing check is
        ONGOING (never NEW again), so the CLI never re-notifies."""
        db = tmp_path / "a.db"
        reconcile([("c1", _ev("svc", Status.DOWN))], now=10.0, path=db)  # NEW
        assert store.ack("c1", "on it", path=db) is True
        trs = reconcile([("c1", _ev("svc", Status.DOWN))], now=20.0, path=db)
        assert [t.kind for t in trs] == ["ONGOING"]
        a = store.get("c1", path=db)
        assert a is not None
        assert a["status"] == store.ACKED  # ack survives ongoing failure
        assert a["occurrences"] == 2
        # the would-be-notified set excludes ONGOING
        assert not any(t.kind in NOTIFY_KINDS for t in trs)

    def test_recurrence_after_resolve_is_new_again(self, tmp_path: Path) -> None:
        db = tmp_path / "a.db"
        reconcile([("c1", _ev("svc", Status.DOWN))], now=10.0, path=db)
        reconcile([("c1", _ev("svc", Status.OK))], now=20.0, path=db)  # RESOLVED
        trs = reconcile([("c1", _ev("svc", Status.DOWN))], now=30.0, path=db)
        assert [t.kind for t in trs] == ["NEW"]  # fresh lifecycle
        a = store.get("c1", path=db)
        assert a is not None
        assert a["first_seen"] == 30.0  # reset on recurrence
        assert a["occurrences"] == 1

    def test_mixed_run_produces_correct_kinds(self, tmp_path: Path) -> None:
        db = tmp_path / "a.db"
        # seed: c_open firing, c_recover firing
        reconcile(
            [
                ("c_open", _ev("a", Status.DOWN)),
                ("c_recover", _ev("b", Status.DOWN)),
            ],
            now=10.0,
            path=db,
        )
        trs = reconcile(
            [
                ("c_open", _ev("a", Status.DOWN)),  # ONGOING
                ("c_recover", _ev("b", Status.OK)),  # RESOLVED
                ("c_new", _ev("c", Status.DEGRADED)),  # NEW
            ],
            now=20.0,
            path=db,
        )
        kinds = {t.key: t.kind for t in trs}
        assert kinds == {
            "c_open": "ONGOING",
            "c_recover": "RESOLVED",
            "c_new": "NEW",
        }
        # only NEW + RESOLVED would be notified
        notified = [t.key for t in trs if t.kind in NOTIFY_KINDS]
        assert set(notified) == {"c_recover", "c_new"}


# ---------------------------------------------------------------------------
# notify-discipline contract (what the CLI uses to decide what to page)
# ---------------------------------------------------------------------------


def test_notify_kinds_excludes_ongoing() -> None:
    assert {"NEW", "RESOLVED"} == NOTIFY_KINDS
    assert "ONGOING" not in NOTIFY_KINDS


def test_transition_shape() -> None:
    ev = _ev("s", Status.DOWN)
    tr = Transition(kind="NEW", key="k", event=ev)
    assert (tr.kind, tr.key, tr.event) == ("NEW", "k", ev)


# ---------------------------------------------------------------------------
# CLI wiring: run notifies only on NEW/RESOLVED; alerts/ack/resolve commands
# ---------------------------------------------------------------------------


def _failing_config(tmp_path: Path) -> str:
    """A one-check config that fails: a CommandJsonCheck whose pointer won't
    match, so the event is DEGRADED."""
    cfg = tmp_path / "checks.toml"
    cfg.write_text(
        "[[check]]\n"
        'type = "CommandJsonCheck"\n'
        'id = "c1"\n'
        'source = "svc"\n'
        'command = ["echo", \'{"ok": false}\']\n'
        'success_field_path = "/ok"\n'
        "success_field_value = true\n"
        'runbook = "https://example.invalid/rb"\n'
    )
    return str(cfg)


def _ok_config(tmp_path: Path) -> str:
    cfg = tmp_path / "checks_ok.toml"
    cfg.write_text(
        "[[check]]\n"
        'type = "CommandJsonCheck"\n'
        'id = "c1"\n'
        'source = "svc"\n'
        'command = ["echo", \'{"ok": true}\']\n'
        'success_field_path = "/ok"\n'
        "success_field_value = true\n"
    )
    return str(cfg)


def test_cli_run_notifies_new_then_silent_then_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end CLI: first failing run pages (NEW), repeat run is silent
    (ONGOING), recovery run pages (RESOLVED). Asserts notify() is invoked only
    for the actionable transitions."""
    from typer.testing import CliRunner

    from pharos import cli

    monkeypatch.setenv("PHAROS_ALERTS_DB", str(tmp_path / "alerts.db"))

    calls: list[Status] = []

    def fake_notify(event: HealthEvent, channels: list[str]) -> dict:
        calls.append(event.status)
        return {}

    monkeypatch.setattr("pharos.notify.notify", fake_notify)

    runner = CliRunner()
    fail_cfg = _failing_config(tmp_path)
    ok_cfg = _ok_config(tmp_path)

    # run 1: first failure → NEW → one notify (DEGRADED firing event)
    r1 = runner.invoke(cli.app, ["run", "--config", fail_cfg])
    assert r1.exit_code == 1  # DEGRADED overall
    assert "1 new" in r1.stdout
    assert calls == [Status.DEGRADED]

    # run 2: still failing → ONGOING → no new notify
    calls.clear()
    r2 = runner.invoke(cli.app, ["run", "--config", fail_cfg])
    assert "1 ongoing" in r2.stdout
    assert calls == []  # silent

    # run 3: now OK → RESOLVED → one notify (synthesized OK recovery event)
    calls.clear()
    r3 = runner.invoke(cli.app, ["run", "--config", ok_cfg])
    assert r3.exit_code == 0
    assert "1 resolved" in r3.stdout
    assert calls == [Status.OK]


def test_cli_alerts_ack_resolve_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from pharos import cli

    monkeypatch.setenv("PHAROS_ALERTS_DB", str(tmp_path / "alerts.db"))
    monkeypatch.setattr("pharos.notify.notify", lambda *a, **k: {})

    runner = CliRunner()
    fail_cfg = _failing_config(tmp_path)
    runner.invoke(cli.app, ["run", "--config", fail_cfg])  # opens c1

    # alerts (active) shows c1
    r = runner.invoke(cli.app, ["alerts"])
    assert r.exit_code == 0
    assert "c1" in r.stdout

    # alerts --json is machine-readable
    rj = runner.invoke(cli.app, ["alerts", "--json"])
    assert rj.exit_code == 0
    payload = __import__("json").loads(rj.stdout)
    assert payload[0]["key"] == "c1"
    assert payload[0]["status"] == "firing"

    # alert <key> shows detail incl. runbook
    rd = runner.invoke(cli.app, ["alert", "c1"])
    assert "runbook" in rd.stdout
    assert "example.invalid/rb" in rd.stdout

    # ack → status becomes acked, still active
    ra = runner.invoke(cli.app, ["ack", "c1", "--note", "looking"])
    assert ra.exit_code == 0
    assert store.get("c1")["status"] == store.ACKED

    # resolve → leaves active view
    rr = runner.invoke(cli.app, ["resolve", "c1"])
    assert rr.exit_code == 0
    assert store.get("c1")["status"] == store.RESOLVED
    assert runner.invoke(cli.app, ["alerts"]).stdout.strip() == "no active alerts"

    # --all still includes the resolved ticket
    assert "c1" in runner.invoke(cli.app, ["alerts", "--all"]).stdout


def test_cli_ack_unknown_key_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from pharos import cli

    monkeypatch.setenv("PHAROS_ALERTS_DB", str(tmp_path / "alerts.db"))
    runner = CliRunner()
    assert runner.invoke(cli.app, ["ack", "nope"]).exit_code == 1
    assert runner.invoke(cli.app, ["resolve", "nope"]).exit_code == 1
    assert runner.invoke(cli.app, ["alert", "nope"]).exit_code == 1
