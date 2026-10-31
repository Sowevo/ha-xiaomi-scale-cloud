"""只使用虚构身份的契约测试，不包含账号或私人报告。"""

import base64
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from custom_components.xiaomi_scale_cloud.api import (
    parse_measurement, ScaleClient, QrLogin, NoMeasurements,
)
from custom_components.xiaomi_scale_cloud._vendor.auth import ServiceSession, rc4, HISTORY_PATH
from custom_components.xiaomi_scale_cloud._vendor.errors import AuthenticationError, ProtocolError, LoginExpired


def record(pid=1, did="test-scale", timestamp=1793398833000, weight="96.95", **extra):
    body = {"user": {"id": pid, "name": "测试档案"}, "weight": weight, "bmi": "29.9",
            "heartRate": 0, "bodyRes": "533.4", "bodyRes2": "471.1",
            "bodyResData": json.dumps({"bfp": "29.1", "slm": "65", "bmc": "3.7", "bmr": "1854", "sbc": "76"})}
    body.update(extra)
    return {"did": did, "model": "xiaomi.scales.ms110", "createTime": timestamp, "data": json.dumps(body)}


class ParsingTests(unittest.TestCase):
    def test_pro_nested_composition_and_precision(self):
        m = parse_measurement(record())
        self.assertEqual(m.values["weight"], 96.95)
        self.assertEqual(m.values["bfp"], 29.1)
        self.assertEqual(m.values["bmr"], 1854)
        self.assertEqual(m.measured_at.isoformat(), "2026-10-30T22:20:33+00:00")

    def test_unknown_heart_is_not_zero(self):
        self.assertIsNone(parse_measurement(record()).values["heartRate"])
        self.assertEqual(parse_measurement(record(heartRate=72)).values["heartRate"], 72)

    def test_partial_report_does_not_retain_old_fields(self):
        m = parse_measurement(record(bodyResData="", bodyRes=0, bodyRes2=0))
        self.assertEqual(m.values["weight"], 96.95)
        self.assertIsNone(m.values["bfp"])
        self.assertIsNone(m.values["bodyRes"])

    def test_bad_weight_rejected(self):
        for weight in (None, "", "nan", "inf", "wrong", 0, -1, True):
            with self.subTest(weight=weight), self.assertRaises(ProtocolError):
                parse_measurement(record(weight=weight))

    def test_bad_composition_rejected(self):
        for composition in ("invalid", "[]", "null"):
            with self.subTest(composition=composition), self.assertRaises(ProtocolError):
                parse_measurement(record(bodyResData=composition))

    def test_null_missing_values_are_not_zero(self):
        m = parse_measurement(record(bodyResData=json.dumps({"bfp": "nan", "slm": None})))
        self.assertIsNone(m.values["bfp"])
        self.assertIsNone(m.values["slm"])


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ScaleClient({"userId": "123", "passToken": "fake"}, "cn", "xiaomi.scales.ms110")

    def test_filters_device_and_profile_then_latest(self):
        self.client.page = Mock(return_value=[record(pid=2, weight="60"), record(did="another", weight="70"), record(timestamp=1793398800000, weight="95"), record()])
        self.assertEqual(self.client.latest("test-scale", "1").values["weight"], 96.95)

    def test_discovery_accepts_unknown_models_and_drops_credentials(self):
        self.client._call = Mock(return_value={"result": {"list": [
            {"did": "a", "model": "xiaomi.scales.ms110", "name": "秤", "token": "sensitive"},
            {"did": "b", "model": "example.scales.future", "name": "其他秤"},
            {"did": "c", "model": "example.vacuum.test", "name": "扫地机"},
        ]}})
        devices = self.client.devices()
        self.assertEqual(set(devices), {"a", "b", "c"})
        self.assertEqual(devices["b"]["model"], "example.scales.future")
        self.assertEqual(set(devices["a"]), {"did", "model", "name"})
        self.client._call.assert_called_once_with("/home/device_list", {"getVirtualModel": False, "getHuamiDevices": 1})

    def test_reports_keep_each_measurement_in_time_order(self):
        rows = [record(timestamp=1793398833000 + i * 60000, weight=str(70 + i)) for i in range(3)]
        self.client.page = Mock(return_value=[rows[2], rows[0], rows[1], rows[1]])
        report = self.client.reports("test-scale")["1"]
        self.assertEqual([m.values["weight"] for m in report.measurements], [70, 71, 72])
        self.assertEqual(report.measurement, report.measurements[-1])

    def test_reports_collect_all_people_across_pages(self):
        self.client.page = Mock(side_effect=[
            [record(pid=1) for _ in range(20)],
            [record(pid=2, timestamp=1793398800000, weight="60"),
             record(pid=1, timestamp=1793398800000, weight="90"),
             record(did="another-scale", pid=3)],
        ])
        reports = self.client.reports("test-scale")
        self.assertEqual(set(reports), {"1", "2"})
        self.assertEqual(reports["1"].measurement.values["weight"], 96.95)
        self.assertEqual(reports["2"].measurement.values["weight"], 60)

    def test_old_and_new_format_share_cloud_profile(self):
        new = record(user={"id": 123, "accountId": 456, "name": "甲"})
        old = record(timestamp=1793398800000, user={"uid": "login-account", "accountId": 456, "name": "甲"})
        body = json.loads(old["data"])
        body.pop("bodyResData")
        body["bfp"] = "22.5"
        old["data"] = json.dumps(body)
        self.assertEqual(parse_measurement(old).values["bfp"], 22.5)
        self.client.page = Mock(return_value=[new, old])
        reports = self.client.reports("test-scale")
        self.assertEqual(list(reports), ["account:456"])
        self.assertEqual(reports["account:456"].measurement.values["bfp"], 29.1)

    def test_ambiguous_cloud_profile_is_not_silently_merged(self):
        self.client.page = Mock(return_value=[
            record(user={"id": 123, "accountId": 456}),
            record(user={"id": 124, "accountId": 456}),
        ])
        with self.assertRaises(ProtocolError):
            self.client.reports("test-scale")

    def test_incremental_reports_stop_before_old_history(self):
        self.client.page = Mock(return_value=[record(timestamp=1793398800000) for _ in range(20)])
        self.client.reports("test-scale", since=1793398833000)
        self.client.page.assert_called_once()

    def test_full_scan_limit_is_an_error_not_partial_success(self):
        self.client.page = Mock(side_effect=[
            [record(timestamp=1793398833000-i*1000) for _ in range(20)]
            for i in range(50)
        ])
        with self.assertRaises(ProtocolError):
            self.client.reports("test-scale")
        self.assertEqual(self.client.page.call_count, 50)

    def test_paginates_when_other_user_has_newer_records(self):
        self.client.page = Mock(side_effect=[[record(pid=2) for _ in range(20)], [record(timestamp=1793398800000)]])
        self.assertEqual(self.client.latest("test-scale", "1").values["weight"], 96.95)
        self.assertEqual(self.client.page.call_args_list[1].args, (1793398832999,))

    def test_no_wrong_user_fallback(self):
        self.client.page = Mock(return_value=[record(pid=2)])
        with self.assertRaises(NoMeasurements):
            self.client.latest("test-scale", "1")

    def test_profiles_use_user_id_not_type(self):
        self.client.page = Mock(return_value=[record(pid=1), record(pid=2), record(pid=1)])
        self.assertEqual(len(self.client.profiles()), 2)

    def test_auth_renewal_is_bounded(self):
        with patch("custom_components.xiaomi_scale_cloud.api.ServiceSession", side_effect=AuthenticationError) as session:
            with self.assertRaises(AuthenticationError):
                self.client.page()
            self.assertEqual(session.call_count, 2)

    def test_qr_expiry(self):
        qr = QrLogin()
        try:
            with self.assertRaises(LoginExpired):
                qr.poll()
        finally:
            qr.close()


class WireTests(unittest.TestCase):
    def test_model_header_and_encrypted_request(self):
        service = ServiceSession.__new__(ServiceSession)
        service.model = "xiaomi.scales.ms110"
        service.region = "cn"
        service.ssecurity = bytes(range(16))
        service.cookies = {"serviceToken": "test-token"}
        service.http = Mock()
        nonce = bytes(range(8)) + (1234).to_bytes(4, "big")
        signed = hashlib.sha256(service.ssecurity + nonce).digest()
        response = Mock(status_code=200, text=base64.b64encode(rc4(signed, b'{"code":0,"result":[]}')).decode())
        service.http.post.return_value = response
        with patch("custom_components.xiaomi_scale_cloud._vendor.auth.os.urandom", return_value=bytes(range(8))), patch("custom_components.xiaomi_scale_cloud._vendor.auth.time.time", return_value=1234*60):
            self.assertEqual(service.call(HISTORY_PATH, {"model": service.model})["result"], [])
        call = service.http.post.call_args
        self.assertEqual(call.kwargs["headers"]["MIOT-REQUEST-MODEL"], "xiaomi.scales.ms110")
        data = json.loads(rc4(signed, base64.b64decode(call.kwargs["data"]["data"])))
        self.assertEqual(data["model"], service.model)
        self.assertNotIn("test-token", str(call.kwargs["data"]))


if __name__ == "__main__":
    unittest.main()
