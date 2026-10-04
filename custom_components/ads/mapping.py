"""Validated, connection-scoped device and variable mappings."""

import math
from typing import Any, NotRequired, TypedDict
from uuid import uuid4

import voluptuous as vol

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.cover import CoverDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.valve import ValveDeviceClass

CONF_MAPPING = "mapping"
CONF_MAPPING_REVISION = "mapping_revision"

NUMERIC_TYPES = (
    "byte",
    "int",
    "uint",
    "sint",
    "usint",
    "dint",
    "udint",
    "word",
    "dword",
    "real",
    "lreal",
)
INTEGER_TYPES = tuple(t for t in NUMERIC_TYPES if t not in ("real", "lreal"))
BOOL_TYPES = ("bool",)

# Required roles, optional roles, and the ADS types accepted for each role.
ROLE_TYPES = {
    "sensor": {"state": (*NUMERIC_TYPES, "bool", "string")},
    "binary_sensor": {"state": BOOL_TYPES},
    "switch": {"state": BOOL_TYPES, "command": BOOL_TYPES},
    "number": {"state": NUMERIC_TYPES, "command": NUMERIC_TYPES},
    "light": {
        "state": BOOL_TYPES,
        "command": BOOL_TYPES,
        "brightness": INTEGER_TYPES,
        "brightness_command": INTEGER_TYPES,
    },
    "cover": {
        "state": BOOL_TYPES,
        "open": BOOL_TYPES,
        "close": BOOL_TYPES,
        "stop": BOOL_TYPES,
        "position": INTEGER_TYPES,
        "position_command": INTEGER_TYPES,
    },
    "valve": {
        "state": BOOL_TYPES,
        "command": BOOL_TYPES,
        "position": INTEGER_TYPES,
        "position_command": INTEGER_TYPES,
    },
}
REQUIRED_ROLES = {
    "sensor": ("state",),
    "binary_sensor": ("state",),
    "switch": ("state", "command"),
    "number": ("state", "command"),
    "light": ("state", "command"),
    "cover": ("state", "open", "close"),
    "valve": ("state", "command"),
}
PLATFORM_ALIASES = {"pump": "number"}
WRITE_ROLES = frozenset(
    ("command", "open", "close", "stop", "brightness_command", "position_command")
)
DEVICE_CLASSES = {
    "sensor": [str(value) for value in SensorDeviceClass],
    "binary_sensor": [str(value) for value in BinarySensorDeviceClass],
    "cover": [str(value) for value in CoverDeviceClass],
    "valve": [str(value) for value in ValveDeviceClass],
    "switch": [],
    "number": [],
    "light": [],
}


class VariableRole(TypedDict):
    """A PLC symbol with an explicitly selected ADS type."""

    name: str
    type: str


class MappedDevice(TypedDict):
    """A stable logical device within one connection."""

    id: str
    name: str
    ha_device_id: NotRequired[str]


class MappedEntity(TypedDict):
    """An entity and its read/write roles."""

    id: str
    device_id: str
    name: str
    platform: str
    roles: dict[str, VariableRole]
    device_class: str
    unit: str
    min_value: NotRequired[float]
    max_value: NotRequired[float]
    step: NotRequired[float]


class MappingConfig(TypedDict):
    """Persistent mapping document."""

    devices: list[MappedDevice]
    entities: list[MappedEntity]


_TEXT = vol.All(str, vol.Length(min=1, max=255))
_ID = vol.All(str, vol.Match(r"^[a-zA-Z0-9_-]{1,64}$"))
_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("id"): _ID,
        vol.Required("name"): _TEXT,
        vol.Optional("ha_device_id"): _TEXT,
    }
)
_ENTITY_SCHEMA = vol.Schema(
    {
        vol.Required("id"): _ID,
        vol.Required("device_id"): _ID,
        vol.Required("name"): _TEXT,
        vol.Required("platform"): vol.In(ROLE_TYPES),
        vol.Required("roles"): {
            str: {vol.Required("name"): _TEXT, vol.Required("type"): str}
        },
        vol.Optional("device_class", default=""): str,
        vol.Optional("unit", default=""): vol.All(str, vol.Length(max=64)),
        vol.Optional("min_value", default=0.0): vol.Coerce(float),
        vol.Optional("max_value", default=100.0): vol.Coerce(float),
        vol.Optional("step", default=1.0): vol.Coerce(float),
    }
)
_SCHEMA = vol.Schema(
    {
        vol.Required("devices"): vol.All([_DEVICE_SCHEMA], vol.Length(max=500)),
        vol.Required("entities"): vol.All([_ENTITY_SCHEMA], vol.Length(max=2000)),
    }
)


def empty_mapping() -> MappingConfig:
    """Return a fresh empty document."""
    return {"devices": [], "entities": []}


def entity_unique_id(entry_id: str, entity_id: str) -> str:
    """Scope identity to a connection, independent of PLC symbol names."""
    return f"{entry_id}:mapped:{entity_id}"


def device_identifier(entry_id: str, device_id: str) -> str:
    """Scope a logical device to its integration entry."""
    return f"{entry_id}:mapped-device:{device_id}"


def _validate_number_entity(entity: MappedEntity) -> None:
    """Validate an analog setpoint's numeric range and ADS command type."""
    min_value = entity.get("min_value", 0.0)
    max_value = entity.get("max_value", 100.0)
    step = entity.get("step", 1.0)
    if not all(math.isfinite(value) for value in (min_value, max_value, step)):
        raise vol.Invalid("Number limits and step must be finite")
    if min_value >= max_value:
        raise vol.Invalid("Number minimum must be less than maximum")
    if step <= 0 or step > max_value - min_value:
        raise vol.Invalid("Number step must be positive and fit its range")
    if entity["roles"]["command"]["type"] in INTEGER_TYPES and not all(
        value.is_integer() for value in (min_value, max_value, step)
    ):
        raise vol.Invalid(
            "Integer ADS command types require whole-number limits and steps"
        )


def new_revision() -> str:
    """Create an optimistic concurrency token."""
    return uuid4().hex


def validate_mapping(
    document: Any, legacy_entities: dict[str, list[dict[str, Any]]]
) -> MappingConfig:
    """Reject ambiguous, unsupported or conflicting mappings before saving."""
    result: MappingConfig = _SCHEMA(document)
    devices = {device["id"] for device in result["devices"]}
    if len(devices) != len(result["devices"]):
        raise vol.Invalid("Duplicate device ID")
    ids: set[str] = set()
    states: set[tuple[str, str]] = {
        (platform, config["adsvar"])
        for platform, configs in legacy_entities.items()
        for config in configs
        if "adsvar" in config
    }
    writes: set[str] = set()
    for platform, configs in legacy_entities.items():
        if platform in ("switch", "light", "valve"):
            writes.update(config["adsvar"] for config in configs if "adsvar" in config)
        if platform in ("light", "cover"):
            keys = (
                ("adsvar_brightness", "adsvar_color_temp_kelvin")
                if platform == "light"
                else (
                    "adsvar_open",
                    "adsvar_close",
                    "adsvar_stop",
                    "adsvar_set_position",
                )
            )
            writes.update(
                config[key] for config in configs for key in keys if key in config
            )
    for entity in result["entities"]:
        if entity["id"] in ids:
            raise vol.Invalid("Duplicate entity ID")
        ids.add(entity["id"])
        if entity["device_id"] not in devices:
            raise vol.Invalid("Entity references an unknown device")
        if not entity["name"].strip():
            raise vol.Invalid("Entity name must not be empty")
        platform = entity["platform"]
        roles = entity["roles"]
        if not set(REQUIRED_ROLES[platform]).issubset(roles):
            raise vol.Invalid(f"Missing required roles for {platform}")
        for role, variable in roles.items():
            if role not in ROLE_TYPES[platform]:
                raise vol.Invalid(f"Unsupported role: {role}")
            if variable["type"] not in ROLE_TYPES[platform][role]:
                raise vol.Invalid(f"Unsupported ADS type for {role}")
            if variable["name"] != variable["name"].strip():
                raise vol.Invalid(
                    "PLC symbol names must not contain surrounding spaces"
                )
            if role in WRITE_ROLES:
                if variable["name"] in writes:
                    raise vol.Invalid(
                        f"PLC command already assigned: {variable['name']}"
                    )
                writes.add(variable["name"])
        for read_role, write_role in (
            ("brightness", "brightness_command"),
            ("position", "position_command"),
        ):
            if write_role in roles and read_role not in roles:
                raise vol.Invalid(f"{write_role} requires {read_role}")
        state_key = (platform, roles["state"]["name"])
        if state_key in states:
            raise vol.Invalid(
                "State variable already mapped on this platform (or in YAML)"
            )
        states.add(state_key)
        device_class = entity["device_class"]
        if device_class and device_class not in DEVICE_CLASSES[platform]:
            raise vol.Invalid(f"Unsupported device class for {platform}")
        if entity["unit"] and platform not in ("sensor", "number"):
            raise vol.Invalid("Only sensors and numbers support a unit")
        if platform == "number":
            _validate_number_entity(entity)
        if (
            device_class
            and platform == "sensor"
            and roles["state"]["type"] in ("bool", "string")
        ):
            raise vol.Invalid("Sensor device classes require a numeric PLC variable")
    if any(not device["name"].strip() for device in result["devices"]):
        raise vol.Invalid("Device name must not be empty")
    return result
