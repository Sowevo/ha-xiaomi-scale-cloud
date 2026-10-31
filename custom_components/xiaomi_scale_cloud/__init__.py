"""小米体重秤云端报告集成。"""

from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr

from .coordinator import ScaleCoordinator
from .entity import scale_device_info, scale_identity
from .const import DOMAIN
from .history import async_setup_history_service

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]


async def async_setup_entry(hass, entry):
    coordinator = ScaleCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await hass.async_add_executor_job(coordinator.client.close)
        raise
    entry.runtime_data = coordinator
    async_setup_history_service(hass)
    dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, **scale_device_info(entry))
    sync_profile_devices(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    return True


def sync_profile_devices(hass, entry):
    """只切换集成管理的禁用状态，不移除人员与历史。"""
    registry = dr.async_get(hass)
    excluded = set(entry.options.get("excluded_profiles", []))
    devices = {identifier: device
               for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
               for identifier in device.identifiers}
    for pid in entry.data.get("profiles", {}):
        device = devices.get((DOMAIN, f"{scale_identity(entry)}_{pid}"))
        if device is None:
            continue
        if pid in excluded and device.disabled_by is None:
            registry.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.INTEGRATION)
        elif pid not in excluded and device.disabled_by == dr.DeviceEntryDisabler.INTEGRATION:
            registry.async_update_device(device.id, disabled_by=None)


async def async_options_updated(hass, entry):
    if dict(entry.options) != entry.runtime_data.applied_options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await hass.async_add_executor_job(entry.runtime_data.client.close)
    return unloaded

