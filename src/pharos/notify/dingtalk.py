"""DingTalkChannel:钉钉自定义机器人(加签)。多后端里的一个实现。

签名口径:HMAC-SHA256(key=secret, msg=f"{ts}\\n{secret}") → base64 →
urlencode,拼 &timestamp=&sign=。HTTP 走 stdlib urllib(零三方依赖)。
webhook URL / secret 从 env(DINGTALK_WEBHOOK_URL / DINGTALK_SECRET)取,绝不进 repo;
任何错误先脱敏 URL+secret 再抛,防 token 泄露。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from pharos.notify.base import HealthEvent, NotifyError, redact

_ENV_URL = "DINGTALK_WEBHOOK_URL"
_ENV_SECRET = "DINGTALK_SECRET"


def _parse_env_file(path: str) -> dict[str, str]:
    """极简 .env 解析: `KEY=VALUE` 逐行, 跳过空行/`#` 注释, 去掉成对的引号。

    不做 export 关键字 / 变量插值等花活——只够读 webhook+secret 两行。
    文件不存在 → NotifyError(脱敏: 只暴露路径, 不暴露内容)。
    """
    out: dict[str, str] = {}
    try:
        with Path(path).open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):  # noqa: PLR2004
                    val = val[1:-1]
                if key:
                    out[key] = val
    except FileNotFoundError as exc:
        raise NotifyError(f"env file not found: {path}") from exc
    return out


class DingTalkChannel:
    name = "dingtalk"

    def __init__(
        self, webhook_url: str, secret: str = "", timeout: float = 10.0
    ) -> None:
        self.webhook_url = webhook_url
        self.secret = secret
        self.timeout = timeout

    @classmethod
    def from_env(cls, timeout: float = 10.0) -> DingTalkChannel:
        url = os.environ.get(_ENV_URL, "")
        if not url:
            raise NotifyError(f"{_ENV_URL} not set")
        return cls(url, os.environ.get(_ENV_SECRET, ""), timeout)

    @classmethod
    def from_env_file(
        cls, env_file: str, name: str = "dingtalk", timeout: float = 10.0
    ) -> DingTalkChannel:
        """从一个 .env 文件构造渠道(routing 用:每个 bot 一份 webhook+secret 文件)。

        解析 `KEY=VALUE` 行(跳过空行/`#` 注释, 去掉可选的成对引号)。读
        DINGTALK_WEBHOOK_URL(必需 → 缺失抛 NotifyError)与 DINGTALK_SECRET(可选)。
        `name` 让被路由的渠道带上各自的 bot 名(进摘要/日志, 不泄 token)。
        """
        env = _parse_env_file(env_file)
        url = env.get(_ENV_URL, "")
        if not url:
            raise NotifyError(f"{_ENV_URL} missing in env file {env_file}")
        ch = cls(url, env.get(_ENV_SECRET, ""), timeout)
        ch.name = name
        return ch

    def _signed_url(self, ts_ms: int) -> str:
        if not self.secret:
            return self.webhook_url
        string_to_sign = f"{ts_ms}\n{self.secret}"
        digest = hmac.new(
            self.secret.encode(), string_to_sign.encode(), hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode())
        sep = "&" if "?" in self.webhook_url else "?"
        return f"{self.webhook_url}{sep}timestamp={ts_ms}&sign={sign}"

    def send(self, event: HealthEvent) -> None:
        ts_ms = int(time.time() * 1000)
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": event.title, "text": event.markdown()},
        }
        req = urllib.request.Request(
            self._signed_url(ts_ms),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout
            ) as resp:  # 固定 https webhook
                body = resp.read().decode("utf-8", "replace")
        except Exception as e:  # 统一脱敏后外抛, 不漏 token
            raise NotifyError(redact(str(e), self.webhook_url, self.secret)) from None
        result = json.loads(body) if body else {}
        if result.get("errcode", 0) != 0:
            raise NotifyError(
                f"dingtalk errcode={result.get('errcode')} errmsg={result.get('errmsg')}"
            )
