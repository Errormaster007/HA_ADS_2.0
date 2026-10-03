"""Support for Automation Device Specification (ADS)."""

import asyncio
from asyncio import timeout
from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.entity import Entity

from .const import CONF_ADS_VAR, CONF_LEGACY_ENTITIES, DOMAIN, STATE_KEY_STATE
from .hub import AdsHub

_LOGGER = logging.getLogger(__name__)


def is_legacy_entity_migrated(
    hass: HomeAssistant, platform: str, ads_var: str
) -> bool:
    """Return whether a YAML ADS entity has been claimed by a config entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        for source in (entry.data, entry.options):
            legacy_entities = source.get(CONF_LEGACY_ENTITIES)
            if not isinstance(legacy_entities, Mapping):
                continue
            platform_entities = legacy_entities.get(platform, [])
            if any(
                isinstance(config, Mapping) and config.get(CONF_ADS_VAR) == ads_var
                for config in platform_entities
            ):
                return True
    return False


async def async_remove_legacy_yaml_entities(
    hass: HomeAssistant, legacy_entities: Mapping[str, list[dict[str, Any]]]
) -> None:
    """Remove matching active YAML entities so config-entry entities can adopt them."""
    entity_registry = er.async_get(hass)
    entity_components = hass.data.get(DATA_INSTANCES, {})

    for platform, configs in legacy_entities.items():
        component = entity_components.get(platform)
        if component is None:
            continue

        for config in configs:
            ads_var = config.get(CONF_ADS_VAR)
            if not ads_var:
                continue

            entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, ads_var)
            if entity_id is None:
                continue

            registry_entry = entity_registry.async_get(entity_id)
            if registry_entry is None or registry_entry.config_entry_id is not None:
                continue

            await component.async_remove_entity(entity_id)


class AdsEntity(Entity):
    """Representation of ADS entity."""

    _attr_should_poll = False

    def __init__(self, ads_hub: AdsHub, name: str, ads_var: str) -> None:
        """Initialize ADS binary sensor."""
        self._state_dict: dict[str, Any] = {}
        self._state_dict[STATE_KEY_STATE] = None
        self._ads_hub = ads_hub
        self._ads_var = ads_var
        self._event: asyncio.Event | None = None
        self._attr_unique_id = ads_var
        self._attr_name = name

    async def async_initialize_device(
        self,
        ads_var: str,
        plctype: type,
        state_key: str = STATE_KEY_STATE,
        factor: int | None = None,
    ) -> bool:
        """Register device notification."""

        def update(name, value):
            """Handle device notifications."""
            _LOGGER.debug("Variable %s changed its value to %d", name, value)

            if factor is None:
                self._state_dict[state_key] = value
            else:
                self._state_dict[state_key] = value / factor

            asyncio.run_coroutine_threadsafe(async_event_set(), self.hass.loop)
            self.schedule_update_ha_state()

        async def async_event_set():
            """Set event in async context."""
            self._event.set()

        self._event = asyncio.Event()

        registered = await self.hass.async_add_executor_job(
            self._ads_hub.add_device_notification, ads_var, plctype, update
        )
        if not registered:
            return False

        try:
            async with timeout(10):
                await self._event.wait()
        except TimeoutError:
            _LOGGER.debug("Variable %s: Timeout during first update", ads_var)

        return True

    @property
    def available(self) -> bool:
        """Return False if state has not been updated yet."""
        return self._state_dict[STATE_KEY_STATE] is not None
