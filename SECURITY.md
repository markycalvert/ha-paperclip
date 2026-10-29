# Security policy

## Supported versions

Only the latest release of this integration receives fixes.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Use GitHub's
private vulnerability reporting instead: open the **Security** tab of this
repository and choose **Report a vulnerability**.

Include what you found, how to reproduce it, and the integration, Home
Assistant and Paperclip versions. You should get a first answer within a week.

Problems in the Paperclip server itself belong to the
[Paperclip project](https://github.com/paperclipai/paperclip/security).

## What this integration stores and sends

- The board API key and server URL are stored in Home Assistant's config entry
  storage (`.storage/core.config_entries`), like every other integration's
  credentials. They are redacted from diagnostics.
- The key is sent only to the Paperclip server you configured, as an
  `Authorization: Bearer` header. It is never sent with routine webhooks.
- Routine webhook secrets are passed in the action call and used only to sign
  that request. They are not stored by the integration.
- Issue ids, titles, statuses and conversation-to-issue links are stored in
  `.storage/paperclip.<entry id>` so replies can be matched after a restart.
- Agent reply text (up to 2000 characters) is put on Home Assistant's event bus
  as the `body` of `paperclip_agent_replied`, so the recorder stores it in the
  event history unless that event is excluded. Actions that return a reply
  also keep it in their automation or script trace.

See "Security notes" in the [README](README.md#security-notes) for the
permissions a board key carries.
