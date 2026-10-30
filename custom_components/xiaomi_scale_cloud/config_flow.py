"""米家二维码登录与设备选择。"""

import asyncio
import logging
import time

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .api import QrLogin, ScaleClient, NoMeasurements
from ._vendor.errors import S400Error, LoginExpired, AuthenticationError, NetworkError, ProtocolError
from .const import DOMAIN, DEFAULT_INTERVAL, REGIONS


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

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.region = user_input["region"]
            try:
                await self._new_qr()
                return await self.async_step_qr()
            except S400Error:
                errors["base"] = "cannot_connect"
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
        previous = self.qr
        previous_task = self._poll_task
        self.qr = candidate
        self.credential = None
        self._poll_error = None
        if previous_task is not None:
            previous_task.cancel()
        elif previous:
            await self.hass.async_add_executor_job(previous.close)
        self._poll_task = self.hass.async_create_background_task(
            self._poll_login(candidate), "xiaomi_scale_cloud QR login"
        )

    async def _poll_login(self, qr):
        pending = None
        try:
            while self.qr is qr:
                pending = self.hass.async_add_executor_job(qr.poll)
                # 取消后台任务不会终止线程，需等当前 HTTP 请求退出再关闭会话。
                result = await asyncio.shield(pending)
                pending = None
                if self.qr is not qr:
                    return
                if result:
                    self.credential = result
                    LOGGER.debug("QR login confirmed")
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
            LOGGER.debug("QR login polling ended: %s", type(err).__name__)
        finally:
            await self.hass.async_add_executor_job(qr.close)

    def _qr_menu(self, message=""):
        return self.async_show_menu(
            step_id="qr",
            menu_options=["qr_check", "qr_refresh"],
            description_placeholders={
                "qr_url": self.qr.qr_url,
                "login_url": self.qr.login_url,
                "status": message,
            },
        )

    async def async_step_qr(self, user_input=None):
        return self._qr_menu()

    async def async_step_qr_refresh(self, user_input=None):
        try:
            await self._new_qr()
        except S400Error:
            return self._qr_menu("刷新失败，请检查网络后重试。")
        return self._qr_menu()

    async def async_step_qr_check(self, user_input=None):
        # 按钮只检查后台结果，不再启动另一条登录轮询。
        if self._poll_task is not None and not self._poll_task.done():
            await asyncio.wait({self._poll_task}, timeout=0.2)
        if self.credential:
            if self.reauth_entry and self.credential["userId"] != self.reauth_entry.data["credential"]["userId"]:
                self.credential = None
                return self._qr_menu("请使用原来的米家账号。点击「刷新二维码」后重新扫码。")
            if self.reauth_entry:
                return await self._finish_device()
            return await self._load_devices()
        if isinstance(self._poll_error, LoginExpired) or time.monotonic() >= self.qr.deadline:
            try:
                await self._new_qr()
            except S400Error:
                return self._qr_menu("二维码已过期，刷新失败，请稍后重试。")
            return self._qr_menu("二维码已过期，请扫描新二维码。")
        if self._poll_error:
            return self._qr_menu("登录连接已中断，请点击「刷新二维码」重试。")
        return self._qr_menu("尚未收到登录确认，请先在米家 App 中确认。")

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
        choices = {did: f"{device['name']} · {device['model']} · {did}" for did, device in self.devices.items()}
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
            try:
                await self._new_qr()
            except S400Error:
                return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({}), errors={"base": "cannot_connect"})
            return await self.async_step_qr()
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ScaleOptionsFlow()

    @callback
    def async_remove(self):
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
