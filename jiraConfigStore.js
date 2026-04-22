import Database from 'better-sqlite3';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';


export function createJiraConfigStore(dbPath) {
  mkdirSync(dirname(dbPath), { recursive: true });


  const db = new Database(dbPath);
  db.pragma('journal_mode = WAL');
  db.exec(`
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
  `);

  const getByTeamIdStmt = db.prepare(`
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
  `);

  const upsertStmt = db.prepare(`
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
      @team_id,
      @jira_base_url,
      @jira_email,
      @jira_api_token,
      @jira_bearer_token,
      @approval_field_id,
      @approved_transition_id,
      @declined_transition_id,
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
  `);

  return {
    getByTeamId(teamId) {
      if (!teamId) {
        return null;
      }

      return getByTeamIdStmt.get(teamId) || null;
    },
    save(config) {
      upsertStmt.run({
        team_id: config.team_id,
        jira_base_url: config.jira_base_url,
        jira_email: config.jira_email || null,
        jira_api_token: config.jira_api_token || null,
        jira_bearer_token: config.jira_bearer_token || null,
        approval_field_id: config.approval_field_id,
        approved_transition_id: config.approved_transition_id,
        declined_transition_id: config.declined_transition_id,
      });
    },
  };
}


