"""source→channel 路由:让告警按来源进入对应通知渠道。

配置用 stdlib tomllib + pydantic 校验的 TOML。每个 channel 指向一个 .env 文件,
routes 把 event.source 精确映射到 channel 名, 未命中走 default_channel。

设计同 notify 子系统:secret/URL 绝不进 repo(只存 .env 文件 PATH);发送 best-effort,
单渠道失败不掀翻整轮, 错误先脱敏。配置畸形 → 加载期就 RoutingConfigError 报清楚(mirror
checks.loader 的 CheckConfigError 风格), 不留到运行期才炸。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    PrivateAttr,
    ValidationError,
    model_validator,
)

from pharos.notify.base import HealthEvent, redact
from pharos.notify.dingtalk import DingTalkChannel

_log = structlog.get_logger("pharos.notify.routing")


class RoutingConfigError(ValueError):
    """路由配置畸形(未知 channel、缺字段、非法 TOML)。同 CheckConfigError 风格。"""


class _ChannelCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env_file: str


class _RouteCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    channel: str


class Routing(BaseModel):
    """已校验的路由表。channels:名→env_file;route:source→channel;default_channel 兜底。"""

    model_config = ConfigDict(extra="forbid")

    default_channel: str
    channels: dict[str, _ChannelCfg]
    route: list[_RouteCfg] = []

    _built: dict[str, DingTalkChannel] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _check_channel_refs(self) -> Routing:
        # default_channel 与每条 route.channel 都必须在 channels 里有定义, 否则加载期就报。
        if self.default_channel not in self.channels:
            raise ValueError(
                f"default_channel {self.default_channel!r} not in "
                f"channels {sorted(self.channels)}"
            )
        for r in self.route:
            if r.channel not in self.channels:
                raise ValueError(
                    f"route source={r.source!r} → unknown channel "
                    f"{r.channel!r}; have {sorted(self.channels)}"
                )
        return self

    def channel_for(self, source: str) -> str:
        """source → channel 名。routes 里精确匹配, 否则 default_channel。"""
        for r in self.route:
            if r.source == source:
                return r.channel
        return self.default_channel

    def notify(self, event: HealthEvent) -> dict[str, str | None]:
        """把一条 event 路由到对应 channel 并发送。best-effort:失败 catch+脱敏,
        返回 {channel_name: None成功 | 脱敏错误串}。本轮内按 channel 名缓存已构造的渠道。"""
        name = self.channel_for(event.source)
        try:
            ch = self._built.get(name)
            if ch is None:
                ch = DingTalkChannel.from_env_file(
                    self.channels[name].env_file, name=name
                )
                self._built[name] = ch
            ch.send(event)
            return {name: None}
        except Exception as e:
            # 渠道已构造则用其 url/secret 兜底脱敏;DingTalkChannel.send 已自脱敏,
            # 此处再防 from_env_file 等其它环节漏出。
            ch = self._built.get(name)
            err = redact(str(e), ch.webhook_url, ch.secret) if ch else str(e)
            _log.warning(
                "routed_notify_failed", channel=name, source=event.source, error=err
            )
            return {name: err}


def load_routing(path: str) -> Routing:
    """从 *path* 的 TOML 加载并校验路由表。

    畸形文件 / 缺字段 / route 指向未定义 channel / 非法 TOML → RoutingConfigError。
    """
    try:
        with Path(path).open("rb") as fh:
            doc = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise RoutingConfigError(f"routing config not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RoutingConfigError(f"routing config is not valid TOML: {exc}") from exc

    try:
        return Routing.model_validate(doc)
    except ValidationError as exc:
        raise RoutingConfigError(f"invalid routing config {path}: {exc}") from exc
