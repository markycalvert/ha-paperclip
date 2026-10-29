"""Conversation agents: talk to one Paperclip agent from Assist."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, override

from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.helpers import intent
from homeassistant.helpers.translation import async_get_translations

from .api import Agent, PaperclipError
from .const import (
    CONF_REPLY_WAIT,
    DEFAULT_REPLY_WAIT,
    DOMAIN,
    LOGGER,
    TITLE_MAX_LENGTH,
)
from .coordinator import PaperclipCoordinator, shorten
from .entity import PaperclipAgentEntity, add_agent_entities

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import Comment
    from .data import PaperclipConfigEntry

PARALLEL_UPDATES = 0
ASSIST_FOOTER = "\n\n_Sent from Home Assistant Assist._"

FALLBACK_TEXT = {
    "reply_pending": "Sent to {agent} as {identifier}. The reply will be in Paperclip.",
    "conversation_failed": "Sorry, I could not reach Paperclip: {error}",
}


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: PaperclipConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one conversation agent per Paperclip agent."""

    def _factory(
        coordinator: PaperclipCoordinator, company_device_id: str, agent: Agent
    ) -> list[Entity]:
        return [PaperclipConversationEntity(coordinator, company_device_id, agent)]

    add_agent_entities(entry, async_add_entities, _factory)


async def _async_text(
    hass: HomeAssistant, language: str | None, key: str, **placeholders: str
) -> str:
    """Return a translated reply sentence."""
    template = FALLBACK_TEXT[key]
    for lang in dict.fromkeys((language or hass.config.language, "en")):
        translations = await async_get_translations(hass, lang, "exceptions", {DOMAIN})
        if found := translations.get(f"component.{DOMAIN}.exceptions.{key}.message"):
            template = found
            break
    try:
        return template.format(**placeholders)
    except KeyError:
        return template


class PaperclipConversationEntity(
    PaperclipAgentEntity,
    conversation.ConversationEntity,
):
    """Routes each Assist request to one Paperclip agent as an issue."""

    _attr_name = None

    def __init__(
        self, coordinator: PaperclipCoordinator, company_device_id: str, agent: Agent
    ) -> None:
        """Create the conversation agent (the agent device's main entity)."""
        super().__init__(coordinator, company_device_id, agent, None)

    @property
    @override
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    @override
    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        coordinator = self.coordinator
        options = coordinator.config_entry.options
        agent_name = coordinator.agent_name(self.agent_id)
        conversation_id = chat_log.conversation_id
        try:
            issue_id = coordinator.tracker.conversation_issue(
                conversation_id, self.agent_id
            )
            if issue_id is not None:
                tracked, comment = await coordinator.async_comment(
                    issue_id, user_input.text
                )
                after: Comment | None = comment
            else:
                tracked, _ = await coordinator.async_create_tracked_issue(
                    title=shorten(user_input.text, TITLE_MAX_LENGTH),
                    description=user_input.text + ASSIST_FOOTER,
                    agent_id=self.agent_id,
                    project_id=await self._async_default_project_id(),
                    priority=None,
                    allow_duplicate=True,
                )
                coordinator.tracker.link_conversation(
                    conversation_id, tracked.id, self.agent_id
                )
                after = None
        except PaperclipError as err:
            LOGGER.warning("Could not send the request to Paperclip: %s", err)
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                await _async_text(
                    self.hass,
                    user_input.language,
                    "conversation_failed",
                    error=str(err),
                ),
            )
            return conversation.ConversationResult(
                response=response, conversation_id=conversation_id
            )

        reply = await coordinator.async_wait_for_reply(
            tracked,
            options.get(CONF_REPLY_WAIT, DEFAULT_REPLY_WAIT),
            after=after,
        )
        if reply is not None:
            text = reply.body
        else:
            text = await _async_text(
                self.hass,
                user_input.language,
                "reply_pending",
                agent=agent_name,
                identifier=tracked.identifier or tracked.id,
            )
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=text)
        )
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _async_default_project_id(self) -> str | None:
        """
        Resolve the default project option to a project id.

        A default that no longer resolves (the project was deleted or renamed)
        must not silence the voice assistant, so the issue is then created
        without a project.
        """
        coordinator = self.coordinator
        if (default := coordinator.default_project) is None:
            return None
        project_id = await coordinator.async_resolve_project(default)
        if project_id is None:
            LOGGER.warning(
                "Default project %s of %s was not found; creating the issue "
                "without a project",
                default,
                coordinator.config_entry.title,
            )
        return project_id
