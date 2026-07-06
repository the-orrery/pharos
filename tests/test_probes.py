"""Tests for pharos.checks.probes — local/pure probes only.

Real network calls (http_get / http_post) are skipped; launchd calls are
macOS-only and skipped on other platforms.
"""

from __future__ import annotations

import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pharos.checks.probes import (
    CommandOutput,
    json_from_cmd,
    path_age_secs,
    pgrep_full,
    pid_alive,
    run_cmd,
    unix_socket_ping,
)

# ---------------------------------------------------------------------------
# CommandOutput
# ---------------------------------------------------------------------------


class TestCommandOutput:
    def test_ok_true_when_returncode_zero(self) -> None:
        out = CommandOutput(returncode=0, stdout="hi", stderr="")
        assert out.ok is True

    def test_ok_false_when_nonzero(self) -> None:
        out = CommandOutput(returncode=1, stdout="", stderr="err")
        assert out.ok is False


# ---------------------------------------------------------------------------
# run_cmd
# ---------------------------------------------------------------------------


class TestRunCmd:
    def test_echo_success(self) -> None:
        out = run_cmd("echo", ["hello"])
        assert out.ok
        assert "hello" in out.stdout

    def test_failing_command(self) -> None:
        # `false` exits with code 1 on POSIX
        out = run_cmd("false", [])
        assert not out.ok
        assert out.returncode != 0

    def test_missing_binary_returns_error(self) -> None:
        out = run_cmd("__pharos_no_such_binary__", [])
        assert not out.ok
        assert out.stderr  # some error message present

    def test_env_overlay(self) -> None:
        out = run_cmd("env", [], env={"PHAROS_TEST_VAR": "sentinel"})
        assert out.ok
        assert "PHAROS_TEST_VAR=sentinel" in out.stdout

    def test_stdin_piped(self) -> None:
        out = run_cmd("cat", [], stdin="hello stdin")
        assert out.ok
        assert "hello stdin" in out.stdout

    def test_timeout_returns_error(self) -> None:
        out = run_cmd("sleep", ["10"], timeout=0.1)
        assert not out.ok
        assert "timeout" in out.stderr.lower()

    def test_env_and_stdin_combined(self) -> None:
        out = run_cmd(
            "sh",
            ["-c", 'echo "$PHAROS_VAR $(cat)"'],
            env={"PHAROS_VAR": "foo"},
            stdin="bar",
        )
        assert out.ok
        assert "foo" in out.stdout
        assert "bar" in out.stdout


# ---------------------------------------------------------------------------
# path_age_secs
# ---------------------------------------------------------------------------


class TestPathAgeSecs:
    def test_missing_path_returns_none(self, tmp_path: pytest.TempPathFactory) -> None:
        missing = str(tmp_path / "does_not_exist.txt")
        assert path_age_secs(missing) is None

    def test_fresh_file_near_zero(self, tmp_path: pytest.TempPathFactory) -> None:
        f = tmp_path / "fresh.txt"
        f.write_text("x")
        age = path_age_secs(str(f))
        assert age is not None
        assert 0.0 <= age < 5.0

    def test_old_file_has_positive_age(self, tmp_path: pytest.TempPathFactory) -> None:
        f = tmp_path / "old.txt"
        f.write_text("x")
        # Back-date mtime by 60 s
        past = time.time() - 60
        os.utime(str(f), (past, past))
        age = path_age_secs(str(f))
        assert age is not None
        assert age >= 55  # allow a tiny scheduling margin


# ---------------------------------------------------------------------------
# json_from_cmd
# ---------------------------------------------------------------------------


class TestJsonFromCmd:
    def test_valid_json_object(self) -> None:
        out = CommandOutput(returncode=0, stdout='{"ok": true}', stderr="")
        result = json_from_cmd(out)
        assert result == {"ok": True}

    def test_valid_json_array(self) -> None:
        out = CommandOutput(returncode=0, stdout="[1, 2, 3]", stderr="")
        result = json_from_cmd(out)
        assert result == [1, 2, 3]

    def test_invalid_json_returns_none(self) -> None:
        out = CommandOutput(returncode=0, stdout="not json", stderr="")
        assert json_from_cmd(out) is None

    def test_empty_stdout_returns_none(self) -> None:
        out = CommandOutput(returncode=1, stdout="", stderr="error")
        assert json_from_cmd(out) is None

    def test_json_from_real_cmd(self) -> None:
        out = run_cmd("echo", ['{"key": "value"}'])
        result = json_from_cmd(out)
        assert result == {"key": "value"}


# ---------------------------------------------------------------------------
# unix_socket_ping
# ---------------------------------------------------------------------------


def _serve_unix_socket(
    sock_path: str,
    response: bytes,
    ready: threading.Event,
    stop: threading.Event,
) -> None:
    """Tiny single-connection AF_UNIX server for testing.

    Signals *ready* after listen() so callers don't need a fixed sleep.
    """
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(sock_path)
        srv.listen(1)
        ready.set()  # socket is bound and listening — safe to connect now
        srv.settimeout(2.0)
        conn, _ = srv.accept()
        with conn:
            conn.recv(256)  # drain whatever the client sends
            conn.sendall(response)
    except OSError:
        ready.set()  # unblock caller even on error
    finally:
        srv.close()
        p = Path(sock_path)
        if p.exists():
            p.unlink()
        stop.set()


def _short_sock_path(suffix: str = ".sock") -> str:
    """Return a short AF_UNIX path under /tmp (avoids the 104-byte macOS limit).

    pytest's tmp_path resolves via /private/var/folders/…, which easily
    exceeds the 104-byte sun_path cap on macOS.
    """
    token = secrets.token_hex(6)
    return f"/tmp/pharos_test_{token}{suffix}"


class TestUnixSocketPing:
    def test_connect_and_recv(self) -> None:
        sock_path = _short_sock_path()
        ready = threading.Event()
        stop = threading.Event()
        t = threading.Thread(
            target=_serve_unix_socket,
            args=(sock_path, b'{"pong":true}\n', ready, stop),
            daemon=True,
        )
        t.start()
        ready.wait(timeout=2.0)  # deterministic: wait until listen() is done

        result = unix_socket_ping(sock_path)

        stop.wait(timeout=2.0)
        t.join(timeout=2.0)

        assert result["connected"] is True
        assert result["error"] is None
        assert result["latency_ms"] is not None
        assert result["latency_ms"] >= 0.0
        assert result["reply"] is not None
        assert "pong" in result["reply"]

    def test_missing_socket_not_connected(self) -> None:
        sock_path = _short_sock_path()
        result = unix_socket_ping(sock_path, timeout=0.5)
        assert result["connected"] is False
        assert result["error"] is not None
        assert result["latency_ms"] is None

    def test_connect_only_no_payload(self) -> None:
        """payload=None: just check the socket accepts connections."""
        sock_path = _short_sock_path()
        ready = threading.Event()
        stop = threading.Event()
        t = threading.Thread(
            target=_serve_unix_socket,
            args=(sock_path, b"", ready, stop),
            daemon=True,
        )
        t.start()
        ready.wait(timeout=2.0)

        result = unix_socket_ping(sock_path, payload=None)

        stop.wait(timeout=2.0)
        t.join(timeout=2.0)

        assert result["connected"] is True
        assert result["reply"] is None


# ---------------------------------------------------------------------------
# pid_alive
# ---------------------------------------------------------------------------


class TestPidAlive:
    def test_own_pid_is_alive(self) -> None:
        assert pid_alive(os.getpid()) is True

    def test_pid_zero_is_not_a_process(self) -> None:
        # PID 0 is never a regular user process; ps -p 0 exits non-zero.
        # (On macOS ps -p 0 returns empty; on Linux it may match kernel.)
        # We just assert it doesn't crash; truthfulness is platform-specific.
        result = pid_alive(0)
        assert isinstance(result, bool)

    def test_obviously_dead_pid(self) -> None:
        # PID 2**22 is astronomically unlikely to exist.
        assert pid_alive(2**22 - 1) is False


# ---------------------------------------------------------------------------
# pgrep_full
# ---------------------------------------------------------------------------


class TestPgrepFull:
    def test_returns_list(self) -> None:
        result = pgrep_full("python")
        assert isinstance(result, list)

    def test_no_match_returns_empty(self) -> None:
        result = pgrep_full("__pharos_no_such_process_xyz__")
        assert result == []

    @pytest.mark.skipif(
        not Path("/sbin/launchd").exists(), reason="macOS-only launchd test"
    )
    def test_launchd_found(self) -> None:
        # launchd (PID 1) always exists on macOS — its full command line
        # includes "launchd", so pgrep_full("launchd") must return it.
        result = pgrep_full("launchd")
        assert any("launchd" in line for line in result), (
            f"launchd not found in pgrep output: {result}"
        )


# ---------------------------------------------------------------------------
# http_get / http_post — skip real network; just test error path
# ---------------------------------------------------------------------------


class TestHttpProbesErrorPath:
    def test_http_get_retries_transient_tls_verify_error(self, monkeypatch) -> None:
        from pharos.checks import probes

        calls = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b"ok"

        def fake_urlopen(req, *, timeout, context):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.URLError(
                    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                    "self-signed certificate"
                )
            return Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(probes.time, "sleep", lambda _seconds: None)

        assert probes.http_get("https://example.test/health", timeout=2.5) == (
            200,
            "ok",
        )
        assert len(calls) == 2

    def test_http_get_uses_env_ca_bundle(self, monkeypatch, tmp_path) -> None:
        from pharos.checks import probes

        ca = tmp_path / "ca.pem"
        ca.write_text("test ca")
        sentinel_context = type("Ctx", (), {"verify_flags": 0})()
        captured = {}

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b"ok"

        def fake_default_context(*, cafile):
            captured["cafile"] = cafile
            return sentinel_context

        def fake_urlopen(req, *, timeout, context):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["context"] = context
            return Resp()

        monkeypatch.setenv("PHAROS_CA_BUNDLE", str(ca))
        monkeypatch.setattr(probes.ssl, "create_default_context", fake_default_context)
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        assert probes.http_get("https://example.test/health", timeout=2.5) == (
            200,
            "ok",
        )
        assert captured["cafile"] == str(ca)
        assert captured["context"] is sentinel_context
        assert captured["timeout"] == 2.5

    def test_http_get_explicit_ca_bundle_overrides_env(
        self, monkeypatch, tmp_path
    ) -> None:
        from pharos.checks import probes

        env_ca = tmp_path / "env-ca.pem"
        explicit_ca = tmp_path / "explicit-ca.pem"
        env_ca.write_text("env ca")
        explicit_ca.write_text("explicit ca")
        sentinel_context = type("Ctx", (), {"verify_flags": 0})()
        captured = {}

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b"ok"

        def fake_default_context(*, cafile):
            captured["cafile"] = cafile
            return sentinel_context

        def fake_open_url(req, *, timeout, context, bypass_proxy):
            captured["url"] = req.full_url
            captured["context"] = context
            captured["bypass_proxy"] = bypass_proxy
            return Resp()

        monkeypatch.setenv("PHAROS_CA_BUNDLE", str(env_ca))
        monkeypatch.setattr(probes.ssl, "create_default_context", fake_default_context)
        monkeypatch.setattr(probes, "_open_url", fake_open_url)

        assert probes.http_get(
            "https://example.test/health", ca_bundle=str(explicit_ca)
        ) == (200, "ok")
        assert captured["cafile"] == str(explicit_ca)
        assert captured["context"] is sentinel_context
        assert captured["bypass_proxy"] is True

    def test_http_get_unreachable_raises_runtime_error(self) -> None:
        from pharos.checks.probes import http_get

        with pytest.raises(RuntimeError) as exc_info:
            http_get("http://127.0.0.1:1/no-such-service", timeout=0.5)
        # The error message must NOT contain query strings (token-leak guard).
        # Here we just confirm it raises cleanly.
        assert "http_get failed" in str(exc_info.value)

    def test_http_post_unreachable_raises_runtime_error(self) -> None:
        from pharos.checks.probes import http_post

        with pytest.raises(RuntimeError) as exc_info:
            http_post("http://127.0.0.1:1/no-such-service", {"x": 1}, timeout=0.5)
        assert "http_post failed" in str(exc_info.value)

    def test_http_get_redacts_query_string(self) -> None:
        from pharos.checks.probes import http_get

        with pytest.raises(RuntimeError) as exc_info:
            http_get(
                "http://127.0.0.1:1/path?token=example&key=sample",
                timeout=0.5,
            )
        err = str(exc_info.value)
        assert "example" not in err
        assert "sample" not in err
