"""Support for ADS valves."""

from __future__ import annotations

import pyads
import voluptuous as vol

from homeassistant.components.valve import (
    DEVICE_CLASSES_SCHEMA as VALVE_DEVICE_CLASSES_SCHEMA,
    PLATFORM_SCHEMA as VALVE_PLATFORM_SCHEMA,
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_ADS_VAR, CONF_LEGACY_ENTITIES, DATA_ADS, DATA_ADS_HUBS
from .entity import AdsEntity
from .hub import AdsHub

DEFAULT_NAME = "ADS valve"

PLATFORM_SCHEMA = VALVE_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ADS_VAR): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_DEVICE_CLASS): VALVE_DEVICE_CLASSES_SCHEMA,
    }
)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up an ADS valve device."""
    ads_hub = hass.data[DATA_ADS]
    entity = _build_valve_entity(ads_hub, config)
    if entity is not None:
        add_entities([entity])


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up migrated legacy ADS valves from config entry options."""
    ads_hub = hass.data.get(DATA_ADS_HUBS, {}).get(entry.entry_id)
    if ads_hub is None:
        return

    legacy_entities = entry.options.get(CONF_LEGACY_ENTITIES, {})
    platform_entities = legacy_entities.get("valve", [])
    entities = [
        entity
        for config in platform_entities
        if (entity := _build_valve_entity(ads_hub, config)) is not None
    ]
    if entities:
        async_add_entities(entities)


def _build_valve_entity(ads_hub: AdsHub, config: ConfigType) -> AdsValve | None:
    """Build one ADS valve from YAML style config."""
    ads_var: str = config[CONF_ADS_VAR]
    name: str = config.get(CONF_NAME, DEFAULT_NAME)
    device_class: ValveDeviceClass | None = config.get(CONF_DEVICE_CLASS)

    if not ads_hub.has_variable(ads_var, pyads.PLCTYPE_BOOL):
        ads_hub.record_missing_variable(ads_var, name, "valve")
        return None

    return AdsValve(ads_hub, ads_var, name, device_class)


class AdsValve(AdsEntity, ValveEntity):
    """Representation of an ADS valve entity."""

    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

    def __init__(
        self,
        ads_hub: AdsHub,
        ads_var: str,
        name: str,
        device_class: ValveDeviceClass | None,
    ) -> None:
        """Initialize AdsValve entity."""
        super().__init__(ads_hub, name, ads_var)
        self._attr_device_class = device_class
        self._attr_reports_position = False
        self._attr_is_closed = True

    async def async_added_to_hass(self) -> None:
        """Register device notification."""
        await self.async_initialize_device(self._ads_var, pyads.PLCTYPE_BOOL)

    def open_valve(self, **kwargs) -> None:
        """Open the valve."""
        self._ads_hub.write_by_name(self._ads_var, True, pyads.PLCTYPE_BOOL)
        self._attr_is_closed = False

    def close_valve(self, **kwargs) -> None:
        """Close the valve."""
        self._ads_hub.write_by_name(self._ads_var, False, pyads.PLCTYPE_BOOL)
        self._attr_is_closed = True
