"""Tests for the Paperclip config, reauth, reconfigure and options flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_API_KEY,
    CONF_URL,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.paperclip.const import (
    CONF_ALLOW_APPROVALS,
    CONF_COMPANY_ID,
    CONF_DEFAULT_PROJECT,
    CONF_REPLY_WAIT,
    DOMAIN,
)

from .conftest import API_KEY, BASE_URL, COMPANY_ID, PROJECT_ID, FakePaperclip

USER_INPUT = {CONF_URL: BASE_URL + "/", CONF_API_KEY: API_KEY, CONF_VERIFY_SSL: True}
SECOND_COMPANY = {
    "id": "11111111-1111-4111-8111-111111111112",
    "name": "Acme Labs",
    "status": "active",
    "issuePrefix": "LABS",
}


async def test_user_flow_single_company(
    hass: HomeAssistant, fake: FakePaperclip
) -> None:
    """One visible company creates the entry straight away."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Acme"
    assert result["result"].unique_id == COMPANY_ID
    assert result["data"] == {
        CONF_URL: BASE_URL,
        CONF_API_KEY: API_KEY,
        CONF_VERIFY_SSL: True,
        CONF_COMPANY_ID: COMPANY_ID,
    }
    # The companies call asks only for companies the key can open.
    assert fake.calls_to("GET", "/api/companies")[0][2] == {"scope": "accessible"}


async def test_user_flow_without_key(hass: HomeAssistant, fake: FakePaperclip) -> None:
    """A local_trusted server needs no key, and none is sent."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_VERIFY_SSL: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] == ""


async def test_user_flow_company_picker(
    hass: HomeAssistant, fake: FakePaperclip
) -> None:
    """Several companies lead to a picker step."""
    fake.companies.append(SECOND_COMPANY)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "company"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COMPANY_ID: SECOND_COMPANY["id"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Acme Labs"
    assert result["result"].unique_id == SECOND_COMPANY["id"]


async def test_company_picker_all_configured(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """The picker aborts when every visible company is already set up."""
    mock_config_entry.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, unique_id=SECOND_COMPANY["id"]).add_to_hass(hass)
    fake.companies.append(SECOND_COMPANY)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_already_configured(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """The same company cannot be added twice."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("route", "status", "message", "error"),
    [
        ("/api/health", 503, "database_unreachable", "cannot_connect"),
        ("/api/health", 401, "Agent token did not verify", "invalid_auth"),
        ("/api/companies", 403, "Board access required", "invalid_auth"),
        (
            "/api/health",
            403,
            "This hostname is not allowed for this Paperclip instance.",
            "hostname_not_allowed",
        ),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant,
    fake: FakePaperclip,
    route: str,
    status: int,
    message: str,
    error: str,
) -> None:
    """API failures map to form errors, and the flow can recover."""
    fake.fail("GET", route, status, message)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    fake.clear_failures()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("user_input", "error"),
    [
        ({CONF_URL: "paperclip.local:3100", CONF_VERIFY_SSL: True}, "invalid_url"),
        (
            {
                CONF_URL: BASE_URL,
                CONF_API_KEY: "pcp_" + "1" * 48,
                CONF_VERIFY_SSL: True,
            },
            "agent_key",
        ),
    ],
)
async def test_user_flow_input_errors(
    hass: HomeAssistant,
    fake: FakePaperclip,
    user_input: dict[str, Any],
    error: str,
) -> None:
    """Bad URLs and agent keys are rejected before calling the server."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["errors"] == {"base": error}
    assert not fake.calls


async def test_user_flow_no_companies_and_unknown(
    hass: HomeAssistant, fake: FakePaperclip
) -> None:
    """An empty company list and unexpected errors are reported."""
    fake.companies = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "no_companies"}

    with patch(
        "custom_components.paperclip.config_flow.PaperclipClient.get_health",
        side_effect=RuntimeError,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["errors"] == {"base": "unknown"}


async def test_reauth_flow(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """Reauth stores the new key on the existing entry."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    fake.fail("GET", "/api/health", 401, "Agent token did not verify")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "pcp_board_" + "2" * 48}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    fake.clear_failures()
    new_key = "pcp_board_" + "3" * 48
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: new_key}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_API_KEY] == new_key
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauth_wrong_company(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """A key that cannot open the entry's company is refused."""
    mock_config_entry.add_to_hass(hass)
    fake.companies = [SECOND_COMPANY]
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY}
    )
    assert result["errors"] == {"base": "company_not_found"}


async def test_reconfigure_flow(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure updates the URL and keeps the key when it is left empty."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    fake.fail("GET", "/api/health", 503, "database_unreachable")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_VERIFY_SSL: True}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    fake.clear_failures()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL + "/", CONF_VERIFY_SSL: False}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_URL] == BASE_URL
    assert mock_config_entry.data[CONF_API_KEY] == API_KEY
    assert mock_config_entry.data[CONF_VERIFY_SSL] is False
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_wrong_company(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure refuses a server where the company is missing."""
    mock_config_entry.add_to_hass(hass)
    fake.companies = [SECOND_COMPANY]
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_VERIFY_SSL: True}
    )
    assert result["errors"] == {"base": "company_not_found"}


async def test_options_flow(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """Options are stored as integers and offer the company's projects."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_REPLY_WAIT: 45.0,
            CONF_ALLOW_APPROVALS: True,
            CONF_DEFAULT_PROJECT: PROJECT_ID,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        CONF_REPLY_WAIT: 45,
        CONF_ALLOW_APPROVALS: True,
        CONF_DEFAULT_PROJECT: PROJECT_ID,
    }


async def test_options_flow_resolves_typed_project(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """A typed project name is stored as its id; unknown names are refused."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    options = {
        CONF_REPLY_WAIT: 20,
        CONF_ALLOW_APPROVALS: False,
    }
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**options, CONF_DEFAULT_PROJECT: "Garden"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DEFAULT_PROJECT: "project_not_found"}

    fake.fail("GET", f"/api/companies/{COMPANY_ID}/projects", 500, "boom")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**options, CONF_DEFAULT_PROJECT: "Garden"}
    )
    assert result["errors"] == {CONF_DEFAULT_PROJECT: "cannot_connect"}
    fake.clear_failures()

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**options, CONF_DEFAULT_PROJECT: " household "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_DEFAULT_PROJECT] == PROJECT_ID


async def test_options_flow_typed_name_needs_a_loaded_entry(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """Without a loaded entry only a project id or no project can be stored."""
    mock_config_entry.add_to_hass(hass)
    options = {
        CONF_REPLY_WAIT: 20,
        CONF_ALLOW_APPROVALS: False,
    }
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**options, CONF_DEFAULT_PROJECT: "Household"}
    )
    assert result["errors"] == {CONF_DEFAULT_PROJECT: "project_not_found"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**options, CONF_DEFAULT_PROJECT: "none"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_DEFAULT_PROJECT] == "none"
