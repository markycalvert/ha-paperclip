"""Config flow for the Paperclip integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

import probatio
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_API_KEY,
    CONF_URL,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from yarl import URL

from .api import (
    Company,
    PaperclipAuthError,
    PaperclipClient,
    PaperclipError,
    PaperclipHostnameNotAllowedError,
    is_agent_key,
)
from .const import (
    CONF_ALLOW_APPROVALS,
    CONF_COMPANY_ID,
    CONF_DEFAULT_PROJECT,
    CONF_NOTIFY_LATE_REPLIES,
    CONF_REPLY_WAIT,
    DEFAULT_NOTIFY_LATE_REPLIES,
    DEFAULT_REPLY_WAIT,
    DOMAIN,
    LOGGER,
    MAX_ASSIST_REPLY_WAIT,
    NO_PROJECT,
)
from .coordinator import UUID_RE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .api import Project
    from .coordinator import PaperclipCoordinator
    from .data import PaperclipConfigEntry

EXAMPLE_URL = "http://paperclip.local:3100"
ERR_AGENT_KEY = "agent_key"
ERR_CANNOT_CONNECT = "cannot_connect"
ERR_COMPANY_NOT_FOUND = "company_not_found"
ERR_HOSTNAME_NOT_ALLOWED = "hostname_not_allowed"
ERR_INVALID_AUTH = "invalid_auth"
ERR_INVALID_URL = "invalid_url"
ERR_NO_COMPANIES = "no_companies"
ERR_PROJECT_NOT_FOUND = "project_not_found"
ERR_UNKNOWN = "unknown"


class _FlowError(Exception):
    """A validation failure that maps to a translated form error."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _normalise_url(raw: str) -> str:
    url = URL(raw.strip())
    if url.scheme not in ("http", "https") or not url.host:
        raise _FlowError(ERR_INVALID_URL)
    return str(url.with_query(None).with_fragment(None)).rstrip("/")


def _connection_schema(defaults: Mapping[str, Any]) -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_URL): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            probatio.Optional(CONF_API_KEY): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            probatio.Required(
                CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)
            ): BooleanSelector(),
        }
    )


class PaperclipConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Paperclip company."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._data: dict[str, Any] = {}
        self._companies: list[Company] = []

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: PaperclipConfigEntry,
    ) -> PaperclipOptionsFlow:
        return PaperclipOptionsFlow()

    async def _async_validate(self, user_input: Mapping[str, Any]) -> dict[str, Any]:
        """Check the server and key; return entry data (without company)."""
        url = _normalise_url(user_input[CONF_URL])
        api_key = (user_input.get(CONF_API_KEY) or "").strip()
        if api_key and is_agent_key(api_key):
            raise _FlowError(ERR_AGENT_KEY)
        verify_ssl = bool(user_input.get(CONF_VERIFY_SSL, True))
        client = PaperclipClient(
            async_get_clientsession(self.hass, verify_ssl=verify_ssl), url, api_key
        )
        try:
            await client.get_health()
            self._companies = await client.list_companies()
        except PaperclipHostnameNotAllowedError as err:
            raise _FlowError(ERR_HOSTNAME_NOT_ALLOWED) from err
        except PaperclipAuthError as err:
            raise _FlowError(ERR_INVALID_AUTH) from err
        except PaperclipError as err:
            LOGGER.debug("Cannot connect to Paperclip at %s: %s", url, err)
            raise _FlowError(ERR_CANNOT_CONNECT) from err
        except Exception as err:
            LOGGER.exception("Unexpected error validating Paperclip")
            raise _FlowError(ERR_UNKNOWN) from err
        if not self._companies:
            raise _FlowError(ERR_NO_COMPANIES)
        return {CONF_URL: url, CONF_API_KEY: api_key, CONF_VERIFY_SSL: verify_ssl}

    async def _async_create(self, company: Company) -> ConfigFlowResult:
        await self.async_set_unique_id(company.id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=company.name,
            data={**self._data, CONF_COMPANY_ID: company.id},
        )

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._data = await self._async_validate(user_input)
            except _FlowError as err:
                errors["base"] = err.key
            else:
                if len(self._companies) == 1:
                    return await self._async_create(self._companies[0])
                return await self.async_step_company()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _connection_schema({}), user_input or {}
            ),
            errors=errors,
            description_placeholders={"example_url": EXAMPLE_URL},
        )

    async def async_step_company(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the company when the key can see more than one."""
        if user_input is not None:
            company = next(
                item
                for item in self._companies
                if item.id == user_input[CONF_COMPANY_ID]
            )
            return await self._async_create(company)
        configured = {entry.unique_id for entry in self._async_current_entries()}
        options = [
            SelectOptionDict(value=item.id, label=item.name)
            for item in self._companies
            if item.id not in configured
        ]
        if not options:
            return self.async_abort(reason="already_configured")
        return self.async_show_form(
            step_id="company",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_COMPANY_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],  # noqa: ARG002
    ) -> ConfigFlowResult:
        """Start re-authentication after Paperclip rejected the key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new board API key."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                # A blank key means "no key" (Paperclip's local_trusted mode).
                data = await self._async_validate(
                    {**entry.data, CONF_API_KEY: user_input.get(CONF_API_KEY, "")}
                )
                self._require_company(entry.data[CONF_COMPANY_ID])
            except _FlowError as err:
                errors["base"] = err.key
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: data[CONF_API_KEY]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema(
                {
                    probatio.Optional(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "title": entry.title,
                CONF_URL: entry.data[CONF_URL],
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the URL, key or SSL setting of an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                # A blank key keeps the stored one; reauth can clear it.
                data = await self._async_validate(
                    {
                        **user_input,
                        CONF_API_KEY: user_input.get(CONF_API_KEY)
                        or entry.data.get(CONF_API_KEY, ""),
                    }
                )
                self._require_company(entry.data[CONF_COMPANY_ID])
            except _FlowError as err:
                errors["base"] = err.key
            else:
                await self.async_set_unique_id(entry.data[CONF_COMPANY_ID])
                self._abort_if_unique_id_mismatch(reason="wrong_company")
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _connection_schema(entry.data),
                {CONF_URL: entry.data[CONF_URL], **(user_input or {})},
            ),
            errors=errors,
            description_placeholders={"example_url": EXAMPLE_URL},
        )

    def _require_company(self, company_id: str) -> None:
        if not any(item.id == company_id for item in self._companies):
            raise _FlowError(ERR_COMPANY_NOT_FOUND)


class PaperclipOptionsFlow(OptionsFlowWithReload):
    """Tune replies, approvals and the default project."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        entry: PaperclipConfigEntry = self.config_entry
        coordinator: PaperclipCoordinator | None = getattr(
            getattr(entry, "runtime_data", None), "coordinator", None
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            project = await self._async_validate_project(
                coordinator, user_input.get(CONF_DEFAULT_PROJECT), errors
            )
            if not errors:
                return self.async_create_entry(
                    data={
                        **user_input,
                        CONF_REPLY_WAIT: int(user_input[CONF_REPLY_WAIT]),
                        CONF_DEFAULT_PROJECT: project,
                    }
                )

        project_options = [SelectOptionDict(value=NO_PROJECT, label=NO_PROJECT)]
        projects: list[Project] = []
        if coordinator is not None:
            try:
                projects = await coordinator.client.list_projects(
                    coordinator.company_id
                )
            except PaperclipError:
                projects = []
        project_options.extend(
            SelectOptionDict(value=item.id, label=item.name) for item in projects
        )
        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_REPLY_WAIT, default=DEFAULT_REPLY_WAIT
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=MAX_ASSIST_REPLY_WAIT,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                probatio.Required(
                    CONF_NOTIFY_LATE_REPLIES, default=DEFAULT_NOTIFY_LATE_REPLIES
                ): BooleanSelector(),
                probatio.Required(
                    CONF_ALLOW_APPROVALS, default=False
                ): BooleanSelector(),
                probatio.Required(
                    CONF_DEFAULT_PROJECT, default=NO_PROJECT
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=project_options,
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                        translation_key=CONF_DEFAULT_PROJECT,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or entry.options
            ),
            errors=errors,
        )

    @staticmethod
    async def _async_validate_project(
        coordinator: PaperclipCoordinator | None,
        value: Any,
        errors: dict[str, str],
    ) -> str:
        """
        Turn the default project field into a project id, or NO_PROJECT.

        The dropdown also takes typed text (for when the project list could
        not be read). Paperclip only accepts a project UUID, so a typed name is
        resolved here and an unknown one is refused.
        """
        project = str(value or "").strip()
        if not project or project == NO_PROJECT:
            return NO_PROJECT
        if UUID_RE.match(project):
            return project
        if coordinator is None:
            errors[CONF_DEFAULT_PROJECT] = ERR_PROJECT_NOT_FOUND
            return project
        try:
            project_id = await coordinator.async_resolve_project(project)
        except PaperclipError:
            errors[CONF_DEFAULT_PROJECT] = ERR_CANNOT_CONNECT
            return project
        if project_id is None:
            errors[CONF_DEFAULT_PROJECT] = ERR_PROJECT_NOT_FOUND
            return project
        return project_id
