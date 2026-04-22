import bolt from '@slack/bolt';
import dotenv from 'dotenv';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createJiraConfigStore } from './jiraConfigStore.js';

const { App, ExpressReceiver } = bolt;

dotenv.config();

const DEFAULT_APPROVED_TRANSITION_ID = process.env.APPROVED_TRANSITION_ID || '31';
const DEFAULT_DECLINED_TRANSITION_ID = process.env.DECLINED_TRANSITION_ID || '41';
const DEFAULT_APPROVAL_FIELD_ID = process.env.APPROVAL_FIELD_ID || 'customfield_12345';
const APP_SHARED_SECRET = process.env.APP_SHARED_SECRET || 'super-secret-value';

const dbPath =
  process.env.SQLITE_DB_PATH ||
  join(dirname(fileURLToPath(import.meta.url)), 'data', 'release_scheduler.db');
const configStore = createJiraConfigStore(dbPath);

function normalizeJiraBaseUrl(input) {
  const trimmed = (input || '').trim();
  if (!trimmed) {
    return '';
  }

  return trimmed.replace(/\/+$/, '');
}

function buildJiraAuthHeader(config) {
  if (config.jira_bearer_token) {
    return `Bearer ${config.jira_bearer_token}`;
  }

  if (config.jira_email && config.jira_api_token) {
    return `Basic ${Buffer.from(`${config.jira_email}:${config.jira_api_token}`).toString('base64')}`;
  }

  throw new Error(
    'Workspace Jira auth is missing. Configure bearer token or email + API token in /jira-config.'
  );
}

async function jiraRequest(config, path, options) {
  const baseUrl = normalizeJiraBaseUrl(config.jira_base_url);
  if (!baseUrl) {
    throw new Error('Workspace Jira base URL is missing. Configure /jira-config first.');
  }

  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      Authorization: buildJiraAuthHeader(config),
      ...(options?.headers || {}),
    },
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Jira request failed (${response.status}): ${text}`);
  }

  return response;
}

async function updateIssueApproval(config, issueKey, decision, approverName) {
  const fieldValue = decision === 'approved' ? 'Approved' : 'Declined';
  const transitionId =
    decision === 'approved' ? config.approved_transition_id : config.declined_transition_id;

  await jiraRequest(config, `/rest/api/3/issue/${issueKey}`, {
    method: 'PUT',
    body: JSON.stringify({
      fields: {
        [config.approval_field_id]: fieldValue,
      },
    }),
  });

  await jiraRequest(config, `/rest/api/3/issue/${issueKey}/transitions`, {
    method: 'POST',
    body: JSON.stringify({
      transition: { id: transitionId },
      update: {
        comment: [
          {
            add: {
              body: {
                type: 'doc',
                version: 1,
                content: [
                  {
                    type: 'paragraph',
                    content: [
                      {
                        type: 'text',
                        text: `Release ${fieldValue.toLowerCase()} via Slack by ${approverName}`,
                      },
                    ],
                  },
                ],
              },
            },
          },
        ],
      },
    }),
  });
}

async function parseRequestBody(req) {
  if (req.body && typeof req.body === 'object') {
    if (Buffer.isBuffer(req.body)) {
      const raw = req.body.toString('utf8');
      return raw ? JSON.parse(raw) : {};
    }
    return req.body;
  }

  if (typeof req.body === 'string') {
    return JSON.parse(req.body);
  }

  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString('utf8');

  return raw ? JSON.parse(raw) : {};
}

function getWorkspaceId(payload) {
  return payload?.teamId || payload?.slackTeamId || payload?.workspaceId || payload?.team_id || null;
}

function getInputValue(view, blockId, actionId) {
  return (view.state.values?.[blockId]?.[actionId]?.value || '').trim();
}

function buildSlackAckMessage({ decision, issueKey, releaseName, releaseId, slackUsername }) {
  const verb = decision === 'approved' ? 'approved' : 'declined';
  const actor = slackUsername || 'Slack user';
  const releaseLabel = releaseName || releaseId || 'release';
  return `:white_check_mark: ${actor} ${verb} ${releaseLabel}. Jira issue ${issueKey} was updated successfully.`;
}

async function sendSlackAckMessage(payload, message) {
  const responseUrl = payload?.responseUrl || payload?.response_url || payload?.slackResponseUrl;
  if (responseUrl) {
    const response = await fetch(responseUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        response_type: 'ephemeral',
        replace_original: false,
        text: message,
      }),
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(`Slack response_url ack failed (${response.status}): ${text}`);
    }
    return;
  }

  const channelId =
    payload?.channelId || payload?.channel_id || payload?.slackChannelId || process.env.SLACK_CHANNEL_ID;
  if (!channelId) {
    return;
  }

  await app.client.chat.postMessage({
    channel: channelId,
    text: message,
  });
}

export async function handleReleaseApproval(body) {
  if (!body || body.sharedSecret !== APP_SHARED_SECRET) {
    return { status: 401, body: { message: 'Unauthorized' } };
  }

  const workspaceId = getWorkspaceId(body);
  if (!workspaceId) {
    return {
      status: 400,
      body: {
        message: 'Missing workspace/team id. Provide teamId (or slackTeamId/workspaceId).',
      },
    };
  }

  const workspaceConfig = configStore.getByTeamId(workspaceId);
  if (!workspaceConfig) {
    return {
      status: 400,
      body: {
        message: `No Jira config found for workspace ${workspaceId}. Run /jira-config in Slack first.`,
      },
    };
  }

  const { releaseId, releaseName, issueKey, decision, slackUsername } = body;

  if (!issueKey || !decision) {
    return { status: 400, body: { message: 'Missing issueKey or decision' } };
  }

  if (!['approved', 'declined'].includes(decision)) {
    return { status: 400, body: { message: 'Invalid decision' } };
  }

  await updateIssueApproval(workspaceConfig, issueKey, decision, slackUsername || 'Slack user');

  const slackAckMessage = buildSlackAckMessage({
    decision,
    issueKey,
    releaseName,
    releaseId,
    slackUsername,
  });
  let slackAckSent = true;
  try {
    await sendSlackAckMessage(body, slackAckMessage);
  } catch (ackErr) {
    slackAckSent = false;
    console.error(ackErr);
  }

  return {
    status: 200,
    body: {
      message: `Release ${releaseName || releaseId || 'unknown'} marked ${decision}`,
    },
  };
}

const receiver = new ExpressReceiver({
  signingSecret: process.env.SLACK_SIGNING_SECRET || '',
});

const app = new App({
  token: process.env.SLACK_BOT_TOKEN || '',
  receiver,
});

receiver.router.use((req, res, next) => {
  const start = Date.now();
  res.on('finish', () => {
    const elapsedMs = Date.now() - start;
    console.log(
      `[http] ${req.method} ${req.originalUrl || req.url} -> ${res.statusCode} (${elapsedMs}ms)`
    );
  });
  next();
});

app.command('/jira-config', async ({ ack, body, client, logger }) => {
  await ack();

  try {
    const workspaceId = body.team_id;
    const existing = configStore.getByTeamId(workspaceId);

    await client.views.open({
      trigger_id: body.trigger_id,
      view: {
        type: 'modal',
        callback_id: 'jira_config_modal',
        private_metadata: workspaceId,
        title: {
          type: 'plain_text',
          text: 'Jira Settings',
        },
        submit: {
          type: 'plain_text',
          text: 'Save',
        },
        close: {
          type: 'plain_text',
          text: 'Cancel',
        },
        blocks: [
          {
            type: 'input',
            block_id: 'jira_base_url_block',
            label: {
              type: 'plain_text',
              text: 'Jira Base URL',
            },
            element: {
              type: 'plain_text_input',
              action_id: 'jira_base_url',
              initial_value: existing?.jira_base_url || '',
              placeholder: {
                type: 'plain_text',
                text: 'https://your-company.atlassian.net',
              },
            },
          },
          {
            type: 'input',
            optional: true,
            block_id: 'jira_email_block',
            label: {
              type: 'plain_text',
              text: 'Jira Email (for API token auth)',
            },
            element: {
              type: 'plain_text_input',
              action_id: 'jira_email',
              initial_value: existing?.jira_email || '',
            },
          },
          {
            type: 'input',
            optional: true,
            block_id: 'jira_api_token_block',
            label: {
              type: 'plain_text',
              text: 'Jira API Token',
            },
            element: {
              type: 'plain_text_input',
              action_id: 'jira_api_token',
              initial_value: existing?.jira_api_token || '',
            },
          },
          {
            type: 'input',
            optional: true,
            block_id: 'jira_bearer_token_block',
            label: {
              type: 'plain_text',
              text: 'Jira Bearer Token',
            },
            element: {
              type: 'plain_text_input',
              action_id: 'jira_bearer_token',
              initial_value: existing?.jira_bearer_token || '',
            },
          },
          {
            type: 'input',
            block_id: 'approval_field_id_block',
            label: {
              type: 'plain_text',
              text: 'Approval Field ID',
            },
            element: {
              type: 'plain_text_input',
              action_id: 'approval_field_id',
              initial_value: existing?.approval_field_id || DEFAULT_APPROVAL_FIELD_ID,
            },
          },
          {
            type: 'input',
            block_id: 'approved_transition_id_block',
            label: {
              type: 'plain_text',
              text: 'Approved Transition ID',
            },
            element: {
              type: 'plain_text_input',
              action_id: 'approved_transition_id',
              initial_value: existing?.approved_transition_id || DEFAULT_APPROVED_TRANSITION_ID,
            },
          },
          {
            type: 'input',
            block_id: 'declined_transition_id_block',
            label: {
              type: 'plain_text',
              text: 'Declined Transition ID',
            },
            element: {
              type: 'plain_text_input',
              action_id: 'declined_transition_id',
              initial_value: existing?.declined_transition_id || DEFAULT_DECLINED_TRANSITION_ID,
            },
          },
        ],
      },
    });
  } catch (error) {
    logger.error(error);
  }
});

app.view('jira_config_modal', async ({ ack, body, view, logger }) => {
  const jiraBaseUrl = normalizeJiraBaseUrl(
    getInputValue(view, 'jira_base_url_block', 'jira_base_url')
  );
  const jiraEmail = getInputValue(view, 'jira_email_block', 'jira_email');
  const jiraApiToken = getInputValue(view, 'jira_api_token_block', 'jira_api_token');
  const jiraBearerToken = getInputValue(view, 'jira_bearer_token_block', 'jira_bearer_token');
  const approvalFieldId = getInputValue(view, 'approval_field_id_block', 'approval_field_id');
  const approvedTransitionId = getInputValue(
    view,
    'approved_transition_id_block',
    'approved_transition_id'
  );
  const declinedTransitionId = getInputValue(
    view,
    'declined_transition_id_block',
    'declined_transition_id'
  );

  const errors = {};
  if (!jiraBaseUrl) {
    errors.jira_base_url_block = 'Jira base URL is required.';
  } else {
    try {
      const parsed = new URL(jiraBaseUrl);
      if (!['http:', 'https:'].includes(parsed.protocol)) {
        errors.jira_base_url_block = 'Jira base URL must start with http:// or https://.';
      }
    } catch (_err) {
      errors.jira_base_url_block = 'Jira base URL must be a valid URL.';
    }
  }

  if (!jiraBearerToken && !(jiraEmail && jiraApiToken)) {
    errors.jira_bearer_token_block =
      'Provide a bearer token, or provide both Jira email and API token.';
  }

  if (!approvalFieldId) {
    errors.approval_field_id_block = 'Approval field ID is required.';
  }
  if (!approvedTransitionId) {
    errors.approved_transition_id_block = 'Approved transition ID is required.';
  }
  if (!declinedTransitionId) {
    errors.declined_transition_id_block = 'Declined transition ID is required.';
  }

  if (Object.keys(errors).length > 0) {
    await ack({
      response_action: 'errors',
      errors,
    });
    return;
  }

  try {
    const workspaceId = view.private_metadata || body.team?.id;
    configStore.save({
      team_id: workspaceId,
      jira_base_url: jiraBaseUrl,
      jira_email: jiraEmail || null,
      jira_api_token: jiraApiToken || null,
      jira_bearer_token: jiraBearerToken || null,
      approval_field_id: approvalFieldId,
      approved_transition_id: approvedTransitionId,
      declined_transition_id: declinedTransitionId,
    });
    await ack();
  } catch (error) {
    logger.error(error);
    await ack({
      response_action: 'errors',
      errors: {
        jira_base_url_block: 'Failed to save workspace config. Check logs and retry.',
      },
    });
  }
});

receiver.router.post('/jira/release-approval', async (req, res) => {
  try {
    const body = await parseRequestBody(req);
    const result = await handleReleaseApproval(body);
    res.status(result.status).json(result.body);
  } catch (error) {
    console.error(error);
    res.status(500).json({ message: error.message || 'Internal error' });
  }
});



if (!process.env.SLACK_SIGNING_SECRET || !process.env.SLACK_BOT_TOKEN) {
  console.warn(
    'Missing SLACK_SIGNING_SECRET or SLACK_BOT_TOKEN. Slack event handling will not work until these are set.'
  );
}

const port = Number(process.env.PORT || 3000);
await app.start(port);
console.log(`Bolt app is running on port ${port}`);

