"""Check protocol — the contract every check-type implements.

A *check* is a parameterised, runnable health probe: built from config, it
inspects one aspect of the host and returns a :class:`HealthEvent`.  The
framework (runner / loader / CLI) is public; the *instances* a user wires up
(their commands, paths, ports, URLs) live in private CONFIG, never in code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pharos.notify.base import HealthEvent


@runtime_checkable
class Check(Protocol):
    """A runnable, parameterised health check.

    Implementations carry their own config (constructed by the loader) and
    expose a stable ``id`` plus a ``source`` label that flows onto the emitted
    :class:`HealthEvent`.  ``run`` must not raise for expected failure modes —
    it returns a DEGRADED/DOWN/UNKNOWN event instead.
    """

    id: str
    source: str

    def run(self) -> HealthEvent: ...
