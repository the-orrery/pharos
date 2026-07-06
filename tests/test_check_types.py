"""Tests for the eight generic check-types.

Probe functions are monkeypatched — no real launchd / sockets / http / lsof is
ever touched. Each test asserts the OK/DEGRADED/DOWN + severity mapping.
"""

from __future__ import annotations

from typing import Any

import pytest

from pharos.checks import probes, types
from pharos.checks.probes import CommandOutput
from pharos.notify.base import Severity, Status

# ---------------------------------------------------------------------------
# CommandJsonCheck
# ---------------------------------------------------------------------------


class TestCommandJsonCheck:
    def test_ok_when_pointer_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes, "run_cmd", lambda *a, **k: CommandOutput(0, '{"ok": true}', "")
        )
        chk = types.CommandJsonCheck(
            id="c",
            source="svc",
            command=["x"],
            success_field_path="/ok",
            success_field_value=True,
        )
        ev = chk.run()
        assert ev.status is Status.OK
        assert ev.severity is Severity.INFO

    def test_degraded_when_value_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            probes, "run_cmd", lambda *a, **k: CommandOutput(0, '{"ok": false}', "")
        )
        chk = types.CommandJsonCheck(
            id="c",
            source="svc",
            command=["x"],
            success_field_path="/ok",
            success_field_value=True,
        )
        ev = chk.run()
        assert ev.status is Status.DEGRADED
        assert ev.severity is Severity.WARNING

    def test_custom_fail_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes, "run_cmd", lambda *a, **k: CommandOutput(1, "not json", "boom")
        )
        chk = types.CommandJsonCheck(
            id="c",
            source="svc",
            command=["x"],
            success_field_path="/ok",
            success_field_value=True,
            fail_status=Status.DOWN,
            fail_severity=Severity.CRITICAL,
        )
        ev = chk.run()
        assert ev.status is Status.DOWN
        assert ev.severity is Severity.CRITICAL

    def test_nested_pointer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes,
            "run_cmd",
            lambda *a, **k: CommandOutput(0, '{"data": {"status": "up"}}', ""),
        )
        chk = types.CommandJsonCheck(
            id="c",
            source="svc",
            command=["x"],
            success_field_path="/data/status",
            success_field_value="up",
        )
        assert chk.run().status is Status.OK

    def test_custom_titles_and_failure_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            probes,
            "run_cmd",
            lambda *a, **k: CommandOutput(
                0,
                '{"ok": false, "detail": {"tool": "docket", "reason": "behind"}}',
                "",
            ),
        )
        chk = types.CommandJsonCheck(
            id="c",
            source="svc",
            command=["x"],
            success_field_path="/ok",
            success_field_value=True,
            ok_title="custom ok",
            fail_title="custom fail",
            failure_detail_field_path="/detail",
        )
        ev = chk.run()
        assert ev.title == "custom fail"
        assert ev.detail == '{"reason": "behind", "tool": "docket"}'


# ---------------------------------------------------------------------------
# PidfileCheck
# ---------------------------------------------------------------------------


class TestPidfileCheck:
    def test_absent_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: None)
        monkeypatch.setattr(probes, "path_age_secs", lambda p: None)
        chk = types.PidfileCheck(
            id="p", source="svc", pidfile_path="/x.pid", stale_after_secs=60
        )
        assert chk.run().status is Status.OK

    def test_absent_not_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: None)
        monkeypatch.setattr(probes, "path_age_secs", lambda p: None)
        chk = types.PidfileCheck(
            id="p",
            source="svc",
            pidfile_path="/x.pid",
            stale_after_secs=60,
            absent_is_ok=False,
        )
        assert chk.run().status is Status.DEGRADED

    def test_alive_and_fresh_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: "1234\n")
        monkeypatch.setattr(probes, "path_age_secs", lambda p: 5.0)
        monkeypatch.setattr(probes, "pid_alive", lambda pid: True)
        chk = types.PidfileCheck(
            id="p", source="svc", pidfile_path="/x.pid", stale_after_secs=60
        )
        assert chk.run().status is Status.OK

    def test_dead_pid_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: "1234\n")
        monkeypatch.setattr(probes, "path_age_secs", lambda p: 5.0)
        monkeypatch.setattr(probes, "pid_alive", lambda pid: False)
        chk = types.PidfileCheck(
            id="p", source="svc", pidfile_path="/x.pid", stale_after_secs=60
        )
        assert chk.run().status is Status.DEGRADED

    def test_stale_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: "1234\n")
        monkeypatch.setattr(probes, "path_age_secs", lambda p: 999.0)
        monkeypatch.setattr(probes, "pid_alive", lambda pid: True)
        chk = types.PidfileCheck(
            id="p", source="svc", pidfile_path="/x.pid", stale_after_secs=60
        )
        assert chk.run().status is Status.DEGRADED


# ---------------------------------------------------------------------------
# LaunchdCheck
# ---------------------------------------------------------------------------


def _launchd(running: bool, success: bool, stdout: str = "") -> dict[str, Any]:
    return {
        "label": "lbl",
        "running": running,
        "pid": "42" if running else None,
        "command_success": success,
        "raw_stdout": stdout,
    }


class TestLaunchdCheck:
    def test_not_registered_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes, "launchd_state", lambda label: _launchd(False, False)
        )
        chk = types.LaunchdCheck(
            id="l", source="svc", label="lbl", require_running=True
        )
        ev = chk.run()
        assert ev.status is Status.DOWN
        assert ev.severity is Severity.CRITICAL

    def test_running_but_required_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes, "launchd_state", lambda label: _launchd(False, True)
        )
        chk = types.LaunchdCheck(
            id="l", source="svc", label="lbl", require_running=True
        )
        assert chk.run().status is Status.DOWN

    def test_running_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probes, "launchd_state", lambda label: _launchd(True, True))
        chk = types.LaunchdCheck(
            id="l", source="svc", label="lbl", require_running=True
        )
        assert chk.run().status is Status.OK

    def test_missing_substring_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes,
            "launchd_state",
            lambda label: _launchd(True, True, stdout="state = running"),
        )
        chk = types.LaunchdCheck(
            id="l",
            source="svc",
            label="lbl",
            require_running=True,
            require_output_contains="com.apple.axserver",
        )
        ev = chk.run()
        assert ev.status is Status.DEGRADED
        assert ev.severity is Severity.WARNING

    def test_substring_present_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes,
            "launchd_state",
            lambda label: _launchd(True, True, stdout='"com.apple.axserver"'),
        )
        chk = types.LaunchdCheck(
            id="l",
            source="svc",
            label="lbl",
            require_running=True,
            require_output_contains="com.apple.axserver",
        )
        assert chk.run().status is Status.OK


# ---------------------------------------------------------------------------
# FileContainsCheck
# ---------------------------------------------------------------------------


class TestFileContainsCheck:
    def test_absent_default_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: None)
        chk = types.FileContainsCheck(
            id="f", source="svc", path="/x", required_substrings=["a"]
        )
        assert chk.run().status is Status.DEGRADED

    def test_all_present_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: "alpha beta gamma")
        chk = types.FileContainsCheck(
            id="f", source="svc", path="/x", required_substrings=["alpha", "gamma"]
        )
        assert chk.run().status is Status.OK

    def test_missing_substring_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: "alpha only")
        chk = types.FileContainsCheck(
            id="f", source="svc", path="/x", required_substrings=["alpha", "zeta"]
        )
        ev = chk.run()
        assert ev.status is Status.DEGRADED
        assert "zeta" in ev.evidence["missing_substrings"]


# ---------------------------------------------------------------------------
# UnixSocketPingCheck
# ---------------------------------------------------------------------------


class TestUnixSocketPingCheck:
    def test_ping_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types.Path, "exists", lambda self: True)
        monkeypatch.setattr(
            probes,
            "unix_socket_ping",
            lambda path, **k: {
                "connected": True,
                "reply": "pong\n",
                "latency_ms": 1.0,
                "error": None,
            },
        )
        chk = types.UnixSocketPingCheck(id="u", source="svc", socket_path="/s.sock")
        assert chk.run().status is Status.OK

    def test_idle_ok_when_no_socket_no_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(types.Path, "exists", lambda self: False)
        monkeypatch.setattr(probes, "pgrep_full", lambda pat: [])
        chk = types.UnixSocketPingCheck(
            id="u",
            source="svc",
            socket_path="/s.sock",
            absent_ok_if_no_process="myd.py",
        )
        assert chk.run().status is Status.OK

    def test_absent_but_process_present_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(types.Path, "exists", lambda self: False)
        monkeypatch.setattr(probes, "pgrep_full", lambda pat: ["123 myd.py"])
        chk = types.UnixSocketPingCheck(
            id="u",
            source="svc",
            socket_path="/s.sock",
            absent_ok_if_no_process="myd.py",
        )
        assert chk.run().status is Status.DEGRADED

    def test_ping_fail_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types.Path, "exists", lambda self: True)
        monkeypatch.setattr(
            probes,
            "unix_socket_ping",
            lambda path, **k: {
                "connected": True,
                "reply": "nope",
                "latency_ms": 1.0,
                "error": None,
            },
        )
        chk = types.UnixSocketPingCheck(id="u", source="svc", socket_path="/s.sock")
        assert chk.run().status is Status.DEGRADED


# ---------------------------------------------------------------------------
# HttpCheck
# ---------------------------------------------------------------------------


class TestHttpCheck:
    def test_2xx_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probes, "http_get", lambda url, **k: (200, "{}"))
        chk = types.HttpCheck(id="h", source="svc", url="http://x/healthz")
        ev = chk.run()
        assert ev.status is Status.OK
        assert ev.severity is Severity.INFO

    def test_out_of_range_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probes, "http_get", lambda url, **k: (503, "down"))
        chk = types.HttpCheck(id="h", source="svc", url="http://x/healthz")
        assert chk.run().status is Status.DEGRADED

    def test_transport_error_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(url: str, **k: Any) -> tuple[int, str]:
            raise RuntimeError("connection refused")

        monkeypatch.setattr(probes, "http_get", _boom)
        chk = types.HttpCheck(id="h", source="svc", url="http://x/healthz")
        ev = chk.run()
        assert ev.status is Status.DOWN
        assert ev.severity is Severity.CRITICAL

    def test_post_uses_http_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: dict[str, Any] = {}

        def _post(url: str, body: Any, **k: Any) -> tuple[int, str]:
            called["body"] = body
            return 200, "{}"

        monkeypatch.setattr(probes, "http_post", _post)
        chk = types.HttpCheck(
            id="h",
            source="svc",
            url="http://x/e",
            method="POST",
            json_body={"q": 1},
        )
        assert chk.run().status is Status.OK
        assert called["body"] == {"q": 1}


# ---------------------------------------------------------------------------
# PortOwnerCheck
# ---------------------------------------------------------------------------


class TestPortOwnerCheck:
    def test_owner_matches_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes, "port_listeners", lambda port: ["postgres 999 ... :5432 (LISTEN)"]
        )
        chk = types.PortOwnerCheck(
            id="po", source="svc", port=5432, expected_owner_substring="postgres"
        )
        assert chk.run().status is Status.OK

    def test_owner_mismatch_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes, "port_listeners", lambda port: ["nginx 1 ... :5432 (LISTEN)"]
        )
        chk = types.PortOwnerCheck(
            id="po", source="svc", port=5432, expected_owner_substring="postgres"
        )
        assert chk.run().status is Status.DEGRADED

    def test_no_listener_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probes, "port_listeners", lambda port: [])
        chk = types.PortOwnerCheck(
            id="po", source="svc", port=5432, expected_owner_substring="postgres"
        )
        assert chk.run().status is Status.DEGRADED


# ---------------------------------------------------------------------------
# JsonStatusFileCheck
# ---------------------------------------------------------------------------


class TestJsonStatusFileCheck:
    def _chk(self, **over: Any) -> types.JsonStatusFileCheck:
        kw: dict[str, Any] = {
            "id": "j",
            "source": "svc",
            "status_file_path": "/s.json",
            "state_field": "/state",
            "timestamp_field": "/ts",
            "stale_threshold_secs": 3600,
            "ok_states": ["ok", "running"],
            "degraded_states": ["skipped"],
            "down_states": ["failed", "timeout"],
        }
        kw.update(over)
        return types.JsonStatusFileCheck(**kw)

    def test_missing_file_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: None)
        assert self._chk().run().status is Status.UNKNOWN

    def test_bad_json_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: "not json")
        assert self._chk().run().status is Status.UNKNOWN

    def test_ok_state_fresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        monkeypatch.setattr(
            types,
            "_read_text",
            lambda p: f'{{"state": "ok", "ts": {time.time()}}}',
        )
        assert self._chk().run().status is Status.OK

    def test_ok_state_stale_downgrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(types, "_read_text", lambda p: '{"state": "ok", "ts": 1}')
        ev = self._chk().run()
        assert ev.status is Status.DEGRADED

    def test_ok_state_fresh_iso8601_timestamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import UTC, datetime

        now_iso = datetime.now(UTC).isoformat()
        monkeypatch.setattr(
            types, "_read_text", lambda p: f'{{"state": "ok", "ts": "{now_iso}"}}'
        )
        # RFC3339 字符串时间戳应被解析(真实状态文件用 ISO, 非 epoch)→ 新鲜 → OK。
        assert self._chk().run().status is Status.OK

    def test_ok_state_stale_iso8601_timestamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 远古 ISO 时间戳 → stale → 降级。证明确实读了 in-file 字段:若回退到 mtime,
        # "/s.json" 不存在 → age None → 不 stale → 会是 OK 而非 DEGRADED。
        monkeypatch.setattr(
            types,
            "_read_text",
            lambda p: '{"state": "ok", "ts": "2020-01-01T00:00:00Z"}',
        )
        assert self._chk().run().status is Status.DEGRADED

    def test_down_state_critical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        monkeypatch.setattr(
            types,
            "_read_text",
            lambda p: f'{{"state": "failed", "ts": {time.time()}}}',
        )
        ev = self._chk().run()
        assert ev.status is Status.DOWN
        assert ev.severity is Severity.CRITICAL

    def test_degraded_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        monkeypatch.setattr(
            types,
            "_read_text",
            lambda p: f'{{"state": "skipped", "ts": {time.time()}}}',
        )
        assert self._chk().run().status is Status.DEGRADED

    def test_unknown_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        monkeypatch.setattr(
            types,
            "_read_text",
            lambda p: f'{{"state": "weird", "ts": {time.time()}}}',
        )
        assert self._chk().run().status is Status.UNKNOWN


# ---------------------------------------------------------------------------
# CommandCheck
# ---------------------------------------------------------------------------


class TestCommandCheck:
    def test_exit_zero_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes, "run_cmd", lambda *a, **k: CommandOutput(0, "ok", "")
        )
        chk = types.CommandCheck(id="c", source="svc", command=["true"])
        ev = chk.run()
        assert ev.status is Status.OK
        assert ev.severity is Severity.INFO

    def test_nonzero_uses_fail_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes, "run_cmd", lambda *a, **k: CommandOutput(7, "", "boom")
        )
        chk = types.CommandCheck(id="c", source="svc", command=["false"])
        ev = chk.run()
        assert ev.status is Status.DOWN
        assert ev.severity is Severity.CRITICAL
        assert "boom" in ev.detail
        assert ev.evidence["returncode"] == 7

    def test_custom_fail_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            probes, "run_cmd", lambda *a, **k: CommandOutput(1, "", "nope")
        )
        chk = types.CommandCheck(
            id="c",
            source="svc",
            command=["x"],
            fail_status=Status.DEGRADED,
            fail_severity=Severity.WARNING,
        )
        ev = chk.run()
        assert ev.status is Status.DEGRADED
        assert ev.severity is Severity.WARNING

    def test_require_output_contains_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            probes, "run_cmd", lambda *a, **k: CommandOutput(0, "ready\n  docs", "")
        )
        chk = types.CommandCheck(
            id="c", source="svc", command=["x"], require_output_contains=["ready"]
        )
        assert chk.run().status is Status.OK

    def test_exit_zero_but_missing_required_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the silent-empty-surface case: command "succeeds" but emits no body.
        monkeypatch.setattr(probes, "run_cmd", lambda *a, **k: CommandOutput(0, "", ""))
        chk = types.CommandCheck(
            id="c",
            source="svc",
            command=["x"],
            require_output_contains=["ready"],
            fail_status=Status.DEGRADED,
            fail_severity=Severity.WARNING,
        )
        ev = chk.run()
        assert ev.status is Status.DEGRADED
        assert "ready" in ev.detail
        assert ev.evidence["returncode"] == 0  # exited 0, failed on output

    def test_exit_zero_but_forbidden_substring_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wrapper can regress to printing usage text while still exiting 0.
        monkeypatch.setattr(
            probes,
            "run_cmd",
            lambda *a, **k: CommandOutput(0, "usage: tool <command>", ""),
        )
        chk = types.CommandCheck(
            id="c",
            source="svc",
            command=["x"],
            forbid_output_contains=["usage:"],
            fail_status=Status.DEGRADED,
            fail_severity=Severity.WARNING,
        )
        ev = chk.run()
        assert ev.status is Status.DEGRADED
        assert "usage:" in ev.detail

    def test_exit_zero_but_too_short_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            probes, "run_cmd", lambda *a, **k: CommandOutput(0, "hi", "")
        )
        chk = types.CommandCheck(
            id="c",
            source="svc",
            command=["x"],
            min_stdout_bytes=50,
            fail_status=Status.DEGRADED,
            fail_severity=Severity.WARNING,
        )
        assert chk.run().status is Status.DEGRADED
