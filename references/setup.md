---
document:
  status: DRAFT
  role: PRIMARY_SOURCE
---

# Jira Cloud setup

The worker's `atlassian.json` lives in `KIAO_WORKER_STATE_PATH`. The default is `$KIAO_WORKER_ROOT_PATH/.kiao-config` using `/tmp` as the standalone worker's default root. For a shell outside a worker turn, set the state path to the absolute directory of the intended worker.

Add a `jira` object to this file and retain its other sections. Restrict access to the worker account.

```json
{
  "jira": {
    "url": "https://example.atlassian.net",
    "email": "account@example.com",
    "token": "<API token>"
  }
}
```

For an Atlassian API token with scopes, add `jira.cloudId` with the site's Cloud ID. The script uses the Atlassian API gateway for this credential. The token scopes and the account's Jira permissions both apply. [API tokens](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/), [Cloud ID](https://support.atlassian.com/jira/kb/retrieve-my-atlassian-sites-cloud-id/)

Run `python3 "$J" user me` to check the authenticated account. For `401`, check the account, token and Cloud ID. For `403`, check the token scopes and the permissions for the operation. Jira rate limits may return `retryAfter` in the error JSON. [Jira Cloud authentication](https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/), [Rate limits](https://developer.atlassian.com/cloud/jira/platform/rate-limiting/)
