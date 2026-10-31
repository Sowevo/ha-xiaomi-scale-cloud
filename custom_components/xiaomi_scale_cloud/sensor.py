"""每个人自动创建一组传感器，并在轮询发现新人员时补充。"""

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from dataclasses import dataclass
from datetime import datetime

from homeassistant.core import callback
from homeassistant.const import STATE_UNKNOWN
from homeassistant.helpers.restore_state import RestoreEntity, ExtraStoredData
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DATA_CUSTOMIZE
from homeassistant.helpers import device_registry as dr

from .api import METRICS
from .const import DOMAIN
from .entity import ScaleEntity, scale_identity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known = set()

    @callback
    def discover_people():
        new = set(coordinator.data) - known
        entities = []
        for pid in sorted(new):
            entities.extend(ScaleSensor(coordinator, key, pid) for key in METRICS)
            entities.append(MeasurementTime(coordinator, pid))
        known.update(new)
        if entities:
            async_add_entities(entities)
        # 云端改名只更新展示，不改变 ID；保留用户在 HA 里设置的自定义名称。
        registry = dr.async_get(hass)
        for pid, report in coordinator.data.items():
            device = registry.async_get_device(identifiers={(DOMAIN, f"{scale_identity(entry)}_{pid}")})
            if device is not None:
                name = report.name
                if device.name != name:
                    registry.async_update_device(device.id, name=name)

    discover_people()
    entry.async_on_unload(coordinator.async_add_listener(discover_people))


@dataclass
class MeasurementCursor(ExtraStoredData):
    """断线时也保存已处理位置，避免重启后漏掉间隔内的称重。"""

    measured_at: str | None

    def as_dict(self):
        return {"measured_at": self.measured_at}


class ReportSensor(ScaleEntity, RestoreEntity, SensorEntity):
    """按称重时间发布每条新增记录，不用轮询时间代替测量时间。"""

    def __init__(self, coordinator, key, profile_id):
        super().__init__(coordinator, key, profile_id)
        self._last_recorded = None
        self._measurement_override = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.coordinator.history_entities.add(self)
        self.async_on_remove(lambda: self.coordinator.history_entities.discard(self))
        previous = await self.async_get_last_state()
        stored = await self.async_get_last_extra_data()
        value = (
            stored.as_dict().get("measured_at") if stored
            else previous.attributes.get("measured_at") if previous else None
        )
        try:
            measured_at = datetime.fromisoformat(value)
            self._last_recorded = measured_at if measured_at.tzinfo else None
        except (TypeError, ValueError):
            self._last_recorded = None

    @property
    def extra_restore_state_data(self):
        return MeasurementCursor(self._last_recorded.isoformat() if self._last_recorded else None)

    @property
    def measurement(self):
        return self._measurement_override or self.coordinator.data[self.profile_id].measurement

    @property
    def extra_state_attributes(self):
        return {"measured_at": self.measurement.measured_at.isoformat()}

    @callback
    def _async_write_ha_state(self):
        if not self.available:
            super()._async_write_ha_state()
            return
        report = self.coordinator.data[self.profile_id]
        readings = report.measurements or (report.measurement,)
        # 初次添加只建立当前值；重启后从恢复的测量时间继续补齐。
        pending = (
            [m for m in readings if m.measured_at > self._last_recorded]
            if self._last_recorded else [report.measurement]
        )
        # 没有新记录时只恢复当前状态或更新名称，不强制制造历史点。
        self._write_measurements(pending or [report.measurement], force_update=bool(pending))

    @callback
    def async_import_measurements(self, measurements):
        """手动补齐历史后，实体仍停留在最新记录。"""
        latest = self.coordinator.data[self.profile_id].measurement
        readings = {m.measured_at: m for m in measurements}
        readings[latest.measured_at] = latest
        self._write_measurements([readings[at] for at in sorted(readings)], force_update=True)

    @callback
    def _write_measurements(self, measurements, *, force_update):
        for measurement in measurements:
            self._measurement_override = measurement
            try:
                value = self.state
                attributes = dict(self.capability_attributes or {})
                attributes.update(self.state_attributes or {})
                attributes.update(self.extra_state_attributes)
                if self.unit_of_measurement is not None:
                    attributes["unit_of_measurement"] = self.unit_of_measurement
                entry = self.registry_entry
                device_class = (entry and entry.device_class) or self.device_class
                if device_class:
                    attributes["device_class"] = str(device_class)
                name = (
                    er.async_get_legacy_friendly_name(self.hass, entry, original_name=self.name)
                    if entry else self.name
                )
                if name:
                    attributes["friendly_name"] = name
                icon = (entry and entry.icon) or self.icon
                if icon:
                    attributes["icon"] = icon
                custom = self.hass.data.get(DATA_CUSTOMIZE)
                if custom is not None:
                    attributes.update(custom.get(self.entity_id) or {})
                self.hass.states.async_set(
                    self.entity_id, STATE_UNKNOWN if value is None else str(value),
                    attributes, force_update=force_update,
                    timestamp=measurement.measured_at.timestamp(),
                )
                self._last_recorded = measurement.measured_at
            finally:
                self._measurement_override = None


class ScaleSensor(ReportSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, key, profile_id):
        super().__init__(coordinator, key, profile_id)
        self.key = key
        name, unit, device_class, precision = METRICS[key]
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = SensorDeviceClass(device_class) if device_class else None
        self._attr_suggested_display_precision = precision

    @property
    def native_value(self):
        return self.measurement.values.get(self.key)

class MeasurementTime(ReportSensor):
    _attr_name = "最近测量时间"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, profile_id):
        super().__init__(coordinator, "measured_at", profile_id)

    @property
    def native_value(self):
        return self.measurement.measured_at
