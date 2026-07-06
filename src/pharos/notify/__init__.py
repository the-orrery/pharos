"""通知子系统:渠道注册表 + best-effort 广播。

多渠道可插拔(用户清单驱动):新增后端 = 实现 Channel(base.py)+ 在 _BUILDERS 登记。
notify() 向选定渠道逐个发送,单渠道失败不影响其它(中心健康汇聚不该被一个挂掉的
webhook 拖死)。
"""

from __future__ import annotations

import structlog

from pharos.notify.base import (
    Channel,
    HealthEvent,
    NotifyError,
    Severity,
    Status,
    redact,
)
from pharos.notify.console import ConsoleChannel
from pharos.notify.dingtalk import DingTalkChannel

_log = structlog.get_logger("pharos.notify")

# 渠道注册表: name -> 无参工厂(从 env/默认构造)。新增后端在此登记一行。
_BUILDERS: dict[str, object] = {
    "console": ConsoleChannel,
    "dingtalk": DingTalkChannel.from_env,
}


def available_channels() -> list[str]:
    return sorted(_BUILDERS)


def build_channel(name: str) -> Channel:
    builder = _BUILDERS.get(name)
    if builder is None:
        raise NotifyError(f"unknown channel '{name}'; have {available_channels()}")
    return builder()  # type: ignore[operator]


def notify(event: HealthEvent, channels: list[str]) -> dict[str, str | None]:
    """向给定渠道广播一条 event。best-effort:逐渠道发送,失败互不影响;返回每渠道
    结果(None=成功,否则脱敏后的错误串)。"""
    results: dict[str, str | None] = {}
    for name in channels:
        try:
            build_channel(name).send(event)
            results[name] = None
        except Exception as e:  # 单渠道失败不该掀翻广播
            results[name] = str(e)
            _log.warning("notify_channel_failed", channel=name, error=str(e))
    return results


__all__ = [
    "Channel",
    "HealthEvent",
    "NotifyError",
    "Severity",
    "Status",
    "available_channels",
    "build_channel",
    "notify",
    "redact",
]
