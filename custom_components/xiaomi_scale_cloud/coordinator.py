"""统一轮询，失败时标记不可用，不伪造新测量。"""

from datetime import timedelta
import logging

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ScaleClient, NoMeasurements
from ._vendor.errors import AuthenticationError, NetworkError, ProtocolError
from .const import DEFAULT_INTERVAL, DOMAIN

LOGGER = logging.getLogger(__name__)


class ScaleCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        self.entry = entry
        self.applied_options = dict(entry.options)
        self.history_entities = set()
        self.client = ScaleClient(entry.data["credential"], entry.data["region"], entry.data["model"])
        interval = entry.options.get("interval", DEFAULT_INTERVAL)
        super().__init__(hass, LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=timedelta(minutes=interval) if interval else None,
                         always_update=False)

    async def _async_update_data(self):
        try:
            previous = self.data or {}
            since = max((int(p.measurement.measured_at.timestamp() * 1000) for p in previous.values()), default=None)
            excluded = set(self.entry.options.get("excluded_profiles", []))
            args = (self.entry.data["did"], since, excluded) if excluded else (self.entry.data["did"], since)
            incoming = await self.hass.async_add_executor_job(self.client.reports, *args)
            profiles = dict(self.entry.data.get("profiles", {}))
            profiles.update({pid: report.name for pid, report in incoming.items()})
            if profiles != self.entry.data.get("profiles", {}):
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, "profiles": profiles},
                )
            merged = {pid: report for pid, report in previous.items() if pid not in excluded}
            for pid, report in incoming.items():
                if pid in merged and merged[pid].modern_id and report.modern_id and merged[pid].modern_id != report.modern_id:
                    raise ProtocolError()
                if pid not in merged or report.measurement.measured_at >= merged[pid].measurement.measured_at:
                    merged[pid] = report
            return merged
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed("小米登录已失效，请重新扫码") from err
        except (NetworkError, ProtocolError, NoMeasurements) as err:
            raise UpdateFailed("无法获取所选设备的称重报告，请稍后重试") from err

