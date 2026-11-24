import base64
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jira
from jira_client import InputError, get_credentials

EMAIL, TOKEN = "test@example.com", "fixture-token"
AUTH = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
ADF = {"type": "doc", "version": 1, "content": [
    {"type": "paragraph", "content": [{"type": "text", "text": "First line"}, {"type": "hardBreak"},
                                      {"type": "mention", "attrs": {"text": "@Nadia"}}]},
]}


class MockJiraHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, path.path, parse_qs(path.query), body, self.headers))
        status, data, headers = self.server.reply
        self.send_response(status)
        headers.setdefault("Content-Length", len(data))
        for name, value in headers.items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_PUT = do_GET

    def log_message(self, *args):
        pass


class JiraCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockJiraHandler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.requests = []
        address, port = self.server.server_address
        credentials = patch("jira_client.get_credentials", return_value=(f"http://{address}:{port}", EMAIL, TOKEN))
        self.addCleanup(credentials.stop)
        credentials.start()

    def run_cli(self, *args, response=b"", status=200, headers=None, stdin="", exit_code=0):
        if not isinstance(response, bytes):
            response = json.dumps(response).encode()
        self.server.reply = status, response, headers or {}
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch("sys.stdin", io.StringIO(stdin)):
            code = jira.main(args)
        self.assertEqual(code, exit_code, stderr.getvalue())
        if code:
            self.assertEqual(stdout.getvalue(), "")
            return json.loads(stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        return json.loads(stdout.getvalue())

    def request_is(self, method, path, *, query=None, body=None):
        sent_method, sent_path, sent_query, sent_body, headers = self.server.requests[-1]
        self.assertEqual((sent_method, sent_path, sent_query), (method, "/rest/" + path, query or {}))
        self.assertEqual(headers["Authorization"], f"Basic {AUTH}")
        if body is not None:
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(json.loads(sent_body), body)
        return sent_body, headers

    def test_jql_limits_fields_and_reuses_token(self):
        row = {"key": "ABC-1", "id": "10001", "self": "https://example.com", "fields": {
            "summary": "One", "status": {"id": "5", "name": "Done", "iconUrl": "irrelevant"},
        }}
        result = self.run_cli("issue", "search", "project = ABC", "--fields", "summary,status", "--limit", "3",
                              "--cursor", "previous", "--reconcile", "10001",
                              response={"issues": [row], "nextPageToken": "next", "isLast": False})
        self.request_is("POST", "api/3/search/jql", body={
            "jql": "project = ABC", "fields": ["summary", "status"], "maxResults": 3,
            "nextPageToken": "previous", "reconcileIssues": [10001],
        })
        self.assertEqual(result, {"items": [{"key": "ABC-1", "id": "10001", "fields": {
            "summary": "One", "status": {"id": "5", "name": "Done"},
        }}], "nextPageToken": "next"})

    def test_issue_read_create_and_edit(self):
        issue = {"key": "ABC-1", "fields": {"description": ADF}, "self": "https://example.com"}
        result = self.run_cli("--raw", "issue", "get", "ABC-1", "--fields", "description", response=issue)
        self.request_is("GET", "api/3/issue/ABC-1", query={"fields": ["description"]})
        self.assertEqual(result, issue)

        result = self.run_cli("issue", "create", "ABC", "10001", "Title", "--description", "One\n\nTwo",
                              "--payload", "-", stdin='{"fields":{"labels":["one"]}}',
                              status=201, response={"key": "ABC-2", "id": "42", "self": "unused"})
        description = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "One"}]}, {"type": "paragraph"},
            {"type": "paragraph", "content": [{"type": "text", "text": "Two"}]},
        ]}
        self.request_is("POST", "api/3/issue", body={"fields": {
            "project": {"key": "ABC"}, "issuetype": {"id": "10001"}, "summary": "Title",
            "description": description, "labels": ["one"],
        }})
        self.assertEqual(result, {"key": "ABC-2", "id": "42"})

        payload = {"update": {"labels": [{"add": "two"}]}}
        result = self.run_cli("issue", "edit", "ABC-2", "--summary", "New title", "--payload", "-",
                              stdin=json.dumps(payload), status=204)
        self.request_is("PUT", "api/3/issue/ABC-2", body={**payload, "fields": {"summary": "New title"}})
        self.assertEqual(result, {"ok": True})

    def test_comment_crud_and_offset(self):
        comment = {"id": "77", "body": ADF, "author": {"accountId": "user-id", "displayName": "Nadia"}}
        longer = {**comment, "id": "78", "body": "test " * 100}
        result = self.run_cli("comment", "list", "ABC-2", "--newest", "--start", "1", "--limit", "10",
                              response={"comments": [comment, longer], "startAt": 1, "maxResults": 2, "total": 6})
        self.request_is("GET", "api/3/issue/ABC-2/comment",
                        query={"startAt": ["1"], "maxResults": ["10"], "orderBy": ["-created"]})
        self.assertEqual(result["nextStart"], 3)
        self.assertEqual(result["items"][0]["body"], "First line\n@Nadia")
        self.assertTrue(result["items"][1]["body"].endswith("…"))
        self.assertEqual(len(result["items"][1]["body"]), 240)

        self.run_cli("comment", "get", "ABC-2", "77", response=comment)
        self.request_is("GET", "api/3/issue/ABC-2/comment/77")

        self.run_cli("comment", "create", "ABC-2", "--text-file", "-", stdin="Added", status=201, response=comment)
        expected = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Added"}]},
        ]}
        self.request_is("POST", "api/3/issue/ABC-2/comment", body={"body": expected})

        payload = {"body": ADF, "visibility": {"type": "role", "value": "Developers"}}
        self.run_cli("comment", "edit", "ABC-2", "77", "--payload", "-", stdin=json.dumps(payload), response=comment)
        self.request_is("PUT", "api/3/issue/ABC-2/comment/77", body=payload)

    def test_workflow_and_discovery(self):
        result = self.run_cli("project", "create-fields", "ABC", "10001", response={
            "results": [{"fieldId": "customfield_123", "required": True, "name": "Area"}], "total": 1,
        })
        self.request_is("GET", "api/3/issue/createmeta/ABC/issuetypes/10001",
                        query={"startAt": ["0"], "maxResults": ["20"]})
        self.assertEqual(result, {"items": [{"fieldId": "customfield_123", "required": True, "name": "Area"}], "total": 1})

        result = self.run_cli("issue", "transitions", "ABC-1", "--with-fields", response={"transitions": [
            {"id": "31", "name": "Resolve", "to": {"name": "Done"}, "fields": {"resolution": {"required": True}}},
        ]})
        self.request_is("GET", "api/3/issue/ABC-1/transitions", query={"expand": ["transitions.fields"]})
        self.assertTrue(result[0]["fields"]["resolution"]["required"])
        self.run_cli("issue", "transition", "ABC-1", "31", "--payload", "-", status=204,
                     stdin='{"fields":{"resolution":{"id":"10000"}}}')
        self.request_is("POST", "api/3/issue/ABC-1/transitions", body={
            "transition": {"id": "31"}, "fields": {"resolution": {"id": "10000"}},
        })
        self.run_cli("issue", "assign", "ABC-1", "--unassign", status=204)
        self.request_is("PUT", "api/3/issue/ABC-1/assignee", body={"accountId": None})

    def test_agile_routes_and_worklog(self):
        self.run_cli("board", "backlog", "6", "--cursor", "here", response={"issues": [], "isLast": True})
        self.request_is("GET", "software/1.0/board/6/backlog", query={
            "nextPageToken": ["here"], "fields": ["summary,status,assignee,issuetype"], "maxResults": ["20"],
        })
        self.run_cli("sprint", "move", "3", "ABC-1", "ABC-2", status=204)
        self.request_is("POST", "agile/1.0/sprint/3/issue", body={"issues": ["ABC-1", "ABC-2"]})
        self.run_cli("worklog", "add", "ABC-2", "1800", "2026-11-15T10:30:00+01:00", status=201,
                     response={"id": "99"})
        self.request_is("POST", "api/3/issue/ABC-2/worklog", body={
            "timeSpentSeconds": 1800, "started": "2026-11-15T10:30:00.000+0100",
        })

    def test_attachments_stream_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "rapport é.txt"
            content = b"fixture content\x00"
            source.write_bytes(content)
            result = self.run_cli("attachment", "upload", "ABC-2", str(source), status=200,
                                  response=[{"id": "90", "filename": source.name, "content": "unused"}])
            body, headers = self.request_is("POST", "api/3/issue/ABC-2/attachments")
            self.assertEqual(headers["X-Atlassian-Token"], "no-check")
            self.assertEqual(int(headers["Content-Length"]), len(body))
            self.assertIn(source.name.encode(), body)
            self.assertIn(content, body)
            self.assertEqual(result, [{"id": "90", "filename": source.name}])

            destination = Path(directory) / "received.txt"
            self.run_cli("attachment", "download", "90", str(destination), response=content)
            self.request_is("GET", "api/3/attachment/content/90", query={"redirect": ["false"]})
            self.assertEqual(destination.read_bytes(), content)
            self.run_cli("attachment", "download", "90", str(destination), exit_code=2)
            self.assertEqual(len(self.server.requests), 2)
            self.assertEqual(destination.read_bytes(), content)

            partial = Path(directory) / "partial.txt"
            self.run_cli("attachment", "download", "90", str(partial), response=content,
                         headers={"Content-Length": len(content) + 5}, exit_code=1)
            self.assertFalse(partial.exists())
            self.assertEqual(set(Path(directory).iterdir()), {source, destination})

    def test_validation_precedes_network(self):
        for args, stdin in (
            (("issue", "edit", "ABC-1", "--payload", "-"), '{"fields":{},"fields":{}}'),
            (("issue", "edit", "ABC-1", "--payload", "-"), '{"update":{"labels":{"add":"x"}}}'),
            (("issue", "edit", "ABC-1", "--summary", "x", "--payload", "-"), '{"fields":{"summary":"y"}}'),
            (("comment", "create", "ABC-1", "--payload", "-"), '{"body":{"type":"doc","version":1,"content":[0]}}'),
            (("comment", "create", "ABC-1", "--text-file", "-", "--payload", "-"), "unused"),
            (("api", "GET", "/rest/api/3/../../other"), ""),
            (("api", "GET", "/rest/api/3/%2e%2e/other"), ""),
            (("api", "GET", "https://example.com/rest/api/3/myself"), ""),
        ):
            with self.subTest(args=args, stdin=stdin):
                self.run_cli(*args, stdin=stdin, exit_code=2)
        self.assertEqual(self.server.requests, [])

    def test_http_errors_are_compact_and_redirects_are_refused(self):
        result = self.run_cli("user", "me", status=429, headers={"Retry-After": "5"},
                              response={"errorMessages": [f"Busy {TOKEN} Basic {AUTH}"]}, exit_code=1)
        self.assertEqual(result["status"], 429)
        self.assertEqual(result["retryAfter"], "5")
        self.assertNotIn(TOKEN, result["error"])
        self.assertNotIn(AUTH, result["error"])
        result = self.run_cli("user", "me", status=302, headers={"Location": "/redirect"}, exit_code=1)
        self.assertEqual(result["status"], 302)
        self.assertEqual(len(self.server.requests), 2)

    def test_api_selection_and_scalar_body(self):
        result = self.run_cli("api", "GET", "/rest/api/3/priority/search", "--param", "id=1", "--param", "id=2",
                              "--select", "values", response={"values": [{"id": "1"}, {"id": "2"}], "startAt": 0})
        self.request_is("GET", "api/3/priority/search", query={"id": ["1", "2"]})
        self.assertEqual(result, {"values": [{"id": "1"}, {"id": "2"}]})
        self.run_cli("api", "POST", "/rest/api/3/issue/ABC-1/watchers", "--payload", "-", stdin='"account"', status=204)
        self.request_is("POST", "api/3/issue/ABC-1/watchers", body="account")


class WorkerConfiguration(unittest.TestCase):
    def test_root_default_explicit_state_and_cloud_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".kiao-config"
            state.mkdir()
            jira_config = {"url": "https://example.atlassian.net/", "email": EMAIL, "token": TOKEN}
            (state / "atlassian.json").write_text(json.dumps({"jira": jira_config, "bitbucket": {"token": "unused"}}))
            with patch.dict(os.environ, {"KIAO_WORKER_ROOT_PATH": str(root), "KIAO_WORKER_STATE_PATH": ""}):
                self.assertEqual(get_credentials(), ("https://example.atlassian.net", EMAIL, TOKEN))
            cloud_id = "0987aabc-def0-1234-5678-123456789012"
            (root / "atlassian.json").write_text(json.dumps({"jira": {**jira_config, "cloudId": cloud_id}}))
            with patch.dict(os.environ, {"KIAO_WORKER_ROOT_PATH": "/unused", "KIAO_WORKER_STATE_PATH": str(root)}):
                self.assertEqual(get_credentials(), (f"https://api.atlassian.com/ex/jira/{cloud_id}", EMAIL, TOKEN))

    def test_configuration_errors_do_not_echo_credentials(self):
        with tempfile.TemporaryDirectory() as state, patch.dict(os.environ, {"KIAO_WORKER_STATE_PATH": state}):
            config = Path(state) / "atlassian.json"
            config.write_text(json.dumps({"jira": {"url": f"http://example.com/{TOKEN}", "email": EMAIL, "token": TOKEN}}))
            with self.assertRaisesRegex(InputError, "HTTPS site origin") as error:
                get_credentials()
            self.assertNotIn(TOKEN, str(error.exception))


if __name__ == "__main__":
    unittest.main()
