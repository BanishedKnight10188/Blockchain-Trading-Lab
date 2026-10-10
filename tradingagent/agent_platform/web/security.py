"""Same-origin writes and signed, expiring local browser sessions."""

import hashlib
import hmac
import re
import secrets
import time

from fastapi import HTTPException, Request


class LocalBrowserSession:
    cookie_name = "tradingagent_browser"
    max_age = 3600

    def __init__(self):
        self.key = secrets.token_bytes(32)

    def _digest(self, value: str) -> str:
        return hmac.new(self.key, value.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        content = f"{secrets.token_urlsafe(24)}.{int(time.time())}"
        return f"{content}.{self._digest(content)}"

    def valid(self, cookie: str | None) -> bool:
        if cookie is None or not re.fullmatch(
            r"[A-Za-z0-9_-]{32}\.[0-9]{10,12}\.[a-f0-9]{64}", cookie
        ):
            return False
        content, signature = cookie.rsplit(".", 1)
        timestamp = int(content.rsplit(".", 1)[1])
        return 0 <= time.time() - timestamp <= self.max_age and hmac.compare_digest(
            signature, self._digest(content)
        )

    def csrf(self, cookie: str) -> str:
        return self._digest("csrf:" + cookie)

    def require_read(self, request: Request) -> None:
        if not self.valid(request.cookies.get(self.cookie_name)):
            raise HTTPException(403, "会话已过期，请刷新页面后再试。")

    def require_write(self, request: Request) -> None:
        self.require_read(request)
        expected_origin = f"{request.url.scheme}://{request.headers['host']}"
        csrf = request.headers.get("X-CSRF-Token", "")
        cookie = request.cookies[self.cookie_name]
        if (
            request.headers.get("Origin") != expected_origin
            or not re.fullmatch(r"[a-f0-9]{64}", csrf)
            or not hmac.compare_digest(csrf, self.csrf(cookie))
        ):
            raise HTTPException(403, "保存请求校验失败，请刷新页面后再试。")
