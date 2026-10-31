"""只读米家称重历史客户端；所有调用由 HA executor 执行。"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import time

import requests

from ._vendor.auth import ServiceSession, HISTORY_PATH, parse_response, request, safe_url
from ._vendor.errors import AuthenticationError, LoginExpired, NetworkError, ProtocolError
from ._vendor.session import credentials


class NoMeasurements(Exception):
    """所选设备或档案没有可读取的记录。"""


# 单位按云端报告定义，不根据阻抗重新估算身体成分。
METRICS = {
    "weight": ("体重", "kg", "weight", 2),
    "bmi": ("BMI", None, None, 1),
    "bfp": ("体脂率", "%", None, 1),
    "bfm": ("脂肪量", "kg", "weight", 1),
    "slm": ("肌肉量", "kg", "weight", 1),
    "slp": ("肌肉率", "%", None, 1),
    "smm": ("骨骼肌量", "kg", "weight", 1),
    "bwp": ("身体水分率", "%", None, 1),
    "bwm": ("身体水分量", "kg", "weight", 1),
    "bmc": ("骨矿物含量", "kg", "weight", 1),
    "bmcp": ("骨矿物含量比例", "%", None, 1),
    "pp": ("蛋白质率", "%", None, 1),
    "pm": ("蛋白质量", "kg", "weight", 1),
    "vfl": ("内脏脂肪等级", None, None, 0),
    "bmr": ("基础代谢", "kcal/d", None, 0),
    "ma": ("身体年龄", "年", None, 0),
    "sbc": ("身体评分", None, None, 0),
    "ffm": ("去脂体重", "kg", "weight", 1),
    "bodyRes": ("阻抗 1", "Ω", None, 1),
    "bodyRes2": ("阻抗 2", "Ω", None, 1),
    "heartRate": ("心率", "bpm", None, 0),
}


def number(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def row_body(row):
    if not isinstance(row, dict):
        raise ProtocolError()
    try:
        body = json.loads(row.get("data") or "{}")
    except (ValueError, TypeError):
        raise ProtocolError() from None
    if not isinstance(body, dict):
        raise ProtocolError()
    return body


def profile_id(body):
    user = body.get("user")
    if not isinstance(user, dict):
        raise ProtocolError()
    # 新旧报告都携带云端档案 accountId；旧格式的 uid 是登录账号，不能当人员 ID。
    account = user.get("accountId")
    if account not in (None, "", 0, "0"):
        return f"account:{account}"
    if user.get("id") not in (None, "", 0, "0"):
        return str(user["id"])
    raise ProtocolError()


@dataclass(frozen=True)
class Measurement:
    measured_at: datetime
    values: dict


@dataclass(frozen=True)
class PersonReport:
    name: str
    measurement: Measurement
    modern_id: str | None = None
    measurements: tuple[Measurement, ...] = ()


def parse_measurement(row):
    body = row_body(row)
    try:
        composition = json.loads(body.get("bodyResData") or "{}") if "bodyResData" in body else body
        if not isinstance(composition, dict):
            raise ValueError()
        timestamp = float(row["createTime"])
        if timestamp < 10_000_000_000:
            timestamp *= 1000
        measured = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise ProtocolError() from None
    values = {key: number(body.get(key) if key in ("weight", "bmi", "bodyRes", "bodyRes2", "heartRate") else composition.get(key)) for key in METRICS}
    if values["weight"] is None or values["weight"] <= 0:
        raise ProtocolError()
    # 关闭心率测量时云端用零占位，不发布为 0 bpm。
    if values["heartRate"] == 0:
        values["heartRate"] = None
    for key in ("bodyRes", "bodyRes2"):
        if values[key] == 0:
            values[key] = None
    return Measurement(measured, values)


class QrLogin:
    """申请官方二维码；用户确认后短轮询，不创建后台常驻线程。"""

    def __init__(self):
        self.http = requests.Session()
        self.deadline = 0.0

    def start(self):
        data = parse_response(request(self.http, "get", "https://account.xiaomi.com/longPolling/loginUrl", params={
            "_qrsize": "220", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "callback": "https://sts.api.io.mi.com/sts", "_hasLogo": "false",
            "sid": "xiaomiio", "serviceParam": "", "_locale": "zh_CN", "_dc": str(int(time.time() * 1000)),
        }))
        try:
            self.login_url = safe_url(data["loginUrl"])
            self.qr_url = safe_url(data["qr"])
            self.poll_url = safe_url(data["lp"])
            seconds = float(data["timeout"])
            if not math.isfinite(seconds) or not 0 < seconds <= 3600:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise ProtocolError() from None
        self.deadline = time.monotonic() + seconds

    def poll(self):
        if time.monotonic() >= self.deadline:
            raise LoginExpired()
        try:
            response = self.http.get(self.poll_url, timeout=min(10, self.deadline - time.monotonic()))
        except requests.Timeout:
            return None
        except requests.RequestException:
            raise NetworkError() from None
        data = parse_response(response)
        if data.get("code") in (70016, 70017):
            raise LoginExpired()
        if not data.get("userId"):
            return None
        return credentials(data)

    def close(self):
        self.http.close()


class ScaleClient:
    def __init__(self, credential, region, model):
        self.credential, self.region, self.model = credential, region, model
        self.service = None

    def close(self):
        if self.service is not None:
            self.service.http.close()
            self.service = None

    def _call(self, path, payload):
        for attempt in range(2):
            try:
                if self.service is None:
                    http = requests.Session()
                    try:
                        self.service = ServiceSession(self.credential, self.region, model=self.model, http=http)
                    except Exception:
                        http.close()
                        raise
                return self.service.call(path, payload)
            except AuthenticationError:
                self.close()
                if attempt:
                    raise
        raise AuthenticationError()

    def devices(self):
        response = self._call("/home/device_list", {"getVirtualModel": False, "getHuamiDevices": 1})
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("list"), list):
            raise ProtocolError()
        found = {}
        for device in result["list"]:
            if not isinstance(device, dict):
                raise ProtocolError()
            model = device.get("model")
            if not isinstance(model, str):
                continue
            if not model:
                continue
            did = device.get("did")
            if did is None or str(did) == "":
                continue
            did = str(did)
            # 不保留设备列表携带的 token、位置等无关信息。
            found[did] = {"did": did, "model": model, "name": str(device.get("name") or model)}
        return found

    def page(self, before=None):
        response = self._call(HISTORY_PATH, {
            "uid": str(self.credential["userId"]), "endTime": 1,
            "beginTime": before or int(time.time() * 1000),
            "model": self.model, "did": 0, "accountId": 0,
        })
        rows = response.get("result")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ProtocolError()
        return rows

    def reports(self, did, since=None, excluded_profiles=()):
        """读取所选设备各人的最新报告；后续轮询只扫描新增记录。"""
        found = {}
        modern_ids = {}
        measurements = {}
        before = int(time.time() * 1000)
        for _ in range(50):
            rows = self.page(before)
            for row in rows:
                if str(row.get("did")) != str(did) or row.get("model") != self.model:
                    continue
                body = row_body(row)
                pid = profile_id(body)
                if pid in excluded_profiles:
                    continue
                modern_id = body["user"].get("id")
                if modern_id is not None:
                    modern_id = str(modern_id)
                    if pid in modern_ids and modern_ids[pid] != modern_id:
                        # 若未知型号的 accountId 被多人共用，拒绝合并，防止串人。
                        raise ProtocolError()
                    modern_ids[pid] = modern_id
                report = PersonReport(str(body["user"].get("name") or f"用户 {pid}"), parse_measurement(row), modern_id)
                measurements.setdefault(pid, {})[report.measurement.measured_at] = report.measurement
                if pid not in found or report.measurement.measured_at > found[pid].measurement.measured_at:
                    found[pid] = report
            if len(rows) < 20:
                break
            try:
                oldest = min(int(row["createTime"]) for row in rows)
            except (KeyError, TypeError, ValueError):
                raise ProtocolError() from None
            if since is not None and oldest < since:
                break
            if oldest >= before or oldest <= 0:
                raise ProtocolError()
            before = oldest - 1
        else:
            # 达到上限时明确失败，不能把不完整人员列表当成全部成功。
            raise ProtocolError()
        if not found and since is None and not excluded_profiles:
            raise NoMeasurements()
        return {
            pid: PersonReport(
                report.name, report.measurement, report.modern_id,
                tuple(measurements[pid][at] for at in sorted(measurements[pid])),
            )
            for pid, report in found.items()
        }

    def profiles(self):
        found = {}
        for row in self.page():
            if row.get("model") != self.model:
                continue
            body = row_body(row)
            pid, did = profile_id(body), str(row.get("did", ""))
            if not did:
                continue
            key = f"{did}|{pid}"
            found[key] = {"did": did, "profile_id": pid, "name": str(body["user"].get("name") or "用户"), "model": self.model}
        if not found:
            raise NoMeasurements()
        return found

    def latest(self, did, pid):
        before = int(time.time() * 1000)
        # 有界翻页查找，绝不使用其他家庭成员的数据代替。
        for _ in range(5):
            rows = self.page(before)
            matches = []
            for row in rows:
                if row.get("did") == did and row.get("model") == self.model and profile_id(row_body(row)) == str(pid):
                    matches.append(parse_measurement(row))
            if matches:
                return max(matches, key=lambda x: x.measured_at)
            if len(rows) < 20:
                break
            try:
                oldest = min(int(row["createTime"]) for row in rows)
            except (KeyError, ValueError, TypeError):
                raise ProtocolError() from None
            if oldest >= before or oldest <= 0:
                raise ProtocolError()
            before = oldest - 1
        raise NoMeasurements()
