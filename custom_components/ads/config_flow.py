"""Config flow for ADS integration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import ipaddress
import socket
from typing import Any

import pyads
import voluptuous as vol

from homeassistant.config import load_yaml_config_file
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_DEVICE, CONF_IP_ADDRESS, CONF_PORT
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import (
    CONF_GVL,
    CONF_GVL_IMPORT_REPLACE,
    CONF_GVL_VARIABLES,
    CONF_VERBOSE_LOGGING,
    DOMAIN,
)
from .gvl import parse_gvl_variables


class AdsConfigFlow(ConfigFlow, domain="ads"):
    """Handle an ADS config flow."""

    VERSION = 1
    _yaml_defaults: dict[str, Any] | None = None
    _scan_candidates: list[dict[str, str]] = []
    _pending_scan_data: dict[str, Any] | None = None
    _scan_defaults: dict[str, Any] = {
        "subnet": "192.168.0.0/24",
        "scan_limit": 64,
        CONF_PORT: 851,
        CONF_VERBOSE_LOGGING: False,
    }
    _manual_defaults: dict[str, Any] = {
        CONF_DEVICE: "",
        CONF_PORT: 851,
        CONF_IP_ADDRESS: "",
        CONF_VERBOSE_LOGGING: False,
        "scan_legacy_yaml": False,
    }

    def async_get_options_flow(self, config_entry):
        """Return the options flow for this handler."""
        return AdsOptionsFlow(config_entry)

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
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

        return self.async_create_entry(
            title=f"ADS {net_id} (migrated)",
            data={
                CONF_DEVICE: net_id,
                CONF_PORT: port,
                CONF_IP_ADDRESS: ip_address,
                CONF_VERBOSE_LOGGING: import_data.get(CONF_VERBOSE_LOGGING, False),
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the setup entrypoint."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["auto_discovery", "manual", "yaml_import"],
        )

    async def async_step_auto_discovery(self) -> ConfigFlowResult:
        """Handle auto discovery entrypoint."""
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
            subnet = user_input["subnet"]
            scan_limit = user_input["scan_limit"]
            port = user_input[CONF_PORT]

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
            defaults["subnet"] = await self.hass.async_add_executor_job(_guess_local_subnet)

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
            selected_ip = user_input[CONF_IP_ADDRESS]
            net_id = user_input[CONF_DEVICE]
            port = user_input[CONF_PORT]
            verbose_logging = user_input[CONF_VERBOSE_LOGGING]

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
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=f"ADS {net_id}", data=selected_data)

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
                    vol.Required(CONF_PORT, default=self._scan_defaults[CONF_PORT]): int,
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
            entry_data = dict(self._pending_scan_data)
            if user_input["use_legacy_yaml"] and self._yaml_defaults:
                entry_data.update(
                    {
                        CONF_DEVICE: self._yaml_defaults.get(CONF_DEVICE, entry_data[CONF_DEVICE]),
                        CONF_PORT: self._yaml_defaults.get(CONF_PORT, entry_data[CONF_PORT]),
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
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"ADS {net_id}",
                    data={
                        CONF_DEVICE: net_id,
                        CONF_PORT: port,
                        CONF_IP_ADDRESS: ip_address,
                        CONF_VERBOSE_LOGGING: user_input[CONF_VERBOSE_LOGGING],
                    },
                )

        yaml_defaults = self._yaml_defaults or {}
        return self.async_show_form(
            step_id="manual",
            data_schema=self._user_data_schema(self._manual_form_defaults(yaml_defaults)),
            errors=errors,
        )

    async def async_step_yaml_import(self) -> ConfigFlowResult:
        """Load the first ADS YAML config that can be found."""
        self._yaml_defaults = await self.hass.async_add_executor_job(
            _discover_yaml_ads_config, self.hass.config.config_dir
        )
        return await self.async_step_manual()

    @staticmethod
    def _user_data_schema(defaults: Mapping[str, Any]) -> vol.Schema:
        """Build the setup form schema with optional YAML defaults."""
        return vol.Schema(
            {
                vol.Required(CONF_DEVICE, default=defaults.get(CONF_DEVICE, "")): str,
                vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, 851)): int,
                vol.Optional(
                    CONF_IP_ADDRESS, default=defaults.get(CONF_IP_ADDRESS, "")
                ): str,
                vol.Required(
                    CONF_VERBOSE_LOGGING,
                    default=defaults.get(CONF_VERBOSE_LOGGING, False),
                ): bool,
                vol.Optional("scan_legacy_yaml", default=False): bool,
            }
        )

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


def _validate_ads_connection(net_id: str, port: int, ip_address: str | None) -> bool:
    """Validate ADS connection parameters by opening and closing connection."""
    connection = pyads.Connection(net_id, port, ip_address)

    try:
        connection.open()
        return True
    except pyads.ADSError:
        return False
    finally:
        try:
            connection.close()
        except pyads.ADSError:
            pass


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

    for yaml_path in sorted((*config_path.rglob("*.yaml"), *config_path.rglob("*.yml"))):
        try:
            loaded_config = load_yaml_config_file(str(yaml_path))
        except (FileNotFoundError, HomeAssistantError, OSError):
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
        except (TypeError, ValueError):
            continue

        return {
            CONF_DEVICE: net_id,
            CONF_PORT: port_int,
            CONF_IP_ADDRESS: ads_config.get(CONF_IP_ADDRESS) or None,
            CONF_VERBOSE_LOGGING: bool(ads_config.get(CONF_VERBOSE_LOGGING, False)),
        }

    return None


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
