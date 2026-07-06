"""通知子系统的渠道无关核心:事件模型 + 可插拔 Channel 协议。

设计:pharos 体检/路由产出 HealthEvent;具体怎么送(钉钉/slack/邮件/webhook)由
实现 Channel 协议的后端决定。开源用户加渠道 = 实现 Channel + 在 notify/__init__ 注册,
不碰核心。secret/URL 绝不进 repo(留 ~/.config / env);错误一律先脱敏再外抛。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class Severity(enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Status(enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HealthEvent:
    """一次检查/告警的结构化结果。渠道无关 —— 各 Channel 自行渲染。"""

    source: str  # 检查项/来源标识, e.g. "example-service"
    status: Status
    severity: Severity
    title: str
    detail: str = ""
    evidence: dict = field(default_factory=dict)

    def markdown(self) -> str:
        lines = [
            f"**{self.title}**",
            "",
            f"- source: `{self.source}`",
            f"- status: {self.status.value} · severity: {self.severity.value}",
        ]
        if self.detail:
            lines += ["", self.detail]
        return "\n".join(lines)


class NotifyError(RuntimeError):
    """通知发送失败(已脱敏)。"""


@runtime_checkable
class Channel(Protocol):
    """可插拔通知渠道。新增后端 = 实现本协议(name + send)+ 在 notify/__init__ 注册。"""

    name: str

    def send(self, event: HealthEvent) -> None: ...


def redact(text: str, *secrets: str) -> str:
    """从(错误)文本里抹掉 webhook URL / secret, 防 token 泄进日志或上抛的异常。"""
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "<redacted>")
    return out
