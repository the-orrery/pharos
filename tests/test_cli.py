from pathlib import Path

import pytest
from typer.testing import CliRunner

from pharos.cli import app, canonical_config_path

runner = CliRunner()


def test_hello() -> None:
    result = runner.invoke(app, ["hello", "--name", "seed"])
    assert result.exit_code == 0
    assert "hello, seed" in result.stdout


# ---------------------------------------------------------------------------
# `run` config resolution: canonical default + in-band not-found hint (E-1).
# ---------------------------------------------------------------------------


def _empty_config(path: Path) -> None:
    """An empty checks file: load_checks → [] → aggregate → OK → exit 0,
    so the run path exercises config resolution without hitting the network."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


def test_canonical_config_path_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHAROS_CONFIG", "/some/where/checks.toml")
    assert canonical_config_path() == Path("/some/where/checks.toml")


def test_canonical_config_path_xdg_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PHAROS_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert canonical_config_path() == tmp_path / "pharos" / "checks.toml"


def test_run_no_config_canonical_missing_gives_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare `pharos run` with no canonical config: structured error + a hint
    telling the user to create it or pass --config (the path tried IS the
    canonical one, so 'try --config <canonical>' would just echo itself)."""
    monkeypatch.delenv("PHAROS_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # nothing under it
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    assert "config file not found" in result.output
    assert "create" in result.output and "--config" in result.output


def test_run_explicit_missing_config_gives_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit --config pointing at a missing file: error + did-you-mean
    hint pointing at the canonical config form that works."""
    monkeypatch.delenv("PHAROS_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    missing = tmp_path / "nope.toml"
    result = runner.invoke(app, ["run", "--config", str(missing)])
    assert result.exit_code == 2
    assert "config file not found" in result.output
    assert "hint: try `pharos run --config" in result.output
    assert "pharos/checks.toml" in result.output


def test_run_no_config_uses_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare `pharos run` resolves to the canonical config and runs it (no
    cwd assumption, no not-found hint)."""
    monkeypatch.delenv("PHAROS_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("PHAROS_ALERTS_DB", str(tmp_path / "alerts.db"))
    _empty_config(tmp_path / "pharos" / "checks.toml")
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "config file not found" not in result.output
    assert "overall: OK" in result.output


def test_run_explicit_config_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal path unchanged: an explicit, existing --config loads and runs."""
    monkeypatch.setenv("PHAROS_ALERTS_DB", str(tmp_path / "alerts.db"))
    cfg = tmp_path / "checks.toml"
    _empty_config(cfg)
    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "config file not found" not in result.output
    assert "overall: OK" in result.output
