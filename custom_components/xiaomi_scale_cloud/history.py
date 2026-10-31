"""手动将指定人员的云端称重记录补入 HA。"""

from datetime import timedelta
from functools import partial

import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError

from .api import ScaleClient
from .const import DOMAIN

SERVICE = "import_history"


@callback
def async_setup_history_service(hass):
    if hass.services.has_service(DOMAIN, SERVICE):
        return

    async def import_history(call):
        entry = hass.config_entries.async_get_entry(call.data["entry_id"])
        if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
            raise HomeAssistantError("请先加载指定的体重秤集成。")
        coordinator = entry.runtime_data
        matches = [pid for pid, report in (coordinator.data or {}).items()
                   if report.name.strip() == call.data["profile_name"].strip()]
        if len(matches) != 1:
            raise HomeAssistantError("未找到唯一的人员档案，请核对姓名。")
        pid = matches[0]
        entities = [entity for entity in coordinator.history_entities if entity.profile_id == pid]
        if not entities:
            raise HomeAssistantError("此人员没有已启用的指标实体。")

        def fetch():
            client = ScaleClient(entry.data["credential"], entry.data["region"], entry.data["model"])
            try:
                return client.reports(entry.data["did"])
            finally:
                client.close()

        reports = await hass.async_add_executor_job(fetch)
        if pid not in reports:
            raise HomeAssistantError("云端未返回此人员的称重记录。")
        report = reports[pid]
        current = coordinator.data[pid]
        if current.modern_id and report.modern_id and current.modern_id != report.modern_id:
            raise HomeAssistantError("人员身份发生变化，已停止导入。")
        readings = report.measurements or (report.measurement,)
        existing = await get_instance(hass).async_add_executor_job(partial(
            get_significant_states, hass,
            readings[0].measured_at - timedelta(milliseconds=1),
            readings[-1].measured_at + timedelta(milliseconds=1),
            [entity.entity_id for entity in entities],
            include_start_time_state=False, significant_changes_only=False, no_attributes=True,
        ))
        if report.measurement.measured_at > current.measurement.measured_at:
            coordinator.async_set_updated_data({**coordinator.data, pid: report})
        imported = 0
        for entity in entities:
            seen = {round(state.last_updated.timestamp() * 1000)
                    for state in existing.get(entity.entity_id, [])}
            missing = [m for m in readings if round(m.measured_at.timestamp() * 1000) not in seen]
            if missing:
                entity.async_import_measurements(missing)
                imported += 1
        return {
            "profile_name": report.name,
            "measurements": len(readings),
            "entities": len(entities),
            "updated_entities": imported,
            "first_measurement": readings[0].measured_at.isoformat(),
            "last_measurement": readings[-1].measured_at.isoformat(),
        }

    hass.services.async_register(
        DOMAIN, SERVICE, import_history,
        schema=vol.Schema({vol.Required("entry_id"): str, vol.Required("profile_name"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )
