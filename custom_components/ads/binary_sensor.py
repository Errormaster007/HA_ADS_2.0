"""Support for ADS binary sensors."""

from __future__ import annotations

import pyads
import voluptuous as vol

from homeassistant.components.binary_sensor import (
    DEVICE_CLASSES_SCHEMA,
    PLATFORM_SCHEMA as BINARY_SENSOR_PLATFORM_SCHEMA,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_ADS_VAR, CONF_LEGACY_ENTITIES, DATA_ADS, DATA_ADS_HUBS, STATE_KEY_STATE
from .entity import AdsEntity
from .hub import AdsHub

DEFAULT_NAME = "ADS binary sensor"
PLATFORM_SCHEMA = BINARY_SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ADS_VAR): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
    }
)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Binary Sensor platform for ADS."""
    ads_hub = hass.data[DATA_ADS]
    entity = _build_binary_sensor_entity(ads_hub, config)
    if entity is not None:
        add_entities([entity])


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up migrated legacy ADS binary sensors from config entry options."""
    ads_hub = hass.data.get(DATA_ADS_HUBS, {}).get(entry.entry_id)
    if ads_hub is None:
        return

    legacy_entities = entry.options.get(CONF_LEGACY_ENTITIES, {})
    platform_entities = legacy_entities.get("binary_sensor", [])
    entities = [
        entity
        for config in platform_entities
        if (entity := _build_binary_sensor_entity(ads_hub, config)) is not None
    ]
    if entities:
        async_add_entities(entities)


def _build_binary_sensor_entity(ads_hub: AdsHub, config: ConfigType) -> AdsBinarySensor | None:
    """Build one ADS binary sensor from YAML style config."""
    ads_var: str = config[CONF_ADS_VAR]
    name: str = config.get(CONF_NAME, DEFAULT_NAME)
    device_class: BinarySensorDeviceClass | None = config.get(CONF_DEVICE_CLASS)

    if not ads_hub.has_variable(ads_var, pyads.PLCTYPE_BOOL):
        ads_hub.record_missing_variable(ads_var, name, "binary_sensor")
        return None

    return AdsBinarySensor(ads_hub, name, ads_var, device_class)


class AdsBinarySensor(AdsEntity, BinarySensorEntity):
    """Representation of ADS binary sensors."""

    def __init__(
        self,
        ads_hub: AdsHub,
        name: str,
        ads_var: str,
        device_class: BinarySensorDeviceClass | None,
    ) -> None:
        """Initialize ADS binary sensor."""
        super().__init__(ads_hub, name, ads_var)
        self._attr_device_class = device_class or BinarySensorDeviceClass.MOVING

    async def async_added_to_hass(self) -> None:
        """Register device notification."""
        await self.async_initialize_device(self._ads_var, pyads.PLCTYPE_BOOL)

    @property
    def is_on(self) -> bool:
        """Return True if the entity is on."""
        return self._state_dict[STATE_KEY_STATE]
