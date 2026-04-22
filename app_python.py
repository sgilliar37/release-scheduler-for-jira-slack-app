import json
import os
import sqlite3
from base64 import b64encode
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler


load_dotenv()

DEFAULT_APPROVED_TRANSITION_ID = os.getenv("APPROVED_TRANSITION_ID", "31")
DEFAULT_DECLINED_TRANSITION_ID = os.getenv("DECLINED_TRANSITION_ID", "41")
DEFAULT_APPROVAL_FIELD_ID = os.getenv("APPROVAL_FIELD_ID", "customfield_12345")
APP_SHARED_SECRET = os.getenv("APP_SHARED_SECRET", "super-secret-value")

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH_ENV = os.getenv("SQLITE_DB_PATH", "data/release_scheduler.db")
DB_PATH = Path(DB_PATH_ENV)
if not DB_PATH.is_absolute():
    DB_PATH = (PROJECT_ROOT / DB_PATH).resolve()

SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "").strip()
PORT = int(os.getenv("PORT", "3000"))
BIND_HOST = os.getenv("BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"


class JiraConfigStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jira_workspace_config (
                  team_id TEXT PRIMARY KEY,
                  jira_base_url TEXT NOT NULL,
                  jira_email TEXT,
                  jira_api_token TEXT,
                  jira_bearer_token TEXT,
                  approval_field_id TEXT NOT NULL,
                  approved_transition_id TEXT NOT NULL,
                  declined_transition_id TEXT NOT NULL,
                  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.commit()

    def get_by_team_id(self, team_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not team_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                  team_id,
                  jira_base_url,
                  jira_email,
                  jira_api_token,
                  jira_bearer_token,
                  approval_field_id,
                  approved_transition_id,
                  declined_transition_id
                FROM jira_workspace_config
                WHERE team_id = ?
                """,
                (team_id,),
            ).fetchone()
            return dict(row) if row else None

    def save(self, config: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jira_workspace_config (
                  team_id,
                  jira_base_url,
                  jira_email,
                  jira_api_token,
                  jira_bearer_token,
                  approval_field_id,
                  approved_transition_id,
                  declined_transition_id,
                  updated_at
                ) VALUES (
                  :team_id,
                  :jira_base_url,
                  :jira_email,
                  :jira_api_token,
                  :jira_bearer_token,
                  :approval_field_id,
                  :approved_transition_id,
                  :declined_transition_id,
                  datetime('now')
                )
                ON CONFLICT(team_id) DO UPDATE SET
                  jira_base_url = excluded.jira_base_url,
                  jira_email = excluded.jira_email,
                  jira_api_token = excluded.jira_api_token,
                  jira_bearer_token = excluded.jira_bearer_token,
                  approval_field_id = excluded.approval_field_id,
                  approved_transition_id = excluded.approved_transition_id,
                  declined_transition_id = excluded.declined_transition_id,
                  updated_at = datetime('now')
                """,
                {
                    "team_id": config["team_id"],
                    "jira_base_url": config["jira_base_url"],
                    "jira_email": config.get("jira_email"),
                    "jira_api_token": config.get("jira_api_token"),
                    "jira_bearer_token": config.get("jira_bearer_token"),
                    "approval_field_id": config["approval_field_id"],
                    "approved_transition_id": config["approved_transition_id"],
                    "declined_transition_id": config["declined_transition_id"],
                },
            )
            conn.commit()


def normalize_jira_base_url(value: Optional[str]) -> str:
    return (value or "").strip().rstrip("/")


def get_workspace_id(payload: Dict[str, Any]) -> Optional[str]:
    return (
        payload.get("teamId")
        or payload.get("slackTeamId")
        or payload.get("workspaceId")
        or payload.get("team_id")
    )


def get_input_value(view: Dict[str, Any], block_id: str, action_id: str) -> str:
    return (
        view.get("state", {})
        .get("values", {})
        .get(block_id, {})
        .get(action_id, {})
        .get("value", "")
        .strip()
    )


def build_jira_auth_header(config: Dict[str, Any]) -> str:
    bearer = (config.get("jira_bearer_token") or "").strip()
    if bearer:
        return f"Bearer {bearer}"

    email = (config.get("jira_email") or "").strip()
    api_token = (config.get("jira_api_token") or "").strip()
    if email and api_token:
        token = b64encode(f"{email}:{api_token}".encode("utf-8")).decode("utf-8")
        return f"Basic {token}"

    raise ValueError(
        "Workspace Jira auth is missing. Configure bearer token or email + API token in /jira-config."
    )


def jira_request(config: Dict[str, Any], path: str, payload: Dict[str, Any]) -> None:
    base_url = normalize_jira_base_url(config.get("jira_base_url"))
    if not base_url:
        raise ValueError("Workspace Jira base URL is missing. Configure /jira-config first.")

    response = requests.request(
        method="POST" if path.endswith("/transitions") else "PUT",
        url=f"{base_url}{path}",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": build_jira_auth_header(config),
        },
        data=json.dumps(payload),
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Jira request failed ({response.status_code}): {response.text}")


def update_issue_approval(
    config: Dict[str, Any], issue_key: str, decision: str, approver_name: str
) -> None:
    field_value = "Approved" if decision == "approved" else "Declined"
    transition_id = (
        config["approved_transition_id"]
        if decision == "approved"
        else config["declined_transition_id"]
    )

    jira_request(
        config,
        f"/rest/api/3/issue/{issue_key}",
        {
            "fields": {
                config["approval_field_id"]: field_value,
            }
        },
    )
    jira_request(
        config,
        f"/rest/api/3/issue/{issue_key}/transitions",
        {
            "transition": {"id": transition_id},
            "update": {
                "comment": [
                    {
                        "add": {
                            "body": {
                                "type": "doc",
                                "version": 1,
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": f"Release {field_value.lower()} via Slack by {approver_name}",
                                            }
                                        ],
                                    }
                                ],
                            }
                        }
                    }
                ]
            },
        },
    )


def build_slack_ack_message(payload: Dict[str, Any]) -> str:
    decision = payload.get("decision", "")
    verb = "approved" if decision == "approved" else "declined"
    actor = payload.get("slackUsername") or "Slack user"
    release_label = payload.get("releaseName") or payload.get("releaseId") or "release"
    issue_key = payload.get("issueKey") or "unknown issue"
    return (
        f":white_check_mark: {actor} {verb} {release_label}. "
        f"Jira issue {issue_key} was updated successfully."
    )


def send_slack_ack_message(payload: Dict[str, Any], message: str, slack_app: App) -> None:
    response_url = (
        payload.get("responseUrl")
        or payload.get("response_url")
        or payload.get("slackResponseUrl")
    )
    if response_url:
        response = requests.post(
            response_url,
            headers={"Content-Type": "application/json"},
            data=json.dumps(
                {
                    "response_type": "ephemeral",
                    "replace_original": False,
                    "text": message,
                }
            ),
            timeout=15,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Slack response_url ack failed ({response.status_code}): {response.text}"
            )
        return

    channel_id = (
        payload.get("channelId")
        or payload.get("channel_id")
        or payload.get("slackChannelId")
        or SLACK_CHANNEL_ID
    )
    if channel_id:
        slack_app.client.chat_postMessage(channel=channel_id, text=message)


def handle_release_approval(
    payload: Dict[str, Any], store: JiraConfigStore, slack_app: App
) -> Tuple[int, Dict[str, Any]]:
    if payload.get("sharedSecret") != APP_SHARED_SECRET:
        return 401, {"message": "Unauthorized"}

    workspace_id = get_workspace_id(payload)
    if not workspace_id:
        return 400, {
            "message": "Missing workspace/team id. Provide teamId (or slackTeamId/workspaceId)."
        }

    workspace_config = store.get_by_team_id(workspace_id)
    if not workspace_config:
        return 400, {
            "message": f"No Jira config found for workspace {workspace_id}. Run /jira-config in Slack first."
        }

    issue_key = payload.get("issueKey")
    decision = payload.get("decision")
    if not issue_key or not decision:
        return 400, {"message": "Missing issueKey or decision"}
    if decision not in ("approved", "declined"):
        return 400, {"message": "Invalid decision"}

    update_issue_approval(
        workspace_config, issue_key, decision, payload.get("slackUsername") or "Slack user"
    )
    try:
        send_slack_ack_message(payload, build_slack_ack_message(payload), slack_app)
    except Exception as err:  # noqa: BLE001
        print(err)

    release_name = payload.get("releaseName") or payload.get("releaseId") or "unknown"
    return 200, {"message": f"Release {release_name} marked {decision}"}


config_store = JiraConfigStore(DB_PATH)
bolt_app = App(
    token=os.getenv("SLACK_BOT_TOKEN", ""),
    signing_secret=os.getenv("SLACK_SIGNING_SECRET", ""),
)
flask_app = Flask(__name__)
slack_handler = SlackRequestHandler(bolt_app)


@flask_app.after_request
def log_response(resp):  # type: ignore[no-untyped-def]
    print(f"[http] {request.method} {request.path} -> {resp.status_code}")
    return resp


@bolt_app.command("/jira-config")
def open_jira_config(ack, body, client, logger):  # type: ignore[no-untyped-def]
    ack()
    try:
        workspace_id = body.get("team_id")
        existing = config_store.get_by_team_id(workspace_id) or {}
        client.views_open(
            trigger_id=body.get("trigger_id"),
            view={
                "type": "modal",
                "callback_id": "jira_config_modal",
                "private_metadata": workspace_id or "",
                "title": {"type": "plain_text", "text": "Jira Settings"},
                "submit": {"type": "plain_text", "text": "Save"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "jira_base_url_block",
                        "label": {"type": "plain_text", "text": "Jira Base URL"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "jira_base_url",
                            "initial_value": existing.get("jira_base_url", ""),
                            "placeholder": {
                                "type": "plain_text",
                                "text": "https://your-company.atlassian.net",
                            },
                        },
                    },
                    {
                        "type": "input",
                        "optional": True,
                        "block_id": "jira_email_block",
                        "label": {"type": "plain_text", "text": "Jira Email (for API token auth)"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "jira_email",
                            "initial_value": existing.get("jira_email", ""),
                        },
                    },
                    {
                        "type": "input",
                        "optional": True,
                        "block_id": "jira_api_token_block",
                        "label": {"type": "plain_text", "text": "Jira API Token"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "jira_api_token",
                            "initial_value": existing.get("jira_api_token", ""),
                        },
                    },
                    {
                        "type": "input",
                        "optional": True,
                        "block_id": "jira_bearer_token_block",
                        "label": {"type": "plain_text", "text": "Jira Bearer Token"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "jira_bearer_token",
                            "initial_value": existing.get("jira_bearer_token", ""),
                        },
                    },
                    {
                        "type": "input",
                        "block_id": "approval_field_id_block",
                        "label": {"type": "plain_text", "text": "Approval Field ID"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "approval_field_id",
                            "initial_value": existing.get(
                                "approval_field_id", DEFAULT_APPROVAL_FIELD_ID
                            ),
                        },
                    },
                    {
                        "type": "input",
                        "block_id": "approved_transition_id_block",
                        "label": {"type": "plain_text", "text": "Approved Transition ID"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "approved_transition_id",
                            "initial_value": existing.get(
                                "approved_transition_id", DEFAULT_APPROVED_TRANSITION_ID
                            ),
                        },
                    },
                    {
                        "type": "input",
                        "block_id": "declined_transition_id_block",
                        "label": {"type": "plain_text", "text": "Declined Transition ID"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "declined_transition_id",
                            "initial_value": existing.get(
                                "declined_transition_id", DEFAULT_DECLINED_TRANSITION_ID
                            ),
                        },
                    },
                ],
            },
        )
    except Exception as err:  # noqa: BLE001
        logger.error(err)


@bolt_app.view("jira_config_modal")
def save_jira_config(ack, body, view, logger):  # type: ignore[no-untyped-def]
    jira_base_url = normalize_jira_base_url(
        get_input_value(view, "jira_base_url_block", "jira_base_url")
    )
    jira_email = get_input_value(view, "jira_email_block", "jira_email")
    jira_api_token = get_input_value(view, "jira_api_token_block", "jira_api_token")
    jira_bearer_token = get_input_value(view, "jira_bearer_token_block", "jira_bearer_token")
    approval_field_id = get_input_value(view, "approval_field_id_block", "approval_field_id")
    approved_transition_id = get_input_value(
        view, "approved_transition_id_block", "approved_transition_id"
    )
    declined_transition_id = get_input_value(
        view, "declined_transition_id_block", "declined_transition_id"
    )

    errors = {}
    if not jira_base_url:
        errors["jira_base_url_block"] = "Jira base URL is required."
    elif not (jira_base_url.startswith("http://") or jira_base_url.startswith("https://")):
        errors["jira_base_url_block"] = "Jira base URL must start with http:// or https://."

    if not jira_bearer_token and not (jira_email and jira_api_token):
        errors["jira_bearer_token_block"] = (
            "Provide a bearer token, or provide both Jira email and API token."
        )
    if not approval_field_id:
        errors["approval_field_id_block"] = "Approval field ID is required."
    if not approved_transition_id:
        errors["approved_transition_id_block"] = "Approved transition ID is required."
    if not declined_transition_id:
        errors["declined_transition_id_block"] = "Declined transition ID is required."

    if errors:
        ack(response_action="errors", errors=errors)
        return

    try:
        workspace_id = view.get("private_metadata") or (body.get("team", {}) or {}).get("id")
        config_store.save(
            {
                "team_id": workspace_id,
                "jira_base_url": jira_base_url,
                "jira_email": jira_email or None,
                "jira_api_token": jira_api_token or None,
                "jira_bearer_token": jira_bearer_token or None,
                "approval_field_id": approval_field_id,
                "approved_transition_id": approved_transition_id,
                "declined_transition_id": declined_transition_id,
            }
        )
        ack()
    except Exception as err:  # noqa: BLE001
        logger.error(err)
        ack(
            response_action="errors",
            errors={"jira_base_url_block": "Failed to save workspace config. Check logs and retry."},
        )


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return slack_handler.handle(request)



@flask_app.route("/jira/release-approval", methods=["POST"])
def jira_release_approval():
    payload = request.get_json(silent=True)
    if payload is None:
        raw = request.get_data(as_text=True)
        payload = json.loads(raw) if raw else {}
    status, body = handle_release_approval(payload, config_store, bolt_app)
    return jsonify(body), status


if __name__ == "__main__":
    if not os.getenv("SLACK_SIGNING_SECRET") or not os.getenv("SLACK_BOT_TOKEN"):
        print(
            "Missing SLACK_SIGNING_SECRET or SLACK_BOT_TOKEN. "
            "Slack event handling will not work until these are set."
        )
    print(f"Python Bolt app is running on port {PORT}")
    flask_app.run(host=BIND_HOST, port=PORT)
