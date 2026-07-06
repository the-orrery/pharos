"""Tests for the bespoke domain checks (contrib.py).

SemanticSyncCheck covers state-machine cases that are easier to encode as a
small contrib check than as a generic JsonStatusFileCheck configuration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from pharos.checks import contrib
from pharos.notify.base import Severity, Status

_OLD = "2020-01-01T00:00:00Z"  # far past → stale under any threshold


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _doc(**fields: object) -> str:
    return json.dumps(fields)


def _chk(**over: object) -> contrib.SemanticSyncCheck:
    kw: dict[str, object] = {"id": "s", "source": "svc", "status_file_path": "/x.json"}
    kw.update(over)
    return contrib.SemanticSyncCheck(**kw)  # type: ignore[arg-type]


def test_ok_is_immune_to_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    # state=ok but 33h stale → still OK.
    monkeypatch.setattr(
        contrib, "_read_text", lambda p: _doc(state="ok", last_attempt_at=_OLD)
    )
    assert _chk().run().status is Status.OK


def test_skipped_repo_clean_with_success_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # skipped/repo-clean with a prior success → OK even if stale.
    monkeypatch.setattr(
        contrib,
        "_read_text",
        lambda p: _doc(
            state="skipped",
            skipped_reason="repo-clean",
            last_success_at=_OLD,
            last_attempt_at=_OLD,
        ),
    )
    assert _chk().run().status is Status.OK


def test_skipped_stale_other_reason_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    # skipped, stale, no exemption → DEGRADED.
    monkeypatch.setattr(
        contrib,
        "_read_text",
        lambda p: _doc(state="skipped", skipped_reason="x", last_attempt_at=_OLD),
    )
    assert _chk().run().status is Status.DEGRADED


def test_running_fresh_ok_but_stale_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        contrib,
        "_read_text",
        lambda p: _doc(state="running", last_attempt_at=_now_iso()),
    )
    assert _chk().run().status is Status.OK
    monkeypatch.setattr(
        contrib, "_read_text", lambda p: _doc(state="running", last_attempt_at=_OLD)
    )
    assert _chk().run().status is Status.DEGRADED


def test_failed_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        contrib,
        "_read_text",
        lambda p: _doc(state="failed", last_attempt_at=_now_iso()),
    )
    ev = _chk().run()
    assert ev.status is Status.DOWN and ev.severity is Severity.CRITICAL


def test_collection_mismatch_overrides_ok_to_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contrib,
        "_read_text",
        lambda p: _doc(
            state="ok",
            collection="A",
            actual_collection="B",
            last_attempt_at=_now_iso(),
        ),
    )
    assert _chk().run().status is Status.DOWN


def test_collection_from_stdout_excerpt(monkeypatch: pytest.MonkeyPatch) -> None:
    # actual_collection can come nested inside a stdout_excerpt JSON string.
    monkeypatch.setattr(
        contrib,
        "_read_text",
        lambda p: _doc(
            state="ok",
            collection="A",
            stdout_excerpt=json.dumps({"qdrant_collection": "B"}),
            last_attempt_at=_now_iso(),
        ),
    )
    assert _chk().run().status is Status.DOWN


def test_missing_file_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contrib, "_read_text", lambda p: None)
    assert _chk().run().status is Status.UNKNOWN
