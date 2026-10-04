"""Config flow for ADS integration."""

from collections.abc import Mapping, Sequence
from contextlib import suppress
import ipaddress
import logging
from pathlib import Path
import socket
from typing import Any
from uuid import uuid4

import pyads
import voluptuous as vol

from homeassistant.config import load_yaml_config_file
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_DEVICE, CONF_IP_ADDRESS, CONF_PLATFORM, CONF_PORT
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, selector

from .const import (
    CONF_GVL,
    CONF_GVL_IMPORT_REPLACE,
    CONF_GVL_VARIABLES,
    CONF_HA_DEVICE_ID,
    CONF_LEGACY_ENTITIES,
    CONF_VERBOSE_LOGGING,
    DOMAIN,
    SUBENTRY_TYPE_DEVICE_MAPPING,
)
from .gvl import parse_gvl_variables
from .mapping import (
    CONF_MAPPING,
    DEVICE_CLASSES,
    REQUIRED_ROLES,
    ROLE_TYPES,
    empty_mapping,
    validate_mapping,
)

_LOGGER = logging.getLogger(__name__)


class AdsConfigFlow(ConfigFlow, domain="ads"):
    """Handle an ADS config flow."""

    VERSION = 1
    _manual_defaults: dict[str, Any] = {
        CONF_DEVICE: "",
        CONF_PORT: 851,
        CONF_IP_ADDRESS: "",
        CONF_VERBOSE_LOGGING: False,
        "scan_legacy_yaml": False,
    }

    def __init__(self) -> None:
        """Initialize state for this config flow only."""
        self._yaml_defaults: dict[str, Any] | None = None
        self._scan_candidates: list[dict[str, str]] = []
        self._pending_scan_data: dict[str, Any] | None = None
        self._scan_defaults: dict[str, Any] = {
            "subnet": "192.168.0.0/24",
            "scan_limit": 64,
            CONF_PORT: 851,
            CONF_VERBOSE_LOGGING: False,
        }

    def async_get_options_flow(self, config_entry):
        """Return the options flow for this handler."""
        return AdsOptionsFlow(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentries supported by this integration."""
        return {SUBENTRY_TYPE_DEVICE_MAPPING: AdsDeviceMappingSubentryFlow}

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle import from YAML configuration."""
        net_id = import_data[CONF_DEVICE]
        ip_address = import_data.get(CONF_IP_ADDRESS)
        port = import_data[CONF_PORT]

        unique_id = f"{net_id}:{port}:{ip_address or 'auto'}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        if not await self.hass.async_add_executor_job(
            _validate_ads_connection,
            net_id,
            port,
            ip_address,
        ):
            return self.async_abort(reason="cannot_connect")

        entry_data = {
            CONF_DEVICE: net_id,
            CONF_PORT: port,
            CONF_IP_ADDRESS: ip_address,
            CONF_VERBOSE_LOGGING: import_data.get(CONF_VERBOSE_LOGGING, False),
        }
        if legacy_entities := import_data.get(CONF_LEGACY_ENTITIES):
            entry_data[CONF_LEGACY_ENTITIES] = legacy_entities

        return self.async_create_entry(
            title=f"ADS {net_id} (migrated)", data=entry_data
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the setup entrypoint."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["auto_discovery", "manual", "yaml_import"],
        )

    async def async_step_auto_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle auto discovery entrypoint."""
        _LOGGER.debug("ADS config flow: entering auto_discovery")
        self._yaml_defaults = await self.hass.async_add_executor_job(
            _discover_yaml_ads_config, self.hass.config.config_dir
        )

        menu_options = ["network_scan", "manual"]
        if self._yaml_defaults:
            menu_options.insert(0, "yaml_import")

        return self.async_show_menu(step_id="auto_discovery", menu_options=menu_options)

    async def async_step_network_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scan local subnet for ADS router endpoints."""
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("ADS config flow: network_scan input=%s", user_input)
            subnet = user_input["subnet"]
            scan_limit = user_input["scan_limit"]
            port = user_input[CONF_PORT]
            _apply_debug_logging(bool(user_input[CONF_VERBOSE_LOGGING]))

            try:
                ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                errors["base"] = "invalid_subnet"
            else:
                candidates = await self.hass.async_add_executor_job(
                    _scan_subnet_for_ads_hosts,
                    subnet,
                    scan_limit,
                )
                if not candidates:
                    errors["base"] = "no_ads_hosts"
                else:
                    self._scan_candidates = candidates
                    self._scan_defaults = {
                        "subnet": subnet,
                        "scan_limit": scan_limit,
                        CONF_PORT: port,
                        CONF_VERBOSE_LOGGING: user_input[CONF_VERBOSE_LOGGING],
                    }
                    return await self.async_step_network_pick()

        defaults = dict(self._scan_defaults)
        if defaults.get("subnet") == "192.168.0.0/24":
            defaults["subnet"] = await self.hass.async_add_executor_job(
                _guess_local_subnet
            )

        return self.async_show_form(
            step_id="network_scan",
            data_schema=vol.Schema(
                {
                    vol.Required("subnet", default=defaults["subnet"]): str,
                    vol.Required("scan_limit", default=defaults["scan_limit"]): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=1024)
                    ),
                    vol.Required(CONF_PORT, default=defaults[CONF_PORT]): int,
                    vol.Required(
                        CONF_VERBOSE_LOGGING,
                        default=defaults[CONF_VERBOSE_LOGGING],
                    ): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_network_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one discovered host and finish setup."""
        errors: dict[str, str] = {}

        if not self._scan_candidates:
            return await self.async_step_network_scan()

        options = [
            selector.SelectOptionDict(
                value=item[CONF_IP_ADDRESS],
                label=f"{item[CONF_IP_ADDRESS]} ({item[CONF_DEVICE]})",
            )
            for item in self._scan_candidates
        ]

        default_ip = self._scan_candidates[0][CONF_IP_ADDRESS]
        default_net_id = self._scan_candidates[0][CONF_DEVICE]

        if user_input is not None:
            _LOGGER.debug("ADS config flow: network_pick input=%s", user_input)
            selected_ip = user_input[CONF_IP_ADDRESS]
            net_id = user_input[CONF_DEVICE]
            port = user_input[CONF_PORT]
            verbose_logging = user_input[CONF_VERBOSE_LOGGING]
            _apply_debug_logging(bool(verbose_logging))

            selected_data = {
                CONF_DEVICE: net_id,
                CONF_PORT: port,
                CONF_IP_ADDRESS: selected_ip,
                CONF_VERBOSE_LOGGING: verbose_logging,
            }

            if user_input["search_legacy_yaml"]:
                self._yaml_defaults = await self.hass.async_add_executor_job(
                    _discover_yaml_ads_config, self.hass.config.config_dir
                )
                if self._yaml_defaults:
                    self._pending_scan_data = selected_data
                    return await self.async_step_network_legacy_choice()

            unique_id = f"{net_id}:{port}:{selected_ip or 'auto'}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            if not await self.hass.async_add_executor_job(
                _validate_ads_connection,
                net_id,
                port,
                selected_ip,
            ):
                _LOGGER.debug(
                    "ADS config flow: network_pick connection failed net_id=%s ip=%s port=%s",
                    net_id,
                    selected_ip,
                    port,
                )
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"ADS {net_id}", data=selected_data
                )

        return self.async_show_form(
            step_id="network_pick",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_IP_ADDRESS,
                        default=default_ip,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    ),
                    vol.Required(CONF_DEVICE, default=default_net_id): str,
                    vol.Required(
                        CONF_PORT, default=self._scan_defaults[CONF_PORT]
                    ): int,
                    vol.Required(
                        CONF_VERBOSE_LOGGING,
                        default=self._scan_defaults[CONF_VERBOSE_LOGGING],
                    ): bool,
                    vol.Required("search_legacy_yaml", default=True): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_network_legacy_choice(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow applying discovered legacy YAML values after network scan."""
        errors: dict[str, str] = {}

        if self._pending_scan_data is None:
            return await self.async_step_network_pick()

        if user_input is not None:
            _LOGGER.debug("ADS config flow: network_legacy_choice input=%s", user_input)
            entry_data = dict(self._pending_scan_data)
            if user_input["use_legacy_yaml"] and self._yaml_defaults:
                entry_data.update(
                    {
                        CONF_DEVICE: self._yaml_defaults.get(
                            CONF_DEVICE, entry_data[CONF_DEVICE]
                        ),
                        CONF_PORT: self._yaml_defaults.get(
                            CONF_PORT, entry_data[CONF_PORT]
                        ),
                        CONF_IP_ADDRESS: self._yaml_defaults.get(CONF_IP_ADDRESS),
                        CONF_VERBOSE_LOGGING: self._yaml_defaults.get(
                            CONF_VERBOSE_LOGGING,
                            entry_data[CONF_VERBOSE_LOGGING],
                        ),
                    }
                )

            net_id = entry_data[CONF_DEVICE]
            port = entry_data[CONF_PORT]
            ip_address = entry_data.get(CONF_IP_ADDRESS)

            unique_id = f"{net_id}:{port}:{ip_address or 'auto'}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            if not await self.hass.async_add_executor_job(
                _validate_ads_connection,
                net_id,
                port,
                ip_address,
            ):
                errors["base"] = "cannot_connect"
            else:
                if user_input["use_legacy_yaml"] and self._yaml_defaults:
                    entry_data[CONF_LEGACY_ENTITIES] = self._yaml_defaults.get(
                        CONF_LEGACY_ENTITIES,
                        {},
                    )
                return self.async_create_entry(title=f"ADS {net_id}", data=entry_data)

        return self.async_show_form(
            step_id="network_legacy_choice",
            data_schema=vol.Schema(
                {
                    vol.Required("use_legacy_yaml", default=True): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("ADS config flow: manual input=%s", user_input)
            _apply_debug_logging(bool(user_input.get(CONF_VERBOSE_LOGGING, False)))
            if user_input.get("scan_legacy_yaml"):
                self._yaml_defaults = await self.hass.async_add_executor_job(
                    _discover_yaml_ads_config, self.hass.config.config_dir
                )
                return self.async_show_form(
                    step_id="manual",
                    data_schema=self._user_data_schema(
                        self._manual_form_defaults(self._yaml_defaults or user_input)
                    ),
                    errors={},
                )

            net_id = user_input[CONF_DEVICE]
            ip_address = user_input.get(CONF_IP_ADDRESS)
            port = user_input[CONF_PORT]

            unique_id = f"{net_id}:{port}:{ip_address or 'auto'}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            if not await self.hass.async_add_executor_job(
                _validate_ads_connection,
                net_id,
                port,
                ip_address,
            ):
                _LOGGER.debug(
                    "ADS config flow: manual connection failed net_id=%s ip=%s port=%s",
                    net_id,
                    ip_address,
                    port,
                )
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"ADS {net_id}",
                    data={
                        CONF_DEVICE: net_id,
                        CONF_PORT: port,
                        CONF_IP_ADDRESS: ip_address,
                        CONF_VERBOSE_LOGGING: user_input[CONF_VERBOSE_LOGGING],
                        CONF_LEGACY_ENTITIES: self._yaml_defaults.get(
                            CONF_LEGACY_ENTITIES, {}
                        )
                        if self._yaml_defaults
                        else {},
                    },
                )

        yaml_defaults = self._yaml_defaults or {}
        return self.async_show_form(
            step_id="manual",
            data_schema=self._user_data_schema(
                self._manual_form_defaults(yaml_defaults)
            ),
            errors=errors,
        )

    async def async_step_yaml_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Load the first ADS YAML config that can be found."""
        _LOGGER.debug(
            "ADS config flow: yaml_import triggered user_input=%s", user_input
        )
        self._yaml_defaults = await self.hass.async_add_executor_job(
            _discover_yaml_ads_config, self.hass.config.config_dir
        )
        if not self._yaml_defaults:
            _LOGGER.warning(
                "ADS config flow: yaml_import requested but no valid ADS YAML config found in %s",
                self.hass.config.config_dir,
            )
            return self.async_show_form(
                step_id="manual",
                data_schema=self._user_data_schema(self._manual_form_defaults({})),
                errors={"base": "yaml_not_found"},
            )

        _apply_debug_logging(bool(self._yaml_defaults.get(CONF_VERBOSE_LOGGING, False)))
        _LOGGER.debug("ADS config flow: yaml_import defaults=%s", self._yaml_defaults)
        return self.async_show_form(
            step_id="manual",
            data_schema=self._user_data_schema(
                self._manual_form_defaults(self._yaml_defaults),
                include_scan_legacy=False,
            ),
            errors={},
        )

    @staticmethod
    def _user_data_schema(
        defaults: Mapping[str, Any],
        include_scan_legacy: bool = True,
    ) -> vol.Schema:
        """Build the setup form schema with optional YAML defaults."""
        schema: dict[Any, Any] = {
            vol.Required(CONF_DEVICE, default=defaults.get(CONF_DEVICE, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, 851)): int,
            vol.Optional(
                CONF_IP_ADDRESS, default=defaults.get(CONF_IP_ADDRESS, "")
            ): str,
            vol.Required(
                CONF_VERBOSE_LOGGING,
                default=defaults.get(CONF_VERBOSE_LOGGING, False),
            ): bool,
        }

        if include_scan_legacy:
            schema[vol.Optional("scan_legacy_yaml", default=False)] = bool

        return vol.Schema(schema)

    @staticmethod
    def _manual_form_defaults(defaults: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize defaults for the manual form."""
        normalized = dict(AdsConfigFlow._manual_defaults)
        normalized.update(defaults)
        normalized.pop("scan_legacy_yaml", None)
        return normalized


class AdsOptionsFlow(OptionsFlow):
    """Handle ADS options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the ADS options."""
        current_options = dict(self.config_entry.options)

        if user_input is not None:
            options = {
                CONF_VERBOSE_LOGGING: user_input[CONF_VERBOSE_LOGGING],
            }
            existing_variables = list(
                current_options.get(
                    CONF_GVL_VARIABLES,
                    self.config_entry.data.get(CONF_GVL_VARIABLES, []),
                )
            )
            if existing_variables:
                options[CONF_GVL_VARIABLES] = existing_variables

            imported_gvl = user_input.get(CONF_GVL, "")
            if imported_gvl:
                parsed_variables = parse_gvl_variables(imported_gvl)

                if user_input[CONF_GVL_IMPORT_REPLACE]:
                    options[CONF_GVL_VARIABLES] = parsed_variables
                else:
                    merged = {item["name"]: item for item in existing_variables}
                    for item in parsed_variables:
                        merged[item["name"]] = item
                    options[CONF_GVL_VARIABLES] = list(merged.values())

            return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_VERBOSE_LOGGING,
                        default=current_options.get(
                            CONF_VERBOSE_LOGGING,
                            self.config_entry.data.get(CONF_VERBOSE_LOGGING, False),
                        ),
                    ): bool,
                    vol.Optional(CONF_GVL, default=""): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
                    vol.Required(CONF_GVL_IMPORT_REPLACE, default=False): bool,
                }
            ),
        )


class AdsDeviceMappingSubentryFlow(ConfigSubentryFlow):
    """Configure one or more ADS entities for a selected Home Assistant device."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Select the Home Assistant device to attach mapped entities to."""
        errors: dict[str, str] = {}
        if not hasattr(self, "_entities"):
            self._entities: list[dict[str, Any]] = []
            self._mapped_device_id = uuid4().hex
            if self.source == SOURCE_RECONFIGURE:
                self._initialize_from_subentry()

        if user_input is not None:
            ha_device_id = user_input[CONF_HA_DEVICE_ID]
            device = dr.async_get(self.hass).async_get(ha_device_id)
            if device is None:
                errors["base"] = "device_not_found"
            elif self._device_is_already_mapped(ha_device_id):
                errors["base"] = "already_configured"
            else:
                if user_input.get("replace_mappings", False):
                    self._entities = []
                self._ha_device_id = ha_device_id
                self._device_name = device.name_by_user or device.name or "ADS device"
                return await self.async_step_platform()

        suggested_values = {}
        if self.source == SOURCE_RECONFIGURE and hasattr(self, "_ha_device_id"):
            suggested_values[CONF_HA_DEVICE_ID] = self._ha_device_id
        schema_fields: dict[Any, Any] = {
            vol.Required(CONF_HA_DEVICE_ID): selector.DeviceSelector(
                selector.DeviceSelectorConfig()
            )
        }
        if self.source == SOURCE_RECONFIGURE:
            schema_fields[vol.Optional("replace_mappings", default=False)] = bool
        schema = vol.Schema(schema_fields)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, suggested_values),
            errors=errors,
        )

    async_step_reconfigure = async_step_user

    async def async_step_platform(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose the Home Assistant entity platform to create."""
        if user_input is not None:
            self._platform = user_input["platform"]
            return await self.async_step_entity()

        return self.async_show_form(
            step_id="platform",
            data_schema=vol.Schema(
                {
                    vol.Required("platform"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=list(ROLE_TYPES))
                    )
                }
            ),
        )

    async def async_step_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Map the variables for one entity and optionally add another."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entity = {
                "id": uuid4().hex,
                "device_id": self._mapped_device_id,
                "name": user_input["entity_name"],
                "platform": self._platform,
                "roles": {},
                "device_class": user_input.get("device_class", ""),
                "unit": user_input.get("unit", ""),
            }
            for role in ROLE_TYPES[self._platform]:
                variable_name = user_input.get(f"{role}_variable", "")
                if variable_name:
                    entity["roles"][role] = {
                        "name": variable_name,
                        "type": user_input[f"{role}_type"],
                    }

            candidate_entities = [*self._entities, entity]
            document = self._mapping_document(candidate_entities)
            try:
                self._validate_document(document)
            except vol.Invalid:
                errors["base"] = "invalid_mapping"
            else:
                self._entities = candidate_entities
                if user_input["add_another"]:
                    return await self.async_step_platform()
                return self._create_or_update_subentry(
                    self._mapping_document(self._entities)
                )

        schema: dict[Any, Any] = {
            vol.Required("entity_name"): vol.All(str, vol.Length(min=1, max=255)),
            vol.Required("add_another", default=False): bool,
        }
        required_roles = REQUIRED_ROLES[self._platform]
        for role, supported_types in ROLE_TYPES[self._platform].items():
            role_name = f"{role}_variable"
            role_type = f"{role}_type"
            if role in required_roles:
                schema[vol.Required(role_name)] = vol.All(
                    str, vol.Length(min=1, max=255)
                )
                schema[vol.Required(role_type, default=supported_types[0])] = (
                    selector.SelectSelector(
                        selector.SelectSelectorConfig(options=list(supported_types))
                    )
                )
            else:
                schema[vol.Optional(role_name, default="")] = str
                schema[vol.Optional(role_type, default=supported_types[0])] = (
                    selector.SelectSelector(
                        selector.SelectSelectorConfig(options=list(supported_types))
                    )
                )
        if DEVICE_CLASSES[self._platform]:
            schema[vol.Optional("device_class", default="")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["", *DEVICE_CLASSES[self._platform]]
                )
            )
        if self._platform == "sensor":
            schema[vol.Optional("unit", default="")] = str

        return self.async_show_form(
            step_id="entity",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    def _initialize_from_subentry(self) -> None:
        """Load the current configuration when editing a mapping subentry."""
        subentry = self._get_reconfigure_subentry()
        document = subentry.data[CONF_MAPPING]
        mapped_device = document["devices"][0]
        self._ha_device_id = mapped_device[CONF_HA_DEVICE_ID]
        self._mapped_device_id = mapped_device["id"]
        self._device_name = mapped_device["name"]
        self._entities = list(document["entities"])

    def _device_is_already_mapped(self, ha_device_id: str) -> bool:
        """Prevent multiple subentries from independently claiming one HA device."""
        entry = self._get_entry()
        excluded_subentry_id = (
            self._get_reconfigure_subentry().subentry_id
            if self.source == SOURCE_RECONFIGURE
            else None
        )
        existing_mapping = entry.options.get(CONF_MAPPING, empty_mapping())
        if any(
            device.get(CONF_HA_DEVICE_ID) == ha_device_id
            for device in existing_mapping["devices"]
        ):
            return True
        return any(
            subentry.subentry_id != excluded_subentry_id
            and any(
                device.get(CONF_HA_DEVICE_ID) == ha_device_id
                for device in subentry.data.get(CONF_MAPPING, {}).get("devices", [])
            )
            for subentry in entry.subentries.values()
        )

    def _mapping_document(self, entities: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the per-device mapping document stored in the subentry."""
        return {
            "devices": [
                {
                    "id": self._mapped_device_id,
                    "name": self._device_name,
                    CONF_HA_DEVICE_ID: self._ha_device_id,
                }
            ],
            "entities": entities,
        }

    def _validate_document(self, document: dict[str, Any]) -> None:
        """Validate new mappings against legacy and other configured mappings."""
        entry = self._get_entry()
        devices = list(entry.options.get(CONF_MAPPING, empty_mapping())["devices"])
        entities = list(entry.options.get(CONF_MAPPING, empty_mapping())["entities"])
        excluded_subentry_id = (
            self._get_reconfigure_subentry().subentry_id
            if self.source == SOURCE_RECONFIGURE
            else None
        )
        for subentry in entry.subentries.values():
            if subentry.subentry_id == excluded_subentry_id:
                continue
            existing_document = subentry.data.get(CONF_MAPPING)
            if existing_document is not None:
                devices.extend(existing_document["devices"])
                entities.extend(existing_document["entities"])
        devices.extend(document["devices"])
        entities.extend(document["entities"])
        validate_mapping(
            {"devices": devices, "entities": entities},
            entry.options.get(
                CONF_LEGACY_ENTITIES,
                entry.data.get(CONF_LEGACY_ENTITIES, {}),
            ),
        )

    def _create_or_update_subentry(
        self, document: dict[str, Any]
    ) -> SubentryFlowResult:
        """Persist the selected device and its mapped entities."""
        title = self._device_name
        data = {CONF_MAPPING: document}
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=title,
                data=data,
            )
        return self.async_create_entry(title=title, data=data)


def _validate_ads_connection(net_id: str, port: int, ip_address: str | None) -> bool:
    """Validate ADS connection parameters by opening and closing connection."""
    _LOGGER.debug(
        "ADS config flow: validating ADS connection net_id=%s ip=%s port=%s",
        net_id,
        ip_address,
        port,
    )
    connection = pyads.Connection(net_id, port, ip_address)

    try:
        connection.open()
    except pyads.ADSError as err:
        _LOGGER.debug("ADS config flow: ADS connection validation failed: %s", err)
        return False
    else:
        _LOGGER.debug("ADS config flow: ADS connection validation succeeded")
        return True
    finally:
        with suppress(pyads.ADSError):
            connection.close()


def _scan_subnet_for_ads_hosts(subnet: str, scan_limit: int) -> list[dict[str, str]]:
    """Find likely ADS participants by probing ADS router TCP port on the subnet."""
    network = ipaddress.ip_network(subnet, strict=False)
    results: list[dict[str, str]] = []

    for index, host in enumerate(network.hosts()):
        if index >= scan_limit:
            break

        ip_address = str(host)
        if not _is_tcp_port_open(ip_address, 48898):
            continue

        results.append(
            {
                CONF_IP_ADDRESS: ip_address,
                CONF_DEVICE: f"{ip_address}.1.1",
            }
        )

    return results


def _is_tcp_port_open(ip_address: str, port: int, timeout: float = 0.25) -> bool:
    """Check if a remote TCP port is reachable within a short timeout."""
    try:
        with socket.create_connection((ip_address, port), timeout=timeout):
            return True
    except OSError:
        return False


def _guess_local_subnet() -> str:
    """Guess a suitable local /24 subnet from the current host IP."""
    try:
        candidate_ips = {
            item[4][0]
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            if item and item[4]
        }
    except OSError:
        candidate_ips = set()

    for ip_address in sorted(candidate_ips):
        if ip_address.startswith("127."):
            continue

        ip_obj = ipaddress.ip_address(ip_address)
        network = ipaddress.ip_network(f"{ip_obj}/24", strict=False)
        return str(network)

    return "192.168.0.0/24"


def _discover_yaml_ads_config(config_dir: str) -> dict[str, Any] | None:
    """Find the first ADS YAML config in the Home Assistant config directory."""
    config_path = Path(config_dir)
    _LOGGER.debug("ADS config flow: searching for YAML ADS config in %s", config_dir)

    for yaml_path in sorted(
        (*config_path.rglob("*.yaml"), *config_path.rglob("*.yml"))
    ):
        try:
            loaded_config = load_yaml_config_file(str(yaml_path))
        except (FileNotFoundError, HomeAssistantError, OSError) as err:
            _LOGGER.debug(
                "ADS config flow: failed reading YAML file %s: %s",
                yaml_path,
                err,
            )
            continue

        ads_config = _find_ads_config(loaded_config)
        if not isinstance(ads_config, Mapping):
            continue

        net_id = ads_config.get(CONF_DEVICE)
        port = ads_config.get(CONF_PORT)

        if not isinstance(net_id, str):
            continue

        try:
            port_int = int(port)
        except TypeError, ValueError:
            _LOGGER.debug(
                "ADS config flow: invalid port in YAML file %s (value=%s)",
                yaml_path,
                port,
            )
            continue

        _LOGGER.info("ADS config flow: found legacy ADS YAML config in %s", yaml_path)
        return {
            CONF_DEVICE: net_id,
            CONF_PORT: port_int,
            CONF_IP_ADDRESS: ads_config.get(CONF_IP_ADDRESS) or None,
            CONF_VERBOSE_LOGGING: bool(ads_config.get(CONF_VERBOSE_LOGGING, False)),
            CONF_LEGACY_ENTITIES: _collect_legacy_entities_from_loaded_yaml(
                loaded_config
            ),
        }

    _LOGGER.debug("ADS config flow: no valid ADS YAML config found")
    return None


def _apply_debug_logging(verbose_logging: bool) -> None:
    """Enable ADS-specific debug logging during setup flow when requested."""
    if not verbose_logging:
        return

    logging.getLogger("custom_components.ads").setLevel(logging.DEBUG)
    logging.getLogger("custom_components.ads.config_flow").setLevel(logging.DEBUG)
    logging.getLogger("pyads").setLevel(logging.DEBUG)


def _find_ads_config(data: Any) -> Any:
    """Recursively find an ADS config mapping in nested YAML data."""
    if isinstance(data, Mapping):
        if DOMAIN in data:
            return data[DOMAIN]

        for value in data.values():
            ads_config = _find_ads_config(value)
            if ads_config is not None:
                return ads_config

    elif isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        for value in data:
            ads_config = _find_ads_config(value)
            if ads_config is not None:
                return ads_config

    return None


def _collect_legacy_entities_from_loaded_yaml(
    loaded_config: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Collect legacy ADS platform entities from one loaded YAML document."""
    if not isinstance(loaded_config, Mapping):
        return {}

    legacy_platforms = (
        "binary_sensor",
        "cover",
        "light",
        "select",
        "sensor",
        "switch",
        "valve",
    )
    collected: dict[str, list[dict[str, Any]]] = {}

    for platform in legacy_platforms:
        platform_config = loaded_config.get(platform)
        if platform_config is None:
            continue

        if isinstance(platform_config, Mapping):
            candidates = [platform_config]
        elif isinstance(platform_config, Sequence) and not isinstance(
            platform_config,
            (str, bytes, bytearray),
        ):
            candidates = list(platform_config)
        else:
            continue

        items: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue

            if candidate.get(CONF_PLATFORM) != DOMAIN:
                continue

            cleaned = {
                key: value for key, value in candidate.items() if key != CONF_PLATFORM
            }
            items.append(dict(cleaned))

        if items:
            collected[platform] = items

    return collected
