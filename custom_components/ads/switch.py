"""Support for ADS switch platform."""

from __future__ import annotations

from typing import Any

import pyads
import voluptuous as vol

from homeassistant.components.switch import (
    PLATFORM_SCHEMA as SWITCH_PLATFORM_SCHEMA,
    SwitchEntity,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_ADS_VAR, CONF_LEGACY_ENTITIES, DATA_ADS, DATA_ADS_HUBS, STATE_KEY_STATE
from .entity import AdsEntity

DEFAULT_NAME = "ADS Switch"

PLATFORM_SCHEMA = SWITCH_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ADS_VAR): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up switch platform for ADS."""
    ads_hub = hass.data[DATA_ADS]
    entity = _build_switch_entity(ads_hub, config)
    if entity is not None:
        add_entities([entity])


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up migrated legacy ADS switches from config entry options."""
    ads_hub = hass.data.get(DATA_ADS_HUBS, {}).get(entry.entry_id)
    if ads_hub is None:
        return

    legacy_entities = entry.options.get(CONF_LEGACY_ENTITIES, {})
    platform_entities = legacy_entities.get("switch", [])
    entities = [
        entity
        for config in platform_entities
        if (entity := _build_switch_entity(ads_hub, config)) is not None
    ]
    if entities:
        async_add_entities(entities)


def _build_switch_entity(ads_hub, config: ConfigType) -> AdsSwitch | None:
    """Build one ADS switch from YAML style config."""
    name: str = config.get(CONF_NAME, DEFAULT_NAME)
    ads_var: str = config[CONF_ADS_VAR]

    if not ads_hub.has_variable(ads_var, pyads.PLCTYPE_BOOL):
        ads_hub.record_missing_variable(ads_var, name, "switch")
        return None

    return AdsSwitch(ads_hub, name, ads_var)


class AdsSwitch(AdsEntity, SwitchEntity):
    """Representation of an ADS switch device."""

    async def async_added_to_hass(self) -> None:
        """Register device notification."""
        await self.async_initialize_device(self._ads_var, pyads.PLCTYPE_BOOL)

    @property
    def is_on(self) -> bool:
        """Return True if the entity is on."""
        return self._state_dict[STATE_KEY_STATE]

    def turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._ads_hub.write_by_name(self._ads_var, True, pyads.PLCTYPE_BOOL)

    def turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        self._ads_hub.write_by_name(self._ads_var, False, pyads.PLCTYPE_BOOL)
