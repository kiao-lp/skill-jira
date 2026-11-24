---
name: jira
description: Search and manage Jira Cloud issues and comments, discover project fields and accounts, and work with transitions, links, boards, sprints, worklogs and attachments through the Kiao worker.
---

# Jira

Set `J` to the absolute path of [scripts/jira.py](scripts/jira.py) in this skill directory. Run it with Python 3.11 or later.

```bash
python3 "$J" issue search 'project = ABC ORDER BY updated DESC' --limit 10
python3 "$J" issue get ABC-123 --fields summary,status,description
python3 "$J" issue create ABC Task 'Fix the export' --description 'CSV exports drop the last row.'
python3 "$J" issue edit ABC-123 --summary 'Keep the last row in CSV exports'
python3 "$J" comment list ABC-123 --newest --limit 5
python3 "$J" comment get ABC-123 10001
python3 "$J" comment create ABC-123 --text 'Reproduced with a two-row export.'
python3 "$J" comment edit ABC-123 10001 --text-file comment.txt
```

Use the issue key directly when known, including the key from a Jira `/browse/` link. Search with specific JQL and request only the fields needed. Responses are compact JSON. Paginated lists return one page in `items`. Pass `nextPageToken` as `--cursor` or `nextStart` as `--start` for more. Use `--raw` before the resource when you need the original Jira JSON, including full rich text.

Use the relevant `RESOURCE ACTION --help` for arguments. After an uncertain write, check Jira for the result before repeating it.

- Read [operations](references/operations.md) for rich text, custom fields, workflows, Agile operations or other REST endpoints.
- Read [worker setup](references/setup.md) for configuration or authentication errors. The script reads the worker's Atlassian credentials directly.
