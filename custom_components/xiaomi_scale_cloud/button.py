"""手动刷新最新报告。"""

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import ScaleEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RefreshButton(entry.runtime_data)])


class RefreshButton(ScaleEntity, ButtonEntity):
    _attr_name = "刷新报告"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator):
        super().__init__(coordinator, "refresh")

    @property
    def available(self):
        # 网络恢复后即使上次更新失败，仍可手动重试。
        return True

    async def async_press(self):
        await self.coordinator.async_request_refresh()

