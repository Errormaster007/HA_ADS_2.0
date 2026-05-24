"""Support for ADS select entities."""

from __future__ import annotations

import pyads
import voluptuous as vol

from homeassistant.components.select import (
    PLATFORM_SCHEMA as SELECT_PLATFORM_SCHEMA,
    SelectEntity,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_ADS_VAR, CONF_LEGACY_ENTITIES, DATA_ADS, DATA_ADS_HUBS
from .entity import AdsEntity
from .hub import AdsHub

DEFAULT_NAME = "ADS select"

# pylint: disable-next=home-assistant-duplicate-const
CONF_OPTIONS = "options"

PLATFORM_SCHEMA = SELECT_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ADS_VAR): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Required(CONF_OPTIONS): vol.All(cv.ensure_list, [cv.string]),
    }
)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up an ADS select device."""
    ads_hub = hass.data[DATA_ADS]
    entity = _build_select_entity(ads_hub, config)
    if entity is not None:
        add_entities([entity])


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up migrated legacy ADS selects from config entry options."""
    ads_hub = hass.data.get(DATA_ADS_HUBS, {}).get(entry.entry_id)
    if ads_hub is None:
        return

    legacy_entities = entry.options.get(CONF_LEGACY_ENTITIES, {})
    platform_entities = legacy_entities.get("select", [])
    entities = [
        entity
        for config in platform_entities
        if (entity := _build_select_entity(ads_hub, config)) is not None
    ]
    if entities:
        async_add_entities(entities)


def _build_select_entity(ads_hub: AdsHub, config: ConfigType) -> AdsSelect | None:
    """Build one ADS select from YAML style config."""
    ads_var: str = config[CONF_ADS_VAR]
    name: str = config.get(CONF_NAME, DEFAULT_NAME)
    options: list[str] = config.get(CONF_OPTIONS, [])

    if not options:
        return None

    if not ads_hub.has_variable(ads_var, pyads.PLCTYPE_INT):
        ads_hub.record_missing_variable(ads_var, name, "select")
        return None

    return AdsSelect(ads_hub, ads_var, name, options)


class AdsSelect(AdsEntity, SelectEntity):
    """Representation of an ADS select entity."""

    def __init__(
        self,
        ads_hub: AdsHub,
        ads_var: str,
        name: str,
        options: list[str],
    ) -> None:
        """Initialize the AdsSelect entity."""
        super().__init__(ads_hub, name, ads_var)
        self._attr_options = options
        self._attr_current_option = None

    async def async_added_to_hass(self) -> None:
        """Register device notification."""
        await self.async_initialize_device(self._ads_var, pyads.PLCTYPE_INT)
        self._ads_hub.add_device_notification(
            self._ads_var, pyads.PLCTYPE_INT, self._handle_ads_value
        )

    def select_option(self, option: str) -> None:
        """Change the selected option."""
        if option in self._attr_options:
            index = self._attr_options.index(option)
            self._ads_hub.write_by_name(self._ads_var, index, pyads.PLCTYPE_INT)
            self._attr_current_option = option

    def _handle_ads_value(self, name: str, value: int) -> None:
        """Handle the value update from ADS."""
        if 0 <= value < len(self._attr_options):
            self._attr_current_option = self._attr_options[value]
            self.schedule_update_ha_state()
