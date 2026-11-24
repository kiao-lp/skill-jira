---
document:
  status: DRAFT
  role: PRIMARY_SOURCE
---

# Jira operations

## Discovery and writes

- Use `project list`, `project types ABC` and `project create-fields ABC TYPE_ID` to find the project, issue type and required fields. For an existing issue, `issue edit-field ABC-123 FIELD_ID` shows the field metadata. `field search QUERY`, `project components ABC` and `project versions ABC` provide IDs.
- Use `issue transitions ABC-123` then `issue transition ABC-123 TRANSITION_ID`. When a transition has a screen, use `issue transitions ABC-123 --with-fields` to find its inputs.
- Use `user search QUERY` and pass the exact `accountId` to `issue assign ABC-123 --account-id ID`. Use `issue assign ABC-123 --unassign` to clear the assignment.
- Use `issue link-types` to find the link name and its inward and outward labels. Pass the two ends as named by Jira to `issue link INWARD_ISSUE OUTWARD_ISSUE TYPE`. Read `issue get ABC-123 --fields issuelinks` to inspect links. [Link semantics](https://developer.atlassian.com/cloud/jira/platform/issue-linking-model/)
- Use `filter list` and `filter get ID` to find a saved JQL query. Run it with `issue search 'filter = 10001'`. To include a recent change in a search, pass its numeric issue ID with `--reconcile`.

Use `--payload FILE` or `--payload -` for Jira JSON on issue and comment writes. Send only the fields the task changes. `fields` sets values and `update` applies Jira field operations. For example, pass this payload to add one label and set the parent.

```json
{
  "fields": {"parent": {"key": "ABC-10"}},
  "update": {"labels": [{"add": "export"}]}
}
```

String descriptions and comment bodies become ADF paragraphs. Use ADF in `fields.description`, other rich text fields and comment `body` to preserve formatting. With a comment payload, supply `visibility` when changing a Jira group or role restriction. For a Jira Service Management request, use the request comment API and set `public` according to the intended audience. [Issue API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/), [ADF](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/), [Request comments](https://developer.atlassian.com/cloud/jira/service-desk/rest/api-group-request/)

## Boards, sprints, time and files

| Operation | Commands |
| --- | --- |
| Find a board and inspect its scope | `board list --project ABC`, `board get BOARD_ID`, `board issues BOARD_ID`, `board backlog BOARD_ID` |
| Find and read sprints | `sprint list BOARD_ID --state active,future`, `sprint get SPRINT_ID`, `sprint issues SPRINT_ID` |
| Manage sprint planning | `sprint create BOARD_ID NAME`, `sprint edit SPRINT_ID --payload FILE`, `sprint move SPRINT_ID ABC-123`, `sprint backlog ABC-123` |
| Record time | `worklog list ABC-123`, `worklog get ABC-123 LOG_ID`, `worklog add ABC-123 1800 '2026-11-20T14:30:00+01:00'` |
| Work with files | `attachment list ABC-123`, `attachment upload ABC-123 FILE`, `attachment download ATTACHMENT_ID NEW_FILE` |

## Other endpoints

Use `api METHOD /rest/...` to send a JSON request to the configured Jira site. It accepts `/rest/api/3`, `/rest/agile/1.0`, `/rest/software/1.0` and `/rest/servicedeskapi` routes. Pass query parameters as repeated `--param NAME=VALUE` and send a JSON body with `--payload FILE` or `--payload -`. Limit results at the server and use `--select KEY1,KEY2` to project response keys. [Platform REST API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/), [Software REST API](https://developer.atlassian.com/cloud/jira/software/rest/intro/)

```bash
python3 "$J" api GET /rest/api/3/issue/ABC-123/changelog --param maxResults=5 --select values,total
```
