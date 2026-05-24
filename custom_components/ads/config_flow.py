"""Config flow for ADS integration."""

from __future__ import annotations

from typing import Any

import pyads
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_DEVICE, CONF_IP_ADDRESS, CONF_PORT
from homeassistant.core import callback
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

    def async_get_options_flow(self, config_entry):
        """Return the options flow for this handler."""
        return AdsOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
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

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE): str,
                    vol.Required(CONF_PORT, default=851): int,
                    vol.Optional(CONF_IP_ADDRESS): str,
                    vol.Required(CONF_VERBOSE_LOGGING, default=False): bool,
                }
            ),
            errors=errors,
        )


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
