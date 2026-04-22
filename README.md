# Release Scheduler Slack App (Jira + Slack)

This project includes both JavaScript and Python Slack Bolt implementations that:

- receives release approval/decline decisions,
- updates Jira issue fields and transitions,
- sends an acknowledgement message back to Slack,
- stores Jira config per Slack workspace (`team_id`) in SQLite.


## What This App Does

1. A Slack workspace admin configures Jira settings with `/jira-config`.
2. The app stores that workspace's Jira connection and field/transition settings.
3. An external system (or workflow) calls `POST /jira/release-approval`.
4. The app validates the shared secret and workspace id.
5. The app updates Jira:
- custom approval field (`Approved`/`Declined`)
- issue workflow transition
6. The app sends an ack back to Slack (via `response_url` or channel message).

---

## Prerequisites

- Node.js 20+ (Node 24 works) for JavaScript runtime
- Python 3.11+ for Python runtime
- npm
- A Slack app in your company workspace
- A Jira Cloud site with API access
- Ability to expose local server for Slack callbacks in development (for example, `ngrok`)

---

## Project Setup (Step-by-Step)

### 1. Clone and install

```bash
git clone <your-repo-url>
cd release_scheduler_slack_app
npm install
```

If you want to run the Python app:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-python.txt
```

### 2. Create `.env` from template

```bash
cp .env .env
```

Update `.env` values (details in the next section).

### 3. Start app

Run one runtime (not both on the same port).

JavaScript:

```bash
npm run start
```

Python:

```bash
python3 app_python.py
```

Expected log (JS):

```text
Bolt app is running on port 3000
```

Expected log (Python):

```text
Python Bolt app is running on port 3000
```

---

## `.env.example` - What Users Must Update

Use [`.env.example`](/Users/steve/jira-apps/release_scheduler_slack_app/.env.example) as the template.

### Required

- `APP_SHARED_SECRET`
  - Used by `POST /jira/release-approval`.
  - Caller must send this same value in payload `sharedSecret`.
  - Use a long random value.

- `SLACK_SIGNING_SECRET`
  - From Slack App -> Basic Information -> Signing Secret.

- `SLACK_BOT_TOKEN`
  - Bot OAuth token (`xoxb-...`) from Slack App -> OAuth & Permissions.

- `SLACK_CHANNEL_ID`
  - Fallback channel for ack messages if payload has no `response_url` and no `channelId`.

### Recommended / Optional

- `PORT`
  - Defaults to `3000`.

- `SQLITE_DB_PATH`
  - Defaults to `./data/release_scheduler.db`.

- `APPROVAL_FIELD_ID`, `APPROVED_TRANSITION_ID`, `DECLINED_TRANSITION_ID`
  - Default values used to prefill `/jira-config`.
  - Each workspace can override these in modal settings.

- `SLACK_APP_TOKEN`
  - Not currently required for this HTTP mode app; only needed if you implement Socket Mode features.

---

## Slack App Setup for Your Company

Do these steps in `https://api.slack.com/apps`.

### 1. Create or open your Slack app

- Choose your company workspace.

### 2. OAuth scopes

In **OAuth & Permissions**, add bot scopes:

- `commands`
- `chat:write`

Then click **Install/Reinstall to Workspace**.

### 3. Slash command setup

In **Slash Commands**, create:

- Command: `/jira-config`
- Request URL: `https://<public-host>/slack/events`
- Short description: `Configure Jira workspace settings`

### 4. Interactivity setup

In **Interactivity & Shortcuts**:

- Enable Interactivity
- Request URL: `https://<public-host>/slack/events`

### 5. Set secrets/tokens in `.env`

Copy values from Slack app settings:

- `SLACK_SIGNING_SECRET`
- `SLACK_BOT_TOKEN`

Restart app after updates:

```bash
npm run start
```

or:

```bash
python3 app_python.py
```

---

## Local Development with ngrok

Slack cannot call `localhost` directly.

### 1. Run app

JavaScript:

```bash
npm run start
```

Python:

```bash
python3 app_python.py
```

### 2. Expose app

```bash
ngrok http 3000
```

### 3. Copy ngrok URL into Slack settings

Use:

- `https://<ngrok-id>.ngrok.io/slack/events` for Slash Command
- same URL for Interactivity

### 4. Reinstall Slack app

After URL/scope changes, reinstall app to workspace.

---

## Configure Jira Per Workspace (`/jira-config`)

In Slack, run:

```text
/jira-config
```

Fill modal fields:

- Jira Base URL (example: `https://your-company.atlassian.net`)
- Jira Bearer Token **or** Jira Email + Jira API Token
- Approval Field ID
- Approved Transition ID
- Declined Transition ID

Notes:

- Settings are stored per Slack workspace (`team_id`) in SQLite.
- Different workspaces can use different Jira sites/settings.

---

## API Endpoint: Release Approval

### Endpoint

- `POST /jira/release-approval`

### Required payload fields

- `sharedSecret`
- `teamId` (or `slackTeamId` / `workspaceId` / `team_id`)
- `issueKey`
- `decision` (`approved` or `declined`)

### Optional payload fields

- `releaseId`
- `releaseName`
- `slackUsername`
- `response_url` (recommended when coming from interactive Slack flow)
- `channelId` (fallback for channel ack post)

### Example curl

```bash
curl -X POST http://localhost:3000/jira/release-approval \
  -H "Content-Type: application/json" \
  -d '{
    "sharedSecret":"replace-with-your-secret",
    "teamId":"T0123456789",
    "releaseId":"rel-123",
    "releaseName":"Test Release",
    "issueKey":"PROJ-123",
    "decision":"approved",
    "slackUsername":"jdoe",
    "channelId":"C0123456789"
  }'
```

Expected success response:

```json
{
  "message": "Release Test Release marked approved"
}
```

---

## How Multi-Company / Multi-Workspace Works

- Shared `.env` values cover runtime and Slack app credentials.
- Jira connection details are **not global** in `.env` for this app design.
- Jira settings are saved per Slack workspace via `/jira-config`.
- If another company/workspace installs the Slack app, they run `/jira-config` in their workspace and save their own Jira values.

---

## Files and Data

- JavaScript app: [app.js](/Users/steve/jira-apps/release_scheduler_slack_app/app.js)
- Python app: [app_python.py](/Users/steve/jira-apps/release_scheduler_slack_app/app_python.py)
- Jira config store: [jiraConfigStore.js](/Users/steve/jira-apps/release_scheduler_slack_app/jiraConfigStore.js)
- Python dependencies: [requirements-python.txt](/Users/steve/jira-apps/release_scheduler_slack_app/requirements-python.txt)
- SQLite DB default path: `./data/release_scheduler.db`
- Env template: [.env.example](/Users/steve/jira-apps/release_scheduler_slack_app/.env.example)

---

## Troubleshooting

### `/jira-config` says `dispatch_failed`

Usually one of:

- Slash command URL is wrong
- Interactivity URL is wrong
- ngrok URL changed but Slack still points to old URL
- `SLACK_SIGNING_SECRET` does not match the Slack app
- App not reinstalled after scope/config changes

### `Unauthorized` from `/jira/release-approval`

- `sharedSecret` in payload does not match `.env` `APP_SHARED_SECRET`.

### `No Jira config found for workspace ...`

- Run `/jira-config` in that Slack workspace and save settings.
- Ensure payload `teamId` matches that workspace team id.

### Jira auth error

- In `/jira-config`, provide:
  - bearer token, or
  - Jira email + Jira API token

### `EADDRINUSE: 3000`

- Port already in use. Kill process or run with another port:

```bash
PORT=3001 npm run start
```

or:

```bash
PORT=3001 python3 app_python.py
```

---

## Security Notes

- Never commit real `.env` secrets.
- Rotate Slack/Jira secrets if exposed.
- SQLite currently stores Jira credentials as plain text on disk.
  - For production, add encryption-at-rest and secrets management.

---

## Quick Start Checklist

1. Install runtime dependencies (`npm install` for JS or `pip install -r requirements-python.txt` for Python)
2. `cp .env.example .env`
3. Fill `.env` (`APP_SHARED_SECRET`, Slack secrets/token)
4. Start one app (`npm run start` or `python3 app_python.py`)
5. `ngrok http 3000`
6. Configure Slack command/interactivity URLs to `/slack/events`
7. Add scopes (`commands`, `chat:write`) and reinstall app
8. Run `/jira-config` in Slack and save Jira settings
9. Call `POST /jira/release-approval` with `teamId` + `sharedSecret`
