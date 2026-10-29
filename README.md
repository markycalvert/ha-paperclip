# Paperclip for Home Assistant

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=markycalvert&repository=ha-paperclip&category=integration)

[![Release](https://img.shields.io/github/v/release/markycalvert/ha-paperclip)](https://github.com/markycalvert/ha-paperclip/releases)
[![Validate](https://github.com/markycalvert/ha-paperclip/actions/workflows/validate.yml/badge.svg)](https://github.com/markycalvert/ha-paperclip/actions/workflows/validate.yml)
[![Tests](https://github.com/markycalvert/ha-paperclip/actions/workflows/tests.yml/badge.svg)](https://github.com/markycalvert/ha-paperclip/actions/workflows/tests.yml)
[![Lint](https://github.com/markycalvert/ha-paperclip/actions/workflows/lint.yml/badge.svg)](https://github.com/markycalvert/ha-paperclip/actions/workflows/lint.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
![Home Assistant 2026.10+](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-41BDF5)

A Home Assistant integration for [Paperclip](https://github.com/paperclipai/paperclip),
the open-source platform that runs a company of AI agents. It shows your
Paperclip agents and their work in Home Assistant, lets automations hand work to
a chosen agent, and turns every agent into an Assist conversation agent you can
talk to.

> This is an unofficial, community integration. It is not affiliated with or
> endorsed by the Paperclip project.

## Contents

- [Features](#features)
- [The agent picker](#the-agent-picker)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Entities](#entities)
- [Actions](#actions)
- [Events](#events)
- [How data is updated](#how-data-is-updated)
- [Security notes](#security-notes)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Removal](#removal)
- [Compatibility](#compatibility)
- [Contributing](#contributing)

## Features

- **One device per agent**, linked to a device for the company, with the
  agent's status, current task, a pause/resume switch and a wake button.
- **Company sensors**: server connectivity, open issues, pending approvals,
  active runs and spend this month.
- **Agent picker**: send a request from Home Assistant to the agent you choose,
  from an automation (`paperclip.ask_agent`) or by voice (one Assist
  conversation agent per Paperclip agent).
- **Replies come back**: wait for the agent's answer in the action response,
  hear it spoken by Assist, or react to the `paperclip_agent_replied` event.
- **Events** for new approvals, issue status changes and agent status changes.
- **Actions** to create issues, comment, wake agents, fire routine webhooks
  (HMAC-signed) and, if you allow it, approve or reject approvals.
- UI setup, re-authentication, reconfiguration, diagnostics with secrets
  redacted, and a repair issue when the Paperclip server is too old.

## The agent picker

Paperclip agents have roles: a CEO who plans, an engineer who writes code, a
"Home Ops" agent you might set up to look after the house. The integration lets
you choose **which agent** a request from Home Assistant goes to. A request
becomes a Paperclip issue assigned to that agent; Paperclip wakes the agent;
the agent's comment on the issue is its reply.

### Talk to an agent with Assist

Each Paperclip agent appears as a conversation agent named after it (for
example "Home Ops").

1. Go to **Settings → Voice assistants** and add (or edit) an assistant.
2. Under **Conversation agent**, pick the Paperclip agent, for example
   **Home Ops**.
3. Talk to that assistant from the Assist dialog, a voice satellite or the
   companion app.

What happens:

- The **first message** of a conversation creates an issue assigned to the
  agent (title: the first 60 characters of what you said).
- **Follow-ups in the same conversation** are added as comments on that same
  issue, which wakes the agent again. Home Assistant keeps a conversation only
  while it is in use: after about 5 minutes without a message, the next
  message starts a new conversation, and so a new issue. Within those
  minutes, even an unrelated request to the same agent from the same device
  is added to the open issue.
- Assist waits up to the **conversation reply wait** (default 20 s) for the
  agent's comment and speaks it. If the agent is slower, Assist answers
  "Sent to Home Ops as ACME-12. The reply will be in Paperclip." The reply
  then arrives as a [`paperclip_agent_replied`](#paperclip_agent_replied)
  event. Nothing tells you about it unless you add an automation such as
  [Get late replies as notifications](#get-late-replies-as-notifications).
- To answer a late reply, use `paperclip.add_comment` with the issue
  identifier from the event (see the example below). Speaking to Assist again
  after the conversation ended starts a new issue without the earlier context.

You can create several assistants, each with a different Paperclip agent, and
give each its own wake word.

### Ask an agent from an automation

```yaml
automation:
  - alias: "Ask Home Ops when the garage is left open"
    triggers:
      - trigger: state
        entity_id: cover.garage_door
        to: "open"
        for: "00:30:00"
    actions:
      - action: paperclip.ask_agent
        data:
          agent: Home Ops # or pick the agent device in the UI
          message: >
            The garage door has been open for 30 minutes and nobody is home.
            Decide whether to close it and tell me what you did.
          priority: high
          wait_for_reply: 60
        response_variable: answer
      - if: "{{ answer.replied }}"
        then:
          - action: notify.mobile_app_phone
            data:
              title: "Home Ops ({{ answer.identifier }})"
              message: "{{ answer.reply }}"
```

### Get late replies as notifications

```yaml
automation:
  - alias: "Paperclip replies to my phone"
    triggers:
      - trigger: event
        event_type: paperclip_agent_replied
        event_data:
          delivered: false # skip replies already spoken by Assist or returned to an action
    actions:
      - action: notify.mobile_app_phone
        data:
          title: "{{ trigger.event.data.agent_name }} replied on {{ trigger.event.data.identifier }}"
          message: "{{ trigger.event.data.body[:500] }}"
          data:
            url: "{{ trigger.event.data.url }}"
            actions:
              - action: "PAPERCLIP_REPLY_{{ trigger.event.data.identifier }}"
                title: Reply
                behavior: textInput

  - alias: "Answer a Paperclip reply from the notification"
    triggers:
      - trigger: event
        event_type: mobile_app_notification_action
    conditions:
      - condition: template
        value_template: "{{ trigger.event.data.action.startswith('PAPERCLIP_REPLY_') }}"
    actions:
      - action: paperclip.add_comment
        data:
          config_entry_id: 01J0000000000000000000000 # your Paperclip entry
          issue: "{{ trigger.event.data.action[16:] }}"
          message: "{{ trigger.event.data.reply_text }}"
```

The comment continues the same issue, so the agent sees the whole thread.

## Requirements

- Home Assistant **2026.10** or newer.
- A Paperclip server that Home Assistant can reach, for example
  `http://paperclip.local:3100`.
- A **board API key** (it starts with `pcp_board_`) for a user who is a member
  of the company, with a role other than `viewer` if you want to create issues
  or pause agents. Agent API keys are not supported.

  Paperclip has no web page for board keys. Create one with the Paperclip CLI
  (Node.js), on any computer that can reach the server. Log in as that user
  first (this opens the browser to approve the login), then create the key:

  ```bash
  npx paperclipai auth login --api-base http://paperclip.local:3100
  npx paperclipai token board create --api-base http://paperclip.local:3100 --name "Home Assistant" --never-expires
  ```

  Use your server's address for `--api-base` (without `/api`). Copy the
  `token` value from the output (it starts with `pcp_board_`). Keys created
  without `--never-expires` expire after 30 days; Home Assistant then asks you
  to re-authenticate.

- A server in Paperclip's `local_trusted` mode needs no key, but that mode only
  listens on the loopback interface, so it only works when Home Assistant runs
  on the same host or behind a proxy.

> **Private hostname guard.** A private Paperclip server only answers requests
> for host names it knows. If setup says the hostname is not allowed, run
> `npx paperclipai allowed-hostname paperclip.local` on the Paperclip server
> (with the host name or IP address you entered in Home Assistant), then
> **restart Paperclip**: it reads the list only when it starts. In Docker, set
> the environment variable `PAPERCLIP_ALLOWED_HOSTNAMES=paperclip.local`
> (comma-separated for several) and recreate the container instead.

## Installation

### HACS (recommended)

1. Click the button below, or in HACS open the three-dot menu, choose
   **Custom repositories**, and add `https://github.com/markycalvert/ha-paperclip`
   with type **Integration**.

   [![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=markycalvert&repository=ha-paperclip&category=integration)

2. Download **Paperclip** and restart Home Assistant.
3. Add the integration:

   [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=paperclip)

   or go to **Settings → Devices & services → Add integration → Paperclip**.

From Home Assistant 2026.11 the built-in Marketplace reads the same catalog;
the button above opens it there.

### Manual

1. Download `paperclip.zip` from the latest GitHub release of this repository
   (or copy the `custom_components/paperclip` folder from it).
2. Unpack it to `<config>/custom_components/paperclip/` so that
   `manifest.json` is at `<config>/custom_components/paperclip/manifest.json`.
3. Restart Home Assistant and add the integration as above.

## Configuration

### Setup

| Field | Required | Description |
|---|---|---|
| Server URL | yes | The address you open Paperclip at, without `/api`, for example `http://paperclip.local:3100` or `http://192.0.2.10:3100`. |
| Board API key | no | A `pcp_board_…` key. Leave empty for a `local_trusted` server. |
| Verify SSL certificate | yes (default on) | Turn off only for an HTTPS server with a self-signed certificate. |
| Company | only if the key sees several | Each Paperclip company becomes its own entry. Add the integration again for another company. |

Change the URL or key later with **Reconfigure** in the integration's menu. If
Paperclip rejects the key, Home Assistant starts a re-authentication flow.

### Options

| Option | Default | Description |
|---|---|---|
| Conversation reply wait | 20 s (0 to 300) | How long an Assist conversation waits for the agent's reply. |
| Allow approvals from Home Assistant | off | Lets `paperclip.approve` and `paperclip.reject` decide approvals. |
| Default project | none | Project for issues created from Home Assistant when an action does not name one. Pick it from the list, or type its name or id; a typed name is checked and stored as the project id. |

## Entities

Entity ids below assume a company called "Acme" and agents called "CEO",
"Engineer" and "Home Ops".

### Company device ("Acme")

| Entity | Example | Notes |
|---|---|---|
| Server (connectivity) | `binary_sensor.acme_server` | On while Paperclip answers and reports `ok`. Diagnostic. |
| Open issues | `sensor.acme_open_issues` | Every status except done and cancelled (includes backlog). |
| Pending approvals | `sensor.acme_pending_approvals` | Approvals waiting for a decision. |
| Active runs | `sensor.acme_active_runs` | Queued plus running agent runs. |
| Spend this month | `sensor.acme_spend_this_month` | USD, current UTC month. A total that resets at the start of each UTC month (`last_reset`), so long-term statistics stay correct. |

The device's firmware version is the Paperclip server version.

### Agent devices ("CEO", "Engineer", "Home Ops")

| Entity | Example | Notes |
|---|---|---|
| Conversation agent | `conversation.home_ops` | Pick it in an Assist pipeline. |
| Status | `sensor.home_ops_status` | `active`, `idle`, `running`, `paused`, `error`, `pending_approval` or `terminated`. |
| Current task | `sensor.engineer_current_task` | Identifier and title of the issue the agent's live run works on, or "No current task". A run without an issue (a timer heartbeat, for example) shows its run status (`queued` or `running`); an issue whose title could not be read yet shows its issue id. Attributes: `run_id`, `run_status`, `issue_id`, `identifier`, `url`. |
| Enabled | `switch.home_ops_enabled` | Off pauses the agent (Paperclip cancels its active runs); on resumes it. |
| Wake | `button.home_ops_wake` | Starts an on-demand run. |
| Spend this month | `sensor.home_ops_spend_this_month` | Disabled by default. |
| Last heartbeat | `sensor.home_ops_last_heartbeat` | Diagnostic, disabled by default. |

The device model is the agent's title, or its role. New agents appear
automatically; agents that are terminated in Paperclip lose their device.

## Actions

Actions that act on a company or server (`create_issue`, `add_comment`,
`fire_routine_webhook`, `approve`, `reject`) need `config_entry_id`: pick the
Paperclip entry in the UI, or use the entry id in YAML. There is no default, so
adding a second company later cannot change what an automation does.
`ask_agent` and `wake_agent` target an agent instead; for them
`config_entry_id` is optional and only tells apart agents with the same name in
different companies.

Every action can return response data; add `response_variable:` to use it.
Without a response variable an action returns nothing, and `wait_for_reply` is
ignored (the action does not hold the script for a reply nobody receives). The
reply still arrives as a `paperclip_agent_replied` event.

The agent pickers only list Paperclip agent devices, not the company device.

### `paperclip.ask_agent`

Creates an issue assigned to the chosen agent (which wakes it) and optionally
waits for its reply.

| Field | Required | Description |
|---|---|---|
| `agent` | yes | Agent device (UI picker), agent id, or agent name. |
| `message` | yes | The request. Becomes the issue description. |
| `title` | no | Issue title. Default: the first 60 characters of the message. |
| `project` | no | Project id or name. Default: the project from the options. |
| `priority` | no | `critical`, `high`, `medium` or `low`. Paperclip's default is `medium`. |
| `wait_for_reply` | no | Seconds to wait for the agent's comment (0 to 300, default 0). Only used with `response_variable`. |

```yaml
action: paperclip.ask_agent
data:
  agent: Engineer
  message: Summarise last night's failed backups.
  wait_for_reply: 30
response_variable: answer
```

Response:

```yaml
issue_id: 55555555-5555-4555-8555-555555555555
identifier: ACME-12
url: http://paperclip.local:3100/ACME/issues/ACME-12
status: todo
agent_id: 33333333-3333-4333-8333-333333333334
agent_name: Engineer
replied: true
reply: "Two jobs failed: ..."
reply_comment_id: 66666666-6666-4666-8666-666666666666
```

`reply` holds the first 2000 characters of the agent's comment; open `url` for
the full text. Assist speaks the whole comment.

Each call creates a new issue, even with the same text as an earlier one.

### `paperclip.add_comment`

Adds a comment to an issue (which wakes its assignee; a comment on a done or
cancelled issue reopens it). The issue is followed from then on, so agent
replies on it fire `paperclip_agent_replied`.

| Field | Required | Description |
|---|---|---|
| `config_entry_id` | yes | The Paperclip entry (company). |
| `issue` | yes | Issue id or identifier, for example `ACME-12`. It must belong to the entry's company. |
| `message` | yes | Comment text (Markdown). |
| `wait_for_reply` | no | Seconds to wait for an agent comment posted after yours (0 to 300). Only used with `response_variable`. |

```yaml
action: paperclip.add_comment
data:
  config_entry_id: 01J0000000000000000000000
  issue: ACME-12
  message: The sensor reports closed again.
  wait_for_reply: 20
response_variable: answer
```

Response: `comment_id`, `issue_id`, `identifier`, `url`, `status`, `replied`,
`reply` (the first 2000 characters of the agent's comment), `reply_comment_id`.

### `paperclip.create_issue`

| Field | Required | Description |
|---|---|---|
| `config_entry_id` | yes | The Paperclip entry (company). |
| `title` | yes | Issue title. |
| `description` | no | Markdown description. |
| `assignee` | no | Agent device, id or name. It must be an agent of the company in `config_entry_id`; the picker also lists agents of other entries, and those are refused. Without one the issue goes to the backlog and no agent is woken. |
| `project` | no | Project id or name. |
| `priority` | no | `critical`, `high`, `medium` or `low`. |
| `allow_duplicate` | no (default false) | Paperclip returns an existing open issue with the same title from the last 48 hours instead of creating a new one, unless this is on. Paperclip matches the title only, so if that issue is assigned to a different agent than `assignee`, the action fails instead (nothing is created or assigned). Comments already on such an issue are not reported as new replies. |

```yaml
action: paperclip.create_issue
data:
  config_entry_id: 01J0000000000000000000000
  title: Replace the hallway smoke alarm battery
  assignee: Home Ops
  priority: low
response_variable: issue
```

Response: `issue_id`, `identifier`, `url`, `status`, `assignee_agent_id` (the
issue's actual assignee), `deduplicated` (`true` when Paperclip returned an
existing issue).

### `paperclip.wake_agent`

| Field | Required | Description |
|---|---|---|
| `agent` | yes | Agent device, id or name. |
| `reason` | no | Shown in Paperclip. |

Response: `agent_id`, `status` (`queued`, or `skipped` when Paperclip merged
the wake into existing work). A paused agent cannot be woken.

### `paperclip.fire_routine_webhook`

Fires a Paperclip routine's webhook trigger. The request is signed with
HMAC-SHA256 over `"<unix time in milliseconds>.<raw body>"` and sent with the headers
`X-Paperclip-Timestamp` and `X-Paperclip-Signature: sha256=<hex>`. The board
API key is **not** sent; only the trigger's own secret counts.

| Field | Required | Description |
|---|---|---|
| `config_entry_id` | yes | The Paperclip entry whose server receives the webhook. |
| `public_id` | yes | The trigger's public id (24 hex characters) or its full webhook URL. |
| `secret` | for signed triggers | The trigger's webhook secret. Set the trigger's signing mode to **HMAC SHA-256** in Paperclip. Leave empty only for unsigned triggers. |
| `payload` | no | JSON object sent as the body. Top-level fields or a nested `variables` object fill the routine's variables. |
| `idempotency_key` | no | Only for unsigned (`none`) triggers: Paperclip runs a delivery with the same key once. Signed (`hmac_sha256`) triggers ignore it; there each call is a new delivery, and only an exact re-send of the same signed request is refused (HTTP 409, "Webhook replay detected"). |

```yaml
action: paperclip.fire_routine_webhook
data:
  config_entry_id: 01J0000000000000000000000
  public_id: 0123456789abcdef01234567
  secret: !secret paperclip_doorbell_secret
  payload:
    event: doorbell
    variables:
      room: hall
response_variable: run
```

Response: `http_status` (202), `status` (`issue_created`, `coalesced`,
`skipped`, `failed`, or `test_received` while the trigger is still being set up
in Paperclip, when no run starts and `run_id` and `linked_issue_id` are empty),
`run_id`, `linked_issue_id`. A rejected secret raises an
error with the HTTP status. Paperclip's default signing mode for new webhook
triggers is `bearer`; this action supports `hmac_sha256` (and unsigned
triggers), so switch the trigger to HMAC.

### `paperclip.approve` and `paperclip.reject`

Only work when **Allow approvals from Home Assistant** is on for the entry;
otherwise they fail with a clear error. The approval must belong to that
entry's company: an approval of another company is refused, even when the
board key could decide it.

| Field | Required | Description |
|---|---|---|
| `config_entry_id` | yes | The Paperclip entry (company); the event carries it. |
| `approval_id` | yes | The approval's UUID, from the `paperclip_approval_requested` event. |
| `decision_note` | no | Stored with the decision. |

Response: `approval_id`, `status`.

## Events

Every event carries `config_entry_id` and `company_id`. Payloads contain ids
and short titles, never issue descriptions. The one exception is
`paperclip_agent_replied`, which carries the first 2000 characters of the
agent's reply.

### `paperclip_agent_replied`

An agent commented on an issue this integration created or commented on.

```yaml
event_type: paperclip_agent_replied
data:
  config_entry_id: 01J0000000000000000000000
  company_id: 11111111-1111-4111-8111-111111111111
  issue_id: 55555555-5555-4555-8555-555555555555
  identifier: ACME-12
  title: Check the garage door sensor
  url: http://paperclip.local:3100/ACME/issues/ACME-12
  agent_id: 33333333-3333-4333-8333-333333333335
  agent_name: Home Ops
  comment_id: 66666666-6666-4666-8666-666666666666
  body: "The door is closed now. ..." # first 2000 characters
  delivered: false
```

`delivered` is `true` when the reply was already returned to an action's
`wait_for_reply` or spoken by Assist, so automations can skip it.

### `paperclip_issue_status_changed`

```yaml
data:
  issue_id: 55555555-5555-4555-8555-555555555555
  identifier: ACME-12
  title: Check the garage door sensor
  url: http://paperclip.local:3100/ACME/issues/ACME-12
  agent_id: 33333333-3333-4333-8333-333333333335
  old_status: todo
  new_status: done
```

Only for issues this integration created or commented on. Statuses: `backlog`,
`todo`, `in_progress`, `in_review`, `done`, `blocked`, `cancelled`.

### `paperclip_agent_status_changed`

```yaml
data:
  agent_id: 33333333-3333-4333-8333-333333333334
  agent_name: Engineer
  old_status: running
  new_status: error
```

### `paperclip_approval_requested`

A new pending approval appeared since the last poll (approvals that were
already pending when Home Assistant started do not fire).

```yaml
data:
  approval_id: 99999999-9999-4999-8999-999999999999
  type: hire_agent
  title: "Hire Agent: Researcher"
  requested_by_agent_id: 33333333-3333-4333-8333-333333333333
  requested_by_agent_name: CEO
  url: http://paperclip.local:3100/ACME/approvals/99999999-9999-4999-8999-999999999999
```

Example: ask for a decision on your phone, and approve from an actionable
notification (requires the approvals option):

```yaml
automation:
  - alias: "Paperclip approval on my phone"
    triggers:
      - trigger: event
        event_type: paperclip_approval_requested
    actions:
      - action: notify.mobile_app_phone
        data:
          title: Paperclip approval
          message: "{{ trigger.event.data.title }}"
          data:
            actions:
              - action: "PAPERCLIP_APPROVE_{{ trigger.event.data.approval_id }}"
                title: Approve

  - alias: "Paperclip approval from the notification button"
    triggers:
      - trigger: event
        event_type: mobile_app_notification_action
    conditions:
      - condition: template
        value_template: "{{ trigger.event.data.action.startswith('PAPERCLIP_APPROVE_') }}"
    actions:
      - action: paperclip.approve
        data:
          config_entry_id: 01J0000000000000000000000 # your Paperclip entry
          approval_id: "{{ trigger.event.data.action[18:] }}"
          decision_note: Approved from the Home Assistant app.
```

## How data is updated

The integration polls Paperclip every 60 seconds. Each poll reads the server
health, the company, its agents, the dashboard counts, pending approvals and
live runs (about six requests), plus one request per issue the integration
follows (more only when it has new comments). While an action or Assist waits
for a reply it checks that issue every 2 seconds. Pausing, resuming, waking or
deciding an approval triggers an immediate refresh.

To poll on your own schedule instead, open the Paperclip entry's three-dot
menu, choose **System options**, turn off **Enable polling for changes**, and
run the `homeassistant.update_entity` action on any Paperclip entity from an
automation whenever you want fresh data (see
[Defining a custom polling interval](https://www.home-assistant.io/common-tasks/general/#defining-a-custom-polling-interval)).

## Security notes

- **A board API key carries the full power of its user** in Paperclip: it can
  create and change issues, pause agents, change agent configuration and decide
  approvals in every company the user belongs to. Create a dedicated Paperclip
  user for Home Assistant if you want to limit that, give it only the companies
  it needs, and revoke the key in Paperclip if it leaks.
- **Approvals are off by default.** Turning them on lets anyone who can run
  actions in Home Assistant (including automations and any LLM agent that may
  call actions) approve hires, strategies and budget overrides in that entry's
  company. Approvals of other companies stay refused.
- **What data goes where**: requests go only to the Paperclip server you
  configured. What you say to a Paperclip conversation agent is stored in
  Paperclip as an issue and comments, and Paperclip passes it to whichever AI
  model that agent uses. Home Assistant stores the key and URL in its config
  entry storage and the ids and titles of followed issues in
  `.storage/paperclip.<entry id>`. Agent replies (up to 2000 characters) are
  also kept by Home Assistant itself: the recorder stores the
  `paperclip_agent_replied` event with its `body` in the event history, and an
  action that returns a reply keeps it in the automation or script trace.
  Exclude the event from the recorder if you do not want reply text in the
  database.
- Diagnostics redact the key, the URL, names, titles and comment bodies.
- Use `https://` if Home Assistant reaches Paperclip over an untrusted network.

## Known limitations

- Paperclip's API is unversioned and in beta; a future Paperclip release may
  change it. Optional routes that disappear are skipped rather than breaking
  setup.
- Updates are polled; there is no push channel. Paperclip's live-events
  websocket does not accept board keys.
- Replies are matched by agent comments on the issue. If an agent posts a
  progress note first, that note is the reply that is returned or spoken.
- Spend is reported in USD, as Paperclip records it, for the current UTC month.
- A 403 from Paperclip on an action (for example a read-only membership, or a
  key without the grant to wake an agent) is reported as an error with
  Paperclip's reason; it does not ask you to re-authenticate.
- Approval events only fire for approvals that appear while Home Assistant is
  running.
- An Assist conversation continues the same issue only while Home Assistant
  keeps the conversation (about 5 minutes without a message).
- Routine webhooks are supported in `hmac_sha256` and unsigned modes, not
  `bearer`.

## Troubleshooting

- **"Paperclip refused this hostname"**: allow the host you entered with
  `npx paperclipai allowed-hostname <host>` on the Paperclip server, then
  restart Paperclip (it reads the list only at start). In Docker, set
  `PAPERCLIP_ALLOWED_HOSTNAMES=<host>` and recreate the container.
- **"That is an agent API key"**: use a board key (`pcp_board_…`).
- **Re-authentication keeps appearing**: the key expired (default 30 days) or
  was revoked. Create one with `--never-expires`.
- **An agent never replies**: check in Paperclip that the agent is not paused,
  that the issue is assigned to it, and that it has budget left. A paused agent
  receives the issue but does not run.
- **Debug logging**: add this to `configuration.yaml` and restart, or use
  **Enable debug logging** on the integration page:

  ```yaml
  logger:
    logs:
      custom_components.paperclip: debug
  ```

- **Diagnostics**: Settings → Devices & services → Paperclip → three-dot menu →
  Download diagnostics. Attach the file to a bug report.

## Removal

1. Go to **Settings → Devices & services → Paperclip**, open the three-dot menu
   of each entry and choose **Delete**. This removes the devices, entities and
   the stored issue tracking data.
2. If you installed with HACS, remove the repository in HACS; otherwise delete
   `<config>/custom_components/paperclip`.
3. Restart Home Assistant.
4. Revoke the board API key in Paperclip if you no longer need it
   (`npx paperclipai token board revoke <key id>` or `DELETE /api/board-api-keys/<key id>`).

## Compatibility

| Component | Version |
|---|---|
| Home Assistant | 2026.10.0 or newer (tested with 2026.10.4) |
| Paperclip server | tested with 2026.1006.0-beta.0 (tag `beta/v2026.1006.0-beta.0`); older releases get a repair issue. A source checkout without release tags reports the placeholder version 0.3.1, which is not compared. |
| Python | 3.14 (as required by Home Assistant) |

## Contributing

Bug reports and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
Report security problems privately as described in [SECURITY.md](SECURITY.md).
Release notes are published with each GitHub release of this repository.

Licensed under the [MIT License](LICENSE).
