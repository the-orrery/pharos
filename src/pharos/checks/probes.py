"""Reusable, generic probe helpers — stdlib-only, no business logic.

All functions are parameterised; no hardcoded paths, labels, or URLs live here.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT: float = 30.0
_SOCKET_TIMEOUT: float = 2.0


# ---------------------------------------------------------------------------
# Core subprocess primitive
# ---------------------------------------------------------------------------


@dataclass
class CommandOutput:
    """Result of a subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """True when the process exited with code 0."""
        return self.returncode == 0


def run_cmd(
    program: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> CommandOutput:
    """Run *program* with *args*, returning a :class:`CommandOutput`.

    The *env* dict is **overlaid** on the current environment (not a
    replacement), matching common subprocess env overlay semantics.
    """
    merged_env: dict[str, str] | None = None
    if env is not None:
        merged_env = {**os.environ, **env}

    try:
        proc = subprocess.run(
            [program, *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=merged_env,
            timeout=timeout,
        )
        return CommandOutput(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    except FileNotFoundError as exc:
        return CommandOutput(returncode=1, stdout="", stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        return CommandOutput(
            returncode=1, stdout="", stderr=f"timeout after {exc.timeout}s"
        )
    except OSError as exc:
        return CommandOutput(returncode=1, stdout="", stderr=str(exc))


# ---------------------------------------------------------------------------
# launchd helpers (macOS)
# ---------------------------------------------------------------------------


def launchd_state(label: str) -> dict[str, Any]:
    """Query launchctl for the running state of a launchd job.

    Runs ``launchctl print gui/<uid>/<label>``, checks for
    ``state = running`` and extracts the pid line.

    Returns::

        {
            "label": str,
            "running": bool,
            "pid": str | None,   # raw pid string from launchctl output
            "command_success": bool,
            "raw_stdout": str,
        }
    """
    uid = os.getuid()
    service = f"gui/{uid}/{label}"
    out = run_cmd("launchctl", ["print", service])

    running = "state = running" in out.stdout
    pid: str | None = None
    for line in out.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            pid = stripped[len("pid = ") :].strip()
            break

    return {
        "label": label,
        "running": running,
        "pid": pid,
        "command_success": out.ok,
        "raw_stdout": out.stdout,
    }


def launchd_registered_state(label: str) -> dict[str, Any]:
    """Query launchctl for the registration state of a launchd job.

    Runs ``launchctl print gui/<uid>/<label>`` and extracts the ``state = ...``
    value without requiring it to be ``running``. Presence of a successful
    response means the service is registered.

    Returns::

        {
            "label": str,
            "state": str | None,   # e.g. "waiting", "running", "stopped"
            "registered": bool,    # True when command_success
            "command_success": bool,
            "raw_stdout": str,
        }
    """
    uid = os.getuid()
    service = f"gui/{uid}/{label}"
    out = run_cmd("launchctl", ["print", service])

    state: str | None = None
    for line in out.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state = "):
            state = stripped[len("state = ") :].strip()
            break

    return {
        "label": label,
        "state": state,
        "registered": out.ok,
        "command_success": out.ok,
        "raw_stdout": out.stdout,
    }


# ---------------------------------------------------------------------------
# Unix-socket ping
# ---------------------------------------------------------------------------


def unix_socket_ping(
    path: str,
    *,
    payload: bytes | None = b'{"ping":true}\n',
    timeout: float = _SOCKET_TIMEOUT,
) -> dict[str, Any]:
    """Connect to a Unix-domain socket, optionally send *payload*, read reply.

    The caller controls *payload* so the helper stays generic. Pass
    ``payload=None`` to skip the write/read round-trip (connect-only ping).

    Returns::

        {
            "connected": bool,
            "reply": str | None,
            "latency_ms": float | None,
            "error": str | None,
        }
    """
    t0 = time.monotonic()
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
    except OSError as exc:
        return {
            "connected": False,
            "reply": None,
            "latency_ms": None,
            "error": str(exc),
        }

    try:
        if payload is not None:
            sock.sendall(payload)
            data = sock.recv(256)
            reply = data.decode(errors="replace")
        else:
            reply = None
        latency_ms = (time.monotonic() - t0) * 1000
        return {
            "connected": True,
            "reply": reply,
            "latency_ms": latency_ms,
            "error": None,
        }
    except OSError as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        return {
            "connected": True,
            "reply": None,
            "latency_ms": latency_ms,
            "error": str(exc),
        }
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def pgrep_full(pattern: str) -> list[str]:
    """Return lines from ``pgrep -fl <pattern>`` (full command lines).

    Returns an empty list when pgrep finds nothing (exit code 1) or when the
    binary is unavailable.
    """
    out = run_cmd("pgrep", ["-fl", pattern])
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def pid_alive(pid: int) -> bool:
    """Return True when the process with *pid* is alive.

    Runs ``ps -p <pid> -o pid=`` and checks that the trimmed output matches
    the pid string.
    """
    pid_s = str(pid)
    out = run_cmd("ps", ["-p", pid_s, "-o", "pid="])
    return out.ok and out.stdout.strip() == pid_s


# ---------------------------------------------------------------------------
# TCP port-owner helper (macOS / lsof)
# ---------------------------------------------------------------------------


def port_listeners(port: int) -> list[str]:
    """Return ``lsof`` listener lines whose text mentions ``:<port>``.

    Runs ``lsof -nP -iTCP -sTCP:LISTEN`` and filters the output lines for
    ``:<port>``. Returns an empty list when nothing listens (or lsof is
    unavailable); the caller decides what a match means.
    """
    out = run_cmd("lsof", ["-nP", "-iTCP", "-sTCP:LISTEN"])
    needle = f":{port}"
    return [line for line in out.stdout.splitlines() if needle in line]


# ---------------------------------------------------------------------------
# File-age helper
# ---------------------------------------------------------------------------


def path_age_secs(path: str) -> float | None:
    """Return ``now - mtime`` in seconds, or ``None`` if the path is missing.

    Returns a *float* because ``os.stat`` gives sub-second mtime.
    """
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    return time.time() - mtime


# ---------------------------------------------------------------------------
# JSON parsing from command output
# ---------------------------------------------------------------------------


def json_from_cmd(out: CommandOutput) -> object | None:
    """Parse JSON from *out.stdout*; return ``None`` on failure.

    Useful for checks that shell out to small JSON-producing commands.
    """
    try:
        return json.loads(out.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib urllib, secrets redacted in errors)
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT: float = 5.0
_TLS_RETRY_ATTEMPTS: int = 5


def _redact_url(url: str) -> str:
    """Strip query string and fragment from URL to avoid leaking tokens."""
    # Keep only scheme + host + path; drop ?query#fragment.
    try:
        from urllib.parse import urlparse

        p = urlparse(url)
    except Exception:
        return "<url>"
    else:
        return f"{p.scheme}://{p.netloc}{p.path}"


def _ssl_context(ca_bundle: str | None) -> ssl.SSLContext | None:
    """Build an SSLContext from a CA bundle path, or None for system default."""
    ca = ca_bundle or os.environ.get("PHAROS_CA_BUNDLE")
    if not ca:
        return None
    p = Path(ca).expanduser()
    if not p.exists():
        return None
    ctx = ssl.create_default_context(cafile=str(p))
    if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _open_url(
    req: urllib.request.Request,
    *,
    timeout: float,
    context: ssl.SSLContext | None,
    bypass_proxy: bool,
) -> Any:
    for attempt in range(_TLS_RETRY_ATTEMPTS):
        try:
            if not bypass_proxy:
                return urllib.request.urlopen(req, timeout=timeout, context=context)
            handlers: list[urllib.request.BaseHandler] = [
                urllib.request.ProxyHandler({})
            ]
            if context is not None:
                handlers.append(urllib.request.HTTPSHandler(context=context))
            return urllib.request.build_opener(*handlers).open(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            if attempt < _TLS_RETRY_ATTEMPTS - 1 and "CERTIFICATE_VERIFY_FAILED" in str(
                exc
            ):
                time.sleep(0.2 * (attempt + 1))
                continue
            raise
    raise RuntimeError("http open retry exhausted")


def http_get(
    url: str,
    *,
    timeout: float = _HTTP_TIMEOUT,
    ca_bundle: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    """GET *url*, return ``(status_code, body_text)``.

    On network/HTTP error raises :class:`RuntimeError` with URL redacted.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        ctx = _ssl_context(ca_bundle)
        with _open_url(
            req, timeout=timeout, context=ctx, bypass_proxy=bool(ca_bundle)
        ) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        # HTTPError carries a status code — surface it instead of raising.
        try:
            body = exc.read().decode(errors="replace")
        except Exception:
            body = ""
        return exc.code, body
    except urllib.error.URLError as exc:
        safe = _redact_url(url)
        raise RuntimeError(f"http_get failed for {safe}: {exc.reason}") from exc
    except OSError as exc:
        safe = _redact_url(url)
        raise RuntimeError(f"http_get failed for {safe}: {exc}") from exc


def http_post(
    url: str,
    json_body: object,
    *,
    timeout: float = _HTTP_TIMEOUT,
    ca_bundle: str | None = None,
) -> tuple[int, str]:
    """POST *json_body* (serialised to JSON) to *url*, return ``(status_code, body_text)``.

    On network/HTTP error raises :class:`RuntimeError` with URL redacted.
    """
    encoded = json.dumps(json_body).encode()
    try:
        req = urllib.request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ctx = _ssl_context(ca_bundle)
        with _open_url(
            req, timeout=timeout, context=ctx, bypass_proxy=bool(ca_bundle)
        ) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode(errors="replace")
        except Exception:
            body = ""
        return exc.code, body
    except urllib.error.URLError as exc:
        safe = _redact_url(url)
        raise RuntimeError(f"http_post failed for {safe}: {exc.reason}") from exc
    except OSError as exc:
        safe = _redact_url(url)
        raise RuntimeError(f"http_post failed for {safe}: {exc}") from exc
