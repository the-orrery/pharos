"""The nine generic, parameterised check-types.

Each class is built entirely from config params (no hardcoded paths/labels/
URLs), drives one or more probes from :mod:`pharos.checks.probes`, and returns a
:class:`HealthEvent`.  OK/DEGRADED/DOWN + severity semantics are ported
from the original pharos design and kept stable by tests.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pharos.checks import probes
from pharos.notify.base import HealthEvent, Severity, Status

# ---------------------------------------------------------------------------
# JSON-pointer helper (RFC 6901) — used by CommandJsonCheck & JsonStatusFileCheck
# ---------------------------------------------------------------------------


_PTR_MISSING = object()


def _resolve_pointer(doc: object, pointer: str) -> object:
    """Resolve an RFC-6901 JSON pointer against *doc*.

    Returns a sentinel (``_PTR_MISSING``) when any segment is absent so callers
    can distinguish "missing" from a real ``None`` value.  An empty pointer
    (``""``) returns the whole document.
    """
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        # Be lenient: treat a bare field name as a single top-level segment.
        pointer = "/" + pointer
    cur: object = doc
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if token not in cur:
                return _PTR_MISSING
            cur = cur[token]
        elif isinstance(cur, list):
            try:
                idx = int(token)
            except ValueError:
                return _PTR_MISSING
            if idx < 0 or idx >= len(cur):
                return _PTR_MISSING
            cur = cur[idx]
        else:
            return _PTR_MISSING
    return cur


def _read_text(path: str) -> str | None:
    """Read a text file, returning ``None`` on any OS error (missing/denied)."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 1. CommandJsonCheck
# ---------------------------------------------------------------------------


class CommandJsonCheck:
    """Run a command, parse its stdout as JSON, assert a pointer equals a value.

    OK when the command exits 0 *and* the JSON pointer resolves to
    ``success_field_value``; otherwise the configured ``fail_status`` /
    ``fail_severity`` (default DEGRADED / WARNING). Non-JSON / unparseable
    output is treated as a failure.
    """

    def __init__(
        self,
        id: str,
        source: str,
        command: list[str],
        success_field_path: str,
        success_field_value: object,
        fail_status: Status = Status.DEGRADED,
        fail_severity: Severity = Severity.WARNING,
        runbook: str = "",
        ok_title: str = "",
        fail_title: str = "",
        failure_detail_field_path: str = "",
    ) -> None:
        self.id = id
        self.source = source
        self.command = command
        self.success_field_path = success_field_path
        self.success_field_value = success_field_value
        self.fail_status = fail_status
        self.fail_severity = fail_severity
        self.runbook = runbook
        self.ok_title = ok_title
        self.fail_title = fail_title
        self.failure_detail_field_path = failure_detail_field_path

    def run(self) -> HealthEvent:
        if not self.command:
            return self._fail("command is empty", {"command": self.command})
        out = probes.run_cmd(self.command[0], self.command[1:])
        parsed = probes.json_from_cmd(out)
        if parsed is None:
            return self._fail(
                f"{self.source} command did not return JSON",
                {
                    "success": out.ok,
                    "returncode": out.returncode,
                    "stderr": out.stderr,
                    "stdout": out.stdout,
                },
            )
        actual = _resolve_pointer(parsed, self.success_field_path)
        matched = actual is not _PTR_MISSING and actual == self.success_field_value
        if out.ok and matched:
            return HealthEvent(
                source=self.source,
                status=Status.OK,
                severity=Severity.INFO,
                title=self.ok_title or f"{self.source} command JSON check passed",
                detail=(f"{self.success_field_path} == {self.success_field_value!r}"),
                evidence={
                    "command": self.command,
                    "pointer": self.success_field_path,
                    "value": actual,
                    "runbook": self.runbook,
                },
            )
        return self._fail(
            f"{self.source} command JSON check failed",
            {
                "command": self.command,
                "success": out.ok,
                "pointer": self.success_field_path,
                "expected": self.success_field_value,
                "actual": None if actual is _PTR_MISSING else actual,
                "stderr": out.stderr,
            },
            detail=self._failure_detail(parsed),
        )

    def _failure_detail(self, parsed: object) -> str:
        if not self.failure_detail_field_path:
            return ""
        value = _resolve_pointer(parsed, self.failure_detail_field_path)
        if value is _PTR_MISSING or value is None:
            return ""
        if isinstance(value, str):
            return value[:1024]
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)[:1024]
        except TypeError:
            return str(value)[:1024]

    def _fail(
        self, title: str, evidence: dict[str, Any], detail: str = ""
    ) -> HealthEvent:
        evidence.setdefault("runbook", self.runbook)
        return HealthEvent(
            source=self.source,
            status=self.fail_status,
            severity=self.fail_severity,
            title=self.fail_title or title,
            detail=detail,
            evidence=evidence,
        )


# ---------------------------------------------------------------------------
# 2. PidfileCheck
# ---------------------------------------------------------------------------


class PidfileCheck:
    """Verify a pidfile points at a live, non-stale process.

    Absent pidfile → OK when ``absent_is_ok``; otherwise DEGRADED. Present but
    unparseable / dead / older than ``stale_after_secs`` → DEGRADED.
    """

    def __init__(
        self,
        id: str,
        source: str,
        pidfile_path: str,
        stale_after_secs: float,
        absent_is_ok: bool = True,
        runbook: str = "",
    ) -> None:
        self.id = id
        self.source = source
        self.pidfile_path = pidfile_path
        self.stale_after_secs = stale_after_secs
        self.absent_is_ok = absent_is_ok
        self.runbook = runbook

    def run(self) -> HealthEvent:
        age_secs = probes.path_age_secs(self.pidfile_path)
        raw = _read_text(self.pidfile_path)
        if raw is None:
            absent = self.absent_is_ok
            return HealthEvent(
                source=self.source,
                status=Status.OK if absent else Status.DEGRADED,
                severity=Severity.INFO if absent else Severity.WARNING,
                title=(
                    f"{self.source} has no active pidfile"
                    if absent
                    else f"{self.source} pidfile is missing"
                ),
                evidence={
                    "pidfile": self.pidfile_path,
                    "exists": False,
                    "runbook": self.runbook,
                },
            )
        pid: int | None
        try:
            pid = int(raw.strip())
        except ValueError:
            pid = None
        alive = probes.pid_alive(pid) if pid is not None else False
        stale = age_secs is not None and age_secs > self.stale_after_secs
        ok = pid is not None and alive and not stale
        if ok:
            title = f"{self.source} process running pid={pid} age_secs={age_secs}"
        elif pid is None:
            title = f"{self.source} pidfile is invalid age_secs={age_secs}"
        elif not alive:
            title = f"{self.source} stale pidfile; pid={pid} is not alive"
        else:
            title = f"{self.source} pidfile is older than expected age_secs={age_secs}"
        return HealthEvent(
            source=self.source,
            status=Status.OK if ok else Status.DEGRADED,
            severity=Severity.INFO if ok else Severity.WARNING,
            title=title,
            evidence={
                "pidfile": self.pidfile_path,
                "pid": pid,
                "pid_alive": alive,
                "age_secs": age_secs,
                "stale_after_secs": self.stale_after_secs,
                "runbook": self.runbook,
            },
        )


# ---------------------------------------------------------------------------
# 3. LaunchdCheck
# ---------------------------------------------------------------------------


class LaunchdCheck:
    """Verify a launchd job is registered / running (and optionally has a substring).

    Not found (launchctl print failed) or ``require_running`` and not running →
    DOWN / CRITICAL.  Registered+running but a configured
    ``require_output_contains`` substring is missing → DEGRADED / WARNING.
    Otherwise OK.
    """

    def __init__(
        self,
        id: str,
        source: str,
        label: str,
        require_running: bool,
        require_output_contains: str | None = None,
        runbook: str = "",
    ) -> None:
        self.id = id
        self.source = source
        self.label = label
        self.require_running = require_running
        self.require_output_contains = require_output_contains
        self.runbook = runbook

    def run(self) -> HealthEvent:
        state = probes.launchd_state(self.label)
        registered = bool(state["command_success"])
        running = bool(state["running"])
        raw_stdout: str = state["raw_stdout"]
        substr_ok = (
            self.require_output_contains is None
            or self.require_output_contains in raw_stdout
        )

        if not registered or (self.require_running and not running):
            status, severity = Status.DOWN, Severity.CRITICAL
            title = f"{self.source} launchd job {self.label} not running/registered"
        elif not substr_ok:
            status, severity = Status.DEGRADED, Severity.WARNING
            title = (
                f"{self.source} launchd job {self.label} missing expected output "
                f"{self.require_output_contains!r}"
            )
        else:
            status, severity = Status.OK, Severity.INFO
            title = f"{self.source} launchd job {self.label} healthy"

        return HealthEvent(
            source=self.source,
            status=status,
            severity=severity,
            title=title,
            evidence={
                "label": self.label,
                "registered": registered,
                "running": running,
                "pid": state["pid"],
                "require_running": self.require_running,
                "require_output_contains": self.require_output_contains,
                "output_contains_substring": substr_ok,
                "runbook": self.runbook,
            },
        )


# ---------------------------------------------------------------------------
# 4. FileContainsCheck
# ---------------------------------------------------------------------------


class FileContainsCheck:
    """Assert a file is readable and contains every required substring.

    Unreadable/absent file → ``absent_status`` / ``absent_severity`` (default
    DEGRADED / WARNING).  Readable but any required substring missing →
    DEGRADED / WARNING.  All present → OK.
    """

    def __init__(
        self,
        id: str,
        source: str,
        path: str,
        required_substrings: list[str],
        absent_status: Status = Status.DEGRADED,
        absent_severity: Severity = Severity.WARNING,
        runbook: str = "",
    ) -> None:
        self.id = id
        self.source = source
        self.path = path
        self.required_substrings = required_substrings
        self.absent_status = absent_status
        self.absent_severity = absent_severity
        self.runbook = runbook

    def run(self) -> HealthEvent:
        content = _read_text(self.path)
        if content is None:
            return HealthEvent(
                source=self.source,
                status=self.absent_status,
                severity=self.absent_severity,
                title=f"{self.source} file is not readable: {self.path}",
                evidence={
                    "path": self.path,
                    "exists": False,
                    "required_substrings": self.required_substrings,
                    "runbook": self.runbook,
                },
            )
        missing = [s for s in self.required_substrings if s not in content]
        ok = not missing
        return HealthEvent(
            source=self.source,
            status=Status.OK if ok else Status.DEGRADED,
            severity=Severity.INFO if ok else Severity.WARNING,
            title=(
                f"{self.source} file contains all required substrings"
                if ok
                else f"{self.source} file is missing required substrings"
            ),
            evidence={
                "path": self.path,
                "required_substrings": self.required_substrings,
                "missing_substrings": missing,
                "runbook": self.runbook,
            },
        )


# ---------------------------------------------------------------------------
# 5. UnixSocketPingCheck
# ---------------------------------------------------------------------------


class UnixSocketPingCheck:
    """Ping a unix-domain socket and check the reply for a pong substring.

    OK when the ping reply contains ``pong_substring``.  If
    ``absent_ok_if_no_process`` is a pgrep pattern AND the socket is absent AND
    no matching process is running → OK (the daemon is idle and may cold-start
    on demand). Anything else (socket absent but process present, or ping
    failed) → DEGRADED / WARNING.
    """

    def __init__(
        self,
        id: str,
        source: str,
        socket_path: str,
        ping_payload: bytes = b'{"ping":true}\n',
        pong_substring: str = "pong",
        absent_ok_if_no_process: str | None = None,
        runbook: str = "",
    ) -> None:
        self.id = id
        self.source = source
        self.socket_path = socket_path
        self.ping_payload = ping_payload
        self.pong_substring = pong_substring
        self.absent_ok_if_no_process = absent_ok_if_no_process
        self.runbook = runbook

    def run(self) -> HealthEvent:
        socket_exists = Path(self.socket_path).exists()
        ping: dict[str, Any] | None = None
        if socket_exists:
            ping = probes.unix_socket_ping(self.socket_path, payload=self.ping_payload)
        reply = ping.get("reply") if ping else None
        ping_ok = bool(
            ping
            and ping.get("connected")
            and reply is not None
            and self.pong_substring in reply
        )

        processes: list[str] = []
        if self.absent_ok_if_no_process is not None:
            processes = probes.pgrep_full(self.absent_ok_if_no_process)

        if ping_ok:
            status, severity = Status.OK, Severity.INFO
            title = f"{self.source} socket ping ok"
        elif (
            self.absent_ok_if_no_process is not None
            and not socket_exists
            and not processes
        ):
            status, severity = Status.OK, Severity.INFO
            title = f"{self.source} daemon is idle (no socket, no process)"
        else:
            status, severity = Status.DEGRADED, Severity.WARNING
            title = f"{self.source} socket is not responding"

        return HealthEvent(
            source=self.source,
            status=status,
            severity=severity,
            title=title,
            evidence={
                "socket_path": self.socket_path,
                "socket_exists": socket_exists,
                "ping_ok": ping_ok,
                "reply": reply,
                "latency_ms": ping.get("latency_ms") if ping else None,
                "error": ping.get("error") if ping else None,
                "processes": processes,
                "runbook": self.runbook,
            },
        )


# ---------------------------------------------------------------------------
# 6. HttpCheck
# ---------------------------------------------------------------------------


class HttpCheck:
    """Make a single HTTP request and check the status code against a range.

    Status code inside ``success_status_range`` (inclusive) → OK.  Reachable but
    out-of-range → DEGRADED / WARNING. Network/transport error → DOWN /
    CRITICAL.
    """

    def __init__(
        self,
        id: str,
        source: str,
        url: str,
        method: str = "GET",
        json_body: object | None = None,
        timeout: float = 5.0,
        success_status_range: tuple[int, int] = (200, 299),
        runbook: str = "",
        ca_bundle: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.id = id
        self.source = source
        self.url = url
        self.method = method.upper()
        self.json_body = json_body
        self.timeout = timeout
        self.success_status_range = success_status_range
        self.runbook = runbook
        self.ca_bundle = ca_bundle or None
        self.headers = headers

    def run(self) -> HealthEvent:
        lo, hi = self.success_status_range
        try:
            if self.method == "POST":
                code, body = probes.http_post(
                    self.url,
                    self.json_body,
                    timeout=self.timeout,
                    ca_bundle=self.ca_bundle,
                )
            else:
                code, body = probes.http_get(
                    self.url,
                    timeout=self.timeout,
                    ca_bundle=self.ca_bundle,
                    headers=self.headers,
                )
        except RuntimeError as exc:
            return HealthEvent(
                source=self.source,
                status=Status.DOWN,
                severity=Severity.CRITICAL,
                title=f"{self.source} HTTP request failed",
                detail=str(exc),
                evidence={
                    "url": probes._redact_url(self.url),
                    "method": self.method,
                    "error": str(exc),
                    "runbook": self.runbook,
                },
            )
        ok = lo <= code <= hi
        return HealthEvent(
            source=self.source,
            status=Status.OK if ok else Status.DEGRADED,
            severity=Severity.INFO if ok else Severity.WARNING,
            title=f"{self.source} HTTP status={code}",
            evidence={
                "url": probes._redact_url(self.url),
                "method": self.method,
                "status_code": code,
                "success_status_range": list(self.success_status_range),
                "body_excerpt": body[:512],
                "runbook": self.runbook,
            },
        )


# ---------------------------------------------------------------------------
# 7. PortOwnerCheck
# ---------------------------------------------------------------------------


class PortOwnerCheck:
    """Assert the process listening on a TCP port matches an expected owner.

    OK when any listener line for the port contains ``expected_owner_substring``;
    otherwise DEGRADED / WARNING.
    """

    def __init__(
        self,
        id: str,
        source: str,
        port: int,
        expected_owner_substring: str,
        runbook: str = "",
    ) -> None:
        self.id = id
        self.source = source
        self.port = port
        self.expected_owner_substring = expected_owner_substring
        self.runbook = runbook

    def run(self) -> HealthEvent:
        listeners = probes.port_listeners(self.port)
        matched = any(self.expected_owner_substring in line for line in listeners)
        return HealthEvent(
            source=self.source,
            status=Status.OK if matched else Status.DEGRADED,
            severity=Severity.INFO if matched else Severity.WARNING,
            title=(
                f"port {self.port} owner contains "
                f"{self.expected_owner_substring!r} matched={matched}"
            ),
            evidence={
                "port": self.port,
                "expected": self.expected_owner_substring,
                "listeners": listeners,
                "runbook": self.runbook,
            },
        )


# ---------------------------------------------------------------------------
# 8. JsonStatusFileCheck
# ---------------------------------------------------------------------------


class JsonStatusFileCheck:
    """Read a JSON status file, map a state field to a status, honour staleness.

    Missing / non-JSON file → UNKNOWN / WARNING. The state value is mapped via
    ``ok_states`` / ``degraded_states`` / ``down_states``; an unknown state →
    UNKNOWN / WARNING.
    Independently, when the ``timestamp_field`` (epoch seconds) is older than
    ``stale_threshold_secs`` the result is downgraded to ``stale_status`` —
    except for down/failed states, which stay DOWN regardless.
    """

    def __init__(
        self,
        id: str,
        source: str,
        status_file_path: str,
        state_field: str,
        timestamp_field: str,
        stale_threshold_secs: int,
        ok_states: list[str],
        degraded_states: list[str],
        down_states: list[str],
        stale_status: Status = Status.DEGRADED,
        runbook: str = "",
    ) -> None:
        self.id = id
        self.source = source
        self.status_file_path = status_file_path
        self.state_field = state_field
        self.timestamp_field = timestamp_field
        self.stale_threshold_secs = stale_threshold_secs
        self.ok_states = ok_states
        self.degraded_states = degraded_states
        self.down_states = down_states
        self.stale_status = stale_status
        self.runbook = runbook

    def run(self) -> HealthEvent:
        content = _read_text(self.status_file_path)
        if content is None:
            return self._unknown("status file missing", exists=False)
        try:
            doc = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return self._unknown("status file is not valid JSON", exists=True)

        state_val = _resolve_pointer(doc, self.state_field)
        state = state_val if isinstance(state_val, str) else "unknown"

        # File age via mtime (matches path_age_secs); also surface the in-file
        # timestamp when it is a parseable epoch-seconds number.
        age_secs = probes.path_age_secs(self.status_file_path)
        ts_val = _resolve_pointer(doc, self.timestamp_field)
        if isinstance(ts_val, bool):
            pass  # bool is an int subclass; not a timestamp
        elif isinstance(ts_val, (int, float)):
            age_secs = time.time() - float(ts_val)
        elif isinstance(ts_val, str):
            parsed = _parse_iso8601(ts_val)
            if parsed is not None:
                age_secs = time.time() - parsed
        stale = age_secs is not None and age_secs > self.stale_threshold_secs

        if state in self.down_states:
            status, severity = Status.DOWN, Severity.CRITICAL
        elif state in self.ok_states:
            if stale:
                status, severity = self.stale_status, _severity_for(self.stale_status)
            else:
                status, severity = Status.OK, Severity.INFO
        elif state in self.degraded_states:
            status, severity = Status.DEGRADED, Severity.WARNING
        elif stale:
            status, severity = self.stale_status, _severity_for(self.stale_status)
        else:
            status, severity = Status.UNKNOWN, Severity.WARNING

        return HealthEvent(
            source=self.source,
            status=status,
            severity=severity,
            title=f"{self.source} status state={state} age_secs={age_secs} stale={stale}",
            evidence={
                "path": self.status_file_path,
                "state": state,
                "age_secs": age_secs,
                "stale": stale,
                "stale_threshold_secs": self.stale_threshold_secs,
                "runbook": self.runbook,
            },
        )

    def _unknown(self, why: str, *, exists: bool) -> HealthEvent:
        return HealthEvent(
            source=self.source,
            status=Status.UNKNOWN,
            severity=Severity.WARNING,
            title=f"{self.source} {why}",
            evidence={
                "path": self.status_file_path,
                "exists": exists,
                "runbook": self.runbook,
            },
        )


# ---------------------------------------------------------------------------
# 9. CommandCheck  — generic "run a command, exit 0 = healthy" probe
# ---------------------------------------------------------------------------


class CommandCheck:
    """Run a command and map its exit code (and optionally its stdout) to a status.

    Exit 0 → OK / INFO.  Non-zero exit (or the command could not run) →
    the configured ``fail_status`` / ``fail_severity`` (default DOWN /
    CRITICAL). This can also wrap SSH-proxied or otherwise delegated health
    probes.

    When any of ``require_output_contains`` / ``forbid_output_contains`` /
    ``min_stdout_bytes`` is set, exit 0 is necessary but not sufficient: an
    exit-0 command whose stdout misses a required substring, carries a
    forbidden one, or is shorter than ``min_stdout_bytes`` also fails.  This
    catches commands that "succeed" while silently emitting empty/degraded
    output — e.g. a session-start surface whose generator stopped producing a
    body but still exits 0.  With none of them set, behaviour is unchanged
    (pure exit-code mapping).
    """

    def __init__(
        self,
        id: str,
        source: str,
        command: list[str],
        timeout: float = 30.0,
        fail_status: Status = Status.DOWN,
        fail_severity: Severity = Severity.CRITICAL,
        require_output_contains: list[str] | None = None,
        forbid_output_contains: list[str] | None = None,
        min_stdout_bytes: int = 0,
        runbook: str = "",
    ) -> None:
        self.id = id
        self.source = source
        self.command = command
        self.timeout = timeout
        self.fail_status = fail_status
        self.fail_severity = fail_severity
        self.require_output_contains = require_output_contains or []
        self.forbid_output_contains = forbid_output_contains or []
        self.min_stdout_bytes = min_stdout_bytes
        self.runbook = runbook

    def run(self) -> HealthEvent:
        if not self.command:
            return self._fail(
                f"{self.source} command failed (empty command)",
                "",
                {"command": self.command},
            )
        out = probes.run_cmd(self.command[0], self.command[1:], timeout=self.timeout)
        if not out.ok:
            # Bound stderr so a noisy/secret-laden tail can't flood the alert.
            stderr = out.stderr.strip()[:512]
            return self._fail(
                f"{self.source} command failed (exit {out.returncode})",
                stderr,
                {
                    "command": self.command,
                    "returncode": out.returncode,
                    "stderr": stderr,
                },
            )
        problems = self._output_problems(out.stdout)
        if problems:
            return self._fail(
                f"{self.source} command exited 0 but output check failed",
                "; ".join(problems),
                {
                    "command": self.command,
                    "returncode": out.returncode,
                    "problems": problems,
                    "stdout_excerpt": out.stdout.strip()[:512],
                },
            )
        return HealthEvent(
            source=self.source,
            status=Status.OK,
            severity=Severity.INFO,
            title=f"{self.source} command ok",
            evidence={
                "command": self.command,
                "returncode": out.returncode,
                "runbook": self.runbook,
            },
        )

    def _output_problems(self, stdout: str) -> list[str]:
        problems: list[str] = []
        if self.min_stdout_bytes:
            nbytes = len(stdout.strip().encode("utf-8"))
            if nbytes < self.min_stdout_bytes:
                problems.append(
                    f"stdout too short ({nbytes} < {self.min_stdout_bytes} bytes)"
                )
        problems += [
            f"missing required substring {s!r}"
            for s in self.require_output_contains
            if s not in stdout
        ]
        problems += [
            f"contains forbidden substring {s!r}"
            for s in self.forbid_output_contains
            if s in stdout
        ]
        return problems

    def _fail(self, title: str, detail: str, evidence: dict[str, Any]) -> HealthEvent:
        evidence.setdefault("runbook", self.runbook)
        return HealthEvent(
            source=self.source,
            status=self.fail_status,
            severity=self.fail_severity,
            title=title,
            detail=detail,
            evidence=evidence,
        )


def _parse_iso8601(value: str) -> float | None:
    """ISO-8601 / RFC3339 时间戳 → epoch 秒(失败 None)。有些状态文件的
    timestamp 字段是 ISO 字符串而非 epoch 数字, 不解析就只能回退文件 mtime。"""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _severity_for(status: Status) -> Severity:
    """Default severity paired with a status when a check downgrades to it."""
    if status is Status.DOWN:
        return Severity.CRITICAL
    if status is Status.DEGRADED:
        return Severity.WARNING
    return Severity.INFO
