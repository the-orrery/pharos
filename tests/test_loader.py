"""Tests for the TOML check loader: valid parse + clear errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from pharos.checks import types
from pharos.checks.loader import (
    CheckConfigError,
    known_types,
    load_checks,
)


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "checks.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)


VALID = """
[[check]]
type = "HttpCheck"
id = "api"
source = "svc"
url = "http://127.0.0.1:8080/healthz"

[[check]]
type = "CommandJsonCheck"
id = "doctor"
source = "svc"
command = ["echo", "{}"]
success_field_path = "/ok"
success_field_value = true
ok_title = "doctor ok"
fail_title = "doctor failed"
failure_detail_field_path = "/detail"

[[check]]
type = "PortOwnerCheck"
id = "db"
source = "svc"
port = 5432
expected_owner_substring = "postgres"
"""


class TestLoadChecks:
    def test_parses_all_entries(self, tmp_path: Path) -> None:
        checks = load_checks(_write(tmp_path, VALID))
        assert len(checks) == 3
        assert isinstance(checks[0], types.HttpCheck)
        assert isinstance(checks[1], types.CommandJsonCheck)
        assert isinstance(checks[2], types.PortOwnerCheck)
        assert checks[1].ok_title == "doctor ok"
        assert checks[1].fail_title == "doctor failed"
        assert checks[1].failure_detail_field_path == "/detail"

    def test_coerces_and_keeps_params(self, tmp_path: Path) -> None:
        checks = load_checks(_write(tmp_path, VALID))
        po = checks[2]
        assert isinstance(po, types.PortOwnerCheck)
        assert po.port == 5432
        assert po.expected_owner_substring == "postgres"

    def test_empty_file_is_empty_list(self, tmp_path: Path) -> None:
        assert load_checks(_write(tmp_path, "")) == []

    def test_unknown_type_raises(self, tmp_path: Path) -> None:
        body = """
[[check]]
type = "NoSuchCheck"
id = "x"
source = "svc"
"""
        with pytest.raises(CheckConfigError, match="unknown type"):
            load_checks(_write(tmp_path, body))

    def test_missing_type_raises(self, tmp_path: Path) -> None:
        body = """
[[check]]
id = "x"
source = "svc"
"""
        with pytest.raises(CheckConfigError, match="missing required 'type'"):
            load_checks(_write(tmp_path, body))

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        body = """
[[check]]
type = "HttpCheck"
id = "x"
source = "svc"
"""
        with pytest.raises(CheckConfigError, match="invalid config"):
            load_checks(_write(tmp_path, body))

    def test_extra_field_raises(self, tmp_path: Path) -> None:
        body = """
[[check]]
type = "PortOwnerCheck"
id = "x"
source = "svc"
port = 1
expected_owner_substring = "y"
bogus = "z"
"""
        with pytest.raises(CheckConfigError, match="invalid config"):
            load_checks(_write(tmp_path, body))

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CheckConfigError, match="not found"):
            load_checks(str(tmp_path / "nope.toml"))

    def test_bad_toml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CheckConfigError, match="not valid TOML"):
            load_checks(_write(tmp_path, "this is = = not toml ["))

    def test_known_types_registered(self) -> None:
        # 9 generic types + 1 contrib domain check (SemanticSyncCheck).
        assert len(known_types()) == 10
        assert "CommandCheck" in known_types()
        assert "SemanticSyncCheck" in known_types()

    def test_command_check_parses(self, tmp_path: Path) -> None:
        body = """
[[check]]
type = "CommandCheck"
id = "remote"
source = "svc"
command = ["ssh", "host", "curl -fsS http://127.0.0.1:9099/healthz"]
"""
        checks = load_checks(_write(tmp_path, body))
        assert len(checks) == 1
        assert isinstance(checks[0], types.CommandCheck)
        assert checks[0].command[0] == "ssh"


class TestEnabledField:
    def test_disabled_entry_is_skipped(self, tmp_path: Path) -> None:
        body = """
[[check]]
type = "HttpCheck"
id = "on"
source = "svc"
url = "http://127.0.0.1:8080/healthz"

[[check]]
type = "HttpCheck"
id = "off"
source = "svc"
url = "http://127.0.0.1:8081/healthz"
enabled = false
"""
        checks = load_checks(_write(tmp_path, body))
        assert len(checks) == 1
        assert checks[0].id == "on"

    def test_enabled_true_is_kept(self, tmp_path: Path) -> None:
        body = """
[[check]]
type = "HttpCheck"
id = "on"
source = "svc"
url = "http://127.0.0.1:8080/healthz"
enabled = true
"""
        checks = load_checks(_write(tmp_path, body))
        assert len(checks) == 1
        assert checks[0].id == "on"


class TestLocalOverlay:
    def test_local_overlay_disables_check(self, tmp_path: Path) -> None:
        _write(tmp_path, VALID)
        local = tmp_path / "checks.local.toml"
        local.write_text('[[check]]\nid = "api"\nenabled = false\n')
        checks = load_checks(str(tmp_path / "checks.toml"))
        assert len(checks) == 2
        assert all(c.id != "api" for c in checks)

    def test_local_overlay_overrides_field(self, tmp_path: Path) -> None:
        _write(tmp_path, VALID)
        local = tmp_path / "checks.local.toml"
        local.write_text('[[check]]\nid = "db"\nexpected_owner_substring = "mysql"\n')
        checks = load_checks(str(tmp_path / "checks.toml"))
        po = [c for c in checks if c.id == "db"][0]
        assert po.expected_owner_substring == "mysql"

    def test_no_local_overlay_is_noop(self, tmp_path: Path) -> None:
        checks = load_checks(_write(tmp_path, VALID))
        assert len(checks) == 3


class TestSampleConfig:
    def test_example_checks_toml_loads(self) -> None:
        # The shipped sample must always parse so the docs stay honest.
        sample = Path(__file__).resolve().parents[1] / "examples" / "checks.toml"
        checks = load_checks(str(sample))
        assert len(checks) >= 6
        ids = {c.id for c in checks}
        assert "api-healthz" in ids
