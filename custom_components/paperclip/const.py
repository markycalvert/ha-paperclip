"""Constants for the Paperclip integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

DOMAIN: Final = "paperclip"
LOGGER: Final = logging.getLogger(__package__)
MANUFACTURER: Final = "Paperclip"

# Config entry data
CONF_COMPANY_ID: Final = "company_id"

# Options
CONF_REPLY_WAIT: Final = "reply_wait"
CONF_ALLOW_APPROVALS: Final = "allow_approvals"
CONF_DEFAULT_PROJECT: Final = "default_project"

# Polling interval in seconds. Not an option: users who need other timing
# disable polling for the entry and call homeassistant.update_entity.
DEFAULT_SCAN_INTERVAL: Final = 60
DEFAULT_REPLY_WAIT: Final = 20
MAX_REPLY_WAIT: Final = 300
NO_PROJECT: Final = "none"

# Paperclip server version the integration was tested against. Paperclip
# releases use CalVer (YYYY.MDD.N, betas YYYY.MDD.N-beta.N). Older servers get a
# repair issue; newer ones are assumed to be compatible.
MIN_PAPERCLIP_VERSION: Final = "2026.1006.0-beta.0"
TESTED_PAPERCLIP_VERSION: Final = "2026.1006.0-beta.0"
# Versions a server reports when it cannot tell its real release (the source
# package.json placeholder, or no version at all). They are never compared.
PLACEHOLDER_PAPERCLIP_VERSIONS: Final = frozenset({"0.3.1", "0.0.0"})

# Tracking of issues this integration created or commented on.
STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 5
MAX_TRACKED_ISSUES: Final = 200
TRACK_CLOSED_FOR: Final = timedelta(days=1)
TRACK_AT_MOST: Final = timedelta(days=30)
POLL_COMMENTS_AFTER_CLOSE: Final = timedelta(hours=1)
# Home Assistant drops a chat session after 5 idle minutes, and a turn can wait
# up to MAX_REPLY_WAIT for a reply, so a link outlives any reusable session.
CONVERSATION_EXPIRY: Final = timedelta(minutes=15)
REPLY_POLL_INTERVAL: Final = 2.0
COMMENT_FETCH_CONCURRENCY: Final = 4
ISSUE_PAGE_SIZE: Final = 500
# Issue updates are listed from this far before the cursor: Paperclip stamps
# updatedAt before a transaction commits, so a change can appear after a later
# one was already listed. Re-reading is harmless (statuses are diffed).
ISSUE_UPDATE_OVERLAP: Final = timedelta(seconds=60)
MAX_ISSUE_PAGES: Final = 10
# An optional route that answered Paperclip's own 404 is tried again after this.
UNSUPPORTED_ROUTE_RETRY: Final = timedelta(hours=1)
ISSUE_CACHE_SIZE: Final = 256

TITLE_MAX_LENGTH: Final = 60
EVENT_TITLE_MAX_LENGTH: Final = 120
REPLY_MAX_LENGTH: Final = 2000

# Paperclip enums (packages/shared/src/constants.ts). The agent list leaves out
# terminated agents (their devices are removed), so no sensor reports it.
AGENT_STATUSES: Final = [
    "active",
    "idle",
    "running",
    "paused",
    "error",
    "pending_approval",
]
ISSUE_PRIORITIES: Final = ["critical", "high", "medium", "low"]
CLOSED_ISSUE_STATUSES: Final = frozenset({"done", "cancelled"})
AGENT_ROLE_LABELS: Final = {
    "ceo": "CEO",
    "cto": "CTO",
    "cmo": "CMO",
    "cfo": "CFO",
    "security": "Security",
    "engineer": "Engineer",
    "designer": "Designer",
    "pm": "PM",
    "qa": "QA",
    "devops": "DevOps",
    "researcher": "Researcher",
    "general": "General",
}

# Bus events
EVENT_APPROVAL_REQUESTED: Final = "paperclip_approval_requested"
EVENT_ISSUE_STATUS_CHANGED: Final = "paperclip_issue_status_changed"
EVENT_AGENT_STATUS_CHANGED: Final = "paperclip_agent_status_changed"
EVENT_AGENT_REPLIED: Final = "paperclip_agent_replied"

# Actions
SERVICE_ASK_AGENT: Final = "ask_agent"
SERVICE_ADD_COMMENT: Final = "add_comment"
SERVICE_CREATE_ISSUE: Final = "create_issue"
SERVICE_WAKE_AGENT: Final = "wake_agent"
SERVICE_FIRE_ROUTINE_WEBHOOK: Final = "fire_routine_webhook"
SERVICE_APPROVE: Final = "approve"
SERVICE_REJECT: Final = "reject"

ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_AGENT: Final = "agent"
ATTR_ASSIGNEE: Final = "assignee"
ATTR_MESSAGE: Final = "message"
ATTR_TITLE: Final = "title"
ATTR_DESCRIPTION: Final = "description"
ATTR_PROJECT: Final = "project"
ATTR_PRIORITY: Final = "priority"
ATTR_WAIT_FOR_REPLY: Final = "wait_for_reply"
ATTR_ALLOW_DUPLICATE: Final = "allow_duplicate"
ATTR_ISSUE: Final = "issue"
ATTR_REASON: Final = "reason"
ATTR_PUBLIC_ID: Final = "public_id"
ATTR_SECRET: Final = "secret"  # noqa: S105 - a field name, not a secret
ATTR_PAYLOAD: Final = "payload"
ATTR_IDEMPOTENCY_KEY: Final = "idempotency_key"
ATTR_APPROVAL_ID: Final = "approval_id"
ATTR_DECISION_NOTE: Final = "decision_note"
