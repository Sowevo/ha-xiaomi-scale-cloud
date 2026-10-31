"""米家二维码登录与设备选择。"""

import asyncio
import logging
import time

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.network import get_url, NoURLAvailableError

from .api import QrLogin, ScaleClient, NoMeasurements
from ._vendor.errors import S400Error, LoginExpired, LoginCancelled, AuthenticationError, NetworkError, ProtocolError
from .const import DOMAIN, DEFAULT_INTERVAL, REGIONS
from .web_login import create_session, QR_LIFETIME


LOGGER = logging.getLogger(__name__)


class ScaleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.region = "cn"
        self.model = ""
        self.devices = {}
        self.device = None
        self.qr = None
        self.credential = None
        self.reports = {}
        self.reauth_entry = None
        self.device_error = "no_measurements"
        self._poll_task = None
        self._poll_error = None
        self._web_session = None

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.region = user_input["region"]
            return await self.async_step_qr()
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required("region", default=self.region): vol.In(REGIONS),
        }), errors=errors)

    async def _new_qr(self):
        candidate = QrLogin()
        try:
            await self.hass.async_add_executor_job(candidate.start)
        except Exception:
            await self.hass.async_add_executor_job(candidate.close)
            raise
        if self._web_session and (
            not self._web_session.alive() or self._web_session.status != "starting"
        ):
            await self.hass.async_add_executor_job(candidate.close)
            raise LoginCancelled()
        previous = self.qr
        previous_task = self._poll_task
        self.qr = candidate
        self.credential = None
        self._poll_error = None
        if previous_task is not None:
            previous_task.cancel()
        elif previous:
            await self.hass.async_add_executor_job(previous.close)
        if self._web_session:
            candidate.deadline = min(candidate.deadline, time.monotonic() + QR_LIFETIME)
            self._web_session.status = "pending"
        self._poll_task = self.hass.async_create_background_task(
            self._poll_login(candidate), "xiaomi_scale_cloud QR login"
        )

    async def _poll_login(self, qr):
        pending = None
        try:
            while self.qr is qr:
                if self._web_session and not self._web_session.polling_allowed():
                    return
                pending = self.hass.async_add_executor_job(qr.poll)
                # 取消后台任务不会终止线程，需等当前 HTTP 请求退出再关闭会话。
                result = await asyncio.shield(pending)
                pending = None
                if self.qr is not qr:
                    return
                if self._web_session and not self._web_session.polling_allowed():
                    return
                if self._web_session and time.monotonic() >= qr.deadline:
                    raise LoginExpired()
                if result:
                    if self.reauth_entry and result["userId"] != self.reauth_entry.data["credential"]["userId"]:
                        if self._web_session:
                            self._web_session.status = "wrong_account"
                        return
                    self.credential = result
                    LOGGER.debug("QR login confirmed")
                    if self._web_session:
                        self._web_session.status = "success"
                        try:
                            await self.hass.config_entries.flow.async_configure(self.flow_id)
                        except UnknownFlow:
                            self.credential = None
                            self._web_session.expire()
                    return
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            if pending is not None:
                try:
                    await pending
                except Exception:
                    pass
            raise
        except S400Error as err:
            if self.qr is qr:
                self._poll_error = err
                if self._web_session:
                    self._web_session.status = "expired" if isinstance(err, LoginExpired) else "error"
            LOGGER.debug("QR login polling ended: %s", type(err).__name__)
        finally:
            await self.hass.async_add_executor_job(qr.close)

    async def async_step_qr(self, user_input=None):
        if self.credential:
            return self.async_external_step_done(next_step_id="login_complete")
        if self._web_session is None or not self._web_session.alive():
            self._web_session = create_session(self.hass, self)
        try:
            login_url = get_url(self.hass, prefer_external=True).rstrip("/") + self._web_session.url
        except NoURLAvailableError:
            return self.async_abort(reason="ha_url_missing")
        return self.async_external_step(
            step_id="qr", url=login_url,
            description_placeholders={"login_url": login_url},
        )

    async def async_step_login_complete(self, user_input=None):
        if not self.credential:
            return self.async_abort(reason="authentication_failed")
        if self.reauth_entry:
            return await self._finish_device()
        return await self._load_devices()

    async def _load_devices(self):
        client = ScaleClient(self.credential, self.region, "")
        try:
            self.devices = await self.hass.async_add_executor_job(client.devices)
        except S400Error:
            return self.async_abort(reason="cannot_connect")
        finally:
            await self.hass.async_add_executor_job(client.close)
        if not self.devices:
            return self.async_abort(reason="no_devices")
        return await self.async_step_device()

    async def async_step_device(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.device = self.devices[user_input["device"]]
            self.model = self.device["model"]
            result = await self._finish_device()
            if result is not None:
                return result
            errors["base"] = self.device_error
        ordered_devices = sorted(
            self.devices.items(),
            key=lambda item: not (
                "秤" in item[1]["name"] or "scale" in item[1]["name"].casefold()
            ),
        )
        choices = {did: f"{device['name']} · {device['model']} · {did}" for did, device in ordered_devices}
        return self.async_show_form(step_id="device", data_schema=vol.Schema({vol.Required("device"): vol.In(choices)}), errors=errors)

    async def _finish_device(self):
        did = self.reauth_entry.data["did"] if self.reauth_entry else self.device["did"]
        client = ScaleClient(self.credential, self.region, self.model)
        try:
            self.reports = await self.hass.async_add_executor_job(client.reports, did)
        except NoMeasurements:
            self.device_error = "no_measurements"
        except AuthenticationError:
            self.device_error = "authentication_failed"
        except NetworkError:
            self.device_error = "cannot_connect"
        except ProtocolError:
            self.device_error = "unsupported_response"
        except S400Error:
            self.device_error = "cannot_connect"
        else:
            self.device_error = ""
        finally:
            await self.hass.async_add_executor_job(client.close)
        if self.device_error:
            if self.reauth_entry:
                return self.async_abort(reason=self.device_error)
            return None
        if self.reauth_entry:
            return self.async_update_reload_and_abort(self.reauth_entry, data_updates={"credential": self.credential})
        identity = f"{self.credential['userId']}_{self.region}_{did}"
        await self.async_set_unique_id(identity)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=self.device["name"], data={
            "credential": self.credential, "region": self.region, "model": self.model,
            "did": did, "device_name": self.device["name"],
        })

    async def async_step_reauth(self, entry_data):
        self.reauth_entry = self._get_reauth_entry()
        self.region, self.model = entry_data["region"], entry_data["model"]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        if user_input is not None:
            return await self.async_step_qr()
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ScaleOptionsFlow()

    @callback
    def async_remove(self):
        if self._web_session:
            session = self._web_session
            session.stop()
            # 成功页短暂保留状态供新窗口读取，之后清除临时入口。
            session.timer.cancel()
            session.timer = self.hass.loop.call_later(30, session.expire)
        qr = self.qr
        self.qr = None
        self.credential = None
        if self._poll_task is not None:
            self._poll_task.cancel()
        elif qr:
            self.hass.async_add_executor_job(qr.close)


class ScaleOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required("interval", default=self.config_entry.options.get("interval", DEFAULT_INTERVAL)): vol.All(vol.Coerce(int), vol.Range(min=5, max=1440)),
        }))
