"""Support for Automation Device Specification (ADS)."""

from collections.abc import Mapping
import logging
from pathlib import Path
from typing import Any

import pyads
import voluptuous as vol

from homeassistant.const import (
    CONF_DEVICE,
    CONF_IP_ADDRESS,
    CONF_PLATFORM,
    CONF_PORT,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ADS_VAR,
    CONF_ENTRY_ID,
    CONF_GVL,
    CONF_GVL_FILE_PATH,
    CONF_GVL_IMPORT_REPLACE,
    CONF_GVL_VARIABLES,
    CONF_LEGACY_ENTITIES,
    CONF_VERBOSE_LOGGING,
    DATA_ADS,
    DATA_ADS_HUBS,
    DOMAIN,
    AdsType,
)
from .gvl import parse_gvl_variables
from .hub import AdsHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.LIGHT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
    Platform.VALVE,
]

LEGACY_ENTITY_PLATFORMS = (
    "binary_sensor",
    "cover",
    "light",
    "select",
    "sensor",
    "switch",
    "valve",
)


ADS_TYPEMAP = {
    AdsType.BOOL: pyads.PLCTYPE_BOOL,
    AdsType.BYTE: pyads.PLCTYPE_BYTE,
    AdsType.INT: pyads.PLCTYPE_INT,
    AdsType.UINT: pyads.PLCTYPE_UINT,
    AdsType.SINT: pyads.PLCTYPE_SINT,
    AdsType.USINT: pyads.PLCTYPE_USINT,
    AdsType.DINT: pyads.PLCTYPE_DINT,
    AdsType.UDINT: pyads.PLCTYPE_UDINT,
    AdsType.WORD: pyads.PLCTYPE_WORD,
    AdsType.DWORD: pyads.PLCTYPE_DWORD,
    AdsType.REAL: pyads.PLCTYPE_REAL,
    AdsType.LREAL: pyads.PLCTYPE_LREAL,
    AdsType.STRING: pyads.PLCTYPE_STRING,
    AdsType.TIME: pyads.PLCTYPE_TIME,
    AdsType.DATE: pyads.PLCTYPE_DATE,
    AdsType.DATE_AND_TIME: pyads.PLCTYPE_DT,
    AdsType.TOD: pyads.PLCTYPE_TOD,
}

CONF_ADS_FACTOR = "factor"
CONF_ADS_TYPE = "adstype"
CONF_ADS_VALUE = "value"


SERVICE_WRITE_DATA_BY_NAME = "write_data_by_name"
SERVICE_IMPORT_GVL = "import_gvl"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_DEVICE): cv.string,
                vol.Required(CONF_PORT): cv.port,
                vol.Optional(CONF_IP_ADDRESS): cv.string,
                vol.Optional(CONF_VERBOSE_LOGGING, default=False): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

SCHEMA_SERVICE_WRITE_DATA_BY_NAME = vol.Schema(
    {
        vol.Required(CONF_ADS_TYPE): vol.Coerce(AdsType),
        vol.Required(CONF_ADS_VALUE): vol.Coerce(int),
        vol.Required(CONF_ADS_VAR): cv.string,
        vol.Optional(CONF_ENTRY_ID): cv.string,
    }
)

SCHEMA_SERVICE_IMPORT_GVL = vol.Schema(
    {
        vol.Optional(CONF_GVL): cv.string,
        vol.Optional(CONF_GVL_FILE_PATH): cv.string,
        vol.Optional(CONF_ENTRY_ID): cv.string,
        vol.Optional(CONF_GVL_IMPORT_REPLACE, default=False): cv.boolean,
    }
)


def setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the ADS component."""

    hass.data.setdefault(DATA_ADS_HUBS, {})

    if DOMAIN not in config:
        return True

    conf = config[DOMAIN]
    legacy_entities = _collect_legacy_entity_configs(config)
    if legacy_entities:
        hass.data[f"{DOMAIN}_legacy_entities"] = legacy_entities
        _LOGGER.info(
            "Detected %d legacy ADS entities for migration",
            _count_legacy_entities(legacy_entities),
        )

    net_id = conf[CONF_DEVICE]
    ip_address = conf.get(CONF_IP_ADDRESS)
    port = conf[CONF_PORT]
    verbose_logging = conf[CONF_VERBOSE_LOGGING]

    if verbose_logging:
        _LOGGER.setLevel(logging.DEBUG)

    # Store YAML config for later async migration in async_setup_entry
    hass.data.setdefault(f"{DOMAIN}_yaml_config", conf)

    client = pyads.Connection(net_id, port, ip_address)

    try:
        ads = AdsHub(client, hub_id="yaml")
    except pyads.ADSError:
        _LOGGER.error(
            "Could not connect to ADS host (netid=%s, ip=%s, port=%s)",
            net_id,
            ip_address,
            port,
        )
        return False

    hass.data[DATA_ADS] = ads
    hass.data[DATA_ADS_HUBS]["yaml"] = ads
    hass.bus.listen(EVENT_HOMEASSISTANT_STOP, ads.shutdown)

    return True


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    """Set up ADS from a config entry."""
    hass.data.setdefault(DATA_ADS_HUBS, {})

    # Migrate YAML config on first config entry creation
    existing_entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id]
    if not existing_entries and f"{DOMAIN}_yaml_config" in hass.data:
        yaml_conf = hass.data.pop(f"{DOMAIN}_yaml_config")
        await _async_migrate_yaml_to_entry(hass, yaml_conf)

    net_id: str = entry.data[CONF_DEVICE]
    port: int = entry.data[CONF_PORT]
    ip_address: str | None = entry.data.get(CONF_IP_ADDRESS)
    verbose_logging: bool = entry.options.get(
        CONF_VERBOSE_LOGGING,
        entry.data.get(CONF_VERBOSE_LOGGING, False),
    )

    if verbose_logging:
        _LOGGER.setLevel(logging.DEBUG)

    def _create_hub() -> AdsHub:
        return AdsHub(pyads.Connection(net_id, port, ip_address), hub_id=entry.entry_id)

    try:
        hub = await hass.async_add_executor_job(_create_hub)
    except pyads.ADSError:
        _LOGGER.error(
            "Could not connect to ADS host (netid=%s, ip=%s, port=%s)",
            net_id,
            ip_address,
            port,
        )
        return False

    hass.data[DATA_ADS_HUBS][entry.entry_id] = hub
    if DATA_ADS not in hass.data:
        hass.data[DATA_ADS] = hub

    # Move migrated legacy entities from entry data to options on first setup.
    legacy_entities_data = entry.data.get(CONF_LEGACY_ENTITIES)
    if isinstance(legacy_entities_data, Mapping):
        migrated_legacy_entities = {
            platform: list(items)
            for platform, items in legacy_entities_data.items()
            if platform in LEGACY_ENTITY_PLATFORMS and isinstance(items, list)
        }
        if migrated_legacy_entities:
            merged_options = dict(entry.options)
            existing_legacy_entities = merged_options.get(CONF_LEGACY_ENTITIES, {})
            if isinstance(existing_legacy_entities, Mapping):
                for platform, items in migrated_legacy_entities.items():
                    if platform not in existing_legacy_entities:
                        existing_legacy_entities[platform] = items
                merged_options[CONF_LEGACY_ENTITIES] = dict(existing_legacy_entities)
            else:
                merged_options[CONF_LEGACY_ENTITIES] = migrated_legacy_entities

            new_data = dict(entry.data)
            new_data.pop(CONF_LEGACY_ENTITIES, None)
            hass.config_entries.async_update_entry(entry, data=new_data, options=merged_options)
            _LOGGER.info(
                "Migrated %d legacy ADS entities into config entry options",
                _count_legacy_entities(migrated_legacy_entities),
            )

    await _register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    """Unload ADS config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hubs = hass.data.get(DATA_ADS_HUBS, {})
    hub = hubs.pop(entry.entry_id, None)
    if hub is not None:
        await hass.async_add_executor_job(hub.shutdown)

    if hass.data.get(DATA_ADS) is hub:
        if hubs:
            hass.data[DATA_ADS] = next(iter(hubs.values()))
        else:
            hass.data.pop(DATA_ADS, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry) -> None:
    """Reload ADS entry on options update."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def _register_services(hass: HomeAssistant) -> None:
    """Register ADS services once."""
    if hass.services.has_service(DOMAIN, SERVICE_WRITE_DATA_BY_NAME):
        return

    async def handle_write_data_by_name(call: ServiceCall) -> None:
        """Write a value to the connected ADS device."""
        ads_var: str = call.data[CONF_ADS_VAR]
        ads_type: AdsType = call.data[CONF_ADS_TYPE]
        value: int = call.data[CONF_ADS_VALUE]
        entry_id: str | None = call.data.get(CONF_ENTRY_ID)
        hub = _resolve_hub(hass, entry_id)

        if hub is None:
            _LOGGER.error("No ADS hub available for service call")
            return

        await hass.async_add_executor_job(hub.write_by_name, ads_var, value, ADS_TYPEMAP[ads_type])

    async def handle_import_gvl(call: ServiceCall) -> None:
        """Import GVL variables into a config entry for quick variable matching."""
        entry_id: str | None = call.data.get(CONF_ENTRY_ID)
        raw_gvl = call.data.get(CONF_GVL)
        gvl_file_path = call.data.get(CONF_GVL_FILE_PATH)
        replace_existing = call.data[CONF_GVL_IMPORT_REPLACE]

        if not raw_gvl and not gvl_file_path:
            _LOGGER.error("GVL import needs either '%s' or '%s'", CONF_GVL, CONF_GVL_FILE_PATH)
            return

        if gvl_file_path:
            try:
                raw_gvl = await hass.async_add_executor_job(_read_gvl_file, gvl_file_path)
            except OSError as err:
                _LOGGER.error("Could not read GVL file '%s': %s", gvl_file_path, err)
                return

        parsed_variables = parse_gvl_variables(raw_gvl or "")
        if not parsed_variables:
            _LOGGER.warning("GVL import did not find any variables")
            return

        target_entry = _resolve_entry(hass, entry_id)
        if target_entry is None:
            _LOGGER.warning(
                "Imported %d GVL variables but no ADS config entry found for persistence",
                len(parsed_variables),
            )
            return

        existing_variables = list(target_entry.options.get(CONF_GVL_VARIABLES, []))
        if replace_existing:
            imported_variables = parsed_variables
        else:
            merged = {item["name"]: item for item in existing_variables}
            for item in parsed_variables:
                merged[item["name"]] = item
            imported_variables = list(merged.values())

        new_options = dict(target_entry.options)
        new_options[CONF_GVL_VARIABLES] = imported_variables
        hass.config_entries.async_update_entry(target_entry, options=new_options)

        _LOGGER.info(
            "Imported %d GVL variables for ADS entry '%s' (total: %d)",
            len(parsed_variables),
            target_entry.title,
            len(imported_variables),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_DATA_BY_NAME,
        handle_write_data_by_name,
        schema=SCHEMA_SERVICE_WRITE_DATA_BY_NAME,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_GVL,
        handle_import_gvl,
        schema=SCHEMA_SERVICE_IMPORT_GVL,
    )


def _resolve_hub(hass: HomeAssistant, entry_id: str | None) -> AdsHub | None:
    """Resolve target ADS hub from optional config entry id."""
    if entry_id:
        return hass.data.get(DATA_ADS_HUBS, {}).get(entry_id)

    default_hub = hass.data.get(DATA_ADS)
    if default_hub is not None:
        return default_hub

    hubs: Mapping[str, AdsHub] = hass.data.get(DATA_ADS_HUBS, {})
    if not hubs:
        return None

    return next(iter(hubs.values()))


def _resolve_entry(hass: HomeAssistant, entry_id: str | None):
    """Resolve ADS config entry by id or first available one."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return None

    if entry_id:
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    return entries[0]


def _read_gvl_file(file_path: str) -> str:
    """Read GVL source from disk as text."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(file_path)

    return path.read_text(encoding="utf-8")


async def _async_migrate_yaml_to_entry(hass: HomeAssistant, yaml_config: dict) -> None:
    """Migrate YAML configuration to config entry if not already present."""
    net_id: str = yaml_config.get(CONF_DEVICE, "")
    port: int = yaml_config.get(CONF_PORT, 851)
    ip_address: str | None = yaml_config.get(CONF_IP_ADDRESS)
    verbose_logging: bool = yaml_config.get(CONF_VERBOSE_LOGGING, False)
    legacy_entities: dict[str, list[dict[str, Any]]] = hass.data.pop(
        f"{DOMAIN}_legacy_entities", {}
    )

    if not net_id:
        return

    # Generate unique_id from YAML config
    unique_id = f"{net_id}:{port}:{ip_address or 'auto'}"

    # Check if config entry already exists for this unique_id
    existing_entries = hass.config_entries.async_entries(DOMAIN)
    for entry in existing_entries:
        if entry.unique_id == unique_id:
            _LOGGER.debug(
                "Config entry already exists for YAML ADS config (NetID: %s, Port: %d)",
                net_id,
                port,
            )
            return

    # Create config entry from YAML config
    try:
        _LOGGER.info(
            "Migrating YAML ADS configuration to config entry (NetID: %s, Port: %d)",
            net_id,
            port,
        )

        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "import"},
            data={
                CONF_DEVICE: net_id,
                CONF_PORT: port,
                CONF_IP_ADDRESS: ip_address,
                CONF_VERBOSE_LOGGING: verbose_logging,
            },
        )

        if legacy_entities:
            created_entry = next(
                (
                    item
                    for item in hass.config_entries.async_entries(DOMAIN)
                    if item.unique_id == unique_id
                ),
                None,
            )
            if created_entry is not None:
                merged_options = dict(created_entry.options)
                merged_options[CONF_LEGACY_ENTITIES] = legacy_entities
                hass.config_entries.async_update_entry(created_entry, options=merged_options)
                _LOGGER.info(
                    "Migrated %d legacy ADS entities into config entry '%s'",
                    _count_legacy_entities(legacy_entities),
                    created_entry.title,
                )

        _LOGGER.info(
            "Successfully migrated YAML ADS configuration to config entry (NetID: %s, Port: %d)",
            net_id,
            port,
        )
    except Exception as err:
        _LOGGER.error(
            "Failed to migrate YAML ADS configuration: %s",
            err,
        )


def _collect_legacy_entity_configs(config: ConfigType) -> dict[str, list[dict[str, Any]]]:
    """Collect all legacy ADS platform entities from YAML config."""
    collected: dict[str, list[dict[str, Any]]] = {}

    for platform in LEGACY_ENTITY_PLATFORMS:
        platform_config = config.get(platform)
        if platform_config is None:
            continue

        if isinstance(platform_config, Mapping):
            candidates = [platform_config]
        elif isinstance(platform_config, list):
            candidates = platform_config
        else:
            continue

        items: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue

            if candidate.get(CONF_PLATFORM) != DOMAIN:
                continue

            cleaned = {key: value for key, value in candidate.items() if key != CONF_PLATFORM}
            items.append(dict(cleaned))

        if items:
            collected[platform] = items

    return collected


def _count_legacy_entities(legacy_entities: Mapping[str, list[dict[str, Any]]]) -> int:
    """Return the number of collected legacy entities."""
    return sum(len(items) for items in legacy_entities.values())
