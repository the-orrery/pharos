"""Contrib checks for status formats that need special interpretation.

Keep the generic, fully parameterised checks in :mod:`pharos.checks.types`.
Use this module for optional checks whose state machine is easier to express in
Python than in TOML fields.
"""

from __future__ import annotations

import json
import time

from pharos.checks.types import _parse_iso8601, _read_text
from pharos.notify.base import HealthEvent, Severity, Status

_SEMANTIC_SYNC_STALE_SECS = 7200


class SemanticSyncCheck:
    """Status-file check for a semantic-sync style job.

    The tool-specific rules a generic JsonStatusFileCheck flattens (and so
    false-alarms on):

    * ``state == "ok"`` → OK **regardless of staleness** (a stale-but-ok sync is
      not a problem; the file just hasn't been rewritten).
    * ``state == "running"`` → OK only when fresh.
    * ``state == "skipped"`` → OK when ``skipped_reason`` is ``repo-clean`` (and
      there's a prior success or it's fresh) or ``sync-instance-busy`` (and
      fresh); otherwise DEGRADED when fresh, else stale → DEGRADED.
    * ``failed`` / ``timeout`` → DOWN.
    * a collection mismatch (configured vs actually written) → DOWN,
      overriding everything.
    """

    def __init__(
        self,
        id: str,
        source: str,
        status_file_path: str,
        stale_threshold_secs: int = _SEMANTIC_SYNC_STALE_SECS,
        runbook: str = "",
    ) -> None:
        self.id = id
        self.source = source
        self.status_file_path = status_file_path
        self.stale_threshold_secs = stale_threshold_secs
        self.runbook = runbook

    def run(self) -> HealthEvent:
        content = _read_text(self.status_file_path)
        if content is None:
            return self._unknown("semantic sync status file missing", exists=False)
        try:
            v = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return self._unknown(
                "semantic sync status file is not valid JSON", exists=True
            )
        if not isinstance(v, dict):
            return self._unknown(
                "semantic sync status file is not a JSON object", exists=True
            )

        state = v.get("state") if isinstance(v.get("state"), str) else "unknown"
        last_attempt_at = v.get("last_attempt_at") or ""
        skipped_reason = v.get("skipped_reason") or ""
        expected_collection = v.get("collection") or ""
        actual_collection = v.get("actual_collection") or ""
        if not actual_collection:
            excerpt = v.get("stdout_excerpt")
            if isinstance(excerpt, str):
                try:
                    parsed_excerpt = json.loads(excerpt)
                except (json.JSONDecodeError, ValueError):
                    parsed_excerpt = None
                if isinstance(parsed_excerpt, dict):
                    actual_collection = parsed_excerpt.get("qdrant_collection") or ""

        age_secs: float | None = None
        if isinstance(last_attempt_at, str) and last_attempt_at:
            parsed = _parse_iso8601(last_attempt_at)
            if parsed is not None:
                age_secs = time.time() - parsed
        stale = age_secs is not None and age_secs > self.stale_threshold_secs
        has_last_success = bool(v.get("last_success_at"))
        collection_mismatch = (
            bool(expected_collection)
            and bool(actual_collection)
            and actual_collection != expected_collection
        )

        status, severity = self._classify(
            state, skipped_reason, stale, has_last_success, collection_mismatch
        )
        note = (
            f" collection_mismatch expected={expected_collection} actual={actual_collection}"
            if collection_mismatch
            else ""
        )
        return HealthEvent(
            source=self.source,
            status=status,
            severity=severity,
            title=f"{self.source} semantic sync state={state} stale={stale}{note}",
            evidence={
                "path": self.status_file_path,
                "state": state,
                "skipped_reason": skipped_reason,
                "age_secs": age_secs,
                "stale": stale,
                "collection_mismatch": collection_mismatch,
                "runbook": self.runbook,
            },
        )

    @staticmethod
    def _classify(
        state: str,
        skipped_reason: str,
        stale: bool,
        has_last_success: bool,
        collection_mismatch: bool,
    ) -> tuple[Status, Severity]:
        if collection_mismatch:
            return Status.DOWN, Severity.CRITICAL
        if state == "ok":
            return Status.OK, Severity.INFO
        if state == "running" and not stale:
            return Status.OK, Severity.INFO
        if (
            state == "skipped"
            and skipped_reason == "repo-clean"
            and (has_last_success or not stale)
        ):
            return Status.OK, Severity.INFO
        if state == "skipped" and skipped_reason == "sync-instance-busy" and not stale:
            return Status.OK, Severity.INFO
        if state == "skipped" and not stale:
            return Status.DEGRADED, Severity.WARNING
        if state in ("failed", "timeout"):
            return Status.DOWN, Severity.CRITICAL
        if stale:
            return Status.DEGRADED, Severity.WARNING
        return Status.UNKNOWN, Severity.WARNING

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
