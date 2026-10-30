"""临时登录入口、停止条件和凭据隔离测试。"""

import json
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from homeassistant.core import HomeAssistant

from custom_components.xiaomi_scale_cloud.web_login import LoginSession, LoginView, SESSION_KEY, SESSION_LIFETIME, HEARTBEAT_TIMEOUT
from custom_components.xiaomi_scale_cloud.config_flow import ScaleConfigFlow


class WebLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.flow = Mock()
        self.flow.qr = None
        self.flow._poll_task = None
        async def new_qr():
            self.flow.qr = Mock(deadline=time.monotonic()+900, qr_url="https://account.xiaomi.com/test-qr")
        self.flow._new_qr = AsyncMock(side_effect=new_qr)
        self.session = LoginSession(self.hass, self.flow)
        self.hass.data[SESSION_KEY] = {self.session.token: self.session}
        self.view = LoginView(self.hass)

    async def asyncTearDown(self):
        self.session.expire()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def test_page_has_button_and_no_confirmation_button(self):
        response = await self.view.get(make_mocked_request("GET", self.session.url), self.session.token)
        self.assertIn("刷新二维码", response.text)
        self.assertNotIn("已确认登录", response.text)
        self.assertIn("width:220px", response.text)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("script-src 'nonce-", response.headers["Content-Security-Policy"])
        self.assertNotIn("__NONCE__", response.text)
        self.flow._new_qr.assert_not_awaited()

    async def test_unknown_session_rejected(self):
        with self.assertRaises(web.HTTPGone):
            await self.view.get(make_mocked_request("GET", "/"), "unknown")

    async def test_refresh_requires_session_header(self):
        with self.assertRaises(web.HTTPForbidden):
            await self.view.post(make_mocked_request("POST", self.session.url+"/refresh"), self.session.token, "refresh")
        self.flow._new_qr.assert_not_awaited()

    async def test_manual_refresh_caps_qr_wait_and_hides_credentials(self):
        request = make_mocked_request("POST", self.session.url+"/refresh", headers={"X-Scale-Login": self.session.token})
        self.flow.credential = {"userId": "123", "passToken": "never-in-response"}
        response = await self.view.post(request, self.session.token, "refresh")
        result = json.loads(response.text)
        self.assertEqual(result["status"], "pending")
        self.assertLessEqual(result["expires_in"], 300)
        self.assertNotIn("never-in-response", response.text)
        self.assertNotIn("userId", response.text)
        self.flow._new_qr.assert_awaited_once()

    async def test_duplicate_refresh_is_rate_limited(self):
        await self.session.refresh()
        with self.assertRaises(web.HTTPTooManyRequests):
            await self.session.refresh()
        self.flow._new_qr.assert_awaited_once()

    async def test_expiry_never_generates_another_qr(self):
        await self.session.refresh()
        self.flow.qr.deadline = 0
        for _ in range(3):
            self.assertEqual(self.session.snapshot()["status"], "expired")
        self.flow._new_qr.assert_awaited_once()

    async def test_success_never_generates_another_qr(self):
        self.session.status = "success"
        self.assertEqual((await self.session.refresh())["status"], "success")
        self.flow._new_qr.assert_not_awaited()

    async def test_absolute_session_lifetime(self):
        self.session.created = time.monotonic()-SESSION_LIFETIME-1
        with self.assertRaises(web.HTTPGone):
            await self.session.refresh()
        self.flow._new_qr.assert_not_awaited()

    async def test_closed_page_cancels_pending_request(self):
        self.flow._poll_task = Mock()
        self.flow._poll_task.done.return_value = False
        request = make_mocked_request("POST", self.session.url+"/stop", headers={"X-Scale-Login": self.session.token})
        response = await self.view.post(request, self.session.token, "stop")
        self.assertEqual(json.loads(response.text)["status"], "closed")
        self.flow._poll_task.cancel.assert_called_once()

    async def test_lost_heartbeat_stops_before_next_cloud_request(self):
        flow = ScaleConfigFlow()
        flow.hass = self.hass
        flow._web_session = LoginSession(self.hass, flow)
        flow._web_session.status = "pending"
        flow._web_session.last_seen = time.monotonic()-HEARTBEAT_TIMEOUT-1
        qr = Mock()
        flow.qr = qr
        try:
            await flow._poll_login(qr)
            self.assertEqual(flow._web_session.status, "closed")
            qr.poll.assert_not_called()
            qr.close.assert_called_once()
        finally:
            flow._web_session.expire()

    async def test_status_does_not_restart_expired_session(self):
        self.session.status = "expired"
        response = await self.view.get(make_mocked_request("GET", self.session.url+"/status"), self.session.token, "status")
        self.assertEqual(json.loads(response.text)["status"], "expired")
        self.flow._new_qr.assert_not_awaited()
