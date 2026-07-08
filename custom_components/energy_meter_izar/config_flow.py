"""Config and options flow for the Energy Meter IZAR integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_DELETE_AFTER,
    CONF_DIRECTORY,
    CONF_FILE_PATTERN,
    CONF_POLL_INTERVAL,
    CONF_PROTOCOL,
    CONF_REQUIRE_RDY,
    DEFAULT_DELETE_AFTER,
    DEFAULT_DIRECTORY,
    DEFAULT_FILE_PATTERN,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_PORT_FTP,
    DEFAULT_PORT_SFTP,
    DEFAULT_REQUIRE_RDY,
    DOMAIN,
    PROTOCOL_SFTP,
    PROTOCOLS,
)
from .ftp_client import ConnectionConfig, FetchAuthError, FetchError, create_client

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PROTOCOL, default=PROTOCOLS[0]): SelectSelector(
            SelectSelectorConfig(
                options=PROTOCOLS,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key=CONF_PROTOCOL,
            )
        ),
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_DIRECTORY, default=DEFAULT_DIRECTORY): str,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL_MINUTES
        ): NumberSelector(
            NumberSelectorConfig(
                min=1, max=1440, step=1, mode=NumberSelectorMode.BOX,
                unit_of_measurement="min",
            )
        ),
        vol.Required(CONF_FILE_PATTERN, default=DEFAULT_FILE_PATTERN): str,
        vol.Required(CONF_REQUIRE_RDY, default=DEFAULT_REQUIRE_RDY): bool,
        vol.Required(CONF_DELETE_AFTER, default=DEFAULT_DELETE_AFTER): bool,
    }
)


def _default_port(protocol: str) -> int:
    return DEFAULT_PORT_SFTP if protocol == PROTOCOL_SFTP else DEFAULT_PORT_FTP


async def _validate_connection(data: dict[str, Any]) -> str | None:
    """Try to connect and list the remote directory; return an error key."""
    client = create_client(
        ConnectionConfig(
            protocol=data[CONF_PROTOCOL],
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            directory=data[CONF_DIRECTORY],
        )
    )
    try:
        await client.connect()
        await client.list_files()
    except FetchAuthError:
        return "invalid_auth"
    except FetchError as err:
        _LOGGER.debug("connection validation failed: %s", err)
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("unexpected error validating connection")
        return "unknown"
    finally:
        await client.close()
    return None


class IzarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect and validate the file-server connection."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial setup step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_PORT) is None:
                user_input[CONF_PORT] = _default_port(user_input[CONF_PROTOCOL])
            user_input[CONF_PORT] = int(user_input[CONF_PORT])

            unique_id = (
                f"{user_input[CONF_PROTOCOL]}://{user_input[CONF_HOST]}:"
                f"{user_input[CONF_PORT]}{user_input[CONF_DIRECTORY]}"
            )
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            error = await _validate_connection(user_input)
            if error is None:
                return self.async_create_entry(
                    title=f"{user_input[CONF_HOST]} ({user_input[CONF_PROTOCOL]})",
                    data=user_input,
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The server rejected our credentials — ask for new ones."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate and store the new credentials."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**reauth_entry.data, **user_input}
            error = await _validate_connection(data)
            if error is None:
                return self.async_update_reload_and_abort(reauth_entry, data=data)
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_REAUTH_SCHEMA,
                {CONF_USERNAME: reauth_entry.data[CONF_USERNAME]},
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> IzarOptionsFlow:
        """Return the options flow handler."""
        return IzarOptionsFlow()


class IzarOptionsFlow(OptionsFlow):
    """Edit poll interval and file-handling options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_POLL_INTERVAL] = int(user_input[CONF_POLL_INTERVAL])
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
