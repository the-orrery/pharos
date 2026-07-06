from __future__ import annotations

from pharos.notify import (
    HealthEvent,
    Severity,
    Status,
    available_channels,
    build_channel,
    notify,
)
from pharos.notify.base import redact
from pharos.notify.console import ConsoleChannel
from pharos.notify.dingtalk import DingTalkChannel


def _ev() -> HealthEvent:
    return HealthEvent(
        source="t.check",
        status=Status.DOWN,
        severity=Severity.CRITICAL,
        title="x down",
        detail="boom",
    )


def test_registry_is_pluggable() -> None:
    # 多渠道:注册表里至少有 console + dingtalk 两个后端。
    assert "console" in available_channels()
    assert "dingtalk" in available_channels()


def test_console_send_does_not_raise() -> None:
    ConsoleChannel().send(_ev())


def test_dingtalk_sign_deterministic_and_no_secret_in_url() -> None:
    ch = DingTalkChannel(
        "https://example.com/robot/send?access_token=example",
        "example-signing-key",
    )
    url = ch._signed_url(1700000000000)
    assert "timestamp=1700000000000" in url and "sign=" in url
    assert "example-signing-key" not in url  # HMAC 签名, secret 不出现在 URL
    assert ch._signed_url(1700000000000) == url  # 同 ts 决定性


def test_dingtalk_no_secret_is_bare_url() -> None:
    ch = DingTalkChannel("https://example.com/send", secret="")
    assert ch._signed_url(123) == "https://example.com/send"


def test_redact_strips_url_and_secret() -> None:
    out = redact(
        "POST https://x/send?access_token=example failed; key=example-key",
        "https://x/send?access_token=example",
        "example-key",
    )
    assert "example" not in out and "example-key" not in out


def test_notify_best_effort_console_ok() -> None:
    assert notify(_ev(), ["console"]) == {"console": None}


def test_notify_unknown_channel_is_caught() -> None:
    res = notify(_ev(), ["nope"])
    assert res["nope"] is not None and "unknown channel" in res["nope"]


def test_build_channel_unknown_raises() -> None:
    import pytest

    from pharos.notify import NotifyError

    with pytest.raises(NotifyError):
        build_channel("nope")
