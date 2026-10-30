"""每个人自动创建一组传感器，并在轮询发现新人员时补充。"""

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.core import callback
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
                name = f"{report.name} · {entry.data.get('device_name') or entry.data['model']}"
                if device.name != name:
                    registry.async_update_device(device.id, name=name)

    discover_people()
    entry.async_on_unload(coordinator.async_add_listener(discover_people))


class ScaleSensor(ScaleEntity, SensorEntity):
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
        return self.coordinator.data[self.profile_id].measurement.values.get(self.key)

    @property
    def extra_state_attributes(self):
        return {"measured_at": self.coordinator.data[self.profile_id].measurement.measured_at.isoformat()}


class MeasurementTime(ScaleEntity, SensorEntity):
    _attr_name = "最近测量时间"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, profile_id):
        super().__init__(coordinator, "measured_at", profile_id)

    @property
    def native_value(self):
        return self.coordinator.data[self.profile_id].measurement.measured_at
