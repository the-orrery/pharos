"""ConsoleChannel:把告警打到一个流(默认 stdout)。零配置、无 secret —— 开源开箱
即用 + 测试默认渠道。这是通知输出(给人看的告警),不是诊断日志,故直接 print。"""

from __future__ import annotations

import sys
from typing import TextIO

from pharos.notify.base import HealthEvent


class ConsoleChannel:
    name = "console"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    def send(self, event: HealthEvent) -> None:
        line = f"[{event.severity.value}] {event.status.value} {event.source}: {event.title}"
        if event.detail:
            line += f" — {event.detail}"
        print(line, file=self._stream or sys.stdout)
