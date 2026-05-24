"""Support for ADS sensors."""

import voluptuous as vol

from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    DEVICE_CLASSES_SCHEMA as SENSOR_DEVICE_CLASSES_SCHEMA,
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    STATE_CLASSES_SCHEMA as SENSOR_STATE_CLASSES_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_NAME, CONF_UNIT_OF_MEASUREMENT, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType, StateType

from . import ADS_TYPEMAP, CONF_ADS_FACTOR, CONF_ADS_TYPE
from .const import CONF_ADS_VAR, DATA_ADS, DATA_ADS_HUBS, STATE_KEY_STATE, AdsType, DOMAIN
from .entity import AdsEntity
from .hub import AdsHub

DEFAULT_NAME = "ADS sensor"

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ADS_VAR): cv.string,
        vol.Optional(CONF_ADS_FACTOR): cv.positive_int,
        vol.Optional(CONF_ADS_TYPE, default=AdsType.INT): vol.All(
            vol.Coerce(AdsType),
            vol.In(
                [
                    AdsType.BOOL,
                    AdsType.BYTE,
                    AdsType.INT,
                    AdsType.UINT,
                    AdsType.SINT,
                    AdsType.USINT,
                    AdsType.DINT,
                    AdsType.UDINT,
                    AdsType.WORD,
                    AdsType.DWORD,
                    AdsType.LREAL,
                    AdsType.REAL,
                ]
            ),
        ),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_DEVICE_CLASS): SENSOR_DEVICE_CLASSES_SCHEMA,
        vol.Optional(CONF_STATE_CLASS): SENSOR_STATE_CLASSES_SCHEMA,
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
    }
)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up an ADS sensor device."""
    ads_hub = hass.data[DATA_ADS]

    ads_var: str = config[CONF_ADS_VAR]
    ads_type: AdsType = config[CONF_ADS_TYPE]
    name: str = config[CONF_NAME]
    factor: int | None = config.get(CONF_ADS_FACTOR)
    device_class: SensorDeviceClass | None = config.get(CONF_DEVICE_CLASS)
    state_class: SensorStateClass | None = config.get(CONF_STATE_CLASS)
    unit_of_measurement: str | None = config.get(CONF_UNIT_OF_MEASUREMENT)

    if not ads_hub.has_variable(ads_var, ADS_TYPEMAP[ads_type]):
        ads_hub.record_missing_variable(ads_var, name, "sensor")
        return

    entity = AdsSensor(
        ads_hub,
        ads_var,
        ads_type,
        name,
        factor,
        device_class,
        state_class,
        unit_of_measurement,
    )

    add_entities([entity])


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ADS debug sensor for config entry based setups."""
    async_add_entities([AdsDebugMissingVariablesSensor(hass, entry.entry_id)])


class AdsSensor(AdsEntity, SensorEntity):
    """Representation of an ADS sensor entity."""

    def __init__(
        self,
        ads_hub: AdsHub,
        ads_var: str,
        ads_type: AdsType,
        name: str,
        factor: int | None,
        device_class: SensorDeviceClass | None,
        state_class: SensorStateClass | None,
        unit_of_measurement: str | None,
    ) -> None:
        """Initialize AdsSensor entity."""
        super().__init__(ads_hub, name, ads_var)
        self._ads_type = ads_type
        self._factor = factor
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = unit_of_measurement

    async def async_added_to_hass(self) -> None:
        """Register device notification."""
        await self.async_initialize_device(
            self._ads_var,
            ADS_TYPEMAP[self._ads_type],
            STATE_KEY_STATE,
            self._factor,
        )

    @property
    def native_value(self) -> StateType:
        """Return the state of the device."""
        return self._state_dict[STATE_KEY_STATE]


class AdsDebugMissingVariablesSensor(SensorEntity):
    """Diagnostic sensor exposing missing ADS symbols."""

    _attr_has_entity_name = True
    _attr_name = "Missing ADS symbols"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_missing_symbols"

    @property
    def title(self) -> str:
        """Return the title shown for the debug sensor."""
        return "ADS Debug"

    @property
    def native_value(self) -> int:
        """Return the number of skipped ADS symbols."""
        hub = self._resolve_hub()
        if hub is None:
            return 0

        return len(hub.missing_variables)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the missing symbol list as sensor attributes."""
        hub = self._resolve_hub()
        if hub is None:
            return {"missing_variables": []}

        return {
            "missing_variables": hub.missing_variables,
            "missing_count": len(hub.missing_variables),
        }

    def _resolve_hub(self) -> AdsHub | None:
        """Resolve the current ADS hub for this config entry."""
        hubs = self.hass.data.get(DATA_ADS_HUBS, {})
        return hubs.get(self._entry_id)
