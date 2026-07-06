"""pharos telemetry — thin glue over gnomon shared core.

The shared core (schema / WAL ledger / capture / stats) lives in gnomon.
This module binds the pharos identity (Cfg) once and re-exports the same surface
the rest of pharos uses, so call sites stay unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import gnomon as ot

# re-exported for tests that inspect internal symbols directly
from gnomon.telemetry import (  # noqa: F401
    Tee,
    _is_fault,
    _pctile,
)

from pharos import __version__

CFG = ot.Cfg(tool="pharos", version=__version__)


def db_path() -> Path:
    return ot.db_path(CFG)


def record(rec: dict, *, path: Path | None = None) -> None:
    ot.record(rec, CFG, path=path)


def run_instrumented(
    app: Any,
    argv: list[str],
    *,
    command_path: list[str] | None = None,
    prog_name: str = "pharos",
    meta: dict | None = None,
    path: Path | None = None,
) -> int:
    return ot.run_instrumented(
        app,
        argv,
        CFG,
        command_path=command_path,
        prog_name=prog_name,
        meta=meta,
        path=path,
    )


def stats(path: Path | None = None) -> str:
    return ot.stats(CFG, path=path)


# _connect: alias for tests that call telemetry._connect(path) directly
_connect = ot.connect
