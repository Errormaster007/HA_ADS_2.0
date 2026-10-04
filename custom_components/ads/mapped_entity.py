"""Native HA entities with explicit, independently typed ADS roles."""

from collections.abc import Callable
import logging
from typing import Any

import pyads

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.valve import ValveEntity, ValveEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ADS_TYPEMAP
from .const import DATA_ADS_HUBS, DOMAIN, AdsType
from .hub import AdsHub
from .mapping import (
    CONF_MAPPING,
    WRITE_ROLES,
    MappedDevice,
    MappedEntity,
    device_identifier,
    empty_mapping,
    entity_unique_id,
)

_LOGGER = logging.getLogger(__name__)


class AdsMappedEntity(Entity):
    """A logical entity whose state always comes from PLC feedback."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        hub: AdsHub,
        entry_id: str,
        device: MappedDevice,
        config: MappedEntity,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize stable identity and role bindings."""
        self._hub = hub
        self._config = config
        self._values: dict[str, Any] = {}
        self._active = False
        self._attr_available = False
        self._attr_name = config["name"]
        self._attr_unique_id = entity_unique_id(entry_id, config["id"])
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Subscribe to feedback and read an initial snapshot in the executor."""
        self._active = True
        await self.hass.async_add_executor_job(self._subscribe)
        self.async_write_ha_state()

    def _subscribe(self) -> None:
        for role, variable in self._config["roles"].items():
            if role in WRITE_ROLES:
                continue
            plc_type = ADS_TYPEMAP[AdsType(variable["type"])]
            if not self._hub.add_device_notification(
                variable["name"], plc_type, self._notification
            ):
                _LOGGER.error(
                    "Cannot subscribe to mapped ADS role %s (%s)",
                    role,
                    variable["name"],
                )
                continue
            try:
                value = self._hub.read_mapped_variable(variable["name"], plc_type)
            except pyads.ADSError as err:
                _LOGGER.error(
                    "Cannot read mapped ADS symbol %s: %s", variable["name"], err
                )
            else:
                self.hass.add_job(self._apply_value, variable["name"], value)

    def _notification(self, name: str, value: Any) -> None:
        self.hass.add_job(self._apply_value, name, value)

    @callback
    def _apply_value(self, name: str, value: Any) -> None:
        if not self._active:
            return
        for role, variable in self._config["roles"].items():
            if role not in WRITE_ROLES and variable["name"] == name:
                self._values[role] = value
        self._attr_available = all(
            role in self._values
            for role in self._config["roles"]
            if role not in WRITE_ROLES
        )
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Remove subscriptions before the connection is closed."""
        self._active = False
        try:
            await self.hass.async_add_executor_job(
                self._hub.remove_mapped_notifications, self._notification
            )
        except pyads.ADSError as err:
            _LOGGER.error(
                "Cannot unsubscribe mapped ADS entity %s: %s", self.unique_id, err
            )

    async def _write(self, role: str, value: bool | int) -> None:
        variable = self._config["roles"][role]
        await self.hass.async_add_executor_job(
            self._hub.write_mapped_variable,
            variable["name"],
            value,
            ADS_TYPEMAP[AdsType(variable["type"])],
        )


class AdsMappedSensor(AdsMappedEntity, SensorEntity):
    """A numeric, boolean or text sensor."""

    def __init__(self, *args: Any) -> None:
        """Initialize the optional sensor metadata."""
        super().__init__(*args)
        self._attr_device_class = self._config["device_class"] or None
        self._attr_native_unit_of_measurement = self._config["unit"] or None

    @property
    def native_value(self) -> str | int | float | bool | None:
        """Return the mapped state value."""
        return self._values.get("state")


class AdsMappedBinarySensor(AdsMappedEntity, BinarySensorEntity):
    """A BOOL sensor with an optional HA device class."""

    def __init__(self, *args: Any) -> None:
        """Initialize device class metadata."""
        super().__init__(*args)
        self._attr_device_class = self._config["device_class"] or None

    @property
    def is_on(self) -> bool | None:
        """Return the mapped BOOL state."""
        return self._values.get("state")


class AdsMappedSwitch(AdsMappedEntity, SwitchEntity):
    """A switch with independent feedback and command symbols."""

    @property
    def is_on(self) -> bool | None:
        """Return the mapped BOOL state."""
        return self._values.get("state")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Write the ON command to the PLC."""
        await self._write("command", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Write the OFF command to the PLC."""
        await self._write("command", False)


class AdsMappedLight(AdsMappedEntity, LightEntity):
    """An on/off or dimmable light with explicit feedback."""

    def __init__(self, *args: Any) -> None:
        """Initialize the available light color modes."""
        super().__init__(*args)
        mode = (
            ColorMode.BRIGHTNESS
            if "brightness_command" in self._config["roles"]
            else ColorMode.ONOFF
        )
        self._attr_supported_color_modes = {mode}
        self._attr_color_mode = mode

    @property
    def is_on(self) -> bool | None:
        """Return the mapped BOOL state."""
        return self._values.get("state")

    @property
    def brightness(self) -> int | None:
        """Return the clamped PLC brightness feedback."""
        value = self._values.get("brightness")
        return None if value is None else max(0, min(255, value))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Write the optional brightness and ON commands."""
        if "brightness" in kwargs and "brightness_command" in self._config["roles"]:
            await self._write("brightness_command", kwargs["brightness"])
        await self._write("command", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Write the OFF command to the PLC."""
        await self._write("command", False)


class AdsMappedCover(AdsMappedEntity, CoverEntity):
    """A cover with BOOL closed feedback and separate command flags."""

    def __init__(self, *args: Any) -> None:
        """Initialize the cover class and supported commands."""
        super().__init__(*args)
        self._attr_device_class = self._config["device_class"] or None
        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        if "stop" in self._config["roles"]:
            features |= CoverEntityFeature.STOP
        if "position_command" in self._config["roles"]:
            features |= CoverEntityFeature.SET_POSITION
        self._attr_supported_features = features

    @property
    def is_closed(self) -> bool | None:
        """Return the mapped closed state."""
        return self._values.get("state")

    @property
    def current_cover_position(self) -> int | None:
        """Return the clamped PLC position feedback."""
        value = self._values.get("position")
        return None if value is None else max(0, min(100, value))

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Write the open command to the PLC."""
        await self._write("open", True)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Write the close command to the PLC."""
        await self._write("close", True)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Write the stop command to the PLC."""
        await self._write("stop", True)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Write the requested position to the PLC."""
        await self._write("position_command", kwargs["position"])


class AdsMappedValve(AdsMappedEntity, ValveEntity):
    """A valve whose BOOL feedback is true when open."""

    def __init__(self, *args: Any) -> None:
        """Initialize the valve position and supported commands."""
        super().__init__(*args)
        self._attr_device_class = self._config["device_class"] or None
        self._attr_reports_position = "position" in self._config["roles"]
        features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
        if "position_command" in self._config["roles"]:
            features |= ValveEntityFeature.SET_POSITION
        self._attr_supported_features = features

    @property
    def is_closed(self) -> bool | None:
        """Return the inverse of the mapped open state."""
        value = self._values.get("state")
        return None if value is None else not value

    @property
    def current_valve_position(self) -> int | None:
        """Return the clamped PLC position feedback."""
        value = self._values.get("position")
        return None if value is None else max(0, min(100, value))

    async def async_open_valve(self, **kwargs: Any) -> None:
        """Write the open command to the PLC."""
        await self._write("command", True)

    async def async_close_valve(self, **kwargs: Any) -> None:
        """Write the close command to the PLC."""
        await self._write("command", False)

    async def async_set_valve_position(self, **kwargs: Any) -> None:
        """Write the requested position to the PLC."""
        await self._write("position_command", kwargs["position"])


_ENTITY_CLASSES: dict[str, Callable[..., AdsMappedEntity]] = {
    "sensor": AdsMappedSensor,
    "binary_sensor": AdsMappedBinarySensor,
    "switch": AdsMappedSwitch,
    "light": AdsMappedLight,
    "cover": AdsMappedCover,
    "valve": AdsMappedValve,
}


def async_add_mapped_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    platform: str,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add mapped entities alongside the existing YAML-compatible entities."""
    documents = [
        (entry.options.get(CONF_MAPPING, empty_mapping()), None),
        *(
            (subentry.data[CONF_MAPPING], subentry.subentry_id)
            for subentry in entry.subentries.values()
            if CONF_MAPPING in subentry.data
        ),
    ]
    hub = hass.data[DATA_ADS_HUBS][entry.entry_id]
    device_registry = dr.async_get(hass)
    for document, subentry_id in documents:
        devices = {device["id"]: device for device in document["devices"]}
        entities = []
        for config in document["entities"]:
            if config["platform"] != platform:
                continue
            device = devices[config["device_id"]]
            if ha_device_id := device.get("ha_device_id"):
                if (ha_device := device_registry.async_get(ha_device_id)) is None:
                    _LOGGER.error(
                        "Mapped ADS device %s references missing Home Assistant device %s",
                        device["name"],
                        ha_device_id,
                    )
                    continue
                if not ha_device.identifiers and not ha_device.connections:
                    _LOGGER.error(
                        "Mapped ADS device %s references a Home Assistant device without identifiers",
                        device["name"],
                    )
                    continue
                device_info = DeviceInfo(
                    identifiers=ha_device.identifiers,
                    connections=ha_device.connections,
                )
            else:
                device_info = DeviceInfo(
                    identifiers={
                        (DOMAIN, device_identifier(entry.entry_id, device["id"]))
                    },
                    name=device["name"],
                    manufacturer="ADS",
                    model="Mapped PLC device",
                    configuration_url="/ads-mapping",
                )
            entities.append(
                _ENTITY_CLASSES[platform](
                    hub, entry.entry_id, device, config, device_info
                )
            )
        if entities:
            async_add_entities(entities, config_subentry_id=subentry_id)
