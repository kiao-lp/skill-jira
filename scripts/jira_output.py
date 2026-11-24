"""Compact representations of Jira responses."""

from jira_client import JiraError

PREVIEW = 240
DETAIL = 4_000


def pick(data, *keys):
    if not isinstance(data, dict):
        raise JiraError("Jira returned an unexpected object response")
    return {key: data[key] for key in keys if key in data}


def select(*keys):
    return lambda data: pick(data, *keys)


def rows(data, collection=None):
    if collection is not None and isinstance(data, dict):
        keys = (collection,) if isinstance(collection, str) else collection
        data = next((data[key] for key in keys if isinstance(data.get(key), list)), None)
    if not isinstance(data, list):
        raise JiraError("Jira returned an unexpected list response")
    return data


def items(data, collection, view):
    return [view(item) for item in rows(data, collection)]


def offset_page(data, collection, view, start, limit):
    entries = rows(data, collection)
    page = {"items": [view(item) for item in entries]}
    metadata = data if isinstance(data, dict) else {}
    total = metadata.get("total")
    next_start = metadata.get("startAt", start) + len(entries)
    if total is not None:
        page["total"] = total
    more = metadata.get("isLast") is False or (total is not None and next_start < total)
    if total is None and "isLast" not in metadata:
        more = len(entries) >= metadata.get("maxResults", limit)
    if entries and metadata.get("isLast") is not True and more:
        page["nextStart"] = next_start
    return page


def issue_page(data):
    page = {"items": items(data, "issues", lambda item: issue(item, text_limit=PREVIEW))}
    if data.get("nextPageToken") and not data.get("isLast"):
        page["nextPageToken"] = data["nextPageToken"]
    warnings = data.get("warnings") or data.get("warningMessages")
    if warnings:
        page["warnings"] = [excerpt(warning, PREVIEW) for warning in warnings[:5]]
    return page


def excerpt(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def adf_text(node):
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    attrs = node.get("attrs", {})
    if kind in ("mention", "emoji", "status"):
        return attrs.get("text") or attrs.get("shortName") or str(attrs.get("id", ""))
    if kind == "inlineCard":
        return attrs.get("url", "")
    if kind == "media":
        return "[attachment]"
    text = "".join(adf_text(child) for child in node.get("content", []))
    return text + "\n" if kind in ("paragraph", "heading", "codeBlock") else text


def text_view(data, limit):
    if isinstance(data, dict) and data.get("type") == "doc":
        return excerpt(adf_text(data).removesuffix("\n"), limit)
    return excerpt(data, limit) if isinstance(data, str) else data


def name_and_id(data):
    return pick(data, "id", "name")


def user(data):
    return pick(data, "accountId", "displayName", "active")


def attachment(data):
    return pick(data, "id", "filename", "size", "mimeType", "created")


def link_type(data):
    return pick(data, "id", "name", "inward", "outward")


def issue_link(data):
    link = pick(data, "id")
    if "type" in data:
        link["type"] = link_type(data["type"])
    for end in ("inwardIssue", "outwardIssue"):
        if end in data:
            link[end] = pick(data[end], "key", "id")
    return link


ISSUE_OBJECTS = {
    "status": name_and_id, "issuetype": name_and_id, "priority": name_and_id, "resolution": name_and_id,
    "assignee": user, "reporter": user, "creator": user,
    "project": select("id", "key", "name"), "parent": select("id", "key"),
}
ISSUE_LISTS = {
    "components": name_and_id, "versions": name_and_id, "fixVersions": name_and_id,
    "attachment": attachment, "issuelinks": issue_link, "subtasks": select("id", "key"),
}


def issue(data, *, text_limit=DETAIL):
    result = pick(data, "key", "id")
    if "fields" not in data:
        return result
    fields = {}
    for name, value in data["fields"].items():
        if isinstance(value, dict) and name in ISSUE_OBJECTS:
            value = ISSUE_OBJECTS[name](value)
        elif isinstance(value, list) and name in ISSUE_LISTS:
            value = [ISSUE_LISTS[name](item) for item in value]
        elif isinstance(value, dict) and name in ("comment", "worklog"):
            value = pick(value, "total")
        else:
            value = text_view(value, text_limit)
        fields[name] = value
    result["fields"] = fields
    return result


def comment(data, *, text_limit=DETAIL):
    result = pick(data, "id", "parentId", "created", "updated", "visibility", "jsdPublic")
    if data.get("author"):
        result["author"] = user(data["author"])
    if "body" in data:
        result["body"] = text_view(data["body"], text_limit)
    return result


def worklog(data):
    result = pick(data, "id", "started", "timeSpentSeconds", "visibility")
    if data.get("author"):
        result["author"] = user(data["author"])
    if "comment" in data:
        result["comment"] = text_view(data["comment"], PREVIEW)
    return result


def field(data):
    result = pick(data, "id", "fieldId", "key", "name", "required", "operations", "schema", "hasDefaultValue")
    if isinstance(data.get("allowedValues"), list):
        values = data["allowedValues"]
        result["allowedValues"] = [pick(value, "id", "key", "name", "value", "accountId", "displayName")
                                   if isinstance(value, dict) else value for value in values[:20]]
        if len(values) > 20:
            result["allowedValuesCount"] = len(values)
    return result


def transition(data):
    result = pick(data, "id", "name", "hasScreen")
    if data.get("to"):
        result["to"] = name_and_id(data["to"])
    if data.get("fields"):
        result["fields"] = {key: field(value) for key, value in data["fields"].items()}
    return result
