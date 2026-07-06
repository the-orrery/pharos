from __future__ import annotations

import sys

import pytest
import structlog


@pytest.fixture(autouse=True)
def _structlog_test_logging() -> None:
    # 把 structlog 配成写当前 sys.stderr 且不缓存 logger。否则未配置时的默认 logger 会
    # 缓存一个绑定到 pytest 已关闭 stdout 的 PrintLogger,触发 "I/O on closed file"。
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.KeyValueRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
