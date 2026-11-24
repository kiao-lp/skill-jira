#!/usr/bin/env python3
"""Operate on Jira Cloud with compact JSON output."""

import argparse
from datetime import datetime
from functools import partial
from pathlib import Path
import re
import sys

# The worker verifies the installed package's file tree between turns.
sys.dont_write_bytecode = True

import jira_output as output
from jira_client import AGILE, API, NO_BODY, SOFTWARE, InputError, JiraClient, JiraError, json_text, parse_json

SEARCH_FIELDS = ["summary", "status", "assignee", "issuetype"]
DETAIL_FIELDS = SEARCH_FIELDS + ["description", "priority", "labels", "parent", "updated"]
COMMENT_PATH = API + "/issue/{issue}/comment"
WORKLOG_PATH = API + "/issue/{issue}/worklog"
SPRINT_PATH = AGILE + "/sprint/{sprint_id}"
PROJECT = output.select("id", "key", "name", "projectTypeKey", "style", "archived")
SPRINT = output.select("id", "name", "state", "originBoardId", "startDate", "endDate", "completeDate")


def nonblank(value):
    if not value.strip():
        raise argparse.ArgumentTypeError("provide a non-empty value")
    return value


def identifier(pattern, label):
    def parse(value):
        if not re.fullmatch(pattern, value, re.ASCII):
            raise argparse.ArgumentTypeError(f"expected {label}")
        return value
    return parse


numeric_id = identifier(r"[1-9][0-9]*", "a positive numeric ID")
issue_key = identifier(r"[A-Za-z][\w]*-[1-9][0-9]*|[1-9][0-9]*", "an issue key or numeric ID")
project_key = identifier(r"[A-Za-z][\w]*|[1-9][0-9]*", "a project key or numeric ID")


def bounded_int(lower, upper=None):
    def parse(value):
        number = int(value)
        if number < lower or (upper is not None and number > upper):
            expected = f"{lower}–{upper}" if upper is not None else f">= {lower}"
            raise argparse.ArgumentTypeError(f"expected an integer {expected}")
        return number
    return parse


def csv(value):
    values = [item.strip() for item in value.split(",")]
    if not all(values):
        raise argparse.ArgumentTypeError("use a comma-separated list of non-empty values")
    return values


def query_parameter(value):
    key, separator, parameter = value.partition("=")
    if not separator or not key:
        raise argparse.ArgumentTypeError("use NAME=VALUE")
    return key, parameter


def read_text(path):
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def read_payload(path, *, object_only=False):
    text = read_text(path)
    try:
        value = parse_json(text)
    except ValueError as exc:
        raise InputError(f"Invalid --payload JSON ({exc})") from None
    if object_only and not isinstance(value, dict):
        raise InputError("--payload must contain a JSON object")
    return value


def payload_object(args):
    return read_payload(args.payload, object_only=True) if args.payload is not None else {}


def text_input(args, name):
    path = getattr(args, f"{name}_file", None)
    if path == "-" and getattr(args, "payload", None) == "-":
        raise InputError("Only one input can use standard input")
    return read_text(path) if path is not None else getattr(args, name, None)


def adf(value, name, *, allow_empty=False):
    if isinstance(value, str):
        if not allow_empty and not value.strip():
            raise InputError(f"{name} must contain text")
        paragraphs = []
        for line in value.split("\n"):
            paragraph = {"type": "paragraph"}
            if line:
                paragraph["content"] = [{"type": "text", "text": line}]
            paragraphs.append(paragraph)
        return {"type": "doc", "version": 1, "content": paragraphs}
    if not (isinstance(value, dict) and value.get("type") == "doc"
            and type(value.get("version")) is int and value["version"] == 1
            and isinstance(value.get("content"), list) and (allow_empty or value["content"])):
        raise InputError(f"{name} must be text or an Atlassian Document Format document (version 1)")
    nodes = value["content"].copy()
    while nodes:
        node = nodes.pop()
        if not isinstance(node, dict) or not isinstance(node.get("type"), str) or not node["type"]:
            raise InputError(f"Invalid ADF node in {name}")
        if node["type"] == "text" and (not isinstance(node.get("text"), str) or not node["text"]):
            raise InputError(f"Invalid ADF text node in {name}")
        children = node.get("content", [])
        if not isinstance(children, list):
            raise InputError(f"Invalid ADF content in {name}")
        nodes.extend(children)
    return value


def merge_value(data, key, value):
    if key in data:
        raise InputError(f"Supply {key} either as an argument or in --payload")
    data[key] = value


def reference(value, other):
    return {"id" if value.isascii() and value.isdecimal() else other: value}


def issue_fields(args, action):
    description = text_input(args, "description")
    body = payload_object(args)
    fields = body.get("fields", {})
    updates = body.get("update", {})
    if not isinstance(fields, dict) or not isinstance(updates, dict):
        raise InputError("fields and update must be JSON objects")
    for key, operations in updates.items():
        if not isinstance(operations, list) or not operations:
            raise InputError(f"update.{key} must contain a list of operations")
        for operation in operations:
            if not isinstance(operation, dict) or len(operation) != 1:
                raise InputError(f"Each update.{key} operation must have one action")
            if key in ("description", "environment") and operation.get("set") is not None:
                operation["set"] = adf(operation["set"], key, allow_empty=True)

    if action == "create":
        merge_value(fields, "project", reference(args.project, "key"))
        merge_value(fields, "issuetype", reference(args.issue_type, "name"))
    if getattr(args, "summary", None) is not None:
        merge_value(fields, "summary", args.summary)
    if description is not None:
        merge_value(fields, "description", description)
    for key in ("description", "environment"):
        if key in fields and fields[key] is not None:
            fields[key] = adf(fields[key], key, allow_empty=True)

    duplicate = fields.keys() & updates.keys()
    if duplicate:
        raise InputError(f"Fields appear in both fields and update ({', '.join(sorted(duplicate))})")
    if action == "edit" and not fields and not updates:
        raise InputError("Provide fields or update operations to edit")
    if fields:
        body["fields"] = fields
    return body


def search_issues(client, args):
    body = {"jql": args.jql, "fields": args.fields, "maxResults": args.limit}
    if args.cursor:
        body["nextPageToken"] = args.cursor
    if args.reconcile:
        body["reconcileIssues"] = args.reconcile
    return client.request("POST", API + "/search/jql", payload=body, view=output.issue_page)


def get_issue(client, args):
    return client.request("GET", f"{API}/issue/{args.issue}",
                          query={"fields": ",".join(args.fields)}, view=output.issue)


def write_issue(client, args, *, action):
    body = issue_fields(args, action)
    if action == "create":
        return client.request("POST", API + "/issue", payload=body, view=output.issue)
    path = f"{API}/issue/{args.issue}"
    if action == "edit":
        return client.request("PUT", path, payload=body)
    merge_value(body, "transition", {"id": args.transition_id})
    return client.request("POST", path + "/transitions", payload=body)


def assign_issue(client, args):
    return client.request("PUT", f"{API}/issue/{args.issue}/assignee", payload={"accountId": args.account_id})


def transitions(client, args):
    return client.request("GET", f"{API}/issue/{args.issue}/transitions",
                          query={"expand": "transitions.fields"} if args.with_fields else None,
                          view=lambda data: output.items(data, "transitions", output.transition))


def edit_field(client, args):
    def view(data):
        field = data["fields"].get(args.field)
        if field is None:
            raise JiraError(f"Field {args.field} is unavailable for editing")
        return {"id": args.field, **output.field(field)}
    return client.request("GET", f"{API}/issue/{args.issue}/editmeta", view=view)


def link_issue(client, args):
    if args.inward_issue.upper() == args.outward_issue.upper():
        raise InputError("Link two distinct issues")
    return client.request("POST", API + "/issueLink", payload={
        "inwardIssue": reference(args.inward_issue, "key"),
        "outwardIssue": reference(args.outward_issue, "key"),
        "type": reference(args.link_type, "name"),
    })


def write_comment(client, args, *, action):
    text = text_input(args, "text")
    body = payload_object(args)
    if text is not None:
        merge_value(body, "body", text)
    if "body" in body:
        body["body"] = adf(body["body"], "body")
    elif action == "create" or not body:
        raise InputError("Provide comment text or a --payload with a body")
    path = COMMENT_PATH.format(issue=args.issue)
    if action == "edit":
        return client.request("PUT", f"{path}/{args.comment_id}", payload=body, view=output.comment)
    return client.request("POST", path, payload=body, view=output.comment)


def get(client, args, *, route, view):
    return client.request("GET", route.format_map(vars(args)), view=view)


def list_page(client, args, *, route, collection, view, parameters):
    query = {"startAt": args.start, "maxResults": args.limit}
    for key, name in parameters.items():
        value = getattr(args, name)
        if value is not None:
            query[key] = ",".join(value) if isinstance(value, list) else value
    return client.request("GET", route.format_map(vars(args)), query=query,
                          view=lambda data: output.offset_page(data, collection, view, args.start, args.limit))


def agile_issues(client, args, *, route):
    query = {"fields": ",".join(args.fields), "maxResults": args.limit}
    if args.jql:
        query["jql"] = args.jql
    if args.cursor:
        query["nextPageToken"] = args.cursor
    return client.request("GET", route.format_map(vars(args)), query=query, view=output.issue_page)


def move_issues(client, args, *, route):
    if len(args.issues) > 50:
        raise InputError("Jira accepts up to 50 issues per move")
    return client.request("POST", route.format_map(vars(args)), payload={"issues": args.issues})


def write_sprint(client, args, *, action):
    body = payload_object(args)
    if action == "create":
        merge_value(body, "originBoardId", int(args.board))
        merge_value(body, "name", args.name)
        return client.request("POST", AGILE + "/sprint", payload=body, view=SPRINT)
    if not body:
        raise InputError("Provide the sprint fields to edit in --payload")
    return client.request("POST", SPRINT_PATH.format(sprint_id=args.sprint_id), payload=body, view=SPRINT)


def worklog_start(value):
    try:
        start = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use an ISO 8601 timestamp with a UTC offset") from None
    offset = start.utcoffset()
    if offset is None or offset.total_seconds() % 60:
        raise argparse.ArgumentTypeError("include the UTC offset, for example +00:00")
    return start.replace(tzinfo=None).isoformat(timespec="milliseconds") + start.strftime("%z")


def add_worklog(client, args):
    body = {"timeSpentSeconds": args.seconds, "started": args.started}
    text = text_input(args, "text")
    if text is not None:
        body["comment"] = adf(text, "comment")
    return client.request("POST", WORKLOG_PATH.format(issue=args.issue), payload=body, view=output.worklog)


def list_attachments(client, args):
    return client.request("GET", f"{API}/issue/{args.issue}", query={"fields": "attachment"},
                          view=lambda data: output.items(data["fields"], "attachment", output.attachment))


def upload_attachment(client, args):
    return client.upload(args.issue, args.file, lambda data: output.items(data, None, output.attachment))


def download_attachment(client, args):
    return client.download(args.attachment_id, args.file)


def api_call(client, args):
    if args.payload is not None and args.method in ("GET", "DELETE"):
        raise InputError(f"{args.method} does not accept --payload")
    body = read_payload(args.payload) if args.payload is not None else NO_BODY

    def view(data):
        if isinstance(data, list):
            return [output.pick(item, *args.select) for item in data]
        return output.pick(data, *args.select)

    return client.request(args.method, args.path, query=args.param, payload=body, view=view if args.select else None)


def group(commands, name, help_text):
    parser = commands.add_parser(name, help=help_text, description=help_text)
    return parser.add_subparsers(dest="action", metavar="ACTION", required=True)


def action(commands, name, help_text, handler):
    parser = commands.add_parser(name, help=help_text, description=help_text)
    parser.set_defaults(handler=handler)
    return parser


def get_action(commands, name, help_text, route, view):
    return action(commands, name, help_text, partial(get, route=route, view=view))


def page_action(commands, action_name, help_text, route, collection, view, **parameters):
    parser = action(commands, action_name, help_text,
                    partial(list_page, route=route, collection=collection, view=view, parameters=parameters))
    parser.add_argument("--start", type=bounded_int(0), default=0, help="start offset, default 0")
    add_limit(parser)
    return parser


def add_limit(parser):
    parser.add_argument("--limit", type=bounded_int(1, 100), default=20, help="results per page, default 20")


def search_options(parser):
    parser.add_argument("--fields", type=csv, default=SEARCH_FIELDS, help="comma-separated Jira field IDs")
    parser.add_argument("--cursor", help="nextPageToken from the previous result")
    add_limit(parser)


def text_options(parser, name):
    options = parser.add_mutually_exclusive_group()
    options.add_argument(f"--{name}", help="plain text")
    options.add_argument(f"--{name}-file", help="UTF-8 text file, or - for standard input")


def payload_option(parser, *, required=False):
    parser.add_argument("--payload", required=required, help="JSON file, or - for standard input")


def issue_commands(commands):
    issues = group(commands, "issue", "Search, read and change issues")
    parser = action(issues, "search", "Search with JQL", search_issues)
    parser.add_argument("jql", type=nonblank)
    parser.add_argument("--reconcile", action="append", type=bounded_int(1), metavar="ISSUE_ID",
                        help="include a recent issue change by numeric ID, repeat as needed")
    search_options(parser)
    parser = action(issues, "get", "Read selected fields", get_issue)
    parser.add_argument("issue", type=issue_key)
    parser.add_argument("--fields", type=csv, default=DETAIL_FIELDS, help="comma-separated Jira field IDs")
    for name, description in (("create", "Create an issue"), ("edit", "Edit issue fields")):
        parser = action(issues, name, description, partial(write_issue, action=name))
        if name == "create":
            parser.add_argument("project", type=project_key)
            parser.add_argument("issue_type", type=nonblank, help="type name or ID")
            parser.add_argument("summary", type=nonblank)
        else:
            parser.add_argument("issue", type=issue_key)
            parser.add_argument("--summary", type=nonblank)
        text_options(parser, "description")
        payload_option(parser)
    parser = action(issues, "transitions", "List available workflow transitions", transitions)
    parser.add_argument("issue", type=issue_key)
    parser.add_argument("--with-fields", action="store_true", help="include transition screen field metadata")
    parser = action(issues, "transition", "Apply a workflow transition", partial(write_issue, action="transition"))
    parser.add_argument("issue", type=issue_key)
    parser.add_argument("transition_id", type=numeric_id)
    payload_option(parser)
    parser = action(issues, "assign", "Assign or unassign an issue", assign_issue)
    parser.add_argument("issue", type=issue_key)
    assignment = parser.add_mutually_exclusive_group(required=True)
    assignment.add_argument("--account-id", type=nonblank)
    assignment.add_argument("--unassign", action="store_const", const=None, dest="account_id")
    parser = action(issues, "edit-field", "Read metadata for one editable field", edit_field)
    parser.add_argument("issue", type=issue_key)
    parser.add_argument("field", type=nonblank)
    get_action(issues, "link-types", "List names and directions of link types", API + "/issueLinkType",
               lambda data: output.items(data, "issueLinkTypes", output.link_type))
    parser = action(issues, "link", "Link two issues", link_issue)
    parser.add_argument("inward_issue", type=issue_key)
    parser.add_argument("outward_issue", type=issue_key)
    parser.add_argument("link_type", type=nonblank, help="link type name or ID")


def comment_commands(commands):
    comments = group(commands, "comment", "List, read and change issue comments")
    parser = page_action(comments, "list", "List comments with previews", COMMENT_PATH, "comments",
                         partial(output.comment, text_limit=output.PREVIEW), orderBy="order_by")
    parser.add_argument("issue", type=issue_key)
    parser.add_argument("--newest", action="store_const", const="-created", dest="order_by")
    parser = get_action(comments, "get", "Read one comment", COMMENT_PATH + "/{comment_id}", output.comment)
    parser.add_argument("issue", type=issue_key)
    parser.add_argument("comment_id", type=numeric_id)
    for name in ("create", "edit"):
        parser = action(comments, name, f"{name.capitalize()} a comment", partial(write_comment, action=name))
        parser.add_argument("issue", type=issue_key)
        if name == "edit":
            parser.add_argument("comment_id", type=numeric_id)
        text_options(parser, "text")
        payload_option(parser)


def discovery_commands(commands):
    projects = group(commands, "project", "Find projects, issue types, fields, components and versions")
    parser = page_action(projects, "list", "List visible projects", API + "/project/search", "values", PROJECT,
                         query="query")
    parser.add_argument("--query", type=nonblank)
    parser = get_action(projects, "get", "Read a project", API + "/project/{project}", PROJECT)
    parser.add_argument("project", type=project_key)
    metadata = API + "/issue/createmeta/{project}/issuetypes"
    parser = page_action(projects, "types", "List available issue types", metadata, "issueTypes",
                         output.select("id", "name", "subtask", "hierarchyLevel"))
    parser.add_argument("project", type=project_key)
    parser = page_action(projects, "create-fields", "List fields for issue creation", metadata + "/{type_id}",
                         ("fields", "results"), output.field)
    parser.add_argument("project", type=project_key)
    parser.add_argument("type_id", type=numeric_id)
    for name, path, fields in (("components", "component", ("id", "name")),
                               ("versions", "version", ("id", "name", "releaseDate", "released", "archived"))):
        parser = page_action(projects, name, f"List project {name}", API + "/project/{project}/" + path,
                             "values", output.select(*fields), query="query")
        parser.add_argument("project", type=project_key)
        parser.add_argument("--query", type=nonblank)
    fields = group(commands, "field", "Find field IDs and schemas")
    parser = page_action(fields, "search", "Search fields by name", API + "/field/search", "values",
                         output.field, query="query")
    parser.add_argument("query", type=nonblank)
    users = group(commands, "user", "Find accounts and check which account is connected")
    get_action(users, "me", "Read the authenticated account", API + "/myself", output.user)
    parser = page_action(users, "search", "Search active accounts", API + "/user/search", None, output.user,
                         query="query")
    parser.add_argument("query", type=nonblank)
    filters = group(commands, "filter", "Find and read saved JQL filters")
    parser = page_action(filters, "list", "List visible filters", API + "/filter/search", "values",
                         output.select("id", "name", "favourite"), filterName="name")
    parser.add_argument("--name", type=nonblank)
    parser = get_action(filters, "get", "Read a filter and its JQL", API + "/filter/{filter_id}",
                        output.select("id", "name", "jql"))
    parser.add_argument("filter_id", type=numeric_id)


def agile_commands(commands):
    board = output.select("id", "name", "type")
    boards = group(commands, "board", "Find boards and read their issues or backlog")
    parser = page_action(boards, "list", "List boards", AGILE + "/board", "values", board,
                         projectKeyOrId="project", name="name")
    parser.add_argument("--project", type=project_key)
    parser.add_argument("--name", type=nonblank)
    parser = get_action(boards, "get", "Read a board", AGILE + "/board/{board}", board)
    parser.add_argument("board", type=numeric_id)
    for name, route in (("issues", "issue"), ("backlog", "backlog")):
        parser = action(boards, name, f"List board {name}",
                        partial(agile_issues, route=SOFTWARE + "/board/{board}/" + route))
        parser.add_argument("board", type=numeric_id)
        parser.add_argument("--jql", type=nonblank)
        search_options(parser)

    sprints = group(commands, "sprint", "Read and manage sprints and move issues")
    parser = page_action(sprints, "list", "List sprints from a board", AGILE + "/board/{board}/sprint",
                         "values", SPRINT, state="state")
    parser.add_argument("board", type=numeric_id)
    parser.add_argument("--state", type=csv, help="comma-separated active, future or closed")
    parser = get_action(sprints, "get", "Read a sprint", SPRINT_PATH, SPRINT)
    parser.add_argument("sprint_id", type=numeric_id)
    parser = action(sprints, "issues", "List issues in a sprint",
                    partial(agile_issues, route=SOFTWARE + "/sprint/{sprint_id}/issue"))
    parser.add_argument("sprint_id", type=numeric_id)
    parser.add_argument("--jql", type=nonblank)
    search_options(parser)
    parser = action(sprints, "move", "Move issues into a sprint", partial(move_issues, route=SPRINT_PATH + "/issue"))
    parser.add_argument("sprint_id", type=numeric_id)
    parser.add_argument("issues", nargs="+", type=issue_key)
    parser = action(sprints, "backlog", "Move issues to the backlog",
                    partial(move_issues, route=AGILE + "/backlog/issue"))
    parser.add_argument("issues", nargs="+", type=issue_key)
    parser = action(sprints, "create", "Create a future sprint", partial(write_sprint, action="create"))
    parser.add_argument("board", type=numeric_id)
    parser.add_argument("name", type=nonblank)
    payload_option(parser)
    parser = action(sprints, "edit", "Change selected sprint fields", partial(write_sprint, action="edit"))
    parser.add_argument("sprint_id", type=numeric_id)
    payload_option(parser, required=True)


def extra_commands(commands):
    worklogs = group(commands, "worklog", "Read and record time on issues")
    parser = page_action(worklogs, "list", "List worklogs", WORKLOG_PATH, "worklogs", output.worklog)
    parser.add_argument("issue", type=issue_key)
    parser = get_action(worklogs, "get", "Read a worklog", WORKLOG_PATH + "/{worklog_id}", output.worklog)
    parser.add_argument("issue", type=issue_key)
    parser.add_argument("worklog_id", type=numeric_id)
    parser = action(worklogs, "add", "Log time on an issue", add_worklog)
    parser.add_argument("issue", type=issue_key)
    parser.add_argument("seconds", type=bounded_int(1))
    parser.add_argument("started", type=worklog_start, help="ISO 8601 timestamp with UTC offset")
    text_options(parser, "text")

    attachments = group(commands, "attachment", "List, upload and download attachments")
    parser = action(attachments, "list", "List attachment metadata", list_attachments)
    parser.add_argument("issue", type=issue_key)
    parser = action(attachments, "upload", "Upload a file to an issue", upload_attachment)
    parser.add_argument("issue", type=issue_key)
    parser.add_argument("file", type=Path)
    parser = action(attachments, "download", "Download an attachment to a new file", download_attachment)
    parser.add_argument("attachment_id", type=numeric_id)
    parser.add_argument("file", type=Path)

    parser = action(commands, "api", "Call a Jira REST endpoint with JSON", api_call)
    parser.add_argument("method", choices=("GET", "POST", "PUT", "PATCH", "DELETE"))
    parser.add_argument("path", help="a /rest/... path")
    parser.add_argument("--param", action="append", type=query_parameter, metavar="NAME=VALUE")
    parser.add_argument("--select", type=csv, help="comma-separated top-level response keys")
    payload_option(parser)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", action="store_true", help="return Jira's JSON for this request")
    commands = parser.add_subparsers(dest="resource", metavar="RESOURCE", required=True)
    issue_commands(commands)
    comment_commands(commands)
    discovery_commands(commands)
    agile_commands(commands)
    extra_commands(commands)
    args = parser.parse_args(argv)
    client = JiraClient(raw=args.raw)
    try:
        print(json_text(args.handler(client, args)))
    except JiraError as exc:
        error = {"error": client.redact(str(exc))}
        if exc.status is not None:
            error["status"] = exc.status
        if exc.retry_after:
            error["retryAfter"] = client.redact(exc.retry_after[:100])
        print(json_text(error), file=sys.stderr)
        return 1
    except (InputError, OSError, UnicodeError) as exc:
        print(json_text({"error": client.redact(str(exc))}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
