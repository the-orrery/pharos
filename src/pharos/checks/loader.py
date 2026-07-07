"""Load checks from a TOML file via stdlib ``tomllib`` (no PyYAML).

Schema — a top-level ``[[check]]`` array; each entry has ``type`` (one of the
check-type names), ``id``, ``source``, plus that type's params.  An entry may
also set ``enabled = false`` to be skipped at load time::

    [[check]]
    type = "HttpCheck"
    id   = "api-healthz"
    source = "my-service"
    url = "http://127.0.0.1:8080/healthz"

Each type has a pydantic model that validates/coerces its params and knows how
to ``build()`` the concrete check.  Unknown ``type`` or missing/invalid fields
raise :class:`CheckConfigError` with a clear message.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from pharos.checks import contrib
from pharos.checks import types as t
from pharos.checks.base import Check
from pharos.notify.base import Severity, Status


class CheckConfigError(ValueError):
    """A check config entry is malformed (unknown type, missing/invalid field)."""


# ---------------------------------------------------------------------------
# Per-type config models.  Each validates/coerces params and builds the check.
# `extra="forbid"` surfaces typo'd / stray keys instead of silently dropping.
# ---------------------------------------------------------------------------


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str

    def build(self) -> Check:  # pragma: no cover - overridden
        raise NotImplementedError


class CommandJsonModel(_Base):
    command: list[str]
    success_field_path: str
    success_field_value: Any
    fail_status: Status = Status.DEGRADED
    fail_severity: Severity = Severity.WARNING
    runbook: str = ""
    ok_title: str = ""
    fail_title: str = ""
    failure_detail_field_path: str = ""

    def build(self) -> Check:
        return t.CommandJsonCheck(
            id=self.id,
            source=self.source,
            command=self.command,
            success_field_path=self.success_field_path,
            success_field_value=self.success_field_value,
            fail_status=self.fail_status,
            fail_severity=self.fail_severity,
            runbook=self.runbook,
            ok_title=self.ok_title,
            fail_title=self.fail_title,
            failure_detail_field_path=self.failure_detail_field_path,
        )


class PidfileModel(_Base):
    pidfile_path: str
    stale_after_secs: float
    absent_is_ok: bool = True
    runbook: str = ""

    def build(self) -> Check:
        return t.PidfileCheck(
            id=self.id,
            source=self.source,
            pidfile_path=self.pidfile_path,
            stale_after_secs=self.stale_after_secs,
            absent_is_ok=self.absent_is_ok,
            runbook=self.runbook,
        )


class LaunchdModel(_Base):
    label: str
    require_running: bool
    require_output_contains: str | None = None
    runbook: str = ""

    def build(self) -> Check:
        return t.LaunchdCheck(
            id=self.id,
            source=self.source,
            label=self.label,
            require_running=self.require_running,
            require_output_contains=self.require_output_contains,
            runbook=self.runbook,
        )


class FileContainsModel(_Base):
    path: str
    required_substrings: list[str]
    absent_status: Status = Status.DEGRADED
    absent_severity: Severity = Severity.WARNING
    runbook: str = ""

    def build(self) -> Check:
        return t.FileContainsCheck(
            id=self.id,
            source=self.source,
            path=self.path,
            required_substrings=self.required_substrings,
            absent_status=self.absent_status,
            absent_severity=self.absent_severity,
            runbook=self.runbook,
        )


class UnixSocketPingModel(_Base):
    socket_path: str
    ping_payload: str = '{"ping":true}\n'
    pong_substring: str = "pong"
    absent_ok_if_no_process: str | None = None
    runbook: str = ""

    def build(self) -> Check:
        return t.UnixSocketPingCheck(
            id=self.id,
            source=self.source,
            socket_path=self.socket_path,
            ping_payload=self.ping_payload.encode(),
            pong_substring=self.pong_substring,
            absent_ok_if_no_process=self.absent_ok_if_no_process,
            runbook=self.runbook,
        )


class HttpModel(_Base):
    url: str
    method: str = "GET"
    json_body: Any = None
    timeout: float = 5.0
    success_status_range: tuple[int, int] = (200, 299)
    runbook: str = ""
    ca_bundle: str = ""
    headers: dict[str, str] | None = None

    def build(self) -> Check:
        return t.HttpCheck(
            id=self.id,
            source=self.source,
            url=self.url,
            method=self.method,
            json_body=self.json_body,
            timeout=self.timeout,
            success_status_range=self.success_status_range,
            runbook=self.runbook,
            ca_bundle=self.ca_bundle,
            headers=self.headers,
        )


class PortOwnerModel(_Base):
    port: int
    expected_owner_substring: str
    runbook: str = ""

    def build(self) -> Check:
        return t.PortOwnerCheck(
            id=self.id,
            source=self.source,
            port=self.port,
            expected_owner_substring=self.expected_owner_substring,
            runbook=self.runbook,
        )


class CommandModel(_Base):
    command: list[str]
    timeout: float = 30.0
    fail_status: Status = Status.DOWN
    fail_severity: Severity = Severity.CRITICAL
    require_output_contains: list[str] | None = None
    forbid_output_contains: list[str] | None = None
    min_stdout_bytes: int = 0
    runbook: str = ""

    def build(self) -> Check:
        return t.CommandCheck(
            id=self.id,
            source=self.source,
            command=self.command,
            timeout=self.timeout,
            fail_status=self.fail_status,
            fail_severity=self.fail_severity,
            require_output_contains=self.require_output_contains,
            forbid_output_contains=self.forbid_output_contains,
            min_stdout_bytes=self.min_stdout_bytes,
            runbook=self.runbook,
        )


class JsonStatusFileModel(_Base):
    status_file_path: str
    state_field: str
    timestamp_field: str
    stale_threshold_secs: int
    ok_states: list[str]
    degraded_states: list[str]
    down_states: list[str]
    stale_status: Status = Status.DEGRADED
    runbook: str = ""

    def build(self) -> Check:
        return t.JsonStatusFileCheck(
            id=self.id,
            source=self.source,
            status_file_path=self.status_file_path,
            state_field=self.state_field,
            timestamp_field=self.timestamp_field,
            stale_threshold_secs=self.stale_threshold_secs,
            ok_states=self.ok_states,
            degraded_states=self.degraded_states,
            down_states=self.down_states,
            stale_status=self.stale_status,
            runbook=self.runbook,
        )


class SemanticSyncModel(_Base):
    # Contrib check: semantic-sync style rules, not a fully generic type.
    status_file_path: str
    stale_threshold_secs: int = 7200
    runbook: str = ""

    def build(self) -> Check:
        return contrib.SemanticSyncCheck(
            id=self.id,
            source=self.source,
            status_file_path=self.status_file_path,
            stale_threshold_secs=self.stale_threshold_secs,
            runbook=self.runbook,
        )


# type-name -> pydantic config model
_REGISTRY: dict[str, type[_Base]] = {
    "CommandJsonCheck": CommandJsonModel,
    "PidfileCheck": PidfileModel,
    "LaunchdCheck": LaunchdModel,
    "FileContainsCheck": FileContainsModel,
    "UnixSocketPingCheck": UnixSocketPingModel,
    "HttpCheck": HttpModel,
    "PortOwnerCheck": PortOwnerModel,
    "JsonStatusFileCheck": JsonStatusFileModel,
    "CommandCheck": CommandModel,
    "SemanticSyncCheck": SemanticSyncModel,
}


def known_types() -> list[str]:
    """Return the sorted list of registered check-type names."""
    return sorted(_REGISTRY)


def _build_one(idx: int, entry: dict[str, Any]) -> Check:
    if "type" not in entry:
        raise CheckConfigError(f"check #{idx}: missing required 'type' field")
    type_name = entry["type"]
    model_cls = _REGISTRY.get(type_name)
    if model_cls is None:
        raise CheckConfigError(
            f"check #{idx}: unknown type {type_name!r}; known types: {known_types()}"
        )
    # `enabled` is loader-level metadata (handled in load_checks), never a
    # constructor param — strip it before validating against the type model.
    params = {k: v for k, v in entry.items() if k not in ("type", "enabled")}
    try:
        model = model_cls.model_validate(params)
    except ValidationError as exc:
        raise CheckConfigError(
            f"check #{idx} (type={type_name}): invalid config: {exc}"
        ) from exc
    return model.build()


_ENV_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env(obj: Any) -> Any:
    """Recursively expand ${VAR} references in string values."""
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def _load_local_overrides(config_path: Path) -> dict[str, dict]:
    """Load checks.local.toml sibling for machine-local check overrides.

    Returns {check_id: {field: value}}.  The sibling is computed from
    *config_path* WITHOUT resolving symlinks, so the local file lives next
    to the symlink (e.g. ~/.config/pharos/) not inside the git repo.
    """
    local = config_path.with_name(config_path.stem + ".local" + config_path.suffix)
    if not local.is_file():
        return {}
    try:
        with local.open("rb") as fh:
            doc = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise CheckConfigError(
            f"local override file is not valid TOML: {local}: {exc}"
        ) from exc
    raw = doc.get("check", [])
    if not isinstance(raw, list):
        raise CheckConfigError(f"{local}: top-level 'check' must be an array of tables")
    overrides: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id")
        if not cid:
            continue
        overrides[cid] = {k: v for k, v in entry.items() if k != "id"}
    return overrides


def load_checks(path: str) -> list[Check]:
    """Load and validate checks from the TOML file at *path*.

    Raises :class:`CheckConfigError` on a malformed file, a non-array
    ``check`` key, an unknown ``type``, or invalid/missing params.
    """
    config_path = Path(path)
    try:
        with config_path.open("rb") as fh:
            doc = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise CheckConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CheckConfigError(f"config file is not valid TOML: {exc}") from exc

    raw = doc.get("check", [])
    if not isinstance(raw, list):
        raise CheckConfigError("top-level 'check' must be an array of tables")

    local_overrides = _load_local_overrides(config_path)

    checks: list[Check] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise CheckConfigError(f"check #{idx}: each [[check]] must be a table")
        cid = entry.get("id")
        if cid and cid in local_overrides:
            entry = {**entry, **local_overrides[cid]}
        if not entry.get("enabled", True):
            continue
        checks.append(_build_one(idx, _expand_env(entry)))
    return checks
