"""临时登录页面；只暴露二维码及状态，不向浏览器返回账号凭据。"""

import asyncio
from pathlib import Path
import secrets
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from ._vendor.errors import S400Error
from .const import DOMAIN

SESSION_KEY = f"{DOMAIN}_web_login"
VIEW_KEY = f"{DOMAIN}_web_login_view"
BASE_URL = "/api/xiaomi_scale_cloud/login"
SESSION_LIFETIME = 1800
HEARTBEAT_TIMEOUT = 45
QR_LIFETIME = 300


class LoginSession:
    def __init__(self, hass, flow):
        self.hass = hass
        self.flow = flow
        self.token = secrets.token_urlsafe(32)
        self.created = time.monotonic()
        self.last_seen = self.created
        self.status = "idle"
        self.lock = asyncio.Lock()
        self.last_refresh = float("-inf")
        self.timer = hass.loop.call_later(SESSION_LIFETIME, self.expire)

    @property
    def url(self):
        return f"{BASE_URL}/{self.token}"

    def alive(self):
        return time.monotonic() - self.created < SESSION_LIFETIME

    def expire(self):
        self.stop()
        self.hass.data.get(SESSION_KEY, {}).pop(self.token, None)
        self.timer.cancel()

    def stop(self):
        if self.status != "success":
            self.status = "closed"
        if self.flow._poll_task is not None and not self.flow._poll_task.done():
            self.flow._poll_task.cancel()

    def polling_allowed(self):
        if self.status != "pending":
            return False
        if not self.alive() or time.monotonic() - self.last_seen > HEARTBEAT_TIMEOUT:
            self.status = "closed"
            return False
        return True

    def snapshot(self):
        qr = self.flow.qr
        remaining = max(0, int(qr.deadline - time.monotonic())) if qr else 0
        if self.status == "pending" and not remaining:
            self.status = "expired"
            if self.flow._poll_task is not None:
                self.flow._poll_task.cancel()
        return {
            "status": self.status,
            "qr_url": qr.qr_url if qr and self.status == "pending" else None,
            "expires_in": remaining if self.status == "pending" else 0,
        }

    async def refresh(self):
        if not self.alive():
            raise web.HTTPGone()
        if self.status == "success":
            return self.snapshot()
        if self.lock.locked() or time.monotonic() - self.last_refresh < 2:
            raise web.HTTPTooManyRequests()
        async with self.lock:
            self.last_refresh = time.monotonic()
            self.last_seen = self.last_refresh
            self.status = "starting"
            try:
                await self.flow._new_qr()
            except S400Error:
                if self.status != "closed":
                    self.status = "error"
            else:
                self.flow.qr.deadline = min(self.flow.qr.deadline, time.monotonic() + QR_LIFETIME)
                if self.status == "starting":
                    self.status = "pending"
            return self.snapshot()


class LoginView(HomeAssistantView):
    url = BASE_URL + "/{token}"
    extra_urls = [BASE_URL + "/{token}/{action}"]
    name = "api:xiaomi_scale_cloud:login"
    # URL 中的随机能力令牌仅允许访问这一个短期登录会话。
    requires_auth = False

    def __init__(self, hass):
        self.hass = hass
        self.template = None

    def _session(self, token):
        session = self.hass.data.get(SESSION_KEY, {}).get(token)
        if session is None or not session.alive():
            raise web.HTTPGone(text="登录页面已失效，请回到 Home Assistant 重新开始。")
        return session

    @staticmethod
    def _headers():
        return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}

    async def get(self, request, token, action=None):
        session = self._session(token)
        if action == "status":
            session.last_seen = time.monotonic()
            return web.json_response(session.snapshot(), headers=self._headers())
        if action is not None:
            raise web.HTTPNotFound()
        if self.template is None:
            self.template = await self.hass.async_add_executor_job(
                Path(__file__).with_name("login.html").read_text
            )
        nonce = secrets.token_urlsafe(24)
        headers = self._headers()
        headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src https://*.xiaomi.com https://*.mi.com; "
            "base-uri 'none'; frame-ancestors 'self'; form-action 'none'"
        )
        return web.Response(text=self.template.replace("__NONCE__", nonce), content_type="text/html", headers=headers)

    async def post(self, request, token, action=None):
        session = self._session(token)
        # 自定义请求头不能由跨站表单或图片设置；此接口不开放跨域访问。
        if not secrets.compare_digest(request.headers.get("X-Scale-Login", ""), token):
            raise web.HTTPForbidden()
        if action == "refresh":
            return web.json_response(await session.refresh(), headers=self._headers())
        if action == "stop":
            session.stop()
            return web.json_response({"status": session.status}, headers=self._headers())
        raise web.HTTPNotFound()


def create_session(hass, flow):
    if not hass.data.get(VIEW_KEY):
        hass.http.register_view(LoginView(hass))
        hass.data[VIEW_KEY] = True
    session = LoginSession(hass, flow)
    hass.data.setdefault(SESSION_KEY, {})[session.token] = session
    return session
