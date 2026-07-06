from __future__ import annotations

from pathlib import Path

import pytest

from pharos.notify.base import HealthEvent, NotifyError, Severity, Status
from pharos.notify.dingtalk import DingTalkChannel
from pharos.notify.routing import (
    Routing,
    RoutingConfigError,
    load_routing,
)


def _ev(source: str) -> HealthEvent:
    return HealthEvent(
        source=source,
        status=Status.DOWN,
        severity=Severity.CRITICAL,
        title="x down",
        detail="boom",
    )


# ---------------------------------------------------------------------------
# DingTalkChannel.from_env_file
# ---------------------------------------------------------------------------


def test_from_env_file_loads_url_and_secret(tmp_path: Path) -> None:
    env = tmp_path / "bot.env"
    env.write_text(
        "# a comment\n"
        "\n"
        'DINGTALK_WEBHOOK_URL="https://example.com/robot/send?access_token=example"\n'
        "DINGTALK_SECRET=example-signing-key\n",
        encoding="utf-8",
    )
    ch = DingTalkChannel.from_env_file(str(env), name="service-observability")
    assert ch.webhook_url == "https://example.com/robot/send?access_token=example"
    assert ch.secret == "example-signing-key"
    assert ch.name == "service-observability"  # routed channel carries its bot name


def test_from_env_file_missing_url_raises(tmp_path: Path) -> None:
    env = tmp_path / "bad.env"
    env.write_text("DINGTALK_SECRET=onlysecret\n", encoding="utf-8")
    with pytest.raises(NotifyError):
        DingTalkChannel.from_env_file(str(env))


def test_from_env_file_missing_file_raises() -> None:
    with pytest.raises(NotifyError):
        DingTalkChannel.from_env_file("/no/such/path/nope.env")


# ---------------------------------------------------------------------------
# Routing.channel_for
# ---------------------------------------------------------------------------


def _routing(tmp_path: Path) -> Routing:
    cfg = tmp_path / "channels.toml"
    cfg.write_text(
        f"""
default_channel = "infra-health"

[channels.infra-health]
env_file = "{tmp_path / "infra.env"}"

[channels.service-observability]
env_file = "{tmp_path / "service.env"}"

[[route]]
source = "service-observability-bridge"
channel = "service-observability"

[[route]]
source = "metrics-webhook-bridge"
channel = "service-observability"
""",
        encoding="utf-8",
    )
    return load_routing(str(cfg))


def test_channel_for_routed_and_default(tmp_path: Path) -> None:
    r = _routing(tmp_path)
    assert r.channel_for("service-observability-bridge") == "service-observability"
    assert r.channel_for("metrics-webhook-bridge") == "service-observability"
    # unrouted source falls through to default_channel
    assert r.channel_for("scribe") == "infra-health"
    assert r.channel_for("vector-index-shadow") == "infra-health"


# ---------------------------------------------------------------------------
# Routing.notify — fully mocked, no network, no real env files
# ---------------------------------------------------------------------------


def test_notify_routes_to_right_channel(tmp_path: Path, monkeypatch) -> None:
    r = _routing(tmp_path)
    built: list[str] = []
    sent: list[tuple[str, str]] = []

    def fake_from_env_file(env_file, name="dingtalk", timeout=10.0):
        built.append(name)
        ch = DingTalkChannel("https://fake/send", secret="x", timeout=timeout)
        ch.name = name  # mirror real from_env_file: routed channel carries its bot name
        return ch

    def fake_send(self, event):
        sent.append((self.name, event.source))

    monkeypatch.setattr(
        DingTalkChannel, "from_env_file", staticmethod(fake_from_env_file)
    )
    monkeypatch.setattr(DingTalkChannel, "send", fake_send)

    # routed source -> service-observability bot
    res = r.notify(_ev("service-observability-bridge"))
    assert res == {"service-observability": None}

    # unrouted source -> default infra-health bot
    res2 = r.notify(_ev("scribe"))
    assert res2 == {"infra-health": None}

    # built names mirror the resolved channels (no real env file touched)
    assert built == ["service-observability", "infra-health"]
    assert ("service-observability", "service-observability-bridge") in sent
    assert ("infra-health", "scribe") in sent


def test_notify_caches_channel_per_run(tmp_path: Path, monkeypatch) -> None:
    r = _routing(tmp_path)
    build_count = {"n": 0}

    def fake_from_env_file(env_file, name="dingtalk", timeout=10.0):
        build_count["n"] += 1
        return DingTalkChannel("https://fake/send", secret="", timeout=timeout)

    monkeypatch.setattr(
        DingTalkChannel, "from_env_file", staticmethod(fake_from_env_file)
    )
    monkeypatch.setattr(DingTalkChannel, "send", lambda self, event: None)

    r.notify(_ev("scribe"))
    r.notify(_ev("vector-index-shadow"))  # same (default) channel -> reuse, no rebuild
    assert build_count["n"] == 1


def test_notify_is_best_effort_on_send_failure(tmp_path: Path, monkeypatch) -> None:
    r = _routing(tmp_path)

    def fake_from_env_file(env_file, name="dingtalk", timeout=10.0):
        return DingTalkChannel("https://fake/send", secret="", timeout=timeout)

    def boom(self, event):
        raise NotifyError("send blew up")

    monkeypatch.setattr(
        DingTalkChannel, "from_env_file", staticmethod(fake_from_env_file)
    )
    monkeypatch.setattr(DingTalkChannel, "send", boom)

    res = r.notify(_ev("scribe"))
    assert res["infra-health"] is not None
    assert "send blew up" in res["infra-health"]


# ---------------------------------------------------------------------------
# malformed config
# ---------------------------------------------------------------------------


def test_unknown_channel_in_route_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text(
        f"""
default_channel = "infra-health"

[channels.infra-health]
env_file = "{tmp_path / "infra.env"}"

[[route]]
source = "service-observability-bridge"
channel = "does-not-exist"
""",
        encoding="utf-8",
    )
    with pytest.raises(RoutingConfigError):
        load_routing(str(cfg))


def test_unknown_default_channel_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "bad2.toml"
    cfg.write_text(
        f"""
default_channel = "nope"

[channels.infra-health]
env_file = "{tmp_path / "infra.env"}"
""",
        encoding="utf-8",
    )
    with pytest.raises(RoutingConfigError):
        load_routing(str(cfg))


def test_missing_field_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "bad3.toml"
    cfg.write_text(
        f"""
[channels.infra-health]
env_file = "{tmp_path / "infra.env"}"
""",
        encoding="utf-8",
    )
    # no default_channel
    with pytest.raises(RoutingConfigError):
        load_routing(str(cfg))


def test_not_a_file_raises() -> None:
    with pytest.raises(RoutingConfigError):
        load_routing("/no/such/routing/config.toml")
