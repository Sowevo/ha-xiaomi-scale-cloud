"""小米体重秤云端报告集成。"""

from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr

from .coordinator import ScaleCoordinator
from .entity import scale_device_info

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]


async def async_setup_entry(hass, entry):
    coordinator = ScaleCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await hass.async_add_executor_job(coordinator.client.close)
        raise
    entry.runtime_data = coordinator
    dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, **scale_device_info(entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    return True


async def async_options_updated(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await hass.async_add_executor_job(entry.runtime_data.client.close)
    return unloaded

