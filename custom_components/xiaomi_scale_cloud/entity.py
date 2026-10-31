"""设备与人员的稳定标识。"""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODELS


def scale_identity(entry):
    return f"{entry.data['credential']['userId']}_{entry.data['region']}_{entry.data['did']}"


def scale_device_info(entry):
    return DeviceInfo(
        identifiers={(DOMAIN, scale_identity(entry))},
        name=entry.data.get("device_name") or MODELS.get(entry.data["model"], entry.data["model"]),
        manufacturer="Xiaomi", model=MODELS.get(entry.data["model"], entry.data["model"]),
    )


class ScaleEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, key, profile_id=None):
        super().__init__(coordinator)
        self.profile_id = profile_id
        identity = scale_identity(coordinator.entry)
        if profile_id is not None:
            identity = f"{identity}_{profile_id}"
        self._attr_unique_id = f"{identity}_{key}"

    @property
    def device_info(self):
        entry = self.coordinator.entry
        parent = scale_identity(entry)
        if self.profile_id is None:
            return scale_device_info(entry)
        report = self.coordinator.data[self.profile_id]
        return DeviceInfo(
            identifiers={(DOMAIN, f"{parent}_{self.profile_id}")},
            name=report.name,
            manufacturer=None, model="称重档案",
            via_device=(DOMAIN, parent),
        )
