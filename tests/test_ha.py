"""在真实 Home Assistant 依赖下检查流程、传感器和失败语义。"""

import tempfile
import unittest
from types import MappingProxyType
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntries
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.xiaomi_scale_cloud.api import parse_measurement, PersonReport
from custom_components.xiaomi_scale_cloud.config_flow import ScaleConfigFlow
from custom_components.xiaomi_scale_cloud.coordinator import ScaleCoordinator
from custom_components.xiaomi_scale_cloud.sensor import ScaleSensor, MeasurementTime, async_setup_entry as setup_sensors
from custom_components.xiaomi_scale_cloud.button import RefreshButton
from custom_components.xiaomi_scale_cloud._vendor.errors import NetworkError, AuthenticationError, ProtocolError
from custom_components.xiaomi_scale_cloud.api import NoMeasurements
from test_api import record


class HomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.entry = ConfigEntry(domain="xiaomi_scale_cloud", title="测试", version=1, minor_version=1,
            source="user", unique_id="test", options={}, discovery_keys=MappingProxyType({}), subentries_data=[],
            data={"credential": {"userId": "123", "passToken": "fake"}, "region": "cn", "model": "xiaomi.scales.ms110",
                  "did": "test-scale", "device_name": "小可爱"})
        with patch.object(ConfigEntries, "async_setup", new=AsyncMock(return_value=True)):
            await self.hass.config_entries.async_add(self.entry)

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def test_sensor_native_values_and_zero_heart(self):
        coordinator = ScaleCoordinator(self.hass, self.entry)
        coordinator.async_set_updated_data({"1": PersonReport("测试", parse_measurement(record()))})
        weight = ScaleSensor(coordinator, "weight", "1")
        self.assertEqual(weight.native_value, 96.95)
        self.assertEqual(weight.native_unit_of_measurement, "kg")
        self.assertEqual(weight.suggested_display_precision, 2)
        self.assertIsNone(ScaleSensor(coordinator, "heartRate", "1").native_value)
        self.assertIsNotNone(MeasurementTime(coordinator, "1").native_value.tzinfo)
        self.assertEqual(len(weight.device_info["identifiers"]), 1)
        self.assertEqual(coordinator.update_interval.total_seconds(), 900)
        coordinator.last_update_success = False
        self.assertFalse(weight.available)
        self.assertTrue(RefreshButton(coordinator).available)

    async def test_network_failure_and_expired_credentials_differ(self):
        coordinator = ScaleCoordinator(self.hass, self.entry)
        coordinator.client.reports = Mock(side_effect=NetworkError)
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
        coordinator.client.reports = Mock(side_effect=AuthenticationError)
        with self.assertRaises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()

    async def test_qr_profile_create_flow(self):
        flow = ScaleConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        qr = Mock(qr_url="https://account.xiaomi.com/test-qr", login_url="https://account.xiaomi.com/test-login", deadline=float("inf"))
        qr.poll.return_value = {"userId": "123", "passToken": "fake"}
        client = Mock()
        model = "example.scales.future"
        client.devices.return_value = {"test-scale": {"did": "test-scale", "name": "测试秤", "model": model}}
        client.reports.return_value = {
            "1": PersonReport("测试", parse_measurement(record())),
            "2": PersonReport("家人", parse_measurement(record(pid=2, weight="60"))),
        }
        with patch("custom_components.xiaomi_scale_cloud.config_flow.QrLogin", return_value=qr), patch("custom_components.xiaomi_scale_cloud.config_flow.ScaleClient", return_value=client):
            initial = await flow.async_step_user({"region": "cn"})
            self.assertEqual(initial["step_id"], "qr")
            self.assertIn("qr_url", initial["description_placeholders"])
            result = await flow.async_step_qr({"renew": False})
            self.assertEqual(result["step_id"], "device")
            result = await flow.async_step_device({"device": "test-scale"})
            self.assertEqual(result["type"], "create_entry")
            self.assertNotIn("profile_id", result["data"])
            self.assertEqual(set(flow.reports), {"1", "2"})
            self.assertEqual(result["data"]["credential"]["userId"], "123")
            self.assertEqual(result["data"]["model"], model)
            self.assertEqual(result["title"], "测试秤")
            client.reports.assert_called_once_with("test-scale")
            self.assertEqual(client.close.call_count, 2)

    async def test_unknown_model_entity_name(self):
        self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, "model": "example.scales.future"})
        coordinator = ScaleCoordinator(self.hass, self.entry)
        coordinator.async_set_updated_data({"1": PersonReport("测试", parse_measurement(record()))})
        self.assertEqual(ScaleSensor(coordinator, "weight", "1").device_info["model"], "example.scales.future")

    async def test_empty_device_list_is_reported(self):
        flow = ScaleConfigFlow()
        flow.hass = self.hass
        flow.credential = {"userId": "123", "passToken": "fake"}
        client = Mock()
        client.devices.return_value = {}
        with patch("custom_components.xiaomi_scale_cloud.config_flow.ScaleClient", return_value=client):
            result = await flow._load_devices()
        self.assertEqual(result["reason"], "no_devices")
        client.close.assert_called_once()

    async def test_unconfirmed_qr_does_not_create_entry(self):
        flow = ScaleConfigFlow()
        flow.hass = self.hass
        flow.qr = Mock(qr_url="https://account.xiaomi.com/qr", login_url="https://account.xiaomi.com/login", deadline=float("inf"))
        flow.qr.poll.return_value = None
        result = await flow.async_step_qr({"renew": False})
        self.assertEqual(result["step_id"], "qr")
        self.assertEqual(result["errors"]["base"], "not_scanned")

    async def test_reauth_rejects_another_account(self):
        flow = ScaleConfigFlow()
        flow.hass = self.hass
        flow.reauth_entry = self.entry
        flow.qr = Mock(qr_url="https://account.xiaomi.com/qr", login_url="https://account.xiaomi.com/login", deadline=float("inf"))
        flow.qr.poll.return_value = {"userId": "999", "passToken": "fake"}
        result = await flow.async_step_qr({"renew": False})
        self.assertEqual(result["errors"]["base"], "wrong_account")
        self.assertIsNone(flow.credential)


    async def test_new_people_create_entities_once_and_names_keep_ids(self):
        coordinator = ScaleCoordinator(self.hass, self.entry)
        one = PersonReport("同名", parse_measurement(record()))
        two = PersonReport("同名", parse_measurement(record(pid=2, weight="60")))
        coordinator.async_set_updated_data({"1": one, "2": two})
        self.entry.runtime_data = coordinator
        entities = []
        registry = Mock()
        registry.async_get_device.return_value = None
        with patch("custom_components.xiaomi_scale_cloud.sensor.dr.async_get", return_value=registry):
            await setup_sensors(self.hass, self.entry, entities.extend)
            self.assertEqual(len(entities), 44)
            self.assertEqual(len({e.unique_id for e in entities}), 44)
            before = {e.unique_id for e in entities}
            coordinator.async_set_updated_data({"1": PersonReport("新名字", one.measurement), "2": two})
            self.assertEqual({e.unique_id for e in entities}, before)
            coordinator.async_set_updated_data({"1": one, "2": two, "3": two})
            self.assertEqual(len(entities), 66)
            coordinator.async_set_updated_data({"1": one, "2": two, "3": two})
            self.assertEqual(len(entities), 66)

    async def test_incremental_update_preserves_inactive_person_and_newer_data(self):
        coordinator = ScaleCoordinator(self.hass, self.entry)
        one = PersonReport("甲", parse_measurement(record()))
        two = PersonReport("乙", parse_measurement(record(pid=2, weight="60")))
        coordinator.async_set_updated_data({"1": one, "2": two})
        coordinator.client.reports = Mock(return_value={
            "1": PersonReport("甲", parse_measurement(record(timestamp=1793398800000, weight="90"))),
            "3": PersonReport("丙", parse_measurement(record(pid=3, weight="70"))),
        })
        updated = await coordinator._async_update_data()
        self.assertEqual(set(updated), {"1", "2", "3"})
        self.assertEqual(updated["1"].measurement.values["weight"], 96.95)
        self.assertEqual(updated["2"], two)

    async def test_wrong_device_stays_on_selection_page(self):
        flow = ScaleConfigFlow()
        flow.hass = self.hass
        flow.credential = {"userId": "123", "passToken": "fake"}
        flow.devices = {"vac": {"did": "vac", "name": "小可爱", "model": "example.vacuum.test"}}
        client = Mock()
        client.reports.side_effect = NetworkError
        with patch("custom_components.xiaomi_scale_cloud.config_flow.ScaleClient", return_value=client):
            result = await flow.async_step_device({"device": "vac"})
        self.assertEqual(result["step_id"], "device")
        self.assertEqual(result["errors"]["base"], "cannot_connect")

    async def test_device_errors_are_distinct(self):
        for error, message in ((NoMeasurements, "no_measurements"), (AuthenticationError, "authentication_failed"), (ProtocolError, "unsupported_response"), (NetworkError, "cannot_connect")):
            with self.subTest(error=error):
                flow = ScaleConfigFlow()
                flow.hass = self.hass
                flow.credential = {"userId": "123", "passToken": "fake"}
                flow.devices = {"device": {"did": "device", "name": "测试", "model": "example.device.test"}}
                client = Mock()
                client.reports.side_effect = error
                with patch("custom_components.xiaomi_scale_cloud.config_flow.ScaleClient", return_value=client):
                    result = await flow.async_step_device({"device": "device"})
                self.assertEqual(result["step_id"], "device")
                self.assertEqual(result["errors"]["base"], message)

    async def test_identity_conflict_across_polls_fails(self):
        coordinator = ScaleCoordinator(self.hass, self.entry)
        coordinator.async_set_updated_data({"account:1": PersonReport("甲", parse_measurement(record()), "123")})
        coordinator.client.reports = Mock(return_value={"account:1": PersonReport("乙", parse_measurement(record()), "124")})
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
