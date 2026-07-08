"""Tests for the config, reauth, and options flows."""

from unittest.mock import patch

import pytest
from conftest import FakeRemoteClient
from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_meter_izar.const import (
    CONF_DELETE_AFTER,
    CONF_DIRECTORY,
    CONF_FILE_PATTERN,
    CONF_POLL_INTERVAL,
    CONF_PROTOCOL,
    CONF_REQUIRE_RDY,
    DOMAIN,
)
from custom_components.energy_meter_izar.ftp_client import FetchAuthError, FetchError

USER_INPUT = {
    CONF_PROTOCOL: "ftp",
    CONF_HOST: "gateway.local",
    CONF_USERNAME: "mbus",
    CONF_PASSWORD: "secret",
    CONF_DIRECTORY: "/snapshots",
}

ENTRY_DATA = {**USER_INPUT, CONF_PORT: 21}


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    return


@pytest.fixture(autouse=True)
def mock_setup_entry():
    with patch(
        "custom_components.energy_meter_izar.async_setup_entry", return_value=True
    ) as mock:
        yield mock


def _patch_client(client):
    return patch(
        "custom_components.energy_meter_izar.config_flow.create_client",
        return_value=client,
    )


async def test_user_flow_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    with _patch_client(FakeRemoteClient()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "gateway.local (ftp)"
    assert result["data"] == ENTRY_DATA  # port defaulted to 21 for FTP
    assert result["result"].unique_id == "ftp://gateway.local:21/snapshots"


async def test_user_flow_sftp_default_port(hass):
    with _patch_client(FakeRemoteClient()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_PROTOCOL: "sftp"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PORT] == 22


@pytest.mark.parametrize(
    ("connect_error", "expected"),
    [
        (FetchAuthError("530 login"), "invalid_auth"),
        (FetchError("no route"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_and_recovery(hass, connect_error, expected):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with _patch_client(FakeRemoteClient(connect_error=connect_error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    with _patch_client(FakeRemoteClient()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_server_aborts(hass):
    MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="ftp://gateway.local:21/snapshots"
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow_updates_credentials(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="ftp://gateway.local:21/snapshots"
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with _patch_client(FakeRemoteClient()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "mbus", CONF_PASSWORD: "new-secret"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"


async def test_options_flow(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="ftp://gateway.local:21/snapshots"
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: 30,
            CONF_FILE_PATTERN: "0080A3DB81A5_*.xml",
            CONF_REQUIRE_RDY: False,
            CONF_DELETE_AFTER: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_POLL_INTERVAL: 30,
        CONF_FILE_PATTERN: "0080A3DB81A5_*.xml",
        CONF_REQUIRE_RDY: False,
        CONF_DELETE_AFTER: True,
    }
